"""Private immutable sampled progress stays bound to authoritative observations."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from filelock import FileLock

from ithrl.experiment import (
    SampledConfirmationPhase,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
)
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
    SamplingAttempt,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.reduce import SampledReductionCheckpoint
from ithrl.results import (
    ResultSnapshotError,
    load_sampling_snapshot,
    storage,
    write_sampling_snapshot,
)
from ithrl.run_state import (
    SampledRunState,
    SamplingBlockProgress,
    SamplingBudgetApproval,
    SamplingDispatchReservation,
    SamplingObservationReference,
)
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting

_TIME = datetime(2026, 10, 4, tzinfo=UTC)


def _progress(
    root: Path, *, observations: int = 2, pending: bool = False, stale: bool = False
) -> tuple[SampledRunState, ObservationStore]:
    store = ObservationStore(root / "observations.sqlite")
    store.initialize()
    identity = ObservationIdentity(
        case_digest="a" * 64,
        experiment_digest="b" * 64,
        runner_fingerprint="mock-sampling",
        execution_plan_digest="c" * 64,
        target_digest="d" * 64,
        algorithm_digest="e" * 64,
    )
    target = SamplingArmTarget(required_valid=1, max_attempts=1)
    plan = SamplingBlockPlan(
        block_id="block-1",
        role="search",
        reduction=SampledReductionPlan(
            search=SampledSearchPhase(original=target, candidate=target),
            confirmation=SampledConfirmationPhase(
                original=target, candidate=target, one_sided_confidence=0.95
            ),
            margin=0.2,
            freshness_seconds=60,
        ),
        original_identity=identity,
        candidate_identity=identity.model_copy(update={"execution_plan_digest": "f" * 64}),
        original_candidate_digest="1" * 64,
        candidate_digest="2" * 64,
        frozen_at=_TIME,
        schedule_seed=17,
        schedule=sampling_schedule(1, 1, 17),
        provider_calls_per_attempt=1,
    )
    references = []
    for position, arm in enumerate(plan.schedule[:observations]):
        evidence = AttemptEvidence(
            attempt_id=f"attempt-{position + 1:032x}",
            attempt_digest=f"{position + 1:064x}",
            termination_reason="completed",
            provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
            effective_policy_digest="9" * 64,
            accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
        )
        observation = Observation(
            **(
                plan.original_identity if arm == "baseline" else plan.candidate_identity
            ).model_dump(),
            observation_id=f"obs-{position}",
            candidate_digest=plan.original_candidate_digest
            if arm == "baseline"
            else plan.candidate_digest,
            role=ObservationRole.SEARCH,
            arm=ObservationArm(arm),
            attempt=1,
            recorded_at=_TIME + timedelta(seconds=position + 1 + (100 if stale else 0)),
            outcome=ObservationOutcome.MATCH,
            attempt_evidence=evidence,
        )
        store.append(observation)
        references.append(
            SamplingObservationReference(
                observation_id=observation.observation_id,
                attempt_id=evidence.attempt_id,
                attempt_digest=evidence.attempt_digest,
                schedule_position=position,
            )
        )
    reservation = (
        SamplingDispatchReservation(
            block_id=plan.block_id,
            block_digest=plan.digest,
            schedule_position=observations,
            arm=plan.schedule[observations],
            attempt=1,
            attempt_id="attempt-" + "8" * 32,
            attempt_digest="8" * 64,
            reserved_at=_TIME + timedelta(seconds=3),
            provider_calls=1,
            runner_seconds=10,
        )
        if pending
        else None
    )
    allowance = SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=10,
        max_wall_time_seconds=100,
        max_candidate_comparisons=2,
        confirmation_provider_calls=2,
        confirmation_runner_seconds=20,
    )
    state = SampledRunState(
        run_id="run-1",
        source_case=str(root / "case"),
        observation_store=str(store.path),
        started_at=_TIME,
        original_identity=identity,
        original_candidate_digest=plan.original_candidate_digest,
        provider_calls_per_attempt=plan.provider_calls_per_attempt,
        approvals=(
            SamplingBudgetApproval(
                approval_id="approval-1", approved_at=_TIME, allowance=allowance
            ),
        ),
        blocks=(SamplingBlockProgress(plan=plan, observations=tuple(references)),),
        pending=reservation,
    )
    return state, store


@pytest.mark.parametrize(("count", "pending"), [(2, False), (1, False), (0, False), (1, True)])
def test_sampling_snapshot_round_trip(root: Path, count: int, pending: bool) -> None:
    state, store = _progress(root, observations=count, pending=pending)
    destination = root / "snapshots" / "first"
    assert write_sampling_snapshot(destination=destination, state=state, store=store) == destination
    assert load_sampling_snapshot(destination, store) == state
    assert {path.name for path in destination.iterdir()} == {"sampling-state.json"}
    payload = json.loads((destination / "sampling-state.json").read_text())
    assert all("outcome" not in item for item in payload["blocks"][0]["observations"])
    assert all("accounting" not in item for item in payload["blocks"][0]["observations"])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


def test_sampling_snapshot_private_modes(root: Path) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o700
        assert (destination / "sampling-state.json").stat().st_mode & 0o777 == 0o600


def test_sampling_snapshot_archives_stale_evidence_without_reclaiming_it(root: Path) -> None:
    state, store = _progress(root, stale=True)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    assert load_sampling_snapshot(destination, store) == state


def test_sampling_snapshot_write_rejects_model_copy_validation_bypass(root: Path) -> None:
    state, store = _progress(root)
    altered = state.blocks[0].model_copy(
        update={"observations": (state.blocks[0].observations[0],) * 2}
    )
    invalid_state = state.model_copy(update={"blocks": (altered,)})
    with pytest.raises(ResultSnapshotError, match="state or references are invalid"):
        write_sampling_snapshot(destination=root / "snapshot", state=invalid_state, store=store)
    assert not (root / "snapshot").exists()


def test_sampling_snapshot_does_not_overwrite(root: Path) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    original = (destination / "sampling-state.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_sampling_snapshot(destination=destination, state=state, store=store)
    assert (destination / "sampling-state.json").read_bytes() == original


def test_sampling_snapshot_existing_empty_destination_is_not_replaced(root: Path) -> None:
    state, store = _progress(root)
    destination = root / "empty"
    destination.mkdir()
    before = destination.stat().st_ino
    with pytest.raises(FileExistsError):
        write_sampling_snapshot(destination=destination, state=state, store=store)
    assert destination.stat().st_ino == before
    assert not tuple(destination.iterdir())


def test_sampling_snapshot_publication_lock_is_nonblocking(root: Path) -> None:
    state, store = _progress(root)
    with (
        FileLock(root / ".snapshot.sampling.lock", mode=0o600),
        pytest.raises(ResultSnapshotError, match="already in progress"),
    ):
        write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    assert not (root / "snapshot").exists()


def test_sampling_snapshot_two_supported_publishers_cannot_overwrite(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, store = _progress(root)
    destination = root / "snapshot"
    started, finish = threading.Event(), threading.Event()
    write = storage._write_json

    def paused_write(path: Path, value: object) -> None:
        started.set()
        assert finish.wait(5), "publisher was not released"
        write(path, value)

    monkeypatch.setattr(storage, "_write_json", paused_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            write_sampling_snapshot, destination=destination, state=state, store=store
        )
        try:
            assert started.wait(5), "publisher did not acquire ownership"
            with pytest.raises(ResultSnapshotError, match="already in progress"):
                write_sampling_snapshot(destination=destination, state=state, store=store)
        finally:
            finish.set()
        assert first.result(timeout=5) == destination
    assert load_sampling_snapshot(destination, store) == state


@pytest.mark.parametrize("unsafe", ["link", "hard-link", "mode"])
def test_sampling_snapshot_rejects_unsafe_publication_lock(root: Path, unsafe: str) -> None:
    state, store = _progress(root)
    lock = root / ".snapshot.sampling.lock"
    if unsafe == "link":
        lock.symlink_to(store.path)
    elif unsafe == "hard-link":
        os.link(store.path, lock)
    else:
        if os.name != "posix":
            pytest.skip("POSIX mode enforcement")
        lock.write_text("")
        lock.chmod(0o644)
    with pytest.raises(ResultSnapshotError):
        write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    assert not (root / "snapshot").exists()


def test_sampling_snapshot_checkpoint_is_revalidated_against_database(root: Path) -> None:
    state, store = _progress(root, observations=1)
    progress = state.blocks[0]
    observation = store.get(progress.observations[0].observation_id)
    assert observation is not None
    evidence = SamplingBlockEvidence(
        plan=progress.plan,
        attempts=(SamplingAttempt(schedule_position=0, observation=observation),),
    )
    checkpoint = SampledReductionCheckpoint(
        reduction=progress.plan.reduction,
        original_identity=state.original_identity,
        original_candidate_digest=state.original_candidate_digest,
        best_candidate_digest=state.original_candidate_digest,
        removed_context_ids=(),
        active_block=evidence,
    )
    state = state.model_copy(update={"checkpoint": checkpoint})
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    assert load_sampling_snapshot(destination, store) == state
    path = destination / "sampling-state.json"
    payload = json.loads(path.read_text())
    payload["checkpoint"]["active_block"]["attempts"][0]["observation"]["outcome"] = "no_match"
    path.write_text(json.dumps(payload))
    with pytest.raises(ResultSnapshotError):
        load_sampling_snapshot(destination, store)


@pytest.mark.parametrize("change", ["missing", "reuse", "attempt", "embedded", "provenance"])
def test_sampling_snapshot_rejects_tampered_references(root: Path, change: str) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    path = destination / "sampling-state.json"
    payload = json.loads(path.read_text())
    reference = payload["blocks"][0]["observations"][0]
    if change == "missing":
        reference["observation_id"] = "missing-observation"
    elif change == "reuse":
        payload["blocks"][0]["observations"][1]["observation_id"] = reference["observation_id"]
    elif change == "attempt":
        reference["attempt_digest"] = "0" * 64
    elif change == "embedded":
        reference["outcome"] = "no_match"
    else:
        payload["blocks"][0]["plan"]["candidate_identity"]["execution_plan_digest"] = "0" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(ResultSnapshotError, match="state or references are invalid"):
        load_sampling_snapshot(destination, store)


@pytest.mark.parametrize("count", [0, 2])
def test_sampling_snapshot_missing_database_is_not_created(root: Path, count: int) -> None:
    state, store = _progress(root, observations=count)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    store.path.unlink()
    with pytest.raises(ResultSnapshotError):
        load_sampling_snapshot(destination, store)
    assert not store.path.exists()


@pytest.mark.parametrize(
    "source", ['{"run_id": "x", "run_id": "secret"}', '{"private": NaN}', "[]"]
)
def test_sampling_snapshot_rejects_invalid_json_without_leaking_content(
    root: Path, source: str
) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    (destination / "sampling-state.json").write_text(source)
    with pytest.raises(ResultSnapshotError) as caught:
        load_sampling_snapshot(destination, store)
    assert "secret" not in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "unsafe", ["directory-mode", "file-mode", "file-link", "hard-link", "root-link", "extra"]
)
def test_sampling_snapshot_rejects_unsafe_file_set(root: Path, unsafe: str) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    path = destination / "sampling-state.json"
    if unsafe == "directory-mode":
        if os.name != "posix":
            pytest.skip("POSIX mode enforcement")
        destination.chmod(0o755)
    elif unsafe == "file-mode":
        if os.name != "posix":
            pytest.skip("POSIX mode enforcement")
        path.chmod(0o644)
    elif unsafe == "file-link":
        copied = root / "copied.json"
        path.rename(copied)
        path.symlink_to(copied)
    elif unsafe == "hard-link":
        os.link(path, root / "copied.json")
    elif unsafe == "root-link":
        link = root / "linked"
        link.symlink_to(destination, target_is_directory=True)
        destination = link
    else:
        (destination / "unexpected-private-name").write_text("secret")
    with pytest.raises(ResultSnapshotError) as caught:
        load_sampling_snapshot(destination, store)
    assert "unexpected-private-name" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_sampling_snapshot_enforces_read_and_write_size_limit(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, store = _progress(root)
    destination = write_sampling_snapshot(destination=root / "snapshot", state=state, store=store)
    monkeypatch.setattr(storage, "_MAX_RESULT_FILE_BYTES", 1)
    with pytest.raises(ResultSnapshotError):
        load_sampling_snapshot(destination, store)
    with pytest.raises(ResultSnapshotError):
        write_sampling_snapshot(destination=root / "too-large", state=state, store=store)
    assert not (root / "too-large").exists()
    assert not tuple(root.glob(".too-large-*"))


@pytest.mark.parametrize("failure", ["write", "file-sync", "rename", "staging-sync", "parent-sync"])
def test_sampling_snapshot_publication_failure_is_private_and_coherent(
    root: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state, store = _progress(root)
    destination = root / "snapshot"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected failure")

    if failure == "write":
        monkeypatch.setattr(storage, "_write_json", fail)
    elif failure == "file-sync":
        monkeypatch.setattr(os, "fsync", fail)
    elif failure == "rename":
        monkeypatch.setattr(Path, "rename", fail)
    else:
        sync = storage._sync_directory

        def fail_sync(path: Path) -> None:
            if (failure == "parent-sync") == (path == root):
                fail()
            sync(path)

        monkeypatch.setattr(storage, "_sync_directory", fail_sync)
    with pytest.raises(OSError, match="injected failure"):
        write_sampling_snapshot(destination=destination, state=state, store=store)
    assert not tuple(root.glob(".snapshot-*"))
    if failure == "parent-sync":
        assert load_sampling_snapshot(destination, store) == state
    else:
        assert not destination.exists()
