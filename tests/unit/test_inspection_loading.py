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
from ithrl.reduce import (
    CandidateTrial,
    ReductionCheckpoint,
    SampledCandidateTrial,
    SampledComparison,
    SampledInvalidCandidateTrial,
    SampledReductionCheckpoint,
)
from ithrl.reduce.contracts import trial_key
from ithrl.results import ResultSnapshot, ResultSnapshotError, SampledReductionEvidence
from ithrl.results import inspection as inspection_storage
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


def test_inspection_refuses_a_file_that_acquires_a_link_during_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, result = _sampled(tmp_path)
    selected = result / "result.json"
    original = selected.read_bytes()
    alias = tmp_path / "unexpected-alias"
    read = inspection_storage.read_safe_text

    def link_after_read(path: Path, **kwargs: Any) -> str:
        value = read(path, **kwargs)
        if path == selected:
            os.link(path, alias)
        return value

    monkeypatch.setattr(inspection_storage, "read_safe_text", link_after_read)
    with pytest.raises(ResultSnapshotError, match="cannot inspect"):
        load_retained_result(case, result)
    assert selected.read_bytes() == alias.read_bytes() == original


@pytest.fixture
def deterministic_saved(tmp_path: Path) -> tuple[CaseEvidence, ResultSnapshot]:
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
    loaded = load_retained_result(case, Path(json.loads(outcome.stdout)["result_path"]))
    assert loaded.deterministic is not None
    return loaded.evidence, loaded.deterministic


@pytest.mark.parametrize(
    "fault", ["parent", "unknown", "false-invalid", "unsafe-observed", "candidate", "final"]
)
def test_deterministic_trial_validation_rejects_source_inconsistent_claims(
    deterministic_saved: tuple[CaseEvidence, ResultSnapshot], fault: str
) -> None:
    evidence, snapshot = deterministic_saved
    node = next(node for node in evidence.context.nodes if node.mutable and not node.protected)
    requested = (node.id,)
    base: tuple[str, ...] = ()
    effective = tuple(sorted(evidence.context.dependency_closure(requested)))
    invalid = fault == "false-invalid"
    if fault == "parent":
        base = effective
    elif fault == "unknown":
        requested = ("ctx-unknown",)
    elif fault == "unsafe-observed":
        requested = (next(n.id for n in evidence.context.nodes if n.protected),)
    if invalid:
        effective = ()
    trial = CandidateTrial(
        trial_key=trial_key("chunk", base, requested),
        candidate_digest=None if invalid else "f" * 64,
        base_removals=base,
        requested_removals=requested,
        effective_removals=effective,
        target_observed=None if invalid else False,
        classification="invalid" if invalid else "loses",
        phase="chunk",
        runner_executed=not invalid,
        observation_id=None if invalid else "fixture-observation",
        observation_attempt=None if invalid else 1,
    )
    checkpoint = ReductionCheckpoint.model_validate(
        {
            **snapshot.checkpoint.model_dump(),
            "trials": () if fault == "final" else (trial,),
            "candidate_runs_used": 0 if fault == "final" or invalid else 1,
            "failed_candidate_observation_ids": (),
            "removed_context_ids": effective if fault == "final" else (),
            "final_sweep_complete": False,
        }
    )
    changed = ResultSnapshot.model_validate({**snapshot.model_dump(), "checkpoint": checkpoint})
    with pytest.raises(ValueError, match=r"trial|retained context"):
        inspection_storage._validate_deterministic_trials(changed, evidence)


@pytest.mark.parametrize("fault", ["calibration", "blocks", "plan"])
def test_sampled_state_and_result_require_identical_recorded_evidence(
    tmp_path: Path, fault: str
) -> None:
    _, root = _sampled(tmp_path, with_trial=True)
    result = SampledReductionEvidence.model_validate_json((root / "result.json").read_text())
    state = SampledRunState.model_validate_json((root / "sampling/sampling-state.json").read_text())
    data = state.model_dump()
    if fault == "calibration":
        data["calibration_observation_ids"] = ("unknown-observation",)
    elif fault == "blocks":
        data["blocks"] = ()
    else:
        data["blocks"][0]["plan"]["schedule_seed"] += 1
        plan = state.blocks[0].plan
        data["blocks"][0]["plan"]["schedule"] = sampling_schedule(
            plan.targets_for("baseline").max_attempts,
            plan.targets_for("candidate").max_attempts,
            data["blocks"][0]["plan"]["schedule_seed"],
        )
    changed = SampledRunState.model_validate(data)
    with pytest.raises(ValueError, match=r"sampled result|sampling snapshot"):
        inspection_storage._validate_result_state(result, changed)


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


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_recorded_candidate_refusal_is_inspectable_without_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    case, result = _sampled(tmp_path, harness, with_trial=True)
    for name in ("result.json", "sampling/sampling-state.json"):
        path = result / name
        document = json.loads(path.read_text())
        checkpoint = document["checkpoint"]
        recorded = checkpoint["trials"][0]
        trial = SampledInvalidCandidateTrial(
            trial_key=recorded["trial_key"],
            base_removals=(),
            requested_removals=tuple(recorded["requested_removals"]),
            effective_removals=(),
            phase="chunk",
            classification="invalid",
        )
        checkpoint["trials"] = [trial.model_dump(mode="json")]
        if "blocks" in document:
            document["blocks"] = []
        _write(path, document)
    monkeypatch.setattr(subprocess, "Popen", _forbid_runtime)
    monkeypatch.setattr(sqlite3, "connect", _forbid_runtime)

    loaded = load_retained_result(case, result)

    assert loaded.sampled is not None
    assert loaded.sampled.checkpoint.trials == (trial,)
    assert loaded.retained_ids == tuple(node.id for node in loaded.evidence.context.nodes)
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
        "schema",
        "summary-array",
        "sampling-extra",
        "session-store",
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
    if change == "schema":
        path = result / "summary.json"
        value = json.loads(path.read_text())
        value["schema_version"] = 2
        _write(path, value)
    elif change == "summary-array":
        _write(result / "summary.json", [])
    elif change == "sampling-extra":
        _write(result / "sampling/extra.json", {})
    elif change == "session-store":
        path = result / "cli-session.json"
        value = json.loads(path.read_text())
        value["observation_store"] = str(tmp_path / "another.sqlite")
        _write(path, value)
    elif change in {"kind", "summary-count"}:
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", 1791331200),
        ("started_at", "1791331200"),
        ("started_at", "20261018"),
        ("started_at", "2026-10-07"),
        ("started_at", "2026-10-07T00:00:00z"),
        ("started_at", "2026-10-07 00:00:00Z"),
        ("started_at", "2026-10-07T00:00:00"),
        ("started_at", "0001-01-01T00:00:00+23:59"),
        ("started_at", "9999-12-31T23:59:59-23:59"),
        ("original_workspace", "relative"),
        ("observation_store", "/private/../store"),
        ("run_directory", "/private/foreign-run"),
    ],
)
def test_detached_inspection_uses_shared_metadata_validation(
    tmp_path: Path, field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, result = _sampled(tmp_path)
    path = result / "cli-session.json"
    metadata = json.loads(path.read_text())
    metadata[field] = value
    _write(path, metadata)
    before = {p: p.read_bytes() for root in (case, result) for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(subprocess, "Popen", _forbid_runtime)
    monkeypatch.setattr(sqlite3, "connect", _forbid_runtime)
    with pytest.raises(ResultSnapshotError) as error:
        load_retained_result(case, result)
    assert str(error.value) == "cannot inspect a valid reduction snapshot for this case"
    assert {p: p.read_bytes() for p in before} == before


def test_detached_inspection_compares_recorded_paths_as_paths(tmp_path: Path) -> None:
    case, result = _sampled(tmp_path)
    path = result / "cli-session.json"
    metadata = json.loads(path.read_text())
    metadata["source_case"] += "/."
    metadata["observation_store"] = metadata["observation_store"].replace("/missing-", "//missing-")
    _write(path, metadata)
    assert load_retained_result(case, result).sampled is not None
