"""Comparison purposes bind explicit contexts without relaxing existing sampling roles."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ithrl.comparison.contracts import BoundComparison, ComparisonConfiguration, ComparisonProgress
from ithrl.comparison.engine import comparison_budget_bounds
from ithrl.context import content_digest
from ithrl.execution.budget import SamplingAttemptCost
from ithrl.experiment import (
    SampledComparisonPlan,
    SampledReductionPlan,
    SampledSearchPhase,
)
from ithrl.mutation import ContextSelection, TextAppend, TextMutation
from ithrl.observations import (
    ObservationIdentity,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.run_state import SampledRunState, SamplingBlockProgress, SamplingBudgetApproval
from ithrl.runner import Candidate
from ithrl.tool_output_text import ToolOutputTextSpanRemoval

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


@pytest.mark.parametrize("delay", [-1, True, 86401])
def test_comparison_budget_rejects_invalid_pacing(
    configuration: ComparisonConfiguration, delay: int
) -> None:
    with pytest.raises(ValueError, match="pacing must be a bounded nonnegative integer"):
        comparison_budget_bounds(
            configuration,
            SamplingAttemptCost(provider_calls=1, runner_seconds=1),
            inter_attempt_delay_seconds=delay,
        )


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
    prospective = ComparisonConfiguration.model_validate(
        {**configuration.model_dump(), "provider": "other-provider"}
    )
    assert prospective.digest != configuration.digest
    assert prospective.procedure == configuration.procedure
    assert prospective.provider == "other-provider"


def test_prospective_model_is_bound_separately_from_the_sampling_procedure(
    configuration: ComparisonConfiguration,
) -> None:
    prospective = ComparisonConfiguration.model_validate(
        {**configuration.model_dump(), "model": "gpt-6-luna"}
    )
    assert prospective.digest != configuration.digest
    assert prospective.procedure == configuration.procedure
    assert prospective.model == "gpt-6-luna"
    assert "model" not in configuration.model_dump()
    assert ComparisonConfiguration.model_validate_json(prospective.model_dump_json()) == prospective


def test_comparison_refuses_removed_codex_replay_table(
    configuration: ComparisonConfiguration,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ComparisonConfiguration.model_validate({**configuration.model_dump(), "codex_replay": {}})


def test_reasoning_effort_binds_execution_separately_from_sampling_policy(
    configuration: ComparisonConfiguration,
) -> None:
    selected = ComparisonConfiguration.model_validate(
        {**configuration.model_dump(), "reasoning_effort": "low"}
    )
    assert selected.digest != configuration.digest
    assert selected.procedure == configuration.procedure
    assert "reasoning_effort" not in configuration.model_dump()
    assert ComparisonConfiguration.model_validate_json(selected.model_dump_json()) == selected


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


def _span_selection() -> ContextSelection:
    return ContextSelection(
        mutation=TextMutation(
            edits=(
                ToolOutputTextSpanRemoval(
                    context_id="tool-result",
                    field_id="tool-output-text-" + "a" * 64,
                    original_text_digest="b" * 64,
                    start=1,
                    end=2,
                    removed_text_digest="c" * 64,
                ),
            )
        )
    )


@pytest.mark.parametrize("reference_edit", ["tool", "append", "section"])
def test_tool_comparison_requires_an_unmutated_reference_in_configuration_and_saved_plan(
    configuration: ComparisonConfiguration, reference_edit: str
) -> None:
    from ithrl.text_sections import TEXT_SECTION_POLICY_DIGEST, TextSectionRemoval

    reference = {
        "tool": _span_selection(),
        "append": ContextSelection(
            mutation=TextMutation(
                edits=(
                    TextAppend(
                        context_id="prior",
                        content_index=0,
                        original_text_digest="a" * 64,
                        text="extra",
                    ),
                )
            )
        ),
        "section": ContextSelection(
            mutation=TextMutation(
                edits=(
                    TextSectionRemoval(
                        context_id="prior",
                        content_index=0,
                        original_text_digest="a" * 64,
                        policy_digest=TEXT_SECTION_POLICY_DIGEST,
                        section_ids=("section-" + "b" * 64,),
                    ),
                )
            )
        ),
    }[reference_edit]
    payload = {**configuration.model_dump(), "reference": reference, "candidate": _span_selection()}
    with pytest.raises(ValidationError, match="unmutated reference"):
        ComparisonConfiguration.model_validate(payload)
    valid = ComparisonConfiguration.model_validate(
        {
            **configuration.model_dump(),
            "reference": ContextSelection(),
            "candidate": _span_selection(),
        }
    )
    procedure = valid.procedure.model_copy(update={"reference": reference})
    procedure = procedure.model_copy(
        update={"comparison_digest": content_digest(procedure.policy_projection)}
    )
    with pytest.raises(ValidationError, match="unmutated reference"):
        SampledComparisonPlan.model_validate_json(procedure.model_dump_json())


def test_bound_tool_comparison_requires_identical_materialized_record_partitions(
    configuration: ComparisonConfiguration,
) -> None:
    configuration = ComparisonConfiguration.model_validate(
        {
            **configuration.model_dump(),
            "reference": ContextSelection(),
            "candidate": _span_selection(),
        }
    )
    common = {
        "case_digest": "a" * 64,
        "execution_plan_digest": "b" * 64,
    }
    reference = Candidate(
        **common,
        materialization="source-backed",
        retained_context_ids=("prior", "tool-result"),
        removed_context_ids=(),
    )
    candidate = Candidate(
        **common,
        materialization="source-backed",
        retained_context_ids=("tool-result",),
        removed_context_ids=("prior",),
        mutation=configuration.candidate.mutation,
    )
    with pytest.raises(ValidationError, match="identical record partitions"):
        BoundComparison(procedure=configuration.procedure, reference=reference, candidate=candidate)


def test_tool_comparison_binds_closed_partitions_instead_of_requested_roots(
    configuration: ComparisonConfiguration,
) -> None:
    candidate_selection = _span_selection().model_copy(
        update={"removed_context_ids": ("prior", "dependent")}
    )
    configuration = ComparisonConfiguration.model_validate(
        {
            **configuration.model_dump(),
            "reference": ContextSelection(removed_context_ids=("prior",)),
            "candidate": candidate_selection,
        }
    )
    common = {
        "case_digest": "a" * 64,
        "execution_plan_digest": "b" * 64,
        "retained_context_ids": ("tool-result",),
        "removed_context_ids": ("prior", "dependent"),
    }
    pair = BoundComparison(
        procedure=configuration.procedure,
        reference=Candidate(**common, materialization="source-backed"),
        candidate=Candidate(
            **common, materialization="source-backed", mutation=candidate_selection.mutation
        ),
    )
    assert BoundComparison.model_validate_json(pair.model_dump_json()) == pair
    payload = json.loads(pair.model_dump_json())
    payload["reference"]["removed_context_ids"] = ["prior"]
    payload["reference"]["retained_context_ids"] = ["tool-result", "dependent"]
    with pytest.raises(ValidationError, match="identical record partitions"):
        BoundComparison.model_validate_json(json.dumps(payload))
