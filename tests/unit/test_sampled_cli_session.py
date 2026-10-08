"""Private sampled CLI metadata and immutable publication boundaries."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import ithrl.cli.sampled_session as sessions
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli.sampled_session import (
    SampledCliSession,
    SampledCliSessionError,
    load_cli_run_state,
    load_cli_session,
    save_cli_result,
    save_cli_session,
)
from ithrl.context import content_digest
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
from ithrl.observations import (
    ObservationIdentity,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.reduce import SampledConfirmation, SampledReductionCheckpoint
from ithrl.results import SampledReductionEvidence, load_sampling_snapshot
from ithrl.run_state import (
    SampledFinalSelection,
    SampledRunState,
    SampledRunStateFile,
    SamplingBudgetApproval,
)
from ithrl.synthetic import extract_inference

_TIME = datetime(2026, 10, 7, tzinfo=UTC)
_TRACE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


@dataclass
class Bundle:
    workspace: CaseWorkspace
    evidence: CaseEvidence
    state: SampledRunState
    store: ObservationStore
    result: SampledReductionEvidence
    session: SampledCliSession


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    original = extract_inference(
        _TRACE, inference_call_id="synthetic-ac-001", case_path=tmp_path / "source"
    ).load()
    deterministic = ExperimentDefinition.from_toml(original.default_experiment)
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
        target=deterministic.target,
        oracle=deterministic.oracle,
        runner=NativeRunnerConfig(type="codex", required_capability_digest="a" * 64),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=2),
        reduction=reduction,
    )
    workspace = CaseWorkspace.create(
        tmp_path / "case",
        manifest=original.manifest,
        request=original.request,
        nodes=original.context.nodes,
        default_experiment=experiment.to_toml(),
        decision=original.decision,
    )
    evidence = workspace.load()
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint="credential-free-fixture",
        execution_plan_digest="b" * 64,
        target_digest="c" * 64,
        algorithm_digest="d" * 64,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    allowance = SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=50,
        max_wall_time_seconds=500,
        max_candidate_comparisons=5,
        confirmation_provider_calls=6,
        confirmation_runner_seconds=60,
    )
    state_file = SampledRunStateFile.create(
        case_root=workspace.root,
        run_id="run-1",
        store=store,
        started_at=_TIME,
        original_identity=identity,
        original_candidate_digest="e" * 64,
        provider_calls_per_attempt=1,
        approval=SamplingBudgetApproval(
            approval_id="approval-1", approved_at=_TIME, allowance=allowance
        ),
    )
    checkpoint = SampledReductionCheckpoint(
        reduction=reduction,
        original_identity=identity,
        original_candidate_digest="e" * 64,
        best_candidate_digest="e" * 64,
        removed_context_ids=(),
    )
    with state_file.acquire_lock(store):
        state = state_file.save_checkpoint(store, checkpoint)
    original_workspace = tmp_path / "original"
    original_workspace.mkdir(mode=0o700)
    session = SampledCliSession(
        run_id="run-1",
        run_directory=state_file.path.parent,
        source_case=workspace.root.absolute(),
        original_workspace=original_workspace.absolute(),
        observation_store=store.path.absolute(),
        experiment=experiment,
        resource_limits=ResourceLimits(max_provider_calls=1),
        original_identity=identity,
        started_at=_TIME,
    )
    result = SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-incomplete",
        selected_candidate_digest="e" * 64,
        selected_at=_TIME,
    )
    save_cli_session(state_file.path.parent, session)
    return Bundle(workspace, evidence, state, store, result, session)


def _publish(bundle: Bundle) -> Path:
    return save_cli_result(
        bundle.workspace,
        bundle.evidence,
        bundle.state,
        bundle.store,
        bundle.result,
        bundle.session,
        {"status": "incomplete", "result_path": None},
    )


def test_live_metadata_roundtrip_is_immutable_and_private(bundle: Bundle) -> None:
    assert load_cli_session(bundle.session.run_directory) == bundle.session
    assert load_cli_run_state(bundle.session.run_directory) == bundle.state
    path = bundle.session.run_directory / "cli-session.json"
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SampledCliSessionError):
        save_cli_session(bundle.session.run_directory, bundle.session)
    assert path.read_bytes() == original
    with pytest.raises(ValidationError):
        bundle.session.__setattr__("run_id", "changed")


@pytest.mark.parametrize(
    "change",
    ["relative", "traversal", "foreign-run", "calibration-only", "identity", "naive-time", "extra"],
)
def test_session_contract_rejects_unbound_inputs(bundle: Bundle, change: str) -> None:
    values = bundle.session.model_dump()
    match change:
        case "relative":
            values["original_workspace"] = Path("relative")
        case "traversal":
            values["observation_store"] = bundle.session.source_case / ".." / "store"
        case "foreign-run":
            values["run_directory"] = bundle.session.source_case / ".runs" / "another"
        case "calibration-only":
            values["experiment"] = bundle.session.experiment.model_copy(update={"reduction": None})
        case "identity":
            values["original_identity"] = bundle.session.original_identity.model_copy(
                update={"experiment_digest": "wrong"}
            )
        case "naive-time":
            values["started_at"] = _TIME.replace(tzinfo=None)
        case "extra":
            values["credential"] = "never-accepted"
    with pytest.raises(ValidationError):
        SampledCliSession.model_validate(values)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode", "oversized", "duplicate"])
def test_session_read_refuses_unsafe_files_without_modifying_target(
    bundle: Bundle, unsafe: str, tmp_path: Path
) -> None:
    path = bundle.session.run_directory / "cli-session.json"
    original = path.read_bytes()
    alias = tmp_path / "alias.json"
    if unsafe in {"symlink", "hardlink"}:
        path.rename(alias)
        if unsafe == "symlink":
            path.symlink_to(alias)
        else:
            os.link(alias, path)
    elif unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "oversized":
        path.write_bytes(b" " * (sessions._MAX_SESSION_BYTES + 1))
    else:
        path.write_text('{"run_id":"run-1","run_id":"run-1"}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SampledCliSessionError):
        load_cli_session(bundle.session.run_directory)
    assert path.read_bytes() == before
    if unsafe in {"symlink", "hardlink"}:
        assert alias.read_bytes() == original


def test_result_roundtrip_preserves_source_and_complete_private_snapshot(bundle: Bundle) -> None:
    protected = {
        name: (bundle.workspace.root / name).read_bytes()
        for name in (
            "case.toml",
            "context.jsonl",
            "request.json",
            "experiments/default.toml",
        )
    }
    destination = _publish(bundle)
    assert load_cli_session(destination) == bundle.session
    assert load_sampling_snapshot(destination / "sampling", bundle.store) == bundle.state
    assert json.loads((destination / "summary.json").read_text())["result_path"] == str(destination)
    retained = (destination / "retained-context.jsonl").read_text().splitlines()
    assert retained == [node.model_dump_json() for node in bundle.evidence.context.nodes]
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir() if path.is_file()
    )
    assert protected == {name: (bundle.workspace.root / name).read_bytes() for name in protected}


def test_readonly_live_state_does_not_open_or_create_sqlite(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_self: ObservationStore) -> None:
        raise AssertionError("read-only metadata must not initialize SQLite")

    monkeypatch.setattr(ObservationStore, "initialize", refuse)
    assert load_cli_run_state(bundle.session.run_directory) == bundle.state


def test_result_pointer_cannot_substitute_for_live_state(bundle: Bundle) -> None:
    destination = _publish(bundle)
    with pytest.raises(SampledCliSessionError):
        load_cli_run_state(destination)


def test_historical_result_points_to_current_live_authority(bundle: Bundle) -> None:
    destination = _publish(bundle)
    state_file = SampledRunStateFile.open(bundle.session.run_directory)
    allowance = bundle.state.allowance.model_copy(update={"max_provider_calls": 51})
    with state_file.acquire_lock(bundle.store):
        current = state_file.extend_budget(
            bundle.store,
            SamplingBudgetApproval(
                approval_id="approval-2", approved_at=_TIME, allowance=allowance
            ),
        )
    assert load_cli_session(destination) == bundle.session
    assert load_cli_run_state(bundle.session.run_directory) == current
    assert load_sampling_snapshot(destination / "sampling", bundle.store) == bundle.state
    with pytest.raises(SampledCliSessionError):
        _publish(bundle)
    assert len(tuple(destination.parent.iterdir())) == 1


@pytest.mark.parametrize("change", [None, "selection-time", "missing-confirmation", "plan"])
def test_result_binds_durable_selection_and_unstarted_confirmation(
    bundle: Bundle, change: str | None
) -> None:
    checkpoint = bundle.result.checkpoint
    holdout = content_digest({"run_id": bundle.state.run_id, "role": "confirmation"})
    seed = int(holdout[:16], 16)
    plan = SamplingBlockPlan(
        block_id=f"confirmation-{holdout}",
        role="confirmation",
        reduction=checkpoint.reduction,
        original_identity=bundle.session.original_identity,
        candidate_identity=bundle.session.original_identity,
        original_candidate_digest=checkpoint.original_candidate_digest,
        candidate_digest=checkpoint.best_candidate_digest,
        frozen_at=_TIME + timedelta(seconds=2),
        schedule_seed=seed,
        schedule=sampling_schedule(3, 3, seed),
        provider_calls_per_attempt=1,
    )
    selection = SampledFinalSelection(
        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
        selected_at=_TIME + timedelta(seconds=1),
        plan=plan,
    )
    state_file = SampledRunStateFile.open(bundle.session.run_directory)
    with state_file.acquire_lock(bundle.store):
        bundle.state = state_file.select_candidate(bundle.store, selection)
    confirmation = SampledConfirmation.from_evidence(SamplingBlockEvidence(plan=plan, attempts=()))
    bundle.result = bundle.result.model_copy(
        update={"selected_at": selection.selected_at, "confirmation": confirmation}
    )
    destination = _publish(bundle)
    assert load_cli_session(destination) == bundle.session
    if change is None:
        return
    if change == "selection-time":
        changed = bundle.result.model_copy(
            update={"selected_at": selection.selected_at + timedelta(milliseconds=500)}
        )
    elif change == "missing-confirmation":
        changed = bundle.result.model_copy(update={"confirmation": None})
    else:
        changed_plan = plan.model_copy(update={"frozen_at": plan.frozen_at + timedelta(seconds=1)})
        changed = bundle.result.model_copy(
            update={
                "confirmation": SampledConfirmation.from_evidence(
                    SamplingBlockEvidence(plan=changed_plan, attempts=())
                )
            }
        )
    (destination / "result.json").write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(SampledCliSessionError):
        load_cli_session(destination)
    bundle.result = changed
    with pytest.raises(SampledCliSessionError):
        _publish(bundle)


def test_result_rejects_changed_pointer_and_unrelated_location(
    bundle: Bundle, tmp_path: Path
) -> None:
    destination = _publish(bundle)
    pointer = destination / "cli-session.json"
    changed = bundle.session.model_copy(
        update={"resource_limits": ResourceLimits(max_provider_calls=2)}
    )
    pointer.write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(SampledCliSessionError):
        load_cli_session(destination)
    pointer.write_text(bundle.session.model_dump_json(), encoding="utf-8")
    destination.rename(tmp_path / "unrelated")
    with pytest.raises(SampledCliSessionError):
        load_cli_session(tmp_path / "unrelated")


def test_result_publication_refuses_existing_destination(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    destination = _publish(bundle)
    original = (destination / "result.json").read_bytes()
    with pytest.raises(SampledCliSessionError):
        _publish(bundle)
    assert (destination / "result.json").read_bytes() == original
    assert not tuple(destination.parent.glob(".sampled-*"))


def test_failed_snapshot_publication_cleans_only_owned_staging(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kwargs: object) -> Path:
        raise ValueError("injected safe snapshot refusal")

    monkeypatch.setattr(sessions, "write_sampling_snapshot", refuse)
    with pytest.raises(SampledCliSessionError):
        _publish(bundle)
    assert not tuple((bundle.workspace.root / "results").iterdir())
    assert load_cli_session(bundle.session.run_directory) == bundle.session


def test_snapshot_publisher_side_files_stay_outside_result(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = sessions.write_sampling_snapshot

    def publish(*, destination: Path, state: SampledRunState, store: ObservationStore) -> Path:
        path = publisher(destination=destination, state=state, store=store)
        (path.parent / "private-publication-marker").write_text("owned staging only")
        return path

    monkeypatch.setattr(sessions, "write_sampling_snapshot", publish)
    destination = _publish(bundle)
    assert load_cli_session(destination) == bundle.session
    assert not (destination / "private-publication-marker").exists()
    assert not tuple(destination.parent.glob(".sampled-*"))


def test_publication_rejects_mismatched_state_without_creating_results(bundle: Bundle) -> None:
    bundle.state = bundle.state.model_copy(update={"run_id": "different"})
    with pytest.raises(SampledCliSessionError):
        _publish(bundle)
    assert not (bundle.workspace.root / "results").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "extra", "sampling-extra"])
def test_result_read_refuses_unsafe_or_unexpected_entries(
    bundle: Bundle, unsafe: str, tmp_path: Path
) -> None:
    destination = _publish(bundle)
    path = destination / "result.json"
    original = path.read_bytes()
    if unsafe in {"symlink", "hardlink"}:
        alias = tmp_path / "result-alias.json"
        path.rename(alias)
        if unsafe == "symlink":
            path.symlink_to(alias)
        else:
            os.link(alias, path)
    else:
        parent = destination if unsafe == "extra" else destination / "sampling"
        (parent / "unexpected").write_text("never interpreted", encoding="utf-8")
    with pytest.raises(SampledCliSessionError):
        load_cli_session(destination)
    assert path.read_bytes() == original


def test_active_session_refuses_different_bound_state(bundle: Bundle) -> None:
    path = bundle.session.run_directory / "sampled-state.json"
    changed = json.loads(path.read_text())
    changed["started_at"] = "2026-10-07T00:00:01Z"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SampledCliSessionError):
        load_cli_session(bundle.session.run_directory)
    with pytest.raises(SampledCliSessionError):
        load_cli_run_state(bundle.session.run_directory)


@pytest.mark.parametrize("change", ["materialization", "profile", "selection-time"])
def test_result_rejects_snapshot_with_different_execution_association(
    bundle: Bundle, change: str
) -> None:
    destination = _publish(bundle)
    state_path = destination / "sampling" / "sampling-state.json"
    if change == "selection-time":
        path = destination / "result.json"
        changed = bundle.result.model_copy(update={"selected_at": _TIME - timedelta(seconds=1)})
        path.write_text(changed.model_dump_json(), encoding="utf-8")
    else:
        changed = json.loads(state_path.read_text())
        key = (
            "original_candidate_digest"
            if change == "materialization"
            else "provider_calls_per_attempt"
        )
        changed[key] = "f" * 64 if change == "materialization" else 2
        state_path.write_text(json.dumps(changed), encoding="utf-8")
    before = {path: path.read_bytes() for path in (state_path, destination / "result.json")}
    with pytest.raises(SampledCliSessionError):
        load_cli_session(destination)
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode", "oversized", "duplicate"])
def test_live_state_loader_refuses_unsafe_files(
    bundle: Bundle, unsafe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = bundle.session.run_directory / "sampled-state.json"
    if unsafe in {"symlink", "hardlink"}:
        alias = tmp_path / "state-alias.json"
        path.rename(alias)
        if unsafe == "symlink":
            path.symlink_to(alias)
        else:
            os.link(alias, path)
    elif unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "oversized":
        monkeypatch.setattr(sessions, "_MAX_RESULT_BYTES", path.stat().st_size - 1)
    else:
        path.write_text('{"run_id":"run-1","run_id":"run-1"}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SampledCliSessionError):
        load_cli_run_state(bundle.session.run_directory)
    assert path.read_bytes() == before
