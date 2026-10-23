"""Owned durable sampled dispatch and exact append/publication reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
import tracemalloc
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.context import content_digest
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
    ObservationStoreBusyError,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.reduce import SampledCandidateTrial, SampledComparison, SampledReductionCheckpoint
from ithrl.reduce.contracts import trial_key
from ithrl.run_state import (
    RunStateBusyError,
    RunStateError,
    SampledFinalSelection,
    SampledRunState,
    SampledRunStateFile,
    SamplingBlockProgress,
    SamplingBudgetApproval,
    SamplingDispatchReservation,
)
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting, TokenUsage

_TIME = datetime(2026, 10, 4, tzinfo=UTC)


def _plan(**changes: Any) -> SamplingBlockPlan:
    targets = SamplingArmTarget(required_valid=2, max_attempts=3)
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(original=targets, candidate=targets),
        confirmation=SampledConfirmationPhase(
            original=targets, candidate=targets, one_sided_confidence=0.95
        ),
        margin=0.2,
        freshness_seconds=60,
    )
    identity = ObservationIdentity(
        case_digest="a" * 64,
        experiment_digest="b" * 64,
        runner_fingerprint="fixture",
        execution_plan_digest="c" * 64,
        target_digest="d" * 64,
        algorithm_digest="e" * 64,
    )
    return SamplingBlockPlan.model_validate(
        {
            "block_id": "block-1",
            "role": "search",
            "procedure": reduction,
            "original_identity": identity,
            "candidate_identity": identity.model_copy(update={"execution_plan_digest": "f" * 64}),
            "original_candidate_digest": "1" * 64,
            "candidate_digest": "2" * 64,
            "frozen_at": _TIME,
            "schedule_seed": 3,
            "schedule": sampling_schedule(3, 3, 3),
            "provider_calls_per_attempt": 2,
            **changes,
        }
    )


def _approval(**changes: Any) -> SamplingBudgetApproval:
    allowance = SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=50,
        max_wall_time_seconds=500,
        max_candidate_comparisons=5,
        confirmation_provider_calls=12,
        confirmation_runner_seconds=60,
        max_tokens=10_000,
    )
    return SamplingBudgetApproval(
        approval_id="approval-1",
        approved_at=_TIME,
        allowance=SamplingBudgetAllowance.model_validate({**allowance.model_dump(), **changes}),
    )


def _create(
    tmp_path: Path, *, admit_block: bool = True
) -> tuple[SampledRunStateFile, ObservationStore, SamplingBlockPlan]:
    case = tmp_path / "case"
    case.mkdir()
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    plan = _plan()
    file = SampledRunStateFile.create(
        case_root=case,
        run_id="run-1",
        store=store,
        started_at=_TIME,
        original_identity=plan.original_identity,
        original_candidate_digest=plan.original_candidate_digest,
        provider_calls_per_attempt=plan.provider_calls_per_attempt,
        approval=_approval(),
    )
    if admit_block:
        with file.acquire_lock(store):
            file.admit_block(store, plan)
    return file, store, plan


def _reservation(
    plan: SamplingBlockPlan, position: int = 0, attempt: int = 1
) -> SamplingDispatchReservation:
    return SamplingDispatchReservation(
        block_id=plan.block_id,
        block_digest=plan.digest,
        schedule_position=position,
        arm=plan.schedule[position],
        attempt=attempt,
        attempt_id=f"attempt-{position:032x}",
        attempt_digest=f"{position:064x}",
        reserved_at=plan.frozen_at + timedelta(seconds=position),
        provider_calls=plan.provider_calls_per_attempt,
        runner_seconds=10,
    )


def _observation(
    plan: SamplingBlockPlan, reservation: SamplingDispatchReservation, *, unknown: bool = False
) -> Observation:
    arm = reservation.arm
    identity = plan.original_identity if arm == "baseline" else plan.candidate_identity
    digest = plan.original_candidate_digest if arm == "baseline" else plan.candidate_digest
    accounting = (
        None
        if unknown
        else RunAccounting(
            provider_calls_started=1, provider_calls_completed=1, elapsed_milliseconds=12
        )
    )
    return Observation(
        **identity.model_dump(),
        observation_id=f"observation-{reservation.schedule_position}",
        candidate_digest=digest,
        role=ObservationRole(plan.role),
        arm=ObservationArm(arm),
        attempt=reservation.attempt,
        recorded_at=reservation.reserved_at,
        outcome=ObservationOutcome.RUNNER_ERROR if unknown else ObservationOutcome.MATCH,
        error_category="transport" if unknown else None,
        attempt_evidence=AttemptEvidence(
            attempt_id=reservation.attempt_id,
            attempt_digest=reservation.attempt_digest,
            termination_reason="failed" if unknown else "completed",
            provider_acceptance_state=ProviderAcceptanceState.UNKNOWN
            if unknown
            else ProviderAcceptanceState.ACCEPTED,
            provider_calls_possible=2 if unknown else 0,
            effective_policy_digest="9" * 64,
            accounting=accounting,
            usage=None if unknown else TokenUsage(input_tokens=10, output_tokens=2),
        ),
    )


def test_private_state_roundtrip_hydrates_sqlite_and_preserves_pending_charge(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    assert file.path.stat().st_mode & 0o777 == 0o600
    assert file.path.parent.stat().st_mode & 0o777 == 0o700
    reservation = _reservation(plan)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        pending = file.usage(store, now=_TIME + timedelta(seconds=2))
        assert pending.actual_provider_calls == pending.possible_provider_calls == 0
        assert pending.pending_provider_calls == pending.charged_provider_calls == 2
        assert pending.pending_runner_seconds == 10
    reopened = SampledRunStateFile.open(file.path.parent)
    with reopened.acquire_lock(store):
        assert reopened.load(store).pending == reservation
        state = reopened.append_observation(store, _observation(plan, reservation))
        assert state.pending is None
        evidence = reopened.evidence(store)[0]
        assert evidence.attempts[0].observation == store.get("observation-0")
        usage = reopened.usage(store, now=_TIME + timedelta(seconds=3))
        assert usage.actual_provider_calls == 1
        assert usage.charged_provider_calls == 2
        assert usage.known_tokens == 12
        assert usage.runner_milliseconds == 12
        assert usage.unknown_runner_attempts == usage.unknown_token_attempts == 0
        assert usage.unknown_provider_attempts == 0
        assert usage.wall_time_seconds == 3
        assert (
            reopened.continue_block(store, expected_plan_digest=plan.digest, now=_TIME).plan == plan
        )
    encoded = json.loads(file.path.read_text())
    reference = encoded["blocks"][0]["observations"][0]
    assert "outcome" not in reference
    assert "attempt_evidence" not in reference


def test_interrupt_after_append_reconciles_pinned_attempt_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    observation = _observation(plan, reservation)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        original_write = file._write

        def fail_write(state: SampledRunState) -> None:
            raise OSError("injected publication failure")

        monkeypatch.setattr(file, "_write", fail_write)
        with pytest.raises(OSError, match="injected"):
            file.append_observation(store, observation)
        assert store.get(observation.observation_id) == observation
        assert file.load(store).pending == reservation
        monkeypatch.setattr(file, "_write", original_write)
    reopened = SampledRunStateFile.open(file.path)
    with reopened.acquire_lock(store):
        state = reopened.reconcile(store)
        assert state.pending is None
        assert reopened.reconcile(store) == state
        assert len(reopened.evidence(store)[0].attempts) == 1


def test_interrupt_before_append_keeps_reservation_and_refuses_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)

        def fail_append(observation: Observation) -> None:
            raise OSError("injected append failure")

        monkeypatch.setattr(store, "append", fail_append)
        with pytest.raises(OSError, match="injected"):
            file.append_observation(store, _observation(plan, reservation))
        with pytest.raises(RunStateError, match="unresolved"):
            file.reconcile(store)
        assert file.load(store).pending == reservation
        assert file.usage(store, now=_TIME).charged_provider_calls == 2


@pytest.mark.parametrize(
    "field",
    [
        "attempt_id",
        "attempt_digest",
        "attempt",
        "candidate_digest",
        "role",
        "arm",
        "execution_plan_digest",
    ],
)
def test_conflicting_append_cannot_release_or_replay_reservation(
    tmp_path: Path, field: str
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    observation = _observation(plan, reservation)
    wrong: object = "7" * 64
    if field == "attempt_id":
        wrong = "attempt-" + "7" * 32
    elif field == "attempt":
        wrong = 2
    elif field == "role":
        wrong = ObservationRole.CONFIRMATION
    elif field == "arm":
        wrong = (
            ObservationArm.CANDIDATE
            if observation.arm is ObservationArm.BASELINE
            else ObservationArm.BASELINE
        )
    if field in {"attempt_id", "attempt_digest"}:
        changed = observation.model_copy(
            update={
                "attempt_evidence": observation.attempt_evidence.model_copy(update={field: wrong})
            }
        )
    else:
        changed = observation.model_copy(update={field: wrong})
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        with pytest.raises(RunStateError, match="exact reserved"):
            file.append_observation(store, changed)
        assert store.get(observation.observation_id) is None
        assert file.load(store).pending == reservation


def test_conflicting_sqlite_attempt_reconciliation_refuses_generic_ordinal_match(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    observation = _observation(plan, reservation)
    wrong = observation.model_copy(
        update={
            "attempt_evidence": observation.attempt_evidence.model_copy(
                update={"attempt_id": "attempt-" + "f" * 32}
            )
        }
    )
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        store.append(wrong)
        with pytest.raises(RunStateError, match="exact reserved"):
            file.reconcile(store)
        assert file.load(store).pending == reservation


def test_ambiguous_acceptance_retains_upper_bound_and_unknown_usage(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, _observation(plan, reservation, unknown=True))
        usage = file.usage(store, now=_TIME)
        assert usage.actual_provider_calls == 0
        assert usage.possible_provider_calls == usage.charged_provider_calls == 2
        assert usage.unknown_runner_attempts == usage.unknown_token_attempts == 1
        with pytest.raises(RunStateError, match="incomplete continuation"):
            file.continue_block(store, expected_plan_digest=plan.digest, now=_TIME)


@pytest.mark.parametrize("now", [_TIME - timedelta(microseconds=1), _TIME + timedelta(seconds=60)])
def test_stale_or_future_block_refuses_continuation_without_mutation(
    tmp_path: Path, now: datetime
) -> None:
    file, store, plan = _create(tmp_path)
    before = file.path.read_bytes()
    with file.acquire_lock(store), pytest.raises(RunStateError, match="freshness"):
        file.continue_block(store, expected_plan_digest=plan.digest, now=now)
    assert file.path.read_bytes() == before


def test_budget_extension_keeps_frozen_plans_start_consumption_and_reservation(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    extension = _approval(max_provider_calls=100, max_wall_time_seconds=1000)
    extension = extension.model_copy(
        update={"approval_id": "approval-2", "approved_at": _TIME + timedelta(seconds=5)}
    )
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        before = file.load(store)
        after = file.extend_budget(store, extension)
        assert after.started_at == before.started_at
        assert after.blocks == before.blocks
        assert after.pending == before.pending
        assert after.original_identity == before.original_identity
        assert file.usage(store, now=_TIME + timedelta(seconds=6)).charged_provider_calls == 2
        assert after.allowance.max_provider_calls == 100
        assert after.allowance.confirmation_provider_calls == 12


@pytest.mark.parametrize(
    "changes",
    [
        {"max_provider_calls": 49},
        {"max_wall_time_seconds": 499},
        {"max_candidate_comparisons": 4},
        {"max_tokens": 9999},
        {"confirmation_provider_calls": 13},
        {"confirmation_runner_seconds": 61},
        {"inter_attempt_delay_seconds": 15},
        {"run_id": "another-run"},
    ],
)
def test_budget_extension_refuses_smaller_limits_or_changed_reserves(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    file, store, _ = _create(tmp_path)
    extension = _approval(**changes).model_copy(update={"approval_id": "approval-2"})
    before = file.path.read_bytes()
    with file.acquire_lock(store), pytest.raises(RunStateError, match="transition"):
        file.extend_budget(store, extension)
    assert file.path.read_bytes() == before


def test_mutation_load_and_hydration_require_both_owned_locks(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    for action in (
        lambda: file.load(store),
        lambda: file.admit_block(store, plan),
        lambda: file.reserve_dispatch(store, _reservation(plan)),
        lambda: file.evidence(store),
        lambda: file.snapshot(store, now=_TIME),
        lambda: file.record_calibration(store, ()),
    ):
        with pytest.raises(RunStateError, match="owned"):
            action()
    with file.acquire_lock(store):
        contender = SampledRunStateFile.open(file.path)
        with pytest.raises(RunStateBusyError), contender.acquire_lock(store):
            pytest.fail("concurrent run lock admitted")
        with pytest.raises(ObservationStoreBusyError), store.acquire_sampling_lock():
            pytest.fail("concurrent store lock admitted")
    with file.acquire_lock(store):
        assert file.load(store).run_id == "run-1"


@pytest.mark.parametrize(
    "run_id", ["../other", ".", "..", "/absolute/other", "a/b", "", "a.b", "x" * 129]
)
def test_unsafe_run_ids_refuse_before_creation(tmp_path: Path, run_id: str) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    plan = _plan()
    with pytest.raises(RunStateError, match="initialization"):
        SampledRunStateFile.create(
            case_root=tmp_path,
            run_id=run_id,
            store=store,
            started_at=_TIME,
            original_identity=plan.original_identity,
            original_candidate_digest=plan.original_candidate_digest,
            provider_calls_per_attempt=plan.provider_calls_per_attempt,
            approval=_approval(),
        )
    assert not (tmp_path / ".runs").exists()


def test_duplicate_json_keys_and_untrusted_validation_details_are_content_safe(
    tmp_path: Path,
) -> None:
    file, store, _ = _create(tmp_path)
    raw = file.path.read_text()
    file.path.write_text(
        raw.replace('"run_id": "run-1"', '"run_id": "run-1", "run_id": "private secret"', 1)
    )
    with file.acquire_lock(store), pytest.raises(RunStateError, match="duplicate JSON") as raised:
        file.load(store)
    assert "private secret" not in str(raised.value)
    file.path.write_text('{"untrusted-secret": "private secret"}')
    with (
        file.acquire_lock(store),
        pytest.raises(RunStateError, match="invalid or unavailable") as raised,
    ):
        file.load(store)
    assert "private secret" not in str(raised.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
@pytest.mark.parametrize("mode", [0o644, 0o666, 0o400])
def test_nonprivate_state_modes_refuse(tmp_path: Path, mode: int) -> None:
    file, store, _ = _create(tmp_path)
    file.path.chmod(mode)
    with file.acquire_lock(store), pytest.raises(RunStateError, match="0600"):
        file.load(store)


def test_symlink_state_refuses_and_preserves_target(tmp_path: Path) -> None:
    file, store, _ = _create(tmp_path)
    saved = tmp_path / "saved"
    file.path.rename(saved)
    original = saved.read_bytes()
    file.path.symlink_to(saved)
    with file.acquire_lock(store), pytest.raises(RunStateError, match="unsafe"):
        file.load(store)
    assert saved.read_bytes() == original


def test_reference_missing_from_sqlite_refuses_authoritative_load(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, _observation(plan, reservation))
    payload = json.loads(file.path.read_text())
    payload["blocks"][0]["observations"][0]["observation_id"] = "missing"
    file.path.write_text(json.dumps(payload))
    with file.acquire_lock(store), pytest.raises(RunStateError, match="unavailable observation"):
        file.load(store)


def test_duplicate_reordered_and_mixed_block_contracts_reject() -> None:
    plan = _plan()
    base = SampledRunState(
        run_id="run-1",
        source_case="/case",
        observation_store="/db",
        started_at=_TIME,
        original_identity=plan.original_identity,
        original_candidate_digest=plan.original_candidate_digest,
        provider_calls_per_attempt=plan.provider_calls_per_attempt,
        approvals=(_approval(),),
        blocks=(SamplingBlockProgress(plan=plan),),
    )
    with pytest.raises(ValidationError, match="block IDs"):
        SampledRunState.model_validate(
            {**base.model_dump(), "blocks": (base.blocks[0], base.blocks[0])}
        )
    wrong = plan.model_copy(update={"original_candidate_digest": "7" * 64})
    with pytest.raises(ValidationError, match="materialization"):
        SampledRunState.model_validate(
            {**base.model_dump(), "blocks": (SamplingBlockProgress(plan=wrong),)}
        )


def _complete_first_block(
    file: SampledRunStateFile, store: ObservationStore, plan: SamplingBlockPlan
) -> None:
    dispatched = {"baseline": 0, "candidate": 0}
    for position, arm in enumerate(plan.schedule):
        if dispatched[arm] == plan.targets_for(arm).required_valid:
            continue
        dispatched[arm] += 1
        reservation = _reservation(plan, position, dispatched[arm])
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, _observation(plan, reservation))


def test_cached_checkpoint_keeps_admitted_trials_and_selection_across_append_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file, store, plan = _create(tmp_path)
    with file.acquire_lock(store):
        _complete_first_block(file, store, plan)
        first = file.evidence(store)[0]
        trial = SampledCandidateTrial(
            trial_key=trial_key("chunk", (), ("ctx-a",)),
            base_removals=(),
            requested_removals=("ctx-a",),
            effective_removals=("ctx-a",),
            phase="chunk",
            comparison=SampledComparison(evidence=first, action="admit"),
        )
        file.archive_block(store)
        next_plan = _plan(
            block_id="block-2", candidate_digest="3" * 64, frozen_at=_TIME + timedelta(seconds=10)
        )
        file.admit_block(store, next_plan)
        checkpoint = SampledReductionCheckpoint(
            reduction=plan.preservation,
            original_identity=plan.original_identity,
            original_candidate_digest=plan.original_candidate_digest,
            best_candidate_digest=plan.candidate_digest,
            removed_context_ids=("ctx-a",),
            trials=(trial,),
            active_block=file.evidence(store)[1],
        )
        file.save_checkpoint(store, checkpoint)
        reservation = _reservation(next_plan, attempt=3).model_copy(
            update={"attempt_id": "attempt-" + "f" * 32, "attempt_digest": "f" * 64}
        )
        file.reserve_dispatch(store, reservation)
        observation = _observation(next_plan, reservation).model_copy(
            update={"observation_id": "second-block"}
        )

        def fail_write(state: SampledRunState) -> None:
            raise OSError("injected checkpoint publication failure")

        monkeypatch.setattr(file, "_write", fail_write)
        with pytest.raises(OSError, match="injected checkpoint"):
            file.append_observation(store, observation)
    reopened = SampledRunStateFile.open(file.path)
    with reopened.acquire_lock(store):
        recovered = reopened.reconcile(store)
        assert recovered.checkpoint is not None
        assert recovered.checkpoint.trials == (trial,)
        assert recovered.checkpoint.best_candidate_digest == plan.candidate_digest
        assert recovered.checkpoint.removed_context_ids == ("ctx-a",)
        assert recovered.checkpoint.active_block is not None
        assert len(recovered.checkpoint.active_block.attempts) == 1


def test_completed_block_remains_durable_while_checkpoint_preserves_previous_selection(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    with file.acquire_lock(store):
        checkpoint = SampledReductionCheckpoint(
            reduction=plan.preservation,
            original_identity=plan.original_identity,
            original_candidate_digest=plan.original_candidate_digest,
            best_candidate_digest=plan.original_candidate_digest,
            removed_context_ids=(),
            active_block=file.evidence(store)[0],
        )
        file.save_checkpoint(store, checkpoint)
        _complete_first_block(file, store, plan)
        state = file.load(store)
        assert state.checkpoint is not None
        assert state.checkpoint.active_block is None
        assert state.checkpoint.best_candidate_digest == plan.original_candidate_digest
        assert file.evidence(store)[0].complete


def test_new_dispatch_cannot_overdraw_remaining_call_allowance(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    with file.acquire_lock(store):
        state = file.load(store)
        allowance = state.allowance.model_copy(
            update={"max_provider_calls": 1, "confirmation_provider_calls": 1}
        )
        approval = state.approvals[0].model_copy(update={"allowance": allowance})
        limited = SampledRunState.model_validate({**state.model_dump(), "approvals": (approval,)})
        file._write(limited)
        with pytest.raises(RunStateError, match="remaining provider-call"):
            file.reserve_dispatch(store, _reservation(plan))
        assert file.load(store).pending is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("block_digest", "7" * 64),
        ("schedule_position", 1),
        ("attempt", 2),
        ("provider_calls", 1),
        ("reserved_at", _TIME + timedelta(seconds=60)),
    ],
)
def test_wrong_reservation_cannot_publish_or_dispatch(
    tmp_path: Path, field: str, value: object
) -> None:
    file, store, plan = _create(tmp_path)
    wrong = _reservation(plan).model_copy(update={field: value})
    before = file.path.read_bytes()
    with file.acquire_lock(store), pytest.raises(RunStateError):
        file.reserve_dispatch(store, wrong)
    assert file.path.read_bytes() == before


@pytest.mark.parametrize("boundary", ["file-sync", "replace", "directory-sync"])
def test_atomic_reservation_failure_never_loses_previous_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import ithrl.run_state as module

    file, store, plan = _create(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected durable boundary failure")

    with file.acquire_lock(store):
        if boundary == "file-sync":
            monkeypatch.setattr(module.os, "fsync", fail)
        elif boundary == "replace":
            monkeypatch.setattr(Path, "replace", fail)
        else:
            monkeypatch.setattr(module, "_sync_directory", fail)
        with pytest.raises(OSError, match="injected"):
            file.reserve_dispatch(store, _reservation(plan))
        state = file.load(store)
        assert state.blocks[0].plan == plan
        assert state.blocks[0].observations == ()
        if boundary == "directory-sync":
            assert state.pending == _reservation(plan)
        else:
            assert state.pending is None
    assert not tuple(file.path.parent.glob(".sampled-state-*"))


def test_sampling_state_file_size_bound_refuses_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.run_state as module

    file, store, _ = _create(tmp_path)
    before = file.path.read_bytes()
    with file.acquire_lock(store):
        state = file.load(store)
        monkeypatch.setattr(module, "_MAX_RUN_STATE_BYTES", 1)
        with pytest.raises(RunStateError, match="byte limit"):
            file._write(state)
    assert file.path.read_bytes() == before


def test_safe_preaccept_replacement_preserves_failed_evidence_and_zero_behavior_count(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    original = _observation(plan, reservation)
    safe = Observation.model_validate(
        {
            **original.model_dump(),
            "outcome": ObservationOutcome.RUNNER_ERROR,
            "error_category": "startup",
            "attempt_evidence": AttemptEvidence(
                attempt_id=reservation.attempt_id,
                attempt_digest=reservation.attempt_digest,
                termination_reason="failed",
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                accounting=RunAccounting(),
                retry_safe=True,
            ),
        }
    )
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, safe, independence_evidence_digest="a" * 64)
        block = file.continue_block(store, expected_plan_digest=plan.digest, now=_TIME)
        assert block.eligible
        assert block.counts_for(reservation.arm).valid == 0
        assert block.counts_for(reservation.arm).runner_errors == 1
        assert file.usage(store, now=_TIME).charged_provider_calls == 0
        assert store.get(safe.observation_id) == safe


def test_recorded_calibration_consumption_stays_in_same_run_usage(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    plan = _plan()
    reservation = _reservation(plan)
    observation = _observation(plan, reservation)
    calibration = observation.model_copy(
        update={
            **plan.original_identity.model_dump(),
            "candidate_digest": plan.original_candidate_digest,
            "role": ObservationRole.CALIBRATION,
            "arm": ObservationArm.BASELINE,
            "recorded_at": _TIME - timedelta(seconds=30),
        }
    )
    store.append(calibration)
    file = SampledRunStateFile.create(
        case_root=case,
        run_id="run-1",
        store=store,
        started_at=_TIME,
        original_identity=plan.original_identity,
        original_candidate_digest=plan.original_candidate_digest,
        provider_calls_per_attempt=plan.provider_calls_per_attempt,
        approval=_approval(),
        calibration_observation_ids=(calibration.observation_id,),
    )
    with file.acquire_lock(store):
        assert file.evidence(store) == ()
        usage = file.usage(store, now=_TIME + timedelta(seconds=2))
        assert usage.actual_provider_calls == 1
        assert usage.possible_provider_calls == 1
        assert usage.charged_provider_calls == 2
        assert usage.known_tokens == 12
        assert usage.runner_milliseconds == 12
        assert usage.wall_time_seconds == 2
        extension = _approval(max_provider_calls=100).model_copy(
            update={"approval_id": "approval-2"}
        )
        file.extend_budget(store, extension)
        assert file.usage(store, now=_TIME + timedelta(seconds=2)) == usage


def _calibration_observation(
    plan: SamplingBlockPlan, position: int = 0, *, unknown: bool = False
) -> Observation:
    return _observation(
        plan, _reservation(plan, position, attempt=position + 1), unknown=unknown
    ).model_copy(
        update={
            **plan.original_identity.model_dump(),
            "candidate_digest": plan.original_candidate_digest,
            "role": ObservationRole.CALIBRATION,
            "arm": ObservationArm.BASELINE,
            "recorded_at": _TIME - timedelta(seconds=30 - position),
        }
    )


def test_prior_calibration_charges_known_and_unknown_consumption_without_arm_evidence(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path, admit_block=False)
    observations = (
        _calibration_observation(plan),
        _calibration_observation(plan, 1, unknown=True),
    )
    for observation in observations:
        store.append(observation)
    with file.acquire_lock(store):
        file.record_calibration(store, tuple(item.observation_id for item in observations))
    reopened = SampledRunStateFile.open(file.path)
    with reopened.acquire_lock(store):
        assert reopened.evidence(store) == ()
        usage = reopened.usage(store, now=_TIME + timedelta(seconds=2))
        assert usage.actual_provider_calls == 1
        assert usage.possible_provider_calls == 3
        assert usage.charged_provider_calls == 4
        assert usage.known_tokens == 12
        assert usage.runner_milliseconds == 12
        assert usage.unknown_provider_attempts == 1
        assert usage.unknown_runner_attempts == usage.unknown_token_attempts == 1
        assert usage.pending_provider_calls == usage.pending_runner_seconds == 0
        assert usage.wall_time_seconds == 2
        reopened.admit_block(store, plan)
        assert reopened.evidence(store)[0].attempts == ()
        assert reopened.usage(store, now=_TIME + timedelta(seconds=2)) == usage


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", ObservationRole.SEARCH),
        ("role", ObservationRole.CONFIRMATION),
        ("arm", ObservationArm.CANDIDATE),
        ("arm", ObservationArm.SELECTED),
        ("candidate_digest", "7" * 64),
        ("case_digest", "7" * 64),
        ("experiment_digest", "7" * 64),
        ("runner_fingerprint", "another-fixture"),
        ("execution_plan_digest", "7" * 64),
        ("target_digest", "7" * 64),
        ("algorithm_digest", "7" * 64),
    ],
)
def test_incompatible_calibration_cannot_publish_or_charge_run(
    tmp_path: Path, field: str, value: object
) -> None:
    file, store, plan = _create(tmp_path, admit_block=False)
    observation = _calibration_observation(plan).model_copy(update={field: value})
    store.append(observation)
    before = file.path.read_bytes()
    with file.acquire_lock(store):
        with pytest.raises(RunStateError, match="calibration accounting"):
            file.record_calibration(store, (observation.observation_id,))
        assert file.load(store).calibration_observation_ids == ()
        assert file.usage(store, now=_TIME).charged_provider_calls == 0
    assert file.path.read_bytes() == before
    assert store.get(observation.observation_id) == observation


def test_calibration_continuation_appends_exact_prefix_and_refuses_replacement(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path, admit_block=False)
    first, second = (_calibration_observation(plan, position) for position in range(2))
    store.append(first)
    store.append(second)
    with file.acquire_lock(store):
        file.record_calibration(store, (first.observation_id,))
        ids = (first.observation_id, second.observation_id)
        state = file.record_calibration(store, ids)
        assert state.calibration_observation_ids == ids
        assert file.usage(store, now=_TIME).charged_provider_calls == 4
        before = file.path.read_bytes()
        assert file.record_calibration(store, ids) == state
        for replacement in ((), (first.observation_id,), ids[::-1], (second.observation_id,)):
            with pytest.raises(RunStateError, match="recorded prefix"):
                file.record_calibration(store, replacement)
            assert file.path.read_bytes() == before
            assert file.load(store) == state


@pytest.mark.parametrize("boundary", ["block", "checkpoint", "selection"])
def test_calibration_references_freeze_when_search_starts(tmp_path: Path, boundary: str) -> None:
    file, store, plan = _create(tmp_path, admit_block=False)
    first, second = (_calibration_observation(plan, position) for position in range(2))
    store.append(first)
    store.append(second)
    with file.acquire_lock(store):
        ids = (first.observation_id,)
        file.record_calibration(store, ids)
        if boundary == "block":
            file.admit_block(store, plan)
        else:
            checkpoint = SampledReductionCheckpoint(
                reduction=plan.preservation,
                original_identity=plan.original_identity,
                original_candidate_digest=plan.original_candidate_digest,
                best_candidate_digest=plan.original_candidate_digest,
                removed_context_ids=(),
            )
            file.save_checkpoint(store, checkpoint)
            if boundary == "selection":
                holdout_id = content_digest({"run_id": "run-1", "role": "confirmation"})
                seed = int(holdout_id[:16], 16)
                confirmation = _plan(
                    block_id=f"confirmation-{holdout_id}",
                    role="confirmation",
                    candidate_identity=plan.original_identity,
                    candidate_digest=plan.original_candidate_digest,
                    frozen_at=_TIME + timedelta(seconds=2),
                    schedule_seed=seed,
                    schedule=sampling_schedule(3, 3, seed),
                )
                file.select_candidate(
                    store,
                    SampledFinalSelection(
                        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
                        selected_at=_TIME + timedelta(seconds=1),
                        plan=confirmation,
                    ),
                )
        state = file.load(store)
        before = file.path.read_bytes()
        assert file.record_calibration(store, ids) == state
        with pytest.raises(RunStateError, match="after search starts"):
            file.record_calibration(store, (*ids, second.observation_id))
        assert file.path.read_bytes() == before
        assert file.load(store) == state


def test_unknown_missing_accounting_retains_zero_as_known_lower_bound_only(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    with file.acquire_lock(store):
        reservation = _reservation(plan)
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, _observation(plan, reservation, unknown=True))
        usage = file.usage(store, now=_TIME)
        assert usage.actual_provider_calls == 0
        assert usage.possible_provider_calls == 2
        assert usage.known_tokens == usage.runner_milliseconds == 0
        assert usage.unknown_token_attempts == usage.unknown_runner_attempts == 1


def test_forked_or_other_thread_identity_cannot_mutate_owned_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.run_state as module

    file, store, plan = _create(tmp_path)
    with file.acquire_lock(store):
        actual_pid = os.getpid()
        monkeypatch.setattr(module.os, "getpid", lambda: actual_pid + 1)
        with pytest.raises(RunStateError, match="owned"):
            file.reserve_dispatch(store, _reservation(plan))


def test_state_hardlink_refuses_without_modifying_the_alias(tmp_path: Path) -> None:
    file, store, _ = _create(tmp_path)
    alias = tmp_path / "linked-state"
    os.link(file.path, alias)
    before = alias.read_bytes()
    with file.acquire_lock(store), pytest.raises(RunStateError, match="regular"):
        file.load(store)
    assert alias.read_bytes() == before


def test_small_state_load_does_not_allocate_the_entire_byte_limit(tmp_path: Path) -> None:
    file, store, _ = _create(tmp_path)
    with file.acquire_lock(store):
        tracemalloc.start()
        try:
            state = file.load(store)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert state.run_id == "run-1"
    assert peak < 2 * 1024 * 1024


def test_state_replacement_during_read_refuses_with_content_safe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.case.filesystem as filesystem

    file, store, _ = _create(tmp_path)
    read_descriptor = filesystem._read_descriptor
    replacement = file.path.with_name("replacement-state")
    replacement.write_bytes(file.path.read_bytes())
    replacement.chmod(0o600)

    def replace_after_read(
        descriptor: int, *, expected_size: int, max_bytes: int, label: str
    ) -> tuple[bytes, os.stat_result]:
        result = read_descriptor(
            descriptor, expected_size=expected_size, max_bytes=max_bytes, label=label
        )
        replacement.replace(file.path)
        return result

    monkeypatch.setattr(filesystem, "_read_descriptor", replace_after_read)
    with (
        file.acquire_lock(store),
        pytest.raises(RunStateError, match="invalid or unavailable") as raised,
    ):
        file.load(store)
    assert str(file.path) not in str(raised.value)


def test_state_hardlink_acquired_during_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.run_state as module

    file, store, _ = _create(tmp_path)
    alias = tmp_path / "linked-state"
    read_bytes = module.read_safe_bytes

    def link_after_read(path: Path, *, max_bytes: int, label: str) -> bytes:
        content = read_bytes(path, max_bytes=max_bytes, label=label)
        os.link(path, alias)
        return content

    monkeypatch.setattr(module, "read_safe_bytes", link_after_read)
    with file.acquire_lock(store), pytest.raises(RunStateError, match="regular"):
        file.load(store)
    assert alias.read_bytes() == file.path.read_bytes()


def test_state_load_refuses_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ithrl.run_state as module

    file, store, _ = _create(tmp_path)
    before = file.path.read_bytes()
    monkeypatch.setattr(module, "_MAX_RUN_STATE_BYTES", len(before) - 1)
    with file.acquire_lock(store), pytest.raises(RunStateError, match="invalid or unavailable"):
        file.load(store)
    assert file.path.read_bytes() == before


@pytest.mark.parametrize("unknown", [False, True])
def test_fresh_snapshot_matches_existing_reads_and_preserves_pending_accounting(
    tmp_path: Path, unknown: bool
) -> None:
    file, store, plan = _create(tmp_path, admit_block=False)
    calibration = _calibration_observation(plan, unknown=unknown)
    calibration = calibration.model_copy(
        update={
            "attempt_evidence": calibration.attempt_evidence.model_copy(
                update={"attempt_id": "attempt-" + "a" * 32}
            )
        }
    )
    store.append(calibration)
    with file.acquire_lock(store):
        file.record_calibration(store, (calibration.observation_id,))
        file.admit_block(store, plan)
        pending = _reservation(plan)
        file.reserve_dispatch(store, pending)
        now = _TIME + timedelta(seconds=2)
        snapshot = file.snapshot(store, now=now)
        assert snapshot.state == file.load(store)
        assert snapshot.blocks == file.evidence(store)
        assert snapshot.calibration == (calibration,)
        assert snapshot.usage == file.usage(store, now=now)
        assert snapshot.usage.pending_provider_calls == pending.provider_calls == 2
        assert snapshot.usage.pending_runner_seconds == pending.runner_seconds
        assert snapshot.usage.charged_provider_calls == 4
        assert snapshot.usage.unknown_provider_attempts == int(unknown)
        assert snapshot.usage.unknown_runner_attempts == int(unknown)
        assert snapshot.usage.unknown_token_attempts == int(unknown)
        assert snapshot.usage.wall_time_seconds == 2
        assert str(file.path) not in repr(snapshot)
        assert calibration.observation_id not in repr(snapshot)


def test_snapshot_rejects_earlier_clock_without_changing_clock_free_reads(tmp_path: Path) -> None:
    file, store, _ = _create(tmp_path)
    with file.acquire_lock(store):
        state = file.load(store)
        blocks = file.evidence(store)
        with pytest.raises(RunStateError, match="clock precedes"):
            file.snapshot(store, now=_TIME - timedelta(microseconds=1))
        assert file.load(store) == state
        assert file.evidence(store) == blocks


@pytest.mark.parametrize("corruption", ["attempt-digest", "invalid-body"])
def test_snapshot_rereads_sqlite_and_refuses_corruption_between_calls(
    tmp_path: Path, corruption: str
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    observation = _observation(plan, reservation)
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, observation)
        previous = file.snapshot(store, now=_TIME)
        assert previous.blocks[0].attempts[0].observation == observation
        body = observation.attempt_evidence.model_dump(mode="json")
        if corruption == "attempt-digest":
            body["attempt_digest"] = "7" * 64
        else:
            body["termination_reason"] = "private-invalid-termination"
        # Simulate external database corruption while restoring its exact schema.
        with closing(sqlite3.connect(store.path)) as connection, connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'observations_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER observations_no_update")
            connection.execute(
                "UPDATE observations SET attempt_evidence = ? WHERE observation_id = ?",
                (json.dumps(body), observation.observation_id),
            )
            connection.execute(trigger)
        with pytest.raises(RunStateError) as raised:
            file.snapshot(store, now=_TIME)
        assert "private-invalid-termination" not in str(raised.value)
        assert previous.blocks[0].attempts[0].observation == observation


def test_accepted_without_accounting_preserves_known_lower_bound_and_possible_continuation(
    tmp_path: Path,
) -> None:
    file, store, plan = _create(tmp_path)
    reservation = _reservation(plan)
    observation = _observation(plan, reservation, unknown=True)
    evidence = AttemptEvidence(
        attempt_id=reservation.attempt_id,
        attempt_digest=reservation.attempt_digest,
        termination_reason="failed",
        provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
    )
    observation = observation.model_copy(update={"attempt_evidence": evidence})
    with file.acquire_lock(store):
        file.reserve_dispatch(store, reservation)
        file.append_observation(store, observation)
        usage = file.usage(store, now=_TIME)
        assert usage.actual_provider_calls == 1
        assert usage.possible_provider_calls == 1
        assert usage.charged_provider_calls == 2
        assert usage.unknown_provider_attempts == 1
        assert usage.unknown_runner_attempts == usage.unknown_token_attempts == 1


def test_new_block_cannot_change_exact_run_profile_call_bound(tmp_path: Path) -> None:
    file, store, plan = _create(tmp_path)
    changed = plan.model_copy(update={"block_id": "new-block", "provider_calls_per_attempt": 1})
    with file.acquire_lock(store):
        file.archive_block(store)
        before = file.path.read_bytes()
        with pytest.raises(RunStateError, match="transition"):
            file.admit_block(store, changed)
    assert file.path.read_bytes() == before


def test_sampled_run_lock_rejects_hard_link_without_modifying_state(tmp_path: Path) -> None:
    file, store, _ = _create(tmp_path)
    original = file.path.read_bytes()
    lock_path = file.path.with_name("run.lock")
    # The fixture acquired a regular lock while creating its first block.
    lock_path.unlink()
    os.link(file.path, lock_path)

    with (
        pytest.raises(RunStateError, match="sampled run lock file is unsafe"),
        file.acquire_lock(store),
    ):
        pytest.fail("hard-linked sampled state was accepted as a lock")

    assert file.path.read_bytes() == lock_path.read_bytes() == original
