"""Credential-free end-to-end tests for sampled block collection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, Self, override
from unittest.mock import patch

import pytest

import ithrl.observations as observations_module
import ithrl.sampling as sampling_module
from ithrl.case import CaseEvidence
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    ExperimentDefinition,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
)
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationRole,
    ObservationStore,
    SamplingBlockPlan,
    sampling_schedule,
)
from ithrl.run_state import (
    SampledRunStateFile,
    SamplingBudgetApproval,
)
from ithrl.runner import (
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunnerRegistry,
    RunResult,
    TokenUsage,
    ToolCall,
    prepare_candidate,
)
from ithrl.sampling import collect_sampling_block
from ithrl.synthetic import extract_inference
from ithrl.workflow import PreparedExperiment, prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"
_PROVIDER = "synthetic-provider"
_ENDPOINT = "https://provider.example.invalid"


class SequenceRunner:
    """Local runner that reports synthetic accounting and makes no network calls."""

    def __init__(
        self, outcomes: tuple[bool | RunnerErrorCode, ...], *, tool_name: str, command: str | None
    ) -> None:
        self.outcomes = outcomes
        self.tool_name = tool_name
        self.command = command
        self.calls: list[PreparedAttempt] = []
        self.capability_profile = CapabilityProfile.deterministic_fake().model_copy(
            update={
                "runner_type": "codex",
                "deterministic": False,
                "provider_transport": "required",
                "provider_calls_per_attempt": 1,
                "provider": _PROVIDER,
                "endpoint": _ENDPOINT,
                "protected_state_enforcement": "preventive",
            }
        )

    @property
    def fingerprint(self) -> str:
        return "credential-free-sampling-collector-v1"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        self.calls.append(attempt)
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, RunnerErrorCode):
            overload = outcome is RunnerErrorCode.PROVIDER_OVERLOADED
            raise RunnerError(
                outcome,
                "sanitized synthetic runner failure",
                provider_acceptance_state=(
                    ProviderAcceptanceState.UNKNOWN
                    if overload
                    else ProviderAcceptanceState.NOT_STARTED
                ),
                accounting=RunAccounting(
                    provider_calls_started=int(overload),
                    native_output_bytes=100 if overload else 0,
                    process_outcome="exited" if overload else "not-started",
                    cleanup_outcome="verified" if overload else "not-required",
                ),
                native_output=NativeOutputReference(
                    artifact_id="synthetic-overload",
                    sha256="a" * 64,
                    media_type="application/json",
                    size_bytes=100,
                )
                if overload
                else None,
                behavior_output_observed=not overload,
                model_requested=attempt.execution_plan.model_requested,
                provider=_PROVIDER,
                endpoint=_ENDPOINT,
                effective_policy_digest=attempt.safety_policy.digest,
            )
        tool_calls = (
            (ToolCall(name=self.tool_name, arguments={"command": self.command or ""}),)
            if outcome
            else ()
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            tool_calls=tool_calls,
            final_text="synthetic response",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider=_PROVIDER,
            endpoint=_ENDPOINT,
            usage=TokenUsage(input_tokens=10, output_tokens=2),
            accounting=RunAccounting(
                provider_calls_started=1,
                provider_calls_completed=1,
                provider_connections_started=1,
                actions_requested=len(tool_calls),
                actions_denied=len(tool_calls),
                elapsed_milliseconds=10,
            ),
        )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _prepared(
    tmp_path: Path,
    *,
    outcomes: tuple[bool | RunnerErrorCode, ...],
    required_valid: int = 1,
    max_attempts: int = 2,
    freshness_seconds: int = 600,
    eligibility_policy: Literal[
        "independent-safe-preaccept-only", "safe-provider-overload"
    ] = "independent-safe-preaccept-only",
    calibration_max_attempts: int = 1,
) -> tuple[CaseEvidence, PreparedExperiment, SequenceRunner, tuple[str, ...]]:
    extracted = extract_inference(
        FIXTURE,
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    deterministic = ExperimentDefinition.from_toml(extracted.default_experiment)
    assert isinstance(deterministic.runner, FakeRunnerConfig)
    assert deterministic.runner.tool_name is not None
    runner = SequenceRunner(
        outcomes, tool_name=deterministic.runner.tool_name, command=deterministic.runner.command
    )
    target = SamplingArmTarget(required_valid=required_valid, max_attempts=max_attempts)
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(original=target, candidate=target),
        confirmation=SampledConfirmationPhase(
            original=target, candidate=target, one_sided_confidence=0.95
        ),
        margin=0.2,
        freshness_seconds=freshness_seconds,
        eligibility_policy=eligibility_policy,
    )
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=deterministic.target,
        oracle=deterministic.oracle,
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(
            required_valid=1, minimum_matches=1, max_attempts=calibration_max_attempts
        ),
        reduction=reduction,
    )
    case = CaseEvidence(
        manifest=extracted.manifest,
        request=extracted.request,
        context=extracted.context,
        default_experiment=experiment.to_toml(),
        decision=extracted.decision,
    )
    prepared = prepare_experiment(
        case,
        registry=RunnerRegistry({"codex": lambda _config, _evidence: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider=_PROVIDER,
            allowed_endpoint=_ENDPOINT,
        ),
        resource_limits=ResourceLimits(max_provider_calls=1, max_wall_time_seconds=5),
    )
    removable = tuple(node.id for node in case.context.nodes if node.mutable and not node.protected)
    assert removable, "synthetic fixture must include removable context"
    return case, prepared, runner, removable[:1]


def _run_state(
    tmp_path: Path,
    case: CaseEvidence,
    prepared: PreparedExperiment,
    *,
    now: datetime,
    max_provider_calls: int = 20,
    max_tokens: int | None = None,
    store: ObservationStore | None = None,
    calibration_observation_ids: tuple[str, ...] = (),
    inter_attempt_delay_seconds: int = 0,
    max_wall_time_seconds: int = 120,
) -> tuple[SampledRunStateFile, ObservationStore]:
    store = store or ObservationStore(tmp_path / "private" / "observations.sqlite")
    store.initialize()
    state_file = SampledRunStateFile.create(
        case_root=tmp_path / "case",
        run_id="sampling-integration",
        store=store,
        started_at=now,
        original_identity=prepared.identity,
        original_candidate_digest=prepare_candidate(case, prepared.execution_plan).digest,
        provider_calls_per_attempt=1,
        approval=SamplingBudgetApproval(
            approval_id="initial-approval",
            approved_at=now,
            allowance=_allowance(
                max_provider_calls=max_provider_calls, max_tokens=max_tokens
            ).model_copy(
                update={
                    "inter_attempt_delay_seconds": inter_attempt_delay_seconds,
                    "max_wall_time_seconds": max_wall_time_seconds,
                }
            ),
        ),
        calibration_observation_ids=calibration_observation_ids,
    )
    return state_file, store


def _allowance(*, max_provider_calls: int, max_tokens: int | None = None):
    from ithrl.observations import SamplingBudgetAllowance

    return SamplingBudgetAllowance(
        run_id="sampling-integration",
        max_provider_calls=max_provider_calls,
        max_wall_time_seconds=120,
        max_candidate_comparisons=4,
        confirmation_provider_calls=4,
        confirmation_runner_seconds=20,
        max_tokens=max_tokens,
    )


def _plan(
    case: CaseEvidence,
    prepared: PreparedExperiment,
    removed: tuple[str, ...],
    *,
    now: datetime,
    required_valid: int,
    max_attempts: int,
    seed: int,
    role: Literal["search", "confirmation"] = "search",
    block_id: str | None = None,
) -> SamplingBlockPlan:
    baseline = prepare_candidate(case, prepared.execution_plan)
    candidate = prepare_candidate(case, prepared.execution_plan, removed)
    target = SamplingArmTarget(required_valid=required_valid, max_attempts=max_attempts)
    phase = (
        SampledSearchPhase(original=target, candidate=target)
        if role == "search"
        else SampledConfirmationPhase(original=target, candidate=target, one_sided_confidence=0.95)
    )
    assert isinstance(prepared.experiment, SampledExperimentDefinition)
    reduction = prepared.experiment.reduction
    assert isinstance(reduction, SampledReductionPlan)
    if role == "search":
        reduction = reduction.model_copy(update={"search": phase})
    else:
        reduction = reduction.model_copy(update={"confirmation": phase})
    return SamplingBlockPlan(
        block_id=block_id or f"{role}-block",
        role=role,
        reduction=reduction,
        original_identity=prepared.identity,
        candidate_identity=prepared.identity,
        original_candidate_digest=baseline.digest,
        candidate_digest=candidate.digest,
        frozen_at=now,
        schedule_seed=seed,
        schedule=sampling_schedule(max_attempts, max_attempts, seed),
        provider_calls_per_attempt=1,
    )


async def _collect(
    case: CaseEvidence,
    prepared: PreparedExperiment,
    removed: tuple[str, ...],
    plan: SamplingBlockPlan,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    clock: MutableClock,
    **kwargs: Any,
):
    class ClockDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock().timestamp(), tz)

    # Observation completion and scheduler time must use the same clock. Keep this seam
    # in the fixture; the production store still timestamps its own immutable records.
    with patch.object(observations_module, "datetime", ClockDateTime):
        return await collect_sampling_block(
            evidence=case,
            original=prepared,
            candidate=prepared,
            removed_context_ids=removed,
            plan=plan,
            state_file=state_file,
            store=store,
            clock=clock,
            **kwargs,
        )


def test_collects_seeded_two_arm_block_and_skips_finished_arm_slots(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, False, True))
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=19)
    clock = MutableClock(now)
    case_files = {
        path: path.read_bytes()
        for path in (tmp_path / "case").rglob("*")
        if path.is_file() and ".runs" not in path.parts
    }

    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))

    assert result.complete
    assert result.stop_reason is None
    assert [call.arm for call in runner.calls] == list(plan.schedule[: len(runner.calls)])
    assert len(result.evidence.attempts) == 2
    assert result.evidence.original_counts.valid == 1
    assert result.evidence.candidate_counts.valid == 1
    assert sum(item.observation.target_observed is True for item in result.evidence.attempts) == 1
    assert sum(item.observation.target_observed is False for item in result.evidence.attempts) == 1
    assert all(item.observation.valid for item in result.evidence.attempts)
    assert all(item.observation.recorded_at >= now for item in result.evidence.attempts)
    assert result.usage.actual_provider_calls == 2
    assert result.usage.known_tokens == 24
    assert all(path.read_bytes() == contents for path, contents in case_files.items())


def test_saved_interruption_resumes_without_replaying_and_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(True, False, True, False), required_valid=2, max_attempts=2
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=2, max_attempts=2, seed=4)
    clock = MutableClock(now)
    observe_once = sampling_module.observe_once
    calls_before_interrupt = 1

    async def interrupt_after_saved_observation(**kwargs: Any):
        observation = await observe_once(**kwargs)
        if len(runner.calls) == calls_before_interrupt:
            raise asyncio.CancelledError
        return observation

    monkeypatch.setattr(sampling_module, "observe_once", interrupt_after_saved_observation)
    first = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert first.stop_reason == "user-interrupt"
    assert len(first.evidence.attempts) == 1
    with state_file.acquire_lock(store):
        assert state_file.load(store).pending is None

    monkeypatch.setattr(sampling_module, "observe_once", observe_once)
    resumed = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))

    assert resumed.complete
    assert len(resumed.evidence.attempts) == 4
    assert len(runner.calls) == 4
    assert len({attempt.attempt_id for attempt in runner.calls}) == 4
    assert [item.schedule_position for item in resumed.evidence.attempts] == [0, 1, 2, 3]
    assert resumed.evidence.point_difference is not None


def test_unknown_finite_token_allowance_refuses_before_dispatch(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, True), max_attempts=1)
    state_file, store = _run_state(tmp_path, case, prepared, now=now, max_tokens=100)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)

    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )

    assert result.stop_reason == "token-estimate-unavailable"
    assert not result.complete
    assert runner.calls == []
    assert not result.evidence.attempts


def test_unrecorded_reserved_dispatch_stays_charged_and_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, True), max_attempts=1)
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=1)

    async def lose_dispatch_record(**_kwargs: Any):
        raise RunnerExecutionError("no persisted observation", attempt_started=True)

    monkeypatch.setattr(sampling_module, "observe_once", lose_dispatch_record)
    first = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )
    second = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )

    assert first.stop_reason == second.stop_reason == "unresolved-dispatch"
    assert runner.calls == []
    with state_file.acquire_lock(store):
        assert state_file.load(store).pending is not None
    assert first.usage.pending_provider_calls == plan.provider_calls_per_attempt
    assert second.usage.pending_runner_seconds > 0


def test_safe_preaccept_failure_requires_independence_evidence_to_replace(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.STARTUP, True, True),
        max_attempts=2,
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=9)
    verified: list[str] = []

    def verify(observation: Observation) -> str:
        verified.append(observation.observation_id)
        return "a" * 64

    result = asyncio.run(
        _collect(
            case,
            prepared,
            removed,
            plan,
            state_file,
            store,
            MutableClock(now),
            verify_independence=verify,
        )
    )

    assert result.complete
    assert len(runner.calls) == 3
    assert len(verified) == 1
    excluded = next(item for item in result.evidence.attempts if not item.observation.valid)
    assert excluded.replaceable
    assert excluded.independence_evidence_digest == "a" * 64


def test_safe_preaccept_failure_without_verifier_refuses_replacement(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.STARTUP, True, True),
        max_attempts=2,
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=9)

    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )

    assert result.stop_reason == "ineligible-exclusion"
    assert not result.complete
    assert len(runner.calls) == 1
    assert result.evidence.attempts[0].independence_evidence_digest is None


@pytest.mark.parametrize("role", ["search", "confirmation"])
@pytest.mark.parametrize("seed", [3, 9])
@pytest.mark.parametrize("policy", ["independent-safe-preaccept-only", "safe-provider-overload"])
def test_overload_replacement_is_frozen_bounded_and_keeps_unknown_charge(
    tmp_path: Path,
    role: Literal["search", "confirmation"],
    seed: int,
    policy: Literal["independent-safe-preaccept-only", "safe-provider-overload"],
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.PROVIDER_OVERLOADED, True, True),
        eligibility_policy=policy,
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(
        case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=seed, role=role
    )
    clock = MutableClock(now)
    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    excluded = result.evidence.attempts[0]
    assert excluded.observation.safe_provider_overload
    assert excluded.independence_evidence_digest is None
    assert not excluded.observation.attempt_evidence.retry_safe
    if policy == "independent-safe-preaccept-only":
        assert result.stop_reason == "ineligible-exclusion"
        assert len(runner.calls) == 1
        assert not result.complete
    else:
        assert result.complete
        assert result.evidence.eligible
        assert len(runner.calls) == 3
        assert len(set(result.evidence.attempt_ids)) == 3
        assert result.evidence.original_counts.valid == result.evidence.candidate_counts.valid == 1
        assert result.usage.actual_provider_calls == 3
        assert result.usage.possible_provider_calls == 0
        assert excluded.observation.attempt_evidence.acceptance_unknown
        assert excluded.observation.attempt_evidence.provider_calls_possible == 1
        assert result.usage.charged_provider_calls == 3
        assert result.usage.unknown_token_attempts == 1
        resumed = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
        assert resumed == result
        assert len(runner.calls) == 3


def test_overload_replacements_exhaust_existing_arm_slots(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.PROVIDER_OVERLOADED,) * 4,
        eligibility_policy="safe-provider-overload",
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=3)
    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )
    assert result.stop_reason == "attempt-limit"
    assert result.evidence.eligible
    assert not result.complete
    assert len(runner.calls) == 4
    assert result.usage.charged_provider_calls == 4
    assert result.evidence.original_counts.valid == result.evidence.candidate_counts.valid == 0


@pytest.mark.parametrize("policy", ["independent-safe-preaccept-only", "safe-provider-overload"])
def test_calibration_overload_policy_is_preserved_in_snapshot(
    tmp_path: Path,
    policy: Literal["independent-safe-preaccept-only", "safe-provider-overload"],
) -> None:
    from ithrl.calibration import run_sampled_calibration, save_sampled_calibration_result
    from ithrl.case import CaseWorkspace

    case, prepared, runner, _ = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.PROVIDER_OVERLOADED, True),
        eligibility_policy=policy,
        calibration_max_attempts=2,
    )
    store = ObservationStore(tmp_path / "calibration.sqlite")
    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    if policy == "safe-provider-overload":
        assert result.outcome == "measurable"
        assert result.valid_samples == result.matches == 1
        assert len(runner.calls) == 2
    else:
        assert result.stop_reason == "unsafe-retry"
        assert len(runner.calls) == 1
    snapshot = save_sampled_calibration_result(
        workspace=CaseWorkspace(tmp_path / "case"),
        evidence=case,
        prepared=prepared,
        store=store,
        result=result,
    )
    assert snapshot.summary.runner_errors == 1
    assert snapshot.summary.provider_calls_possible == 1
    assert run_sampled_calibration(evidence=case, prepared=prepared, store=store) == result


def test_completed_search_starts_fresh_confirmation_then_refuses_a_second_block(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, False, True, False))
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    search = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=3)
    searched = asyncio.run(
        _collect(case, prepared, removed, search, state_file, store, MutableClock(now))
    )
    assert searched.complete
    search_attempt_ids = set(searched.evidence.attempt_ids)
    with state_file.acquire_lock(store):
        state_file.archive_block(store)

    confirmation = _plan(
        case,
        prepared,
        removed,
        now=datetime.now(UTC),
        required_valid=1,
        max_attempts=2,
        seed=3,
        role="confirmation",
    )
    confirmed = asyncio.run(
        _collect(
            case,
            prepared,
            removed,
            confirmation,
            state_file,
            store,
            MutableClock(confirmation.frozen_at),
        )
    )
    assert confirmed.complete
    assert not search_attempt_ids.intersection(confirmed.evidence.attempt_ids)
    assert all(
        item.observation.role is ObservationRole.CONFIRMATION
        for item in confirmed.evidence.attempts
    )

    with state_file.acquire_lock(store):
        state_file.archive_block(store)
    later = _plan(
        case,
        prepared,
        removed,
        now=datetime.now(UTC),
        required_valid=1,
        max_attempts=2,
        seed=4,
        role="confirmation",
        block_id="confirmation-block-2",
    )
    refused = asyncio.run(
        _collect(case, prepared, removed, later, state_file, store, MutableClock(later.frozen_at))
    )
    assert refused.stop_reason == "confirmation-already-started"
    assert len(runner.calls) == 4


def test_search_budget_preserves_confirmation_reserve_before_dispatch(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, True), max_attempts=1)
    state_file, store = _run_state(tmp_path, case, prepared, now=now, max_provider_calls=5)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)

    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )

    assert result.stop_reason == "provider-call-budget"
    assert runner.calls == []
    assert not result.evidence.attempts


def test_clock_overrun_stops_before_next_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(True, False, True, False), required_valid=2, max_attempts=2
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=2, max_attempts=2, seed=4)

    clock = MutableClock(now)
    observe_once_impl = sampling_module.observe_once

    async def advance_after_persisted_observation(**kwargs: Any):
        observation = await observe_once_impl(**kwargs)
        clock.advance(121)
        return observation

    monkeypatch.setattr(sampling_module, "observe_once", advance_after_persisted_observation)
    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))

    assert result.stop_reason == "wall-time-exhausted"
    assert len(runner.calls) == 1
    assert len(result.evidence.attempts) == 1


def test_pacing_waits_before_dispatch_and_counts_elapsed_wall_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(True,) * 4, required_valid=2, max_attempts=2
    )
    state_file, store = _run_state(
        tmp_path,
        case,
        prepared,
        now=now,
        inter_attempt_delay_seconds=15,
        max_wall_time_seconds=300,
    )
    plan = _plan(case, prepared, removed, now=now, required_valid=2, max_attempts=2, seed=4)
    clock = MutableClock(now)
    pauses: list[float] = []

    async def wait(seconds: float) -> None:
        # No reservation is published for an attempt which is still waiting.
        assert state_file.load(store).pending is None
        pauses.append(seconds)
        clock.advance(int(seconds))

    monkeypatch.setattr(sampling_module.asyncio, "sleep", wait)
    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert result.complete
    assert len(runner.calls) == 4
    assert pauses == [15, 15, 15]
    assert result.usage.wall_time_seconds == 45


def test_interrupted_pause_resumes_after_elapsed_delay_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True,) * 2, max_attempts=1)
    state_file, store = _run_state(
        tmp_path,
        case,
        prepared,
        now=now,
        inter_attempt_delay_seconds=15,
        max_wall_time_seconds=300,
    )
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)
    clock = MutableClock(now)

    async def interrupt(_seconds: float) -> None:
        assert state_file.load(store).pending is None
        raise asyncio.CancelledError

    monkeypatch.setattr(sampling_module.asyncio, "sleep", interrupt)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert len(runner.calls) == 1
    clock.advance(15)
    # The same interruption hook would fail if resume slept again unnecessarily.
    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert result.complete
    assert len(runner.calls) == 2


def test_wait_overrun_rechecks_budget_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True,) * 2, max_attempts=1)
    state_file, store = _run_state(
        tmp_path,
        case,
        prepared,
        now=now,
        inter_attempt_delay_seconds=15,
    )
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)
    clock = MutableClock(now)

    async def overrun(_seconds: float) -> None:
        clock.advance(121)

    monkeypatch.setattr(sampling_module.asyncio, "sleep", overrun)
    result = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert result.stop_reason == "wall-time-budget"
    assert len(runner.calls) == 1
    with state_file.acquire_lock(store):
        assert state_file.load(store).pending is None


def test_frozen_candidate_mismatch_refuses_before_dispatch(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, True))
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=1)

    with pytest.raises(ValueError, match="sampling inputs do not match"):
        asyncio.run(_collect(case, prepared, (), plan, state_file, store, MutableClock(now)))

    assert runner.calls == []


def test_calibration_evidence_is_accounted_but_not_reused_as_sampling_attempt(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    case, prepared, _runner, removed = _prepared(
        tmp_path, outcomes=(True, False, True), max_attempts=1
    )
    store = ObservationStore(tmp_path / "private" / "observations.sqlite")
    store.initialize()
    full_context = prepare_candidate(case, prepared.execution_plan)
    calibration_attempt = PreparedAttempt(
        evidence=case,
        candidate=full_context,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )
    calibration = asyncio.run(
        observe_once(
            runner=prepared.runner,
            identity=prepared.identity,
            store=store,
            attempt=calibration_attempt,
        )
    )
    state_file, store = _run_state(
        tmp_path,
        case,
        prepared,
        now=now,
        store=store,
        calibration_observation_ids=(calibration.observation_id,),
    )
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)

    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )

    assert result.complete
    assert len(result.evidence.attempts) == 2
    assert all(
        item.observation.role is not ObservationRole.CALIBRATION
        for item in result.evidence.attempts
    )
    assert calibration.observation_id not in result.evidence.observation_ids
    assert result.usage.actual_provider_calls == 3


def test_archived_partial_cannot_redraw_search_but_permits_fresh_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(False, True, True, True, True), required_valid=2, max_attempts=2
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=2, max_attempts=2, seed=4)
    clock = MutableClock(now)
    original_observe = sampling_module.observe_once

    async def interrupt_after_saved(**kwargs: Any) -> None:
        await original_observe(**kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(sampling_module, "observe_once", interrupt_after_saved)
    partial = asyncio.run(_collect(case, prepared, removed, plan, state_file, store, clock))
    assert partial.evidence.eligible
    assert not partial.complete
    assert partial.evidence.point_difference is None
    assert partial.evidence.attempts[0].observation.target_observed is False
    with state_file.acquire_lock(store):
        state_file.archive_block(store)
    monkeypatch.setattr(sampling_module, "observe_once", original_observe)
    redraw = _plan(
        case,
        prepared,
        removed,
        now=now,
        required_valid=2,
        max_attempts=2,
        seed=5,
        block_id="redraw-search",
    )
    refused = asyncio.run(_collect(case, prepared, removed, redraw, state_file, store, clock))
    assert refused.stop_reason == "unfinished-prior-block"
    assert len(runner.calls) == 1

    confirmation = _plan(
        case,
        prepared,
        removed,
        now=now,
        required_valid=2,
        max_attempts=2,
        seed=5,
        role="confirmation",
    )
    confirmed = asyncio.run(
        _collect(case, prepared, removed, confirmation, state_file, store, clock)
    )
    assert confirmed.complete
    assert confirmed.evidence.original_counts.matches == 2
    assert confirmed.evidence.candidate_counts.matches == 2
    assert not set(partial.evidence.observation_ids).intersection(
        confirmed.evidence.observation_ids
    )
    assert len(runner.calls) == 5


def test_exclusive_freshness_capacity_boundary_refuses_before_dispatch(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(True, True), max_attempts=1, freshness_seconds=10
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=1, seed=2)
    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )
    assert result.stop_reason == "freshness-budget"
    assert not result.evidence.attempts
    assert runner.calls == []


def test_independent_safe_failures_stop_at_frozen_attempt_ceiling(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(
        tmp_path, outcomes=(RunnerErrorCode.STARTUP,) * 4, max_attempts=2
    )
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=2)

    def verify(observation: Observation) -> str:
        assert observation.attempt_evidence.retry_safe
        return "a" * 64

    result = asyncio.run(
        _collect(
            case,
            prepared,
            removed,
            plan,
            state_file,
            store,
            MutableClock(now),
            verify_independence=verify,
        )
    )
    assert result.stop_reason == "attempt-limit"
    assert not result.complete
    assert len(runner.calls) == len(plan.schedule)
    assert result.evidence.original_counts.runner_errors == 2
    assert result.evidence.candidate_counts.runner_errors == 2
    assert result.evidence.original_counts.valid == 0
    assert result.evidence.candidate_counts.valid == 0
    assert result.evidence.point_difference is None
    assert result.usage.charged_provider_calls == 0


def test_unknown_acceptance_stays_charged_and_refuses_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    case, prepared, runner, removed = _prepared(tmp_path, outcomes=(True, True))
    state_file, store = _run_state(tmp_path, case, prepared, now=now)
    plan = _plan(case, prepared, removed, now=now, required_valid=1, max_attempts=2, seed=2)

    async def unknown_acceptance(attempt: PreparedAttempt) -> RunResult:
        runner.calls.append(attempt)
        raise RunnerError(
            RunnerErrorCode.TRANSPORT,
            "synthetic transport interruption",
            provider_acceptance_state=ProviderAcceptanceState.UNKNOWN,
        )

    monkeypatch.setattr(runner, "run", unknown_acceptance)
    result = asyncio.run(
        _collect(case, prepared, removed, plan, state_file, store, MutableClock(now))
    )
    assert result.stop_reason == "ineligible-exclusion"
    assert len(runner.calls) == 1
    assert result.usage.charged_provider_calls == plan.provider_calls_per_attempt
    assert result.usage.unknown_provider_attempts == 1
    assert result.evidence.point_difference is None
