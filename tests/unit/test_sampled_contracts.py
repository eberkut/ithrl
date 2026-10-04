"""Content-safe sampled comparisons, checkpoints, and final evidence contracts."""

from datetime import UTC, datetime, timedelta
from math import nextafter
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
    SamplingAttempt,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingRole,
    sampling_schedule,
)
from ithrl.reduce import (
    SampledCandidateTrial,
    SampledComparison,
    SampledConfirmation,
    SampledReductionCheckpoint,
    SampledScreenAction,
    SearchPhase,
)
from ithrl.reduce.contracts import trial_key
from ithrl.results import SampledReductionEvidence
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting
from ithrl.statistics import BinomialSample, FixedSampleOutcome, newcombe_interval

FROZEN = datetime(2026, 10, 4, tzinfo=UTC)
ORIGINAL = "a" * 64
CANDIDATE = "b" * 64
NEIGHBOR = "c" * 64


def _identity(execution: str = "d" * 64) -> ObservationIdentity:
    return ObservationIdentity(
        case_digest="e" * 64,
        experiment_digest="f" * 64,
        runner_fingerprint="sanitized-runner",
        execution_plan_digest=execution,
        target_digest="1" * 64,
        algorithm_digest="2" * 64,
    )


def _reduction() -> SampledReductionPlan:
    return SampledReductionPlan(
        search=SampledSearchPhase(
            original=SamplingArmTarget(required_valid=2, max_attempts=3),
            candidate=SamplingArmTarget(required_valid=2, max_attempts=3),
        ),
        confirmation=SampledConfirmationPhase(
            original=SamplingArmTarget(required_valid=20, max_attempts=21),
            candidate=SamplingArmTarget(required_valid=20, max_attempts=21),
            one_sided_confidence=0.95,
        ),
        margin=0.2,
        freshness_seconds=600,
    )


def _block(
    block_id: str = "screen-1",
    *,
    original: tuple[ObservationOutcome, ...] = (ObservationOutcome.MATCH,) * 2,
    candidate: tuple[ObservationOutcome, ...] = (ObservationOutcome.MATCH,) * 2,
    role: SamplingRole = "search",
    candidate_digest: str = CANDIDATE,
    frozen_at: datetime = FROZEN,
    unsafe: bool = False,
    reduction: SampledReductionPlan | None = None,
) -> SamplingBlockEvidence:
    reduction = reduction or _reduction()
    phase = reduction.search if role == "search" else reduction.confirmation
    plan = SamplingBlockPlan(
        block_id=block_id,
        role=role,
        reduction=reduction,
        original_identity=_identity(),
        candidate_identity=_identity("3" * 64),
        original_candidate_digest=ORIGINAL,
        candidate_digest=candidate_digest,
        frozen_at=frozen_at,
        schedule_seed=17,
        schedule=sampling_schedule(phase.original.max_attempts, phase.candidate.max_attempts, 17),
        provider_calls_per_attempt=1,
    )
    outcomes = {"baseline": original, "candidate": candidate}
    counts = {"baseline": 0, "candidate": 0}
    attempts = []
    for position, arm in enumerate(plan.schedule):
        index = counts[arm]
        if index >= len(outcomes[arm]):
            valid_so_far = sum(
                outcome in {ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH}
                for outcome in outcomes[arm]
            )
            if valid_so_far == plan.targets_for(arm).required_valid:
                continue
            break
        counts[arm] += 1
        outcome = outcomes[arm][index]
        valid = outcome in {ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH}
        digest = content_digest({"block": block_id, "position": position})
        execution = AttemptEvidence(
            attempt_id=f"attempt-{digest[:32]}",
            attempt_digest=digest,
            termination_reason="completed" if valid else "failed",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            effective_policy_digest="4" * 64,
            accounting=RunAccounting(
                original_workspace_effect="may-change" if unsafe else "unchanged"
            ),
            retry_safe=not valid,
        )
        observation = Observation(
            observation_id=digest,
            recorded_at=frozen_at + timedelta(seconds=position + 1),
            **(
                plan.original_identity if arm == "baseline" else plan.candidate_identity
            ).model_dump(),
            candidate_digest=ORIGINAL if arm == "baseline" else candidate_digest,
            role=ObservationRole(role),
            arm=ObservationArm(arm),
            attempt=index + 1,
            outcome=outcome,
            error_category=None if valid else "startup",
            attempt_evidence=execution,
        )
        attempts.append(
            SamplingAttempt(
                observation=observation,
                schedule_position=position,
                independence_evidence_digest=None if valid else "5" * 64,
            )
        )
    return SamplingBlockEvidence(plan=plan, attempts=tuple(attempts))


def _trial(
    block: SamplingBlockEvidence,
    *,
    base: tuple[str, ...] = (),
    requested: tuple[str, ...] = ("ctx-a",),
    phase: SearchPhase = "chunk",
    action: SampledScreenAction | None = "admit",
) -> SampledCandidateTrial:
    return SampledCandidateTrial(
        trial_key=trial_key(phase, base, requested),
        base_removals=base,
        requested_removals=requested,
        effective_removals=tuple(sorted(set(base) | set(requested))),
        phase=phase,
        comparison=SampledComparison(evidence=block, action=action),
    )


def _checkpoint(*trials: SampledCandidateTrial, **changes: Any) -> SampledReductionCheckpoint:
    admitted = tuple(trial for trial in trials if trial.comparison.action == "admit")
    return SampledReductionCheckpoint(
        reduction=_reduction(),
        original_identity=_identity(),
        original_candidate_digest=ORIGINAL,
        best_candidate_digest=(
            admitted[-1].comparison.evidence.plan.candidate_digest if admitted else ORIGINAL
        ),
        removed_context_ids=admitted[-1].effective_removals if admitted else (),
        trials=trials,
        **changes,
    )


def _confirmation(
    *, outcome: FixedSampleOutcome | None = FixedSampleOutcome.PRESERVES, **changes: Any
) -> SampledConfirmation:
    return SampledConfirmation(
        evidence=_block(
            "confirmation-1",
            role="confirmation",
            original=(ObservationOutcome.MATCH,) * 20,
            candidate=(ObservationOutcome.MATCH,) * 20,
            frozen_at=FROZEN + timedelta(seconds=100),
            **changes,
        ),
        outcome=outcome,
    )


def _result(checkpoint: SampledReductionCheckpoint, **changes: Any) -> SampledReductionEvidence:
    return SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-reduced" if checkpoint.final_sweep_complete else "search-incomplete",
        selected_candidate_digest=checkpoint.best_candidate_digest,
        selected_at=FROZEN + timedelta(seconds=90),
        **changes,
    )


def test_search_screen_and_confirmation_are_separate() -> None:
    block = _block()
    assert SampledComparison(evidence=block, action="admit").operational_refusal is None
    with pytest.raises(ValidationError, match="screen action"):
        SampledComparison(evidence=block, action="retain")
    losing = _block(candidate=(ObservationOutcome.NO_MATCH,) * 2)
    assert SampledComparison(evidence=losing, action="retain").action == "retain"
    with pytest.raises(ValidationError, match="search evidence"):
        SampledComparison(evidence=_confirmation().evidence, action="admit")
    with pytest.raises(ValidationError, match="confirmation evidence"):
        SampledConfirmation(evidence=block, outcome=FixedSampleOutcome.PRESERVES)


@pytest.mark.parametrize(
    ("margin", "action"),
    [(nextafter(0.2, 0.0), "retain"), (0.2, "admit"), (nextafter(0.2, 1.0), "admit")],
)
def test_search_admission_preserves_nonbinary_margin_boundary(
    margin: float, action: SampledScreenAction
) -> None:
    target = SamplingArmTarget(required_valid=5, max_attempts=6)
    reduction = _reduction().model_copy(
        update={"search": SampledSearchPhase(original=target, candidate=target), "margin": margin}
    )
    block = _block(
        reduction=reduction,
        original=(ObservationOutcome.MATCH,) * 4 + (ObservationOutcome.NO_MATCH,),
        candidate=(ObservationOutcome.MATCH,) * 3 + (ObservationOutcome.NO_MATCH,) * 2,
    )
    assert block.point_difference == 3 / 5 - 4 / 5
    assert SampledComparison(evidence=block, action=action).action == action
    with pytest.raises(ValidationError, match="screen action"):
        SampledComparison(evidence=block, action="retain" if action == "admit" else "admit")


def test_partial_arms_and_operational_failures_cannot_be_behavior_losses() -> None:
    partial = _block(original=(ObservationOutcome.MATCH,))
    assert SampledComparison(evidence=partial, action="incomplete").action == "incomplete"
    with pytest.raises(ValidationError, match="screen action"):
        SampledComparison(evidence=partial, action="admit")
    safe_failure = _block(
        candidate=(
            ObservationOutcome.RUNNER_ERROR,
            ObservationOutcome.MATCH,
            ObservationOutcome.MATCH,
        )
    )
    assert safe_failure.candidate_counts.runner_errors == 1
    assert safe_failure.candidate_counts.no_matches == 0
    assert SampledComparison(evidence=safe_failure, action="admit").action == "admit"
    refused = _block(unsafe=True)
    screen = SampledComparison(evidence=refused, action=None)
    assert screen.operational_refusal == "unsafe-attempt"
    incomplete = _block(
        role="confirmation",
        original=(ObservationOutcome.MATCH,),
        candidate=(ObservationOutcome.MATCH,),
    )
    assert SampledConfirmation(evidence=incomplete, outcome=FixedSampleOutcome.INCOMPLETE)
    with pytest.raises(ValidationError, match="confirmation outcome"):
        SampledConfirmation(evidence=incomplete, outcome=FixedSampleOutcome.LOSES)


def test_confirmation_rejects_claim_mismatch() -> None:
    confirmation = _confirmation()
    data = confirmation.model_dump()
    data["outcome"] = FixedSampleOutcome.LOSES
    with pytest.raises(ValidationError, match="confirmation outcome"):
        SampledConfirmation.model_validate(data)


def test_checkpoint_binds_original_identity_and_refuses_reference_reuse() -> None:
    first = _trial(_block())
    checkpoint = _checkpoint(first)
    assert checkpoint.best_candidate_digest == CANDIDATE
    data = checkpoint.model_dump()
    data["original_identity"]["experiment_digest"] = "6" * 64
    with pytest.raises(ValidationError, match="frozen reduction and original"):
        SampledReductionCheckpoint.model_validate(data)
    duplicate = _trial(_block(), base=("ctx-a",), requested=("ctx-b",))
    with pytest.raises(ValidationError, match="references must not be reused"):
        _checkpoint(first, duplicate)
    partial = _block("active", candidate=(ObservationOutcome.MATCH,))
    assert _checkpoint(first, active_block=partial).active_block is not None
    with pytest.raises(ValidationError, match="active block must be partial"):
        _checkpoint(first, active_block=_block("completed-active"))


def test_final_sweep_witness_requires_current_candidate_and_exact_coverage() -> None:
    first = _trial(_block())
    neighbor = _trial(
        _block("neighbor", candidate=(ObservationOutcome.NO_MATCH,) * 2, candidate_digest=NEIGHBOR),
        base=("ctx-a",),
        requested=("ctx-b",),
        phase="final-sweep",
        action="retain",
    )
    checkpoint = _checkpoint(
        first,
        neighbor,
        final_sweep_complete=True,
        final_sweep_trial_keys=(neighbor.trial_key,),
        expected_final_neighbors=(("ctx-b",),),
    )
    assert _result(checkpoint).search_result == "search-reduced"
    with pytest.raises(ValidationError, match="exactly the declared"):
        _checkpoint(
            first,
            neighbor,
            final_sweep_complete=True,
            final_sweep_trial_keys=(neighbor.trial_key,),
            expected_final_neighbors=(("ctx-c",),),
        )
    old_neighbor = _trial(
        _block("old-neighbor", candidate=(ObservationOutcome.NO_MATCH,) * 2),
        requested=("ctx-b",),
        phase="final-sweep",
        action="retain",
    )
    with pytest.raises(ValidationError, match="retained final neighbors"):
        _checkpoint(
            old_neighbor,
            first,
            final_sweep_complete=True,
            final_sweep_trial_keys=(old_neighbor.trial_key,),
            expected_final_neighbors=(("ctx-b",),),
        )


def test_final_evidence_binds_selection_time_and_forbids_reuse() -> None:
    checkpoint = _checkpoint(_trial(_block()))
    result = _result(checkpoint, confirmation=_confirmation())
    assert result.confirmation is not None
    assert SampledReductionEvidence.model_validate_json(result.model_dump_json()) == result
    data = result.model_dump()
    data["selected_candidate_digest"] = NEIGHBOR
    with pytest.raises(ValidationError, match="checkpoint best"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    data["selected_at"] = FROZEN + timedelta(seconds=110)
    with pytest.raises(ValidationError, match="after candidate selection"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    data["calibration_observation_ids"] = checkpoint.blocks[0].observation_ids[:1]
    with pytest.raises(ValidationError, match="cannot reuse observation"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    confirmation_attempt = data["confirmation"]["evidence"]["attempts"][0]
    confirmation_attempt["observation"]["attempt_evidence"]["attempt_id"] = checkpoint.blocks[
        0
    ].attempt_ids[0]
    with pytest.raises(ValidationError, match="cannot reuse attempt"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    confirmation_plan = data["confirmation"]["evidence"]["plan"]
    confirmation_plan["candidate_identity"]["execution_plan_digest"] = "7" * 64
    for item in data["confirmation"]["evidence"]["attempts"]:
        if item["observation"]["arm"] == ObservationArm.CANDIDATE:
            item["observation"]["execution_plan_digest"] = "7" * 64
    with pytest.raises(ValidationError, match="frozen original and selected candidate"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    data["selected_at"] = FROZEN
    with pytest.raises(ValidationError, match="follow all search evidence"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    confirmation_attempt = data["confirmation"]["evidence"]["attempts"][0]
    confirmation_attempt["observation"]["observation_id"] = checkpoint.blocks[0].observation_ids[0]
    with pytest.raises(ValidationError, match="cannot reuse observation"):
        SampledReductionEvidence.model_validate(data)


def test_unsafe_search_forbids_confirmation_and_premature_search_claims() -> None:
    trial = _trial(_block(unsafe=True), action=None)
    checkpoint = _checkpoint(trial)
    assert _result(checkpoint).search_result == "search-incomplete"
    with pytest.raises(ValidationError, match="ineligible search evidence"):
        _result(checkpoint, confirmation=_confirmation(candidate_digest=ORIGINAL))
    data = _result(checkpoint).model_dump()
    data["search_result"] = "search-reduced"
    with pytest.raises(ValidationError, match="final sweep witness"):
        SampledReductionEvidence.model_validate(data)
    data["search_result"] = "search-1-minimal"
    with pytest.raises(ValidationError, match="search-reduced"):
        SampledReductionEvidence.model_validate(data)


def test_complete_operational_refusal_has_no_statistical_outcome() -> None:
    screen = _block(unsafe=True)
    assert screen.complete
    assert not screen.eligible
    assert SampledComparison(evidence=screen, action=None).operational_refusal == "unsafe-attempt"
    with pytest.raises(ValidationError, match="screen action"):
        SampledComparison(evidence=screen, action="incomplete")
    confirmation = _confirmation(unsafe=True, outcome=None)
    assert confirmation.evidence.complete
    assert confirmation.outcome is None
    assert confirmation.operational_refusal == "unsafe-attempt"
    with pytest.raises(ValidationError, match="confirmation outcome"):
        SampledConfirmation(evidence=confirmation.evidence, outcome=FixedSampleOutcome.INCOMPLETE)


def test_search_margin_equality_admits_without_a_confidence_claim() -> None:
    plan = _reduction().model_copy(update={"margin": 0.5})
    boundary = _block(
        candidate=(ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH), reduction=plan
    )
    assert boundary.point_difference == -plan.margin
    assert SampledComparison(evidence=boundary, action="admit")
    with pytest.raises(ValidationError, match="screen action"):
        SampledComparison(evidence=boundary, action="retain")


@pytest.mark.parametrize(
    ("candidate_matches", "original_matches", "expected"),
    [
        (20, 20, FixedSampleOutcome.PRESERVES),
        (0, 20, FixedSampleOutcome.LOSES),
        (10, 10, FixedSampleOutcome.INCONCLUSIVE),
    ],
)
def test_confirmation_complete_outcomes(
    candidate_matches: int, original_matches: int, expected: FixedSampleOutcome
) -> None:
    block = _block(
        role="confirmation",
        candidate=(ObservationOutcome.MATCH,) * candidate_matches
        + (ObservationOutcome.NO_MATCH,) * (20 - candidate_matches),
        original=(ObservationOutcome.MATCH,) * original_matches
        + (ObservationOutcome.NO_MATCH,) * (20 - original_matches),
    )
    assert SampledConfirmation(evidence=block, outcome=expected).operational_refusal is None
    wrong = (
        FixedSampleOutcome.LOSES
        if expected != FixedSampleOutcome.LOSES
        else FixedSampleOutcome.PRESERVES
    )
    with pytest.raises(ValidationError, match="confirmation outcome"):
        SampledConfirmation(evidence=block, outcome=wrong)


def test_confirmation_preservation_and_loss_boundaries_are_not_interchangeable() -> None:
    equal_interval = newcombe_interval(BinomialSample(10, 20), BinomialSample(10, 20))
    preserving = _block(
        role="confirmation",
        candidate=(ObservationOutcome.MATCH,) * 10 + (ObservationOutcome.NO_MATCH,) * 10,
        original=(ObservationOutcome.MATCH,) * 10 + (ObservationOutcome.NO_MATCH,) * 10,
        reduction=_reduction().model_copy(update={"margin": -equal_interval.lower}),
    )
    assert SampledConfirmation(evidence=preserving, outcome=FixedSampleOutcome.PRESERVES)
    loss_interval = newcombe_interval(BinomialSample(0, 20), BinomialSample(20, 20))
    losing_boundary = _block(
        role="confirmation",
        candidate=(ObservationOutcome.NO_MATCH,) * 20,
        original=(ObservationOutcome.MATCH,) * 20,
        reduction=_reduction().model_copy(update={"margin": -loss_interval.upper}),
    )
    assert SampledConfirmation(evidence=losing_boundary, outcome=FixedSampleOutcome.INCONCLUSIVE)
    with pytest.raises(ValidationError, match="confirmation outcome"):
        SampledConfirmation(evidence=losing_boundary, outcome=FixedSampleOutcome.LOSES)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("base_removals", ("ctx-b", "ctx-a"), "sorted unique"),
        ("requested_removals", ("ctx-a", "ctx-a"), "nonempty unique"),
        ("effective_removals", (), "include base and requested"),
        ("trial_key", "wrong-key", "trial inputs"),
    ],
)
def test_trial_rejects_metadata_mismatch(field: str, value: Any, message: str) -> None:
    data = _trial(_block()).model_dump()
    data[field] = value
    with pytest.raises(ValidationError, match=message):
        SampledCandidateTrial.model_validate(data)


def test_checkpoint_rejects_best_candidate_and_configuration_mismatch() -> None:
    checkpoint = _checkpoint(_trial(_block()))
    data = checkpoint.model_dump()
    data["best_candidate_digest"] = NEIGHBOR
    with pytest.raises(ValidationError, match="match admitted comparisons"):
        SampledReductionCheckpoint.model_validate(data)
    data = checkpoint.model_dump()
    data["reduction"]["margin"] = 0.3
    with pytest.raises(ValidationError, match="frozen reduction and original"):
        SampledReductionCheckpoint.model_validate(data)
    data = checkpoint.model_dump()
    data["trials"] = (*data["trials"], data["trials"][0])
    with pytest.raises(ValidationError, match="trial keys"):
        SampledReductionCheckpoint.model_validate(data)
    with pytest.raises(ValidationError, match="current best candidate"):
        _checkpoint(_trial(_block(), base=("ctx-z",)))


def test_checkpoint_refuses_completed_claim_with_partial_or_refused_work() -> None:
    partial = _block("partial", original=(ObservationOutcome.MATCH,))
    with pytest.raises(ValidationError, match="complete eligible search evidence"):
        _checkpoint(_trial(partial, action="incomplete"), final_sweep_complete=True)
    refused = _block("refused", unsafe=True)
    with pytest.raises(ValidationError, match="complete eligible search evidence"):
        _checkpoint(_trial(refused, action=None), final_sweep_complete=True)
    with pytest.raises(ValidationError, match="complete eligible search evidence"):
        _checkpoint(active_block=partial, final_sweep_complete=True)
    with pytest.raises(ValidationError, match="completion witness"):
        _checkpoint(expected_final_neighbors=(("ctx-a",),))


def test_final_sweep_rejects_unknown_duplicate_and_wrong_phase_references() -> None:
    trial = _trial(_block(candidate=(ObservationOutcome.NO_MATCH,) * 2), action="retain")
    with pytest.raises(ValidationError, match="belong to checkpoint trials"):
        _checkpoint(
            trial,
            final_sweep_complete=True,
            final_sweep_trial_keys=("unknown",),
            expected_final_neighbors=(("ctx-a",),),
        )
    with pytest.raises(ValidationError, match="duplicates"):
        _checkpoint(
            trial,
            final_sweep_complete=True,
            final_sweep_trial_keys=(trial.trial_key,) * 2,
            expected_final_neighbors=(("ctx-a",),),
        )
    with pytest.raises(ValidationError, match="retained final neighbors"):
        _checkpoint(
            trial,
            final_sweep_complete=True,
            final_sweep_trial_keys=(trial.trial_key,),
            expected_final_neighbors=(("ctx-a",),),
        )


def test_result_rejects_confirmation_of_another_candidate_and_reused_block() -> None:
    checkpoint = _checkpoint(_trial(_block()))
    with pytest.raises(ValidationError, match="frozen original and selected candidate"):
        _result(checkpoint, confirmation=_confirmation(candidate_digest=NEIGHBOR))
    result = _result(checkpoint, confirmation=_confirmation())
    data = result.model_dump()
    data["confirmation"]["evidence"]["plan"]["block_id"] = checkpoint.blocks[0].plan.block_id
    with pytest.raises(ValidationError, match="cannot reuse block"):
        SampledReductionEvidence.model_validate(data)
    data = result.model_dump()
    data["calibration_attempt_ids"] = checkpoint.blocks[0].attempt_ids[:1]
    with pytest.raises(ValidationError, match="cannot reuse attempt"):
        SampledReductionEvidence.model_validate(data)
