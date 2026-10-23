"""Frozen two-arm membership, eligibility, and administrative budget contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

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
    SamplingArmCounts,
    SamplingAttempt,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting

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
        runner_fingerprint="native-test",
        execution_plan_digest="c" * 64,
        target_digest="d" * 64,
        algorithm_digest="e" * 64,
    )
    values = {
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
    }
    values.update(changes)
    return SamplingBlockPlan.model_validate(values)


def _attempts(
    plan: SamplingBlockPlan,
    *,
    failure: bool = False,
    unsafe: bool = False,
    invalid: bool = False,
) -> tuple[SamplingAttempt, ...]:
    attempts: list[SamplingAttempt] = []
    valid = {"baseline": 0, "candidate": 0}
    dispatched = {"baseline": 0, "candidate": 0}
    for position, arm in enumerate(plan.schedule):
        if valid[arm] == plan.targets_for(arm).required_valid:
            continue
        failed = failure and position == 0
        invalid_observation = invalid and position == 0
        dispatched[arm] += 1
        identity = plan.original_identity if arm == "baseline" else plan.candidate_identity
        digest = plan.original_candidate_digest if arm == "baseline" else plan.candidate_digest
        outcome = (
            ObservationOutcome.RUNNER_ERROR
            if failed
            else ObservationOutcome.INVALID_OBSERVATION
            if invalid_observation
            else ObservationOutcome.MATCH
        )
        accounting = RunAccounting(
            provider_calls_started=0 if failed else 1,
            provider_calls_completed=0 if failed else 1,
        )
        evidence = AttemptEvidence(
            attempt_id=f"attempt-{position:032x}",
            attempt_digest=f"{position:064x}",
            termination_reason="failed" if failed else "completed",
            provider_acceptance_state=(
                ProviderAcceptanceState.NOT_STARTED if failed else ProviderAcceptanceState.ACCEPTED
            ),
            effective_policy_digest="9" * 64,
            accounting=accounting,
            retry_safe=failed and not unsafe,
        )
        observation = Observation(
            **identity.model_dump(),
            observation_id=f"observation-{position}",
            candidate_digest=digest,
            role=ObservationRole(plan.role),
            arm=ObservationArm(arm),
            attempt=dispatched[arm],
            recorded_at=plan.frozen_at + timedelta(seconds=position),
            outcome=outcome,
            error_category="startup"
            if failed
            else "oracle-contract"
            if invalid_observation
            else None,
            attempt_evidence=evidence,
        )
        attempts.append(
            SamplingAttempt(
                schedule_position=position,
                observation=observation,
                independence_evidence_digest="8" * 64 if failed else None,
            )
        )
        valid[arm] += int(observation.valid)
    return tuple(attempts)


def test_frozen_block_schedule_and_json_round_trip() -> None:
    plan = _plan()
    assert plan.schedule.count("baseline") == plan.schedule.count("candidate") == 3
    assert plan.required_provider_call_budget == 12
    assert plan.expires_at == _TIME + timedelta(seconds=60)
    assert SamplingBlockPlan.model_validate_json(plan.model_dump_json()) == plan
    evidence = SamplingBlockEvidence(plan=plan, attempts=_attempts(plan))
    assert SamplingBlockEvidence.model_validate_json(evidence.model_dump_json()) == evidence
    assert evidence.complete
    assert evidence.eligible
    assert evidence.point_difference == 0
    assert evidence.original_counts.valid == evidence.candidate_counts.valid == 2


def test_safe_replacements_do_not_use_percentage_cutoff_or_bernoulli_denominator() -> None:
    plan = _plan()
    evidence = SamplingBlockEvidence(plan=plan, attempts=_attempts(plan, failure=True))
    assert evidence.complete
    assert evidence.eligible
    assert len(evidence.attempts) == 5
    assert evidence.original_counts.runner_errors + evidence.candidate_counts.runner_errors == 1
    assert evidence.original_counts.valid + evidence.candidate_counts.valid == 4
    assert evidence.point_difference == 0


@pytest.mark.parametrize("unsafe", [False, True])
def test_failure_requires_both_safe_retry_and_independence_evidence(unsafe: bool) -> None:
    plan = _plan()
    attempts = _attempts(plan, failure=True, unsafe=unsafe)
    if not unsafe:
        attempts = (
            attempts[0].model_copy(update={"independence_evidence_digest": None}),
            *attempts[1:],
        )
    evidence = SamplingBlockEvidence(plan=plan, attempts=attempts)
    assert not evidence.eligible
    assert evidence.eligibility_refusal == "ineligible-exclusion"
    assert evidence.point_difference is None


def test_completed_invalid_observations_refuse_eligibility() -> None:
    plan = _plan()
    evidence = SamplingBlockEvidence(plan=plan, attempts=_attempts(plan, invalid=True))
    assert not evidence.eligible
    assert (
        evidence.original_counts.invalid_observations
        + evidence.candidate_counts.invalid_observations
        == 1
    )
    assert evidence.point_difference is None


def test_partial_block_is_eligible_but_has_no_point_difference() -> None:
    plan = _plan()
    evidence = SamplingBlockEvidence(plan=plan, attempts=_attempts(plan)[:1])
    assert evidence.eligible
    assert not evidence.complete
    assert evidence.point_difference is None


@pytest.mark.parametrize(
    "field", ["case_digest", "experiment_digest", "target_digest", "algorithm_digest"]
)
def test_block_refuses_mixed_arm_identity(field: str) -> None:
    identity = _plan().candidate_identity.model_copy(update={field: "0" * 64})
    with pytest.raises(ValueError, match="both arms"):
        _plan(candidate_identity=identity)


@pytest.mark.parametrize("field", ["candidate_digest", "role", "arm", "execution_plan_digest"])
def test_evidence_refuses_wrong_arm_provenance(field: str) -> None:
    plan = _plan()
    attempts = _attempts(plan)
    value: object = "0" * 64
    if field == "role":
        value = ObservationRole.CALIBRATION
    elif field == "arm":
        value = ObservationArm.SELECTED
    changed = attempts[0].observation.model_copy(update={field: value})
    attempts = (attempts[0].model_copy(update={"observation": changed}), *attempts[1:])
    with pytest.raises(ValueError, match="frozen arm and role"):
        SamplingBlockEvidence(plan=plan, attempts=attempts)


@pytest.mark.parametrize(
    "mutation", ["reordered", "duplicate", "missing", "out-of-range", "arm-order"]
)
def test_evidence_rejects_ambiguous_dispatch_membership(mutation: str) -> None:
    plan = _plan()
    attempts = _attempts(plan)
    if mutation == "reordered":
        attempts = attempts[::-1]
    elif mutation == "duplicate":
        attempts = (attempts[0], *attempts)
    elif mutation == "missing":
        attempts = attempts[1:]
    elif mutation == "out-of-range":
        attempts = (attempts[0].model_copy(update={"schedule_position": 6}),)
    else:
        last = attempts[-1]
        changed = last.observation.model_copy(update={"attempt": 100})
        attempts = (*attempts[:-1], last.model_copy(update={"observation": changed}))
    with pytest.raises(ValueError, match=r"positions|slots|schedule|consecutive"):
        SamplingBlockEvidence(plan=plan, attempts=attempts)


@pytest.mark.parametrize("duplicate", ["observation_id", "attempt_id"])
def test_evidence_rejects_reused_observations_and_attempts(duplicate: str) -> None:
    plan = _plan()
    attempts = list(_attempts(plan))
    observation = attempts[1].observation
    if duplicate == "observation_id":
        observation = observation.model_copy(
            update={"observation_id": attempts[0].observation.observation_id}
        )
    else:
        evidence = observation.attempt_evidence.model_copy(
            update={"attempt_id": attempts[0].observation.attempt_evidence.attempt_id}
        )
        observation = observation.model_copy(update={"attempt_evidence": evidence})
    attempts[1] = attempts[1].model_copy(update={"observation": observation})
    with pytest.raises(ValueError, match="IDs must be unique"):
        SamplingBlockEvidence(plan=plan, attempts=tuple(attempts))


def test_expired_evidence_is_preserved_without_a_behavior_claim() -> None:
    plan = _plan()
    attempts = _attempts(plan)
    late = attempts[-1].observation.model_copy(update={"recorded_at": plan.expires_at})
    evidence = SamplingBlockEvidence(
        plan=plan, attempts=(*attempts[:-1], attempts[-1].model_copy(update={"observation": late}))
    )
    assert evidence.complete
    assert not evidence.eligible
    assert evidence.eligibility_refusal == "stale-observation"


@pytest.mark.parametrize(
    "parameters", [(True, 3, 1), (0, 3, 1), (3, 10001, 1), (3, 3, True), (3, 3, -1)]
)
def test_schedule_rejects_malformed_counts_and_seeds(parameters: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError, match="schedule"):
        sampling_schedule(*parameters)


def test_schedule_and_freeze_time_are_part_of_block_identity() -> None:
    original = _plan()
    changed = _plan(schedule_seed=4, schedule=sampling_schedule(3, 3, 4))
    assert changed.digest != original.digest
    assert _plan(frozen_at=_TIME + timedelta(seconds=1)).digest != original.digest
    with pytest.raises(ValueError, match="seeded two-arm order"):
        _plan(schedule=original.schedule[::-1])
    with pytest.raises(ValueError, match="UTC offset"):
        _plan(frozen_at=datetime(2026, 10, 4))  # noqa: DTZ001 -- rejection case.


def test_budget_extensions_do_not_change_frozen_block_identity() -> None:
    allowance = SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=100,
        max_wall_time_seconds=1000,
        max_candidate_comparisons=6,
        confirmation_provider_calls=40,
        confirmation_runner_seconds=400,
    )
    extended = allowance.model_copy(update={"max_provider_calls": 200})
    plan = _plan()
    assert extended != allowance
    assert "max_provider_calls" not in plan.model_dump()
    assert SamplingBlockPlan.model_validate_json(plan.model_dump_json()).digest == plan.digest
    assert SamplingBudgetAllowance.model_validate_json(allowance.model_dump_json()) == allowance
    with pytest.raises(ValidationError):
        SamplingBlockPlan.model_validate({**plan.model_dump(), "allowance": allowance})
    with pytest.raises(ValidationError):
        SamplingBudgetAllowance.model_validate(
            {**allowance.model_dump(), "token_accounting": "hard"}
        )


@pytest.mark.parametrize("field", ["confirmation_provider_calls", "confirmation_runner_seconds"])
def test_confirmation_reserve_cannot_exceed_administrative_ceiling(field: str) -> None:
    values: dict[str, object] = {
        "run_id": "run-1",
        "max_provider_calls": 100,
        "max_wall_time_seconds": 1000,
        "max_candidate_comparisons": 6,
        "confirmation_provider_calls": 40,
        "confirmation_runner_seconds": 400,
    }
    values[field] = 1001
    with pytest.raises(ValueError, match="confirmation budget exceeds"):
        SamplingBudgetAllowance.model_validate(values)


def test_arm_counts_reject_errors_as_behavioral_losses() -> None:
    with pytest.raises(ValueError, match="valid count"):
        SamplingArmCounts(
            dispatched=3, valid=2, matches=1, no_matches=2, invalid_observations=0, runner_errors=1
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actions_executed", 1),
        ("tool_network_requests", 1),
        ("source_store_effect", "may-change"),
        ("original_workspace_effect", "may-change"),
        ("process_outcome", "unknown"),
        ("cleanup_outcome", "failed"),
        ("provider_calls_started", 2),
    ],
)
def test_completed_observation_cannot_hide_unsafe_effects_or_call_overrun(
    field: str, value: Any
) -> None:
    plan = _plan(provider_calls_per_attempt=1)
    attempts = _attempts(plan)
    observation = attempts[0].observation
    accounting = observation.attempt_evidence.accounting
    assert accounting is not None
    updates = {field: value}
    if field == "actions_executed":
        updates["actions_requested"] = 1
    changed_accounting = RunAccounting.model_validate({**accounting.model_dump(), **updates})
    changed_evidence = observation.attempt_evidence.model_copy(
        update={"accounting": changed_accounting}
    )
    changed = observation.model_copy(update={"attempt_evidence": changed_evidence})
    evidence = SamplingBlockEvidence(
        plan=plan, attempts=(attempts[0].model_copy(update={"observation": changed}), *attempts[1:])
    )
    assert evidence.complete
    assert not evidence.eligible
    assert evidence.point_difference is None


@pytest.mark.parametrize(
    "state", [ProviderAcceptanceState.ACCEPTED, ProviderAcceptanceState.UNKNOWN]
)
def test_failed_accepted_or_ambiguous_work_cannot_be_replaced(
    state: ProviderAcceptanceState,
) -> None:
    plan = _plan()
    attempts = _attempts(plan, failure=True)
    first = attempts[0]
    evidence = AttemptEvidence(
        attempt_id=first.observation.attempt_evidence.attempt_id,
        attempt_digest=first.observation.attempt_evidence.attempt_digest,
        termination_reason="failed",
        provider_acceptance_state=state,
        provider_calls_possible=2 if state is ProviderAcceptanceState.UNKNOWN else 0,
        accounting=RunAccounting(provider_calls_started=1),
    )
    observation = first.observation.model_copy(update={"attempt_evidence": evidence})
    block = SamplingBlockEvidence(
        plan=plan, attempts=(first.model_copy(update={"observation": observation}), *attempts[1:])
    )
    assert not block.eligible
    assert block.original_counts.runner_errors + block.candidate_counts.runner_errors == 1
    assert block.point_difference is None


def test_confirmation_budget_covers_frozen_attempts_and_profile_call_bound() -> None:
    plan = _plan(role="confirmation")
    allowance = SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=20,
        max_wall_time_seconds=1000,
        max_candidate_comparisons=6,
        confirmation_provider_calls=12,
        confirmation_runner_seconds=600,
    )
    allowance.validate_confirmation_plan(plan, attempt_timeout_seconds=100)
    with pytest.raises(ValueError, match="provider-call budget cannot cover"):
        allowance.model_copy(update={"confirmation_provider_calls": 11}).validate_confirmation_plan(
            plan, attempt_timeout_seconds=100
        )
    with pytest.raises(ValueError, match="runner-time budget cannot cover"):
        allowance.model_copy(
            update={"confirmation_runner_seconds": 599}
        ).validate_confirmation_plan(plan, attempt_timeout_seconds=100)
    with pytest.raises(ValueError, match="confirmation plan"):
        allowance.validate_confirmation_plan(_plan(), attempt_timeout_seconds=100)
    with pytest.raises(ValueError, match="positive integer"):
        allowance.validate_confirmation_plan(plan, attempt_timeout_seconds=True)
