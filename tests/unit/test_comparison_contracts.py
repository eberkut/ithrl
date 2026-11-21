"""Comparison purposes bind explicit contexts without relaxing existing sampling roles."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ithrl.comparison.contracts import BoundComparison, ComparisonConfiguration, ComparisonProgress
from ithrl.experiment import (
    CodexReplayConfig,
    SampledComparisonPlan,
    SampledReductionPlan,
    SampledSearchPhase,
)
from ithrl.observations import (
    ObservationIdentity,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.run_state import SampledRunState, SamplingBlockProgress, SamplingBudgetApproval
from ithrl.runner import Candidate

NOW = datetime(2026, 11, 20, tzinfo=UTC)


@pytest.fixture
def configuration() -> ComparisonConfiguration:
    return ComparisonConfiguration.model_validate(
        {
            "objective": "repair",
            "reference": {"removed_context_ids": ("ctx-a",)},
            "candidate": {},
            "calibration": {
                "reference": {"required_valid": 3, "max_attempts": 3},
                "candidate": {"required_valid": 3, "max_attempts": 3},
                "positive_minimum_matches": 2,
                "negative_maximum_matches": 1,
            },
            "confirmation": {
                "reference": {"required_valid": 20, "max_attempts": 20},
                "candidate": {"required_valid": 20, "max_attempts": 20},
                "alpha": 0.05,
            },
            "minimum_effect": 0.1,
            "seed": 9,
            "eligibility_policy": "independent-safe-preaccept-only",
            "freshness_seconds": 3600,
            "limits": {
                "max_provider_calls": 50,
                "max_wall_time_seconds": 7200,
                "attempt_timeout_seconds": 120,
            },
        }
    )


def binding(configuration: ComparisonConfiguration) -> BoundComparison:
    common = {
        "case_digest": "a" * 64,
        "execution_plan_digest": "b" * 64,
    }
    return BoundComparison(
        procedure=configuration.procedure,
        reference=Candidate(
            **common,
            materialization="exact-request",
            retained_context_ids=("trigger",),
            removed_context_ids=("ctx-a",),
        ),
        candidate=Candidate(
            **common,
            materialization="exact-request",
            retained_context_ids=("ctx-a", "trigger"),
            removed_context_ids=(),
        ),
    )


def block(configuration: ComparisonConfiguration) -> SamplingBlockPlan:
    pair = binding(configuration)
    identity = ObservationIdentity(
        case_digest=pair.reference.case_digest,
        execution_plan_digest=pair.reference.execution_plan_digest,
        experiment_digest="c" * 64,
        target_digest="d" * 64,
        runner_fingerprint="fixture",
        algorithm_digest=configuration.policy_digest,
    )
    return SamplingBlockPlan(
        block_id="comparison-calibration",
        role="calibration",
        procedure=configuration.procedure,
        original_identity=identity,
        candidate_identity=identity,
        original_candidate_digest=pair.reference.digest,
        candidate_digest=pair.candidate.digest,
        frozen_at=NOW,
        schedule_seed=9,
        schedule=sampling_schedule(3, 3, 9),
        provider_calls_per_attempt=0,
    )


def test_administrative_limits_do_not_reinterpret_comparison_policy(
    configuration: ComparisonConfiguration,
) -> None:
    payload = configuration.model_dump()
    payload["limits"]["max_provider_calls"] += 1
    extended = ComparisonConfiguration.model_validate(payload)
    assert extended.policy_digest == configuration.policy_digest
    assert extended.procedure == configuration.procedure
    assert extended.digest != configuration.digest


def test_comparison_bounds_the_total_scheduled_attempts(
    configuration: ComparisonConfiguration,
) -> None:
    payload = configuration.model_dump()
    for phase in ("calibration", "confirmation"):
        for arm in ("reference", "candidate"):
            payload[phase][arm]["max_attempts"] = 250
    assert ComparisonConfiguration.model_validate(payload)
    payload["confirmation"]["candidate"]["max_attempts"] += 1
    with pytest.raises(ValidationError, match="at most 1000"):
        ComparisonConfiguration.model_validate(payload)


def test_prospective_execution_is_bound_separately_from_the_sampling_procedure(
    configuration: ComparisonConfiguration,
) -> None:
    replay = CodexReplayConfig(provider="openai", model="test-model", reasoning="omit")
    prospective = ComparisonConfiguration.model_validate(
        {**configuration.model_dump(), "codex_replay": replay}
    )
    assert prospective.digest != configuration.digest
    assert prospective.procedure == configuration.procedure
    assert prospective.codex_replay == replay


@pytest.mark.parametrize("field", ["objective", "alpha", "minimum_effect", "seed"])
def test_saved_procedure_cannot_reinterpret_its_policy_digest(
    configuration: ComparisonConfiguration, field: str
) -> None:
    payload = json.loads(configuration.procedure.model_dump_json())
    if field == "alpha":
        payload["confirmation"]["alpha"] = 0.1
    else:
        payload[field] = {"objective": "degradation", "minimum_effect": 0.2, "seed": 10}[field]
    with pytest.raises(ValidationError, match="policy digest"):
        SampledComparisonPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_comparison_schema_requires_an_integer(
    configuration: ComparisonConfiguration, version: object
) -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        ComparisonConfiguration.model_validate(
            {**configuration.model_dump(), "schema_version": version}
        )


def test_comparison_blocks_use_calibration_targets_and_exclude_search_roles(
    configuration: ComparisonConfiguration,
) -> None:
    plan = block(configuration)
    assert plan.targets_for("baseline").required_valid == 3
    assert plan.targets_for("candidate").required_valid == 3
    for role in ("search", "discovery", "simplification"):
        with pytest.raises(ValidationError, match="role does not belong"):
            SamplingBlockPlan.model_validate({**plan.model_dump(), "role": role})


def test_reduction_does_not_acquire_two_arm_block_calibration(
    configuration: ComparisonConfiguration,
) -> None:
    plan = block(configuration)
    reduction = SampledReductionPlan.model_validate(
        {
            "search": SampledSearchPhase(
                original=configuration.calibration.reference,
                candidate=configuration.calibration.candidate,
            ),
            "confirmation": {
                "original": configuration.confirmation.reference,
                "candidate": configuration.confirmation.candidate,
                "one_sided_confidence": 0.95,
            },
            "margin": 0.2,
            "freshness_seconds": 3600,
        }
    )
    with pytest.raises(ValidationError, match="role does not belong"):
        SamplingBlockPlan.model_validate(
            {**plan.model_dump(), "procedure": reduction, "provider_calls_per_attempt": 1}
        )


def test_state_binds_both_contexts_and_excludes_legacy_calibration_reuse(
    configuration: ComparisonConfiguration,
) -> None:
    plan = block(configuration)
    pair = binding(configuration)
    state = SampledRunState(
        run_id="comparison-test",
        source_case="/case",
        observation_store="/observations.sqlite",
        started_at=NOW,
        original_identity=plan.original_identity,
        original_candidate_digest=pair.reference.digest,
        provider_calls_per_attempt=0,
        approvals=(
            SamplingBudgetApproval(
                approval_id="local",
                approved_at=NOW,
                allowance=SamplingBudgetAllowance(
                    run_id="comparison-test",
                    max_provider_calls=0,
                    max_wall_time_seconds=7200,
                    max_candidate_comparisons=1,
                    confirmation_provider_calls=0,
                    confirmation_runner_seconds=40,
                ),
            ),
        ),
        comparison_progress=ComparisonProgress(binding=pair, implementation_digest="f" * 64),
        blocks=(SamplingBlockProgress(plan=plan),),
    )
    with pytest.raises(ValidationError, match="run identity"):
        SampledRunState.model_validate(
            {**state.model_dump(), "calibration_observation_ids": ("old-calibration",)}
        )
    changed = plan.model_copy(update={"candidate_digest": "f" * 64})
    with pytest.raises(ValidationError, match="both selected contexts"):
        SampledRunState.model_validate(
            {**state.model_dump(), "blocks": (SamplingBlockProgress(plan=changed),)}
        )
