"""Offline inspection binds saved reductions without runtime or SQLite access."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli import app
from ithrl.context import ContextGraphError, content_digest
from ithrl.execution import ResourceLimits
from ithrl.experiment import (
    ExperimentDefinition,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
)
from ithrl.extraction import extract_decision
from ithrl.observations import (
    ObservationIdentity,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.reduce import SampledCandidateTrial, SampledComparison, SampledReductionCheckpoint
from ithrl.reduce.contracts import trial_key
from ithrl.results import ResultSnapshotError, SampledReductionEvidence
from ithrl.results.inspection import load_retained_result
from ithrl.run_state import SampledRunState, SamplingBlockProgress, SamplingBudgetApproval
from ithrl.runner import Candidate

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_TIME = datetime(2026, 10, 7, tzinfo=UTC)


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value) + "\n")
    path.chmod(0o600)


def _case(tmp_path: Path, harness: str) -> tuple[Path, CaseEvidence]:
    case = tmp_path / "case"
    if harness == "synthetic":
        result = CliRunner().invoke(app, ["extract", "--example", "--output", str(case)])
        assert result.exit_code == 0, result.output
    else:
        name = "rollout.jsonl" if harness == "codex" else "events.jsonl"
        session = parse_session(_FIXTURES / (harness + "-session") / name)
        summary = next(
            s
            for s in enumerate_decisions(session)
            if any(t.type == "final-response" for t in s.targets)
        )
        target = next(t for t in summary.targets if t.type == "final-response")
        extract_decision(
            session, decision_id=summary.decision_id, target_id=target.target_id, case_path=case
        )
    return case, CaseWorkspace.open(case).load()


def _candidate(
    evidence: CaseEvidence, identity: ObservationIdentity, removed: tuple[str, ...] = ()
) -> Candidate:
    return Candidate(
        case_digest=evidence.digest,
        execution_plan_digest=identity.execution_plan_digest,
        source_artifact_digests=tuple(a.sha256 for a in evidence.source_artifacts),
        retained_context_ids=tuple(n.id for n in evidence.context.nodes if n.id not in removed),
        removed_context_ids=tuple(n.id for n in evidence.context.nodes if n.id in removed),
        materialization="exact-request" if evidence.has_request else "source-backed",
    )


def _sampled(
    tmp_path: Path, harness: str = "synthetic", *, with_trial: bool = False
) -> tuple[Path, Path]:
    case, evidence = _case(tmp_path, harness)
    base = ExperimentDefinition.from_toml(evidence.default_experiment)
    arm = SamplingArmTarget(required_valid=2, max_attempts=3)
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(original=arm, candidate=arm),
        confirmation=SampledConfirmationPhase(
            original=arm, candidate=arm, one_sided_confidence=0.95
        ),
        margin=0.2,
        freshness_seconds=60,
    )
    experiment = SampledExperimentDefinition(
        target=base.target,
        oracle=base.oracle,
        runner=NativeRunnerConfig(
            type="copilot" if harness == "copilot" else "codex", required_capability_digest="a" * 64
        ),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=2),
        reduction=reduction,
    )
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint="offline-inspection-fixture",
        execution_plan_digest="b" * 64,
        target_digest=experiment.target.digest,
        algorithm_digest="d" * 64,
    )
    original = _candidate(evidence, identity)
    trials = ()
    blocks = ()
    if with_trial:
        for node in evidence.context.nodes:
            if not node.mutable or node.protected:
                continue
            try:
                removed = tuple(sorted(evidence.context.dependency_closure((node.id,))))
                break
            except ContextGraphError:
                continue
        else:
            raise AssertionError("fixture needs one removable unit")
        key = trial_key("chunk", (), (node.id,))
        seed = int(content_digest({"run_id": "run-1", "trial_key": key})[:16], 16)
        plan = SamplingBlockPlan(
            block_id="search-" + key,
            role="search",
            procedure=reduction,
            original_identity=identity,
            candidate_identity=identity,
            original_candidate_digest=original.digest,
            candidate_digest=_candidate(evidence, identity, removed).digest,
            frozen_at=_TIME + timedelta(seconds=1),
            schedule_seed=seed,
            schedule=sampling_schedule(3, 3, seed),
            provider_calls_per_attempt=1,
        )
        block = SamplingBlockEvidence(plan=plan, attempts=())
        trials = (
            SampledCandidateTrial(
                trial_key=key,
                base_removals=(),
                requested_removals=(node.id,),
                effective_removals=removed,
                phase="chunk",
                comparison=SampledComparison.from_evidence(block),
            ),
        )
        blocks = (SamplingBlockProgress(plan=plan),)
    checkpoint = SampledReductionCheckpoint(
        reduction=reduction,
        original_identity=identity,
        original_candidate_digest=original.digest,
        best_candidate_digest=original.digest,
        removed_context_ids=(),
        trials=trials,
    )
    state = SampledRunState(
        run_id="run-1",
        source_case=str(case.absolute()),
        observation_store=str((tmp_path / "missing-observations.sqlite").absolute()),
        started_at=_TIME,
        original_identity=identity,
        original_candidate_digest=original.digest,
        provider_calls_per_attempt=1,
        approvals=(
            SamplingBudgetApproval(
                approval_id="approval-1",
                approved_at=_TIME,
                allowance=SamplingBudgetAllowance(
                    run_id="run-1",
                    max_provider_calls=50,
                    max_wall_time_seconds=500,
                    max_candidate_comparisons=5,
                    confirmation_provider_calls=6,
                    confirmation_runner_seconds=60,
                ),
            ),
        ),
        checkpoint=checkpoint,
        blocks=blocks,
    )
    result = SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-incomplete",
        selected_candidate_digest=original.digest,
        selected_at=_TIME + timedelta(seconds=2),
    )
    destination = tmp_path / "detached-result"
    destination.mkdir(mode=0o700)
    (destination / "sampling").mkdir(mode=0o700)
    _write(destination / "result.json", result.model_dump(mode="json"))
    _write(destination / "sampling/sampling-state.json", state.model_dump(mode="json"))
    _write(
        destination / "cli-session.json",
        {
            "run_id": "run-1",
            "run_directory": str(case.absolute() / ".runs/run-1"),
            "source_case": state.source_case,
            "original_workspace": str((tmp_path / "missing-workspace").absolute()),
            "observation_store": state.observation_store,
            "experiment": experiment.model_dump(mode="json"),
            "resource_limits": ResourceLimits(max_provider_calls=1).model_dump(mode="json"),
            "original_identity": identity.model_dump(mode="json"),
            "started_at": _TIME.isoformat(),
        },
    )
    _write(
        destination / "summary.json",
        {
            "schema_version": 1,
            "kind": "sampled",
            "command": "shrink",
            "plan_digest": identity.execution_plan_digest,
            "target": experiment.target.type,
            "original_context_count": len(evidence.context.nodes),
            "retained_context_count": len(evidence.context.nodes),
            "removed_context_count": 0,
            "search_result": result.search_result,
            "confirmation_result": "not-run",
            "margin": 0.2,
            "one_sided_confidence": 0.95,
        },
    )
    path = destination / "experiment.toml"
    path.write_text(experiment.to_toml())
    path.chmod(0o600)
    path = destination / "retained-context.jsonl"
    path.write_text("".join(n.model_dump_json() + "\n" for n in evidence.context.nodes))
    path.chmod(0o600)
    return case, destination


def _forbid_runtime(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("offline inspection must not access processes or SQLite")


@pytest.mark.parametrize("harness", ["synthetic", "codex", "copilot"])
def test_detached_sampled_snapshot_loads_without_live_state_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    case, result = _sampled(tmp_path, harness)
    before = {p: p.read_bytes() for root in (case, result) for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(subprocess, "Popen", _forbid_runtime)
    monkeypatch.setattr(sqlite3, "connect", _forbid_runtime)
    loaded = load_retained_result(case, result)
    assert loaded.sampled is not None
    assert loaded.deterministic is None
    assert loaded.execution_plan is None
    assert not loaded.recorded_metadata
    assert loaded.retained_ids == tuple(n.id for n in loaded.evidence.context.nodes)
    assert any("observation database" in line for line in loaded.validation_limits)
    assert {p: p.read_bytes() for p in before} == before
    assert not (case / ".runs").exists()
    assert not (tmp_path / "missing-observations.sqlite").exists()


def test_deterministic_snapshot_uses_case_bound_trials(tmp_path: Path) -> None:
    case, _ = _case(tmp_path, "synthetic")
    outcome = CliRunner().invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(tmp_path / "store.sqlite"),
            "--format",
            "json",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    result = Path(json.loads(outcome.stdout)["result_path"])
    shutil.rmtree(case / ".runs")
    loaded = load_retained_result(case, result)
    assert loaded.deterministic is not None
    assert loaded.sampled is None
    assert len(loaded.retained_ids) == 5


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "summary-count",
        "retained",
        "extra",
        "session-extra",
        "session-time",
        "target",
        "case-digest",
        "candidate-digest",
        "duplicate",
    ],
)
def test_sampled_refuses_mismatched_or_unknown_artifacts_without_content_errors(
    tmp_path: Path, change: str
) -> None:
    case, result = _sampled(tmp_path)
    if change in {"kind", "summary-count"}:
        p = result / "summary.json"
        v = json.loads(p.read_text())
        v["kind" if change == "kind" else "retained_context_count"] = "PRIVATE_UNTRUSTED"
        _write(p, v)
    elif change == "retained":
        (result / "retained-context.jsonl").write_text("")
    elif change == "extra":
        _write(result / "PRIVATE_UNTRUSTED.json", {})
    elif change == "session-extra":
        p = result / "cli-session.json"
        v = json.loads(p.read_text())
        v["PRIVATE_UNTRUSTED"] = "value"
        _write(p, v)
    elif change == "session-time":
        p = result / "cli-session.json"
        v = json.loads(p.read_text())
        v["started_at"] = {"PRIVATE_UNTRUSTED": "value"}
        _write(p, v)
    elif change == "target":
        p = result / "experiment.toml"
        experiment = SampledExperimentDefinition.from_toml(p.read_text())
        target = experiment.target.model_copy(update={"command_equals": "PRIVATE_UNTRUSTED"})
        p.write_text(experiment.model_copy(update={"target": target}).to_toml())
    elif change in {"case-digest", "candidate-digest"}:
        p = result / "result.json"
        v = json.loads(p.read_text())
        v["checkpoint"]["original_identity"]["case_digest"] = (
            "f" * 64
            if change == "case-digest"
            else v["checkpoint"]["original_identity"]["case_digest"]
        )
        if change == "candidate-digest":
            v["checkpoint"]["original_candidate_digest"] = "f" * 64
        _write(p, v)
    else:
        (result / "summary.json").write_text('{"kind":"sampled","kind":"sampled"}')
    with pytest.raises(ResultSnapshotError) as error:
        load_retained_result(case, result)
    assert "PRIVATE_UNTRUSTED" not in str(error.value)


@pytest.mark.parametrize(
    ("filename", "field"),
    [("result.json", "kind"), ("sampling/sampling-state.json", "run_id")],
)
def test_sampled_refuses_conflicting_duplicate_fields_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    field: str,
) -> None:
    case, result = _sampled(tmp_path)
    path = result / filename
    original = path.read_text()
    tampered = '{"' + field + '": "PRIVATE_DUPLICATE_VALUE",' + original.lstrip()[1:]
    path.write_text(tampered)
    # Pydantic accepts the final valid value. The shared JSON reader must reject
    # the ambiguous document before current-format model validation starts.
    model = SampledReductionEvidence if filename == "result.json" else SampledRunState
    model.model_validate_json(tampered)
    before = {p: p.read_bytes() for root in (case, result) for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(subprocess, "Popen", _forbid_runtime)
    monkeypatch.setattr(sqlite3, "connect", _forbid_runtime)
    with pytest.raises(ResultSnapshotError) as error:
        load_retained_result(case, result)
    assert str(error.value) == "cannot inspect a valid reduction snapshot for this case"
    assert "PRIVATE_DUPLICATE_VALUE" not in str(error.value)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode", "directory-link"])
def test_sampled_refuses_unsafe_snapshot_paths_without_modifying_targets(
    tmp_path: Path, unsafe: str
) -> None:
    case, result = _sampled(tmp_path)
    path = result / "result.json"
    original = path.read_bytes()
    if unsafe == "symlink":
        alias = tmp_path / "other.json"
        path.rename(alias)
        path.symlink_to(alias)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "other.json")
    elif unsafe == "mode":
        path.chmod(0o644)
    else:
        alias = tmp_path / "sampling-other"
        (result / "sampling").rename(alias)
        (result / "sampling").symlink_to(alias, target_is_directory=True)
    with pytest.raises(ResultSnapshotError):
        load_retained_result(case, result)
    assert path.read_bytes() == original


def test_sampled_trial_binds_requested_removal_to_effective_graph_closure(tmp_path: Path) -> None:
    case, result = _sampled(tmp_path, with_trial=True)
    assert load_retained_result(case, result).sampled is not None
    evidence = CaseWorkspace.open(case).load()
    original = json.loads((result / "result.json").read_text())
    effective = original["checkpoint"]["trials"][0]["effective_removals"]
    extra = next(node.id for node in evidence.context.nodes if node.id not in effective)
    for name in ("result.json", "sampling/sampling-state.json"):
        p = result / name
        v = json.loads(p.read_text())
        checkpoint = v["checkpoint"]
        checkpoint["trials"][0]["effective_removals"] = sorted([*effective, extra])
        _write(p, v)
    # The two persisted contracts remain mutually consistent. Only comparison with the
    # immutable case graph detects the falsely claimed effective removal.
    SampledReductionEvidence.model_validate_json((result / "result.json").read_text())
    SampledRunState.model_validate_json((result / "sampling/sampling-state.json").read_text())
    with pytest.raises(ResultSnapshotError):
        load_retained_result(case, result)
