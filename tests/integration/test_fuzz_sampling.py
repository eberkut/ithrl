"""Credential-free change comparisons through actual reservations and observation storage."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ithrl.behavior import FinalResponseTarget
from ithrl.context import content_digest
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    StructuralOracleConfig,
)
from ithrl.fuzz import FuzzCampaignConfig, FuzzOutcome
from ithrl.fuzz.sampling import (
    confirm_fuzz_block,
    fuzz_block_plan,
    fuzz_budget_bounds,
    prepare_fuzz_sampling,
    screen_fuzz_block,
)
from ithrl.fuzz.simplify import MutationDecision
from ithrl.mutation import TextAppend, TextMutation
from ithrl.observations import ObservationStore, SamplingBudgetAllowance
from ithrl.reduce.contracts import SampledConfirmation
from ithrl.run_state import SampledRunStateFile, SamplingBudgetApproval
from ithrl.runner import (
    PreparedAttempt,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunnerRegistry,
    RunResult,
    prepare_candidate,
)
from ithrl.sampling import collect_sampling_block
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment

pytest.importorskip("scipy")


class FuzzRunner:
    """Synthetic output and usage; no native process, credentials or network."""

    def __init__(self, *, calls_per_attempt: int = 1) -> None:
        self.capability_profile = CapabilityProfile.deterministic_fake().model_copy(
            update={
                "runner_type": "codex",
                "deterministic": False,
                "provider_transport": "required",
                "provider_calls_per_attempt": calls_per_attempt,
                "provider": "synthetic-provider",
                "endpoint": "https://provider.example.invalid",
                "protected_state_enforcement": "preventive",
            }
        )
        self.calls: list[PreparedAttempt] = []
        self.mutant_matches = False
        self.original_matching: int | None = None
        self.mutant_matching: int | None = None
        self.fail = False

    @property
    def fingerprint(self) -> str:
        return "synthetic-fuzz-sampling:1"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        self.calls.append(attempt)
        attempt.evidence.materialize(mutation=attempt.candidate.mutation)
        if self.fail:
            raise RunnerError(RunnerErrorCode.TRANSPORT, "synthetic transport failure")
        match = attempt.candidate.mutation.is_empty or self.mutant_matches
        matching = self.original_matching if attempt.arm == "baseline" else self.mutant_matching
        if matching is not None:
            rank = sum(a.phase == attempt.phase and a.arm == attempt.arm for a in self.calls)
            match = rank <= matching
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            final_text="BLUE" if match else "RED",
            provider="synthetic-provider",
            endpoint="https://provider.example.invalid",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            accounting=RunAccounting(
                provider_calls_started=1, provider_calls_completed=1, elapsed_milliseconds=1
            ),
        )


def setup(
    tmp_path: Path,
    *,
    limit: int = 384,
    calls_per_attempt: int = 1,
    max_wall_time_seconds: int = 10_000,
    max_tokens: int | None = None,
    screen_valid: int = 8,
    screen_attempts: int = 18,
):
    evidence = extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    runner = FuzzRunner(calls_per_attempt=calls_per_attempt)
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=FinalResponseTarget(text_equals="BLUE"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(required_valid=3, minimum_matches=2, max_attempts=7),
    )
    prepared = prepare_experiment(
        evidence,
        experiment=experiment,
        registry=RunnerRegistry({"codex": lambda _c, _e: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider="synthetic-provider",
            allowed_endpoint="https://provider.example.invalid",
        ),
        resource_limits=ResourceLimits(
            max_provider_calls=calls_per_attempt, max_wall_time_seconds=5
        ),
    )
    campaign = FuzzCampaignConfig.model_validate(
        {
            "original": prepare_candidate(evidence, prepared.execution_plan),
            "seed": 1,
            "mutators": ("append-stale-text",),
            "max_candidates": 4,
            "max_simplification_trials": 10,
            "calibration": experiment.calibration,
            "screening": {
                "original": {"required_valid": screen_valid, "max_attempts": screen_attempts},
                "mutant": {"required_valid": screen_valid, "max_attempts": screen_attempts},
                "minimum_decrease": 0.4,
            },
            "confirmation": {
                "original": {"required_valid": 20, "max_attempts": 36},
                "mutant": {"required_valid": 20, "max_attempts": 36},
                "alpha": 0.05,
            },
            "minimum_decrease": 0.1,
            "eligibility_policy": "independent-safe-preaccept-only",
            "freshness_seconds": 86400,
        }
    )
    prepared = prepare_fuzz_sampling(evidence, prepared, campaign)
    now = datetime.now(UTC)
    store = ObservationStore(tmp_path / "private" / "observations.sqlite")
    store.initialize()
    bounds = fuzz_budget_bounds(
        campaign, sampling_attempt_cost(runner.capability_profile, prepared.resource_limits)
    )
    state = SampledRunStateFile.create(
        case_root=tmp_path / "case",
        run_id="fuzz-test",
        store=store,
        started_at=now,
        original_identity=prepared.identity,
        original_candidate_digest=campaign.original.digest,
        provider_calls_per_attempt=calls_per_attempt,
        approval=SamplingBudgetApproval(
            approval_id="synthetic",
            approved_at=now,
            allowance=SamplingBudgetAllowance(
                run_id="fuzz-test",
                max_provider_calls=limit,
                max_wall_time_seconds=max_wall_time_seconds,
                max_tokens=max_tokens,
                max_candidate_comparisons=14,
                confirmation_provider_calls=bounds.confirmation_provider_calls,
                confirmation_runner_seconds=bounds.confirmation_runner_seconds,
            ),
        ),
    )
    site = evidence.editable_text()[0]
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=site.content_index,
                original_text_digest=content_digest(site.original_text),
                text="\n\nsynthetic stale text",
            ),
        )
    )
    return evidence, prepared, campaign, runner, state, store, mutation


def test_discovery_simplification_and_fresh_confirmation_use_separate_owned_samples(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(tmp_path)
        before = evidence.request
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="discovery",
            block_id="discovery-1",
            frozen_at=datetime.now(UTC),
            schedule_seed=1,
        )
        first = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert screen_fuzz_block(first) is MutationDecision.CHANGED
        assert first.usage.actual_provider_calls == 16
        with state.acquire_lock(store):
            state.archive_block(store)
        mutation = TextMutation(
            edits=(mutation.edits[0].model_copy(update={"text": "\n\nsynthetic"}),)
        )
        simplified_plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="simplification",
            block_id="simplification-1",
            frozen_at=datetime.now(UTC),
            schedule_seed=3,
        )
        simplified = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=simplified_plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert screen_fuzz_block(simplified) is MutationDecision.CHANGED
        with state.acquire_lock(store):
            state.archive_block(store)
        selected = datetime.now(UTC)
        holdout = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="confirmation",
            block_id="holdout-1",
            frozen_at=selected + timedelta(microseconds=1),
            selected_at=selected,
            schedule_seed=2,
        )
        final = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=holdout,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert confirm_fuzz_block(final).outcome is FuzzOutcome.CONFIRMED_CHANGE
        assert not set(first.evidence.observation_ids) & set(final.evidence.observation_ids)
        assert final.usage.actual_provider_calls == 72
        assert Counter((a.phase, a.arm) for a in runner.calls) == {
            ("discovery", "baseline"): 8,
            ("discovery", "candidate"): 8,
            ("simplification", "baseline"): 8,
            ("simplification", "candidate"): 8,
            ("confirmation", "baseline"): 20,
            ("confirmation", "candidate"): 20,
        }
        assert all(a.candidate == campaign.original for a in runner.calls if a.arm == "baseline")
        assert evidence.request == before
        with state.acquire_lock(store):
            state.archive_block(store)
        repeated = holdout.model_copy(update={"block_id": "holdout-2"})
        refused = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=repeated,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert refused.stop_reason == "confirmation-already-started"
        assert len(runner.calls) == 72

    asyncio.run(run())


@pytest.mark.parametrize("calls_per_attempt", [1, 2])
def test_exploration_cannot_spend_the_complete_confirmation_budget(
    tmp_path: Path, calls_per_attempt: int
) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(
            tmp_path, limit=107 * calls_per_attempt, calls_per_attempt=calls_per_attempt
        )
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="simplification",
            block_id="trial-1",
            frozen_at=datetime.now(UTC),
            schedule_seed=0,
        )
        result = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert result.stop_reason == "provider-call-budget"
        assert not runner.calls
        with state.acquire_lock(store):
            assert not state.load(store).blocks
        assert screen_fuzz_block(result) is None

    asyncio.run(run())


@pytest.mark.parametrize("calls_per_attempt", [1, 2])
def test_error_does_not_become_a_behavioral_loss_or_confirmed_finding(
    tmp_path: Path, calls_per_attempt: int
) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(
            tmp_path, calls_per_attempt=calls_per_attempt
        )
        runner.fail = True
        selected = datetime.now(UTC)
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="confirmation",
            block_id="holdout-1",
            frozen_at=selected + timedelta(microseconds=1),
            selected_at=selected,
            schedule_seed=0,
        )
        result = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert not result.complete
        assert confirm_fuzz_block(result).outcome is FuzzOutcome.INCOMPLETE
        assert result.usage.actual_provider_calls == 0
        assert result.usage.possible_provider_calls == calls_per_attempt
        assert result.usage.charged_provider_calls == calls_per_attempt
        assert result.usage.unknown_provider_attempts == 1
        with pytest.raises(ValueError, match="reduction procedure"):
            SampledConfirmation.from_evidence(result.evidence)
        assert result.evidence.original_counts.valid + result.evidence.candidate_counts.valid == 0
        assert (
            result.evidence.original_counts.runner_errors
            + result.evidence.candidate_counts.runner_errors
            == 1
        )
        assert len(runner.calls) == 1

    asyncio.run(run())


def test_equal_rates_complete_as_inconclusive(tmp_path: Path) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(tmp_path)
        runner.mutant_matches = True
        selected = datetime.now(UTC)
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="confirmation",
            block_id="null-holdout",
            selected_at=selected,
            frozen_at=selected + timedelta(microseconds=1),
            schedule_seed=2,
        )
        result = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        classified = confirm_fuzz_block(result)
        assert classified.outcome is FuzzOutcome.INCONCLUSIVE
        assert classified.difference == 0
        assert result.evidence.original_counts.matches == 20
        assert result.evidence.candidate_counts.matches == 20

    asyncio.run(run())


def test_decimal_screen_threshold_includes_exact_equality(tmp_path: Path) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(
            tmp_path, screen_valid=5, screen_attempts=5
        )
        runner.original_matching, runner.mutant_matching = 3, 1
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="discovery",
            block_id="boundary",
            frozen_at=datetime.now(UTC),
            schedule_seed=0,
        )
        result = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
        )
        assert result.evidence.original_counts.matches == 3
        assert result.evidence.candidate_counts.matches == 1
        assert screen_fuzz_block(result) is MutationDecision.CHANGED

    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"required_valid": 4}, {"max_attempts": 8}])
def test_campaign_calibration_must_match_actual_execution_limits(
    tmp_path: Path, changes: dict[str, int]
) -> None:
    evidence, prepared, campaign, runner, _state, _store, _mutation = setup(tmp_path)
    different = campaign.model_copy(
        update={"calibration": campaign.calibration.model_copy(update=changes)}
    )
    with pytest.raises(ValueError, match="campaign calibration differs"):
        prepare_fuzz_sampling(evidence, prepared, different)
    assert not runner.calls


@pytest.mark.parametrize(
    "reason", ["wall-time-budget", "token-estimate-unavailable", "freshness-budget"]
)
def test_time_freshness_and_unenforceable_tokens_refuse_before_dispatch(
    tmp_path: Path, reason: str
) -> None:
    async def run() -> None:
        evidence, prepared, campaign, runner, state, store, mutation = setup(
            tmp_path,
            max_wall_time_seconds=539 if reason == "wall-time-budget" else 100_000,
            max_tokens=1 if reason == "token-estimate-unavailable" else None,
        )
        frozen_at = datetime.now(UTC)
        plan = fuzz_block_plan(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            mutation=mutation,
            role="discovery",
            block_id="refused",
            frozen_at=frozen_at,
            schedule_seed=1,
        )
        clock = (
            (lambda: plan.expires_at - timedelta(seconds=1))
            if reason == "freshness-budget"
            else lambda: datetime.now(UTC)
        )
        result = await collect_sampling_block(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state,
            store=store,
            mutation=mutation,
            clock=clock,
        )
        assert result.stop_reason == reason
        assert not runner.calls

    asyncio.run(run())
