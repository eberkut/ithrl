"""Credential-free change comparisons through actual reservations and observation storage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
from ithrl.fuzz import FuzzCampaignConfig
from ithrl.fuzz.sampling import (
    fuzz_budget_bounds,
    prepare_fuzz_sampling,
)
from ithrl.mutation import TextAppend, TextMutation
from ithrl.observations import ObservationStore, SamplingBudgetAllowance
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
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment


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
        Path(__file__).parent / "fixtures/synthetic-trace.json",
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
