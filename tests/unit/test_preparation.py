"""Offline preparation reuses native identities without probing or executing a harness."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence
from ithrl.context import ContextGraph
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy, resolve_execution_plan
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledExperimentDefinition,
    parse_experiment_toml,
)
from ithrl.preparation import (
    NativePreparationReceipt,
    PreparationError,
    ReductionPreparationSettings,
    prepare_reduction,
)
from native_support import codex, copilot

SETTINGS = """schema_version = 1
kind = "reduction-preparation"
[calibration]
required_valid = 3
minimum_matches = 2
max_attempts = 4
[reduction]
margin = 0.20
freshness_seconds = 7200
eligibility_policy = "independent-safe-preaccept-only"
[reduction.search.original]
required_valid = 1
max_attempts = 2
[reduction.search.candidate]
required_valid = 2
max_attempts = 3
[reduction.confirmation]
one_sided_confidence = 0.95
[reduction.confirmation.original]
required_valid = 11
max_attempts = 12
[reduction.confirmation.candidate]
required_valid = 12
max_attempts = 13
"""


@pytest.fixture(params=("codex", "copilot"))
def recorded_case(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[CaseEvidence, NativePreparationReceipt]:
    if request.param == "codex":
        _, evidence = codex._case(tmp_path, context_sensitive=True)
        profile = copilot._capability("final-response").model_copy(
            update={
                "runner_type": "codex",
                "harness": "codex",
                "source_schema": "codex-rollout-jsonl",
                "reconstruction_method": "codex-app-server-inject-checkpoint-v1",
                "provider_calls_per_attempt": 4,
            }
        )
    else:
        _, _, evidence = copilot._case(tmp_path, "final-response", context_sensitive=True)
        profile = copilot._capability("final-response")
    return evidence, _receipt(evidence, profile, tmp_path)


def _receipt(
    evidence: CaseEvidence, profile: CapabilityProfile, parent: Path
) -> NativePreparationReceipt:
    default = parse_experiment_toml(evidence.default_experiment)
    settings = ReductionPreparationSettings.from_toml(SETTINGS)
    calibration = SampledExperimentDefinition(
        target=default.target,
        oracle=default.oracle,
        runner=NativeRunnerConfig(
            type="codex" if profile.runner_type == "codex" else "copilot",
            required_capability_digest=profile.digest,
        ),
        calibration=settings.calibration,
    )
    report = resolve_execution_plan(
        profile,
        calibration,
        evidence=evidence,
        runner_fingerprint="fixture-native-identity",
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider=profile.provider,
            allowed_endpoint=profile.endpoint,
        ),
        resource_limits=ResourceLimits(
            max_provider_calls=profile.provider_calls_per_attempt,
            max_wall_time_seconds=120,
            max_disposable_state_bytes=1024 * 1024,
        ),
    )
    assert report.ready
    assert report.plan is not None
    return NativePreparationReceipt.create(report.plan, calibration, parent / "original")


def _sections(evidence: CaseEvidence, section_ids: tuple[str, ...] | None = None) -> str:
    catalog = next(c for c in evidence.text_sections() if len(c.sections) >= 2)
    ids = section_ids if section_ids is not None else (catalog.sections[0].id,)
    return (
        "\n[sections]\nmax_coarse_comparisons = 1\nmax_fine_comparisons = 2\n"
        "minimum_removed_utf8_bytes = 512\nrequire_criterion_for_confirmation = true\n"
        "stop_when_no_retained_fields = true\n[[sections.fields]]\n"
        f"context_id = {json.dumps(catalog.context_id)}\n"
        f"content_index = {catalog.content_index}\nsection_ids = {json.dumps(ids)}\n"
    )


def test_receipt_creation_rejects_a_different_experiment(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    evidence, receipt = recorded_case
    different = parse_experiment_toml(evidence.default_experiment)
    with pytest.raises(PreparationError, match="preflight-experiment-mismatch"):
        NativePreparationReceipt.create(
            receipt.source_plan, different, Path(receipt.original_workspace)
        )


def test_receipt_requires_native_runner_and_consistent_plan(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    _, receipt = recorded_case
    data = receipt.model_dump()
    data["source_plan"]["capability"]["runner_type"] = "fixture-runner"
    from ithrl.execution import ResolvedExecutionPlan

    plan = ResolvedExecutionPlan.model_validate(data["source_plan"])
    data.update(source_plan=plan, source_plan_digest=plan.digest)
    with pytest.raises(ValidationError, match="receipt requires a native runner"):
        NativePreparationReceipt.model_validate(data)


def test_prepare_roundtrip_without_native_work(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, receipt = recorded_case

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("preparation attempted network or process execution")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    receipt = NativePreparationReceipt.from_json(receipt.model_dump_json())
    experiment = prepare_reduction(
        evidence, receipt, ReductionPreparationSettings.from_toml(SETTINGS)
    )
    default = parse_experiment_toml(evidence.default_experiment)
    assert experiment.target == default.target
    assert experiment.oracle == default.oracle
    assert isinstance(experiment.runner, NativeRunnerConfig)
    assert experiment.runner.required_capability_digest == receipt.source_plan.capability.digest
    assert experiment.digest != receipt.source_plan.experiment_digest
    assert SampledExperimentDefinition.from_toml(experiment.to_toml()) == experiment
    assert experiment.reduction is not None
    assert experiment.reduction.search.original.required_valid == 1
    assert experiment.reduction.search.candidate.required_valid == 2
    assert experiment.reduction.confirmation.original.max_attempts == 12
    assert experiment.reduction.confirmation.candidate.max_attempts == 13


def test_sections_derive_binding_and_preserve_prospective_policy(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    evidence, receipt = recorded_case
    settings = ReductionPreparationSettings.from_toml(SETTINGS + _sections(evidence))
    experiment = prepare_reduction(evidence, receipt, settings)
    assert experiment.reduction is not None
    assert experiment.reduction.section_search is not None
    plan = experiment.reduction.section_search
    catalog = next(c for c in evidence.text_sections() if len(c.sections) >= 2)
    assert plan.fields == (catalog.removal((catalog.sections[0].id,)),)
    assert plan.minimum_removed_utf8_bytes == 512
    assert plan.max_coarse_comparisons == 1
    assert plan.max_fine_comparisons == 2
    assert plan.require_criterion_for_confirmation
    assert plan.stop_when_no_retained_fields
    assert SampledExperimentDefinition.from_toml(experiment.to_toml()) == experiment


@pytest.mark.parametrize("selection", ["unknown", "duplicate", "reordered"])
def test_invalid_section_selection_refuses(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt], selection: str
) -> None:
    evidence, receipt = recorded_case
    catalog = next(c for c in evidence.text_sections() if len(c.sections) >= 2)
    first, second = (section.id for section in catalog.sections[:2])
    ids = {
        "unknown": ("section-" + "a" * 64,),
        "duplicate": (first, first),
        "reordered": (second, first),
    }[selection]
    settings = ReductionPreparationSettings.from_toml(SETTINGS + _sections(evidence, ids))
    with pytest.raises(PreparationError, match="preparation-contract-mismatch"):
        prepare_reduction(evidence, receipt, settings)


@pytest.mark.parametrize(
    "source",
    [
        SETTINGS.replace('kind = "reduction-preparation"', 'kind = "sampled"'),
        SETTINGS.replace(
            'kind = "reduction-preparation"',
            'kind = "reduction-preparation"\nrunner = "private-value"',
        ),
        SETTINGS.replace("minimum_matches = 2", "minimum_matches = 5"),
        SETTINGS.replace("schema_version = 1", "schema_version = true"),
        SETTINGS.replace('eligibility_policy = "independent-safe-preaccept-only"\n', ""),
        SETTINGS.replace("[reduction]", "[reduction]\nsection_search = {}"),
        'private-payload = "secret"\n[',
    ],
)
def test_settings_refuse_incomplete_or_foreign_inputs_without_content(source: str) -> None:
    with pytest.raises(PreparationError) as refused:
        ReductionPreparationSettings.from_toml(source)
    assert str(refused.value) == "invalid-preparation-settings"


@pytest.mark.parametrize(
    "choice",
    [
        "required_valid = 3\n",
        "minimum_matches = 2\n",
        "max_attempts = 4\n",
        "margin = 0.20\n",
        "freshness_seconds = 7200\n",
        'eligibility_policy = "independent-safe-preaccept-only"\n',
        "one_sided_confidence = 0.95\n",
        "required_valid = 2\n",
        "max_attempts = 13\n",
    ],
)
def test_statistical_choices_have_no_preparation_defaults(choice: str) -> None:
    with pytest.raises(PreparationError, match="invalid-preparation-settings"):
        ReductionPreparationSettings.from_toml(SETTINGS.replace(choice, ""))


@pytest.mark.parametrize("changed", ["case_digest", "model_requested", "target_digest"])
def test_recorded_identity_mismatch_refuses(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt], changed: str
) -> None:
    evidence, receipt = recorded_case
    value = "b" * 64 if changed.endswith("digest") else "different-model"
    plan = receipt.source_plan.model_copy(update={changed: value})
    receipt = receipt.model_copy(update={"source_plan": plan, "source_plan_digest": plan.digest})
    with pytest.raises(PreparationError, match="preflight-evidence-mismatch"):
        prepare_reduction(evidence, receipt, ReductionPreparationSettings.from_toml(SETTINGS))


def test_logical_source_is_rederived_even_with_rebound_case_identity(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    evidence, receipt = recorded_case
    selected = next(node for node in evidence.context.nodes if node.mutable and not node.protected)
    altered = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=ContextGraph(
            nodes=tuple(
                node.model_copy(update={"protected": True}) if node.id == selected.id else node
                for node in evidence.context.nodes
            )
        ),
        default_experiment=evidence.default_experiment,
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    assert altered.digest != evidence.digest
    # Rebinding the receipt removes a simple identity mismatch as the refusal cause.
    # The graph is valid structurally, but it no longer matches its immutable source.
    plan = receipt.source_plan.model_copy(update={"case_digest": altered.digest})
    receipt = receipt.model_copy(update={"source_plan": plan, "source_plan_digest": plan.digest})
    with pytest.raises(PreparationError, match="preparation-contract-mismatch"):
        prepare_reduction(altered, receipt, ReductionPreparationSettings.from_toml(SETTINGS))


def test_oracle_is_bound_separately_from_case_digest(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    evidence, receipt = recorded_case
    default = parse_experiment_toml(evidence.default_experiment)
    altered = default.model_copy(
        update={
            "oracle": default.oracle.model_copy(update={"protected_context_ids": ("ctx-other",)})
        }
    )
    assert altered.oracle != default.oracle
    altered_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=evidence.context,
        default_experiment=altered.to_toml(),
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    assert altered_evidence.digest == evidence.digest
    with pytest.raises(PreparationError, match="preflight-oracle-mismatch"):
        prepare_reduction(
            altered_evidence, receipt, ReductionPreparationSettings.from_toml(SETTINGS)
        )


def test_target_is_bound_separately_from_case_digest(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    evidence, receipt = recorded_case
    default = parse_experiment_toml(evidence.default_experiment)
    altered = default.model_copy(
        update={"target": FinalResponseTarget(text_contains="different target")}
    )
    altered_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=evidence.context,
        default_experiment=altered.to_toml(),
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    assert altered_evidence.digest == evidence.digest
    with pytest.raises(PreparationError, match=r"preflight-(target|evidence)-mismatch"):
        prepare_reduction(
            altered_evidence, receipt, ReductionPreparationSettings.from_toml(SETTINGS)
        )


def test_receipt_parse_refuses_inconsistent_or_unbounded_metadata(
    recorded_case: tuple[CaseEvidence, NativePreparationReceipt],
) -> None:
    _, receipt = recorded_case
    for update in (
        {"source_plan_digest": "c" * 64},
        {"schema_version": True},
        {"original_workspace": "relative"},
        {"original_workspace": "/recorded/../private"},
        {"original_workspace": "/recorded/private\x00suffix"},
        {"original_workspace": "/" + "private" * 1024},
        {"unknown": "private-value"},
    ):
        raw = receipt.model_dump(mode="json") | update
        with pytest.raises(PreparationError) as refused:
            NativePreparationReceipt.from_json(json.dumps(raw))
        assert str(refused.value) == "invalid-preflight-receipt"
    for missing in ("schema_version", "kind"):
        raw = receipt.model_dump(mode="json")
        del raw[missing]
        with pytest.raises(PreparationError, match="invalid-preflight-receipt"):
            NativePreparationReceipt.from_json(json.dumps(raw))
    assert "original_workspace" not in repr(receipt)
    assert "source_plan=" not in repr(receipt)


def test_copilot_shell_whole_record_remains_supported(tmp_path: Path) -> None:
    _, _, evidence = copilot._case(tmp_path, "shell-action", context_sensitive=True)
    receipt = _receipt(evidence, copilot._capability("shell-action"), tmp_path)
    experiment = prepare_reduction(
        evidence, receipt, ReductionPreparationSettings.from_toml(SETTINGS)
    )
    assert experiment.target.type == "shell-action"
    with pytest.raises(PreparationError, match="unsupported-section-search"):
        prepare_reduction(
            evidence,
            receipt,
            ReductionPreparationSettings.from_toml(SETTINGS + _sections(evidence)),
        )
