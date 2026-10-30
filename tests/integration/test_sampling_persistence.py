"""Cross-store recovery of a frozen sampled block without another dispatch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.results import load_sampling_snapshot, write_sampling_snapshot
from ithrl.run_state import (
    RunStateError,
    SampledRunState,
    SampledRunStateFile,
    SamplingBudgetApproval,
    SamplingDispatchReservation,
)
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting, TokenUsage


@pytest.mark.parametrize("published_before_failure", [False, True])
def test_interrupted_block_reopens_extends_and_publishes_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published_before_failure: bool
) -> None:
    now = datetime(2026, 10, 4, tzinfo=UTC)
    identity = ObservationIdentity(
        case_digest="a" * 64,
        experiment_digest="b" * 64,
        runner_fingerprint="synthetic-storage-profile",
        execution_plan_digest="c" * 64,
        target_digest="d" * 64,
        algorithm_digest="e" * 64,
    )
    targets = SamplingArmTarget(required_valid=1, max_attempts=2)
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(original=targets, candidate=targets),
        confirmation=SampledConfirmationPhase(
            original=targets, candidate=targets, one_sided_confidence=0.95
        ),
        margin=0.2,
        freshness_seconds=60,
    )
    plan = SamplingBlockPlan(
        block_id="frozen-block",
        role="search",
        procedure=reduction,
        original_identity=identity,
        candidate_identity=identity.model_copy(update={"execution_plan_digest": "f" * 64}),
        original_candidate_digest="1" * 64,
        candidate_digest="2" * 64,
        frozen_at=now,
        schedule_seed=3,
        schedule=sampling_schedule(2, 2, 3),
        provider_calls_per_attempt=2,
    )
    allowance = SamplingBudgetAllowance(
        run_id="storage-run",
        max_provider_calls=20,
        max_wall_time_seconds=1000,
        max_candidate_comparisons=4,
        confirmation_provider_calls=8,
        confirmation_runner_seconds=40,
    )
    case = tmp_path / "case"
    case.mkdir(mode=0o700)
    store = ObservationStore(tmp_path / "private" / "observations.sqlite")
    store.initialize()
    file = SampledRunStateFile.create(
        case_root=case,
        run_id=allowance.run_id,
        store=store,
        started_at=now,
        original_identity=identity,
        original_candidate_digest=plan.original_candidate_digest,
        provider_calls_per_attempt=plan.provider_calls_per_attempt,
        approval=SamplingBudgetApproval(
            approval_id="initial-approval", approved_at=now, allowance=allowance
        ),
    )

    def reservation(position: int) -> SamplingDispatchReservation:
        return SamplingDispatchReservation(
            block_id=plan.block_id,
            block_digest=plan.digest,
            schedule_position=position,
            arm=plan.schedule[position],
            attempt=1,
            attempt_id=f"attempt-{position:032x}",
            attempt_digest=f"{position:064x}",
            reserved_at=now + timedelta(seconds=position + 1),
            provider_calls=2,
            runner_seconds=10,
        )

    def observation(dispatch: SamplingDispatchReservation) -> Observation:
        baseline = dispatch.arm == "baseline"
        return Observation(
            **(identity if baseline else plan.candidate_identity).model_dump(),
            candidate_digest=plan.original_candidate_digest if baseline else plan.candidate_digest,
            role=ObservationRole.SEARCH,
            arm=ObservationArm(dispatch.arm),
            attempt=dispatch.attempt,
            recorded_at=dispatch.reserved_at,
            outcome=ObservationOutcome.MATCH,
            attempt_evidence=AttemptEvidence(
                attempt_id=dispatch.attempt_id,
                attempt_digest=dispatch.attempt_digest,
                termination_reason="completed",
                provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
                effective_policy_digest="9" * 64,
                accounting=RunAccounting(
                    provider_calls_started=1,
                    provider_calls_completed=1,
                    elapsed_milliseconds=25,
                ),
                usage=TokenUsage(input_tokens=10, output_tokens=2),
            ),
        )

    with file.acquire_lock(store):
        file.admit_block(store, plan)
        first_dispatch = reservation(0)
        first_observation = observation(first_dispatch)
        file.reserve_dispatch(store, first_dispatch)
        original_write = file._write

        def fail_publication(state: SampledRunState) -> None:
            if published_before_failure:
                original_write(state)
            raise OSError("simulated progress-publication interruption")

        with monkeypatch.context() as patch:
            patch.setattr(file, "_write", fail_publication)
            with pytest.raises(OSError, match="simulated"):
                file.append_observation(store, first_observation)
        assert store.get(first_observation.observation_id) == first_observation

    reopened = SampledRunStateFile.open(file.path.parent)
    with reopened.acquire_lock(store):
        before_reconciliation = reopened.usage(store, now=now + timedelta(seconds=5))
        assert before_reconciliation.actual_provider_calls == int(published_before_failure)
        assert before_reconciliation.possible_provider_calls == 0
        assert before_reconciliation.pending_provider_calls == (
            0 if published_before_failure else 2
        )
        assert before_reconciliation.charged_provider_calls == (
            1 if published_before_failure else 2
        )
        recovered = reopened.reconcile(store)
        assert recovered.pending is None
        assert len(recovered.blocks[0].observations) == 1
        usage = reopened.usage(store, now=now + timedelta(seconds=5))
        assert usage.actual_provider_calls == 1
        assert usage.possible_provider_calls == 0
        assert usage.charged_provider_calls == 1
        assert usage.known_tokens == 12
        with pytest.raises(RunStateError, match="persisted dispatch reservation"):
            reopened.append_observation(store, first_observation)

        approval = SamplingBudgetApproval(
            approval_id="extension-approval",
            approved_at=now + timedelta(seconds=5),
            allowance=SamplingBudgetAllowance.model_validate(
                {**allowance.model_dump(), "max_provider_calls": 30}
            ),
        )
        extended = reopened.extend_budget(store, approval)
        assert extended.blocks == recovered.blocks
        assert extended.started_at == recovered.started_at
        assert reopened.usage(store, now=now + timedelta(seconds=5)) == usage

        block = reopened.continue_block(
            store, expected_plan_digest=plan.digest, now=now + timedelta(seconds=5)
        )
        for position, arm in enumerate(plan.schedule[1:], start=1):
            if block.counts_for(arm).valid == plan.targets_for(arm).required_valid:
                continue
            dispatch = reservation(position)
            reopened.reserve_dispatch(store, dispatch)
            reopened.append_observation(store, observation(dispatch))
            block = reopened.evidence(store)[0]
        assert block.complete
        assert block.eligible
        assert block.attempts[0].observation == first_observation
        assert len(block.attempts) == 2
        state = reopened.archive_block(store)
        usage = reopened.usage(store, now=now + timedelta(seconds=6))
        assert usage.actual_provider_calls == 2
        assert usage.possible_provider_calls == 0
        assert usage.pending_provider_calls == 0
        assert usage.charged_provider_calls == 2
        assert usage.runner_milliseconds == 50
        assert usage.known_tokens == 24
        destination = case / "results" / "sampling-progress"
        write_sampling_snapshot(destination=destination, state=state, store=store)
        snapshot = load_sampling_snapshot(destination, store)
        assert snapshot == state
        assert snapshot.blocks[0].plan == plan
        assert len(store.list_for_experiment(identity.experiment_digest)) == 2
