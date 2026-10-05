"""Credential-free sampled reduction through the real search, store and run state."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import ithrl.sampling as sampling_module
from ithrl.behavior import ShellActionTarget
from ithrl.case import (
    CaptureFacts,
    CaseEvidence,
    CaseManifest,
    CaseWorkspace,
    ProvenanceFacts,
    ReplayFacts,
)
from ithrl.context import ContextGraph, ContextNode, JsonValue, content_digest
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
    StructuralOracleConfig,
)
from ithrl.measurement import observe_once
from ithrl.observations import Observation, ObservationStore, SamplingBudgetAllowance
from ithrl.reduce import (
    ReductionError,
    ResumeMismatchError,
    SampledCandidateTrial,
    SampledInvalidCandidateTrial,
    SampledReducer,
    SampledReductionCheckpoint,
    SampledSearchResult,
)
from ithrl.results import (
    ResultSnapshotError,
    load_sampling_snapshot,
    validate_sampled_result,
    write_sampling_snapshot,
)
from ithrl.run_state import SampledRunStateFile, SamplingBudgetApproval
from ithrl.runner import (
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
from ithrl.workflow import (
    PreparedExperiment,
    SampledPhaseOutcome,
    prepare_experiment,
    run_sampled_phases,
)

_PROVIDER = "synthetic-provider"
_ENDPOINT = "https://provider.example.invalid"
type Behavior = Callable[[frozenset[str], int], int | RunnerErrorCode]


class ScreenRunner:
    """Emit repeatable arm counts with synthetic usage; never invoke a native harness."""

    def __init__(self, behavior: Behavior, samples: int) -> None:
        self.behavior = behavior
        self.samples = samples
        self.calls: list[PreparedAttempt] = []
        self.counts: Counter[tuple[str, str, str]] = Counter()
        self.confirmation_matches: int | RunnerErrorCode | None = None
        self.capability_profile = CapabilityProfile.deterministic_fake().model_copy(
            update={
                "runner_type": "codex",
                "deterministic": False,
                "provider_transport": "required",
                "provider_calls_per_attempt": 1,
                "provider": _PROVIDER,
                "endpoint": _ENDPOINT,
            }
        )

    @property
    def fingerprint(self) -> str:
        return "credential-free-sampled-reduction"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        self.calls.append(attempt)
        key = (attempt.phase, attempt.arm, attempt.candidate.digest)
        index = self.counts[key]
        self.counts[key] += 1
        removed = frozenset(attempt.candidate.removed_context_ids)
        matches = self.samples if not removed else self.behavior(removed, index // self.samples)
        if (
            attempt.phase == "confirmation"
            and attempt.arm == "candidate"
            and self.confirmation_matches is not None
        ):
            matches = self.confirmation_matches
        if isinstance(matches, RunnerErrorCode):
            raise RunnerError(
                matches,
                "synthetic runner failure",
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                accounting=RunAccounting(),
                model_requested=attempt.execution_plan.model_requested,
                provider=_PROVIDER,
                endpoint=_ENDPOINT,
                effective_policy_digest=attempt.safety_policy.digest,
            )
        tools = (
            (ToolCall(name="exec_command", arguments={}),) if index % self.samples < matches else ()
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider=_PROVIDER,
            endpoint=_ENDPOINT,
            tool_calls=tools,
            usage=TokenUsage(input_tokens=10, output_tokens=2),
            accounting=RunAccounting(
                provider_calls_started=1,
                provider_calls_completed=1,
                provider_connections_started=1,
                elapsed_milliseconds=10,
                actions_requested=len(tools),
                actions_denied=len(tools),
            ),
        )


@dataclass
class SearchFixture:
    evidence: CaseEvidence
    prepared: PreparedExperiment
    runner: ScreenRunner
    state_file: SampledRunStateFile
    store: ObservationStore

    def reducer(self, **kwargs: Any) -> SampledReducer:
        return SampledReducer(
            prepared=self.prepared,
            state_file=self.state_file,
            observation_store=self.store,
            **kwargs,
        )

    def run(self, **kwargs: Any) -> SampledSearchResult:
        return asyncio.run(self.reducer(**kwargs).reduce(self.evidence))

    def extend(self, maximum: int) -> None:
        with self.state_file.acquire_lock(self.store):
            state = self.state_file.load(self.store)
            self.state_file.extend_budget(
                self.store,
                SamplingBudgetApproval(
                    approval_id="extension",
                    approved_at=datetime.now(UTC),
                    allowance=state.allowance.model_copy(
                        update={"max_candidate_comparisons": maximum}
                    ),
                ),
            )


def _fixture(
    tmp_path: Path,
    *,
    markers: str = "ABCDE",
    behavior: Behavior | None = None,
    samples: int = 1,
    comparisons: int = 100,
    calls: int = 10_000,
    dependencies: dict[str, tuple[str, ...]] | None = None,
    protected: frozenset[str] = frozenset(),
    calibration: bool = True,
    run_id: str = "sampled-search",
) -> SearchFixture:
    runner = ScreenRunner(behavior or (lambda removed, _: int("ctx-a" not in removed)), samples)
    items: list[JsonValue] = [{"role": "user", "content": marker} for marker in markers]
    arm = SamplingArmTarget(required_valid=samples, max_attempts=samples)
    experiment = SampledExperimentDefinition(
        target=ShellActionTarget(tool_name_equals="exec_command"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
        reduction=SampledReductionPlan(
            search=SampledSearchPhase(original=arm, candidate=arm),
            confirmation=SampledConfirmationPhase(
                original=arm, candidate=arm, one_sided_confidence=0.95
            ),
            margin=0.2,
            freshness_seconds=600,
        ),
    )
    evidence = CaseEvidence(
        manifest=CaseManifest(
            adapter="synthetic",
            adapter_version="1",
            captured_at=datetime.now(UTC).isoformat(),
            provider=_PROVIDER,
            endpoint=_ENDPOINT,
            model_requested="synthetic-model",
            capture=CaptureFacts(request="provider-request", completeness="complete"),
            replay=ReplayFacts(state="self-contained", environment="local"),
            provenance=ProvenanceFacts(
                source="request.json", inference_call_id="sampling-1", trace_schema="synthetic-v1"
            ),
        ),
        request={"model": "synthetic-model", "input": items},
        context=ContextGraph(
            nodes=tuple(
                ContextNode(
                    id=f"ctx-{marker.lower()}",
                    kind="message",
                    path=f"/input/{index}",
                    source="request.json",
                    digest=content_digest(item),
                    mutable=True,
                    protected=marker in protected,
                    depends_on=(dependencies or {}).get(marker, ()),
                )
                for index, (marker, item) in enumerate(zip(markers, items, strict=True))
            )
        ),
        default_experiment=experiment.to_toml(),
    )
    prepared = prepare_experiment(
        evidence,
        registry=RunnerRegistry({"codex": lambda _config, _evidence: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed", allowed_provider=_PROVIDER, allowed_endpoint=_ENDPOINT
        ),
        resource_limits=ResourceLimits(max_provider_calls=1, max_wall_time_seconds=5),
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    CaseWorkspace.create(
        tmp_path / "case",
        manifest=evidence.manifest,
        request=evidence.request,
        nodes=evidence.context.nodes,
        default_experiment=evidence.default_experiment,
    )
    now = datetime.now(UTC)
    baseline = prepare_candidate(evidence, prepared.execution_plan)
    references: tuple[str, ...] = ()
    if calibration:
        observed = asyncio.run(
            observe_once(
                runner=runner,
                identity=prepared.identity,
                store=store,
                attempt=PreparedAttempt(
                    phase="calibration",
                    arm="baseline",
                    evidence=evidence,
                    candidate=baseline,
                    target=experiment.target,
                    execution_plan=prepared.execution_plan,
                    safety_policy=prepared.safety_policy,
                    resource_limits=prepared.resource_limits,
                ),
            )
        )
        references = (observed.observation_id,)
    state_file = SampledRunStateFile.create(
        case_root=tmp_path / "case",
        run_id=run_id,
        store=store,
        started_at=now,
        original_identity=prepared.identity,
        original_candidate_digest=baseline.digest,
        provider_calls_per_attempt=1,
        calibration_observation_ids=references,
        approval=SamplingBudgetApproval(
            approval_id="initial",
            approved_at=now,
            allowance=SamplingBudgetAllowance(
                run_id=run_id,
                max_provider_calls=calls,
                max_wall_time_seconds=10_000,
                max_candidate_comparisons=comparisons,
                confirmation_provider_calls=2 * samples,
                confirmation_runner_seconds=10 * samples,
            ),
        ),
    )
    return SearchFixture(evidence, prepared, runner, state_file, store)


def test_dependency_closed_search_and_exact_completed_resume(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, dependencies={"C": ("ctx-b",)})
    before = fixture.evidence.digest
    source_files = {
        path: path.read_bytes()
        for path in (tmp_path / "case").rglob("*")
        if path.is_file() and ".runs" not in path.parts
    }
    result = fixture.run()
    checkpoint = result.checkpoint
    assert result.status == "search-reduced"
    assert checkpoint.removed_context_ids == ("ctx-b", "ctx-c", "ctx-d", "ctx-e")
    assert checkpoint.expected_final_neighbors == (("ctx-a",),)
    assert any(
        "ctx-b" in trial.requested_removals and "ctx-c" in trial.effective_removals
        for trial in checkpoint.trials
    )
    assert result.usage.actual_provider_calls == len(fixture.runner.calls)
    assert fixture.evidence.digest == before
    assert all(path.read_bytes() == contents for path, contents in source_files.items())
    assert (
        SampledReductionCheckpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint
    )
    call_count = len(fixture.runner.calls)
    assert fixture.run().checkpoint == checkpoint
    assert len(fixture.runner.calls) == call_count


def test_locally_invalid_neighbors_have_no_sampling_block(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path, markers="AB", dependencies={"B": ("ctx-a",)}, protected=frozenset("B")
    )
    result = fixture.run()
    assert result.status == "search-reduced"
    assert result.checkpoint.removed_context_ids == ()
    assert result.checkpoint.expected_final_neighbors == (("ctx-a",),)
    assert all(
        isinstance(trial, SampledInvalidCandidateTrial) for trial in result.checkpoint.trials
    )
    assert result.checkpoint.blocks == ()
    assert len(fixture.runner.calls) == 1  # Calibration only.
    assert fixture.run().checkpoint == result.checkpoint


def test_cumulative_degradation_is_compared_with_original(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path, markers="ABCD", samples=10, behavior=lambda removed, _: 10 - len(removed)
    )
    result = fixture.run()
    assert result.status == "search-reduced"
    assert len(result.checkpoint.removed_context_ids) == 2
    for trial in result.checkpoint.trials:
        assert isinstance(trial, SampledCandidateTrial)
        assert trial.comparison.evidence.original_counts.matches == 10
        if len(trial.effective_removals) == 3:
            assert trial.comparison.action == "retain"
    baseline_calls = [call for call in fixture.runner.calls if call.arm == "baseline"]
    assert all(call.candidate.removed_context_ids == () for call in baseline_calls)
    references = [
        item.observation.observation_id
        for block in result.checkpoint.blocks
        for item in block.attempts
    ]
    assert len(references) == len(set(references))


def test_nonmonotonic_alternative_reproducers(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        markers="ABCDE",
        behavior=lambda removed, _: int(
            not {"ctx-a", "ctx-c"} & removed or not {"ctx-d", "ctx-e"} & removed
        ),
    )
    result = fixture.run()
    retained = {node.id for node in fixture.evidence.context.nodes} - set(
        result.checkpoint.removed_context_ids
    )
    assert result.status == "search-reduced"
    assert retained in ({"ctx-a", "ctx-c"}, {"ctx-d", "ctx-e"})


def test_final_sweep_restarts_after_admitted_singleton(tmp_path: Path) -> None:
    def behavior(removed: frozenset[str], block: int) -> int:
        return int(
            (removed == frozenset({"ctx-c"}) and block >= 1)
            or removed == frozenset({"ctx-a", "ctx-c"})
        )

    fixture = _fixture(tmp_path, markers="ABC", behavior=behavior)
    result = fixture.run()
    assert result.status == "search-reduced"
    assert result.checkpoint.removed_context_ids == ("ctx-a", "ctx-c")
    final = [trial for trial in result.checkpoint.trials if trial.phase == "final-sweep"]
    admitted = [trial for trial in final if trial.accepts_removal]
    assert [trial.requested_removals for trial in admitted] == [("ctx-c",), ("ctx-a",)]
    assert result.checkpoint.expected_final_neighbors == (("ctx-b",),)
    witness = final[-1]
    assert witness.base_removals == ("ctx-a", "ctx-c")
    assert result.checkpoint.final_sweep_trial_keys == (witness.trial_key,)


def test_comparison_limit_preserves_selection_and_resumes_without_replay(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, comparisons=1)
    stopped = fixture.run()
    assert stopped.status == "search-incomplete"
    assert stopped.stop_reason == "candidate-limit"
    assert len(stopped.checkpoint.blocks) == 1
    first = stopped.checkpoint.blocks[0]
    calls = len(fixture.runner.calls)
    asyncio.run(fixture.reducer().validate_resume(fixture.evidence))
    assert len(fixture.runner.calls) == calls
    fixture.extend(100)
    completed = fixture.run()
    assert completed.status == "search-reduced"
    assert completed.checkpoint.blocks[0] == first
    assert completed.usage.actual_provider_calls == len(fixture.runner.calls)


def test_confirmation_capacity_prevents_partial_block_admission(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, calls=4)
    stopped = fixture.run()
    assert stopped.status == "search-incomplete"
    assert stopped.stop_reason == "provider-call-budget"
    assert stopped.checkpoint.trials == ()
    assert stopped.checkpoint.active_block is None
    assert len(fixture.runner.calls) == 1


@pytest.mark.parametrize("after", [1, 2])
def test_interruption_resumes_exact_partial_or_completed_unclassified_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: int
) -> None:
    fixture = _fixture(tmp_path)
    original = sampling_module.observe_once
    count = 0

    async def interrupted(**kwargs: Any):
        nonlocal count
        observation = await original(**kwargs)
        count += 1
        if count == after:
            raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", interrupted)
        stopped = fixture.run()
    assert stopped.status == "search-incomplete"
    assert stopped.stop_reason == "user-interrupt"
    with fixture.state_file.acquire_lock(fixture.store):
        block = fixture.state_file.evidence(fixture.store)[0]
        assert len(block.attempts) == after
        assert fixture.state_file.load(fixture.store).pending is None
    completed = fixture.run()
    assert completed.status == "search-reduced"
    assert completed.checkpoint.blocks[0].plan == block.plan
    assert completed.checkpoint.blocks[0].attempts[:after] == block.attempts
    identities = [attempt.attempt_id for attempt in fixture.runner.calls]
    assert len(identities) == len(set(identities))


def test_unrecorded_dispatch_remains_charged_and_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)

    async def unrecorded(**_kwargs: Any):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", unrecorded)
        stopped = fixture.run()
    assert stopped.usage.pending_provider_calls == 1
    resumed = fixture.run()
    assert resumed.stop_reason == "unresolved-dispatch"
    assert resumed.usage.pending_provider_calls == 1
    assert len(fixture.runner.calls) == 1


def test_errors_stop_search_and_never_count_as_behavioral_loss(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, behavior=lambda _removed, _block: RunnerErrorCode.STARTUP)
    stopped = fixture.run()
    assert stopped.status == "search-incomplete"
    assert stopped.checkpoint.removed_context_ids == ()
    assert stopped.checkpoint.active_block is not None
    assert not stopped.checkpoint.active_block.eligible
    assert stopped.checkpoint.active_block.candidate_counts.no_matches == 0
    calls = len(fixture.runner.calls)
    assert fixture.run().status == "search-incomplete"
    assert len(fixture.runner.calls) == calls


def test_search_requires_successful_calibration_before_any_sampling(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, calibration=False)
    with pytest.raises(ReductionError, match="successful safe full-context calibration"):
        fixture.run()
    assert fixture.runner.calls == []


@pytest.mark.parametrize("tamper", ["neighbor", "omit", "order"])
def test_independent_resume_validation_rejects_forged_generator_evidence(
    tmp_path: Path, tamper: str
) -> None:
    fixture = _fixture(tmp_path, behavior=lambda _removed, _block: 0)
    completed = fixture.run().checkpoint
    if tamper == "neighbor":
        forged = completed.model_copy(
            update={"expected_final_neighbors": (), "final_sweep_trial_keys": ()}
        )
    elif tamper == "omit":
        forged = completed.model_copy(
            update={
                "trials": completed.trials[1:],
                "final_sweep_complete": False,
                "expected_final_neighbors": (),
                "final_sweep_trial_keys": (),
            }
        )
    else:
        forged = completed.model_copy(
            update={"trials": (completed.trials[1], completed.trials[0], *completed.trials[2:])}
        )
    forged = SampledReductionCheckpoint.model_validate_json(forged.model_dump_json())
    with fixture.state_file.acquire_lock(fixture.store):
        fixture.state_file.save_checkpoint(fixture.store, forged)
    calls = len(fixture.runner.calls)
    with pytest.raises(ResumeMismatchError):
        asyncio.run(fixture.reducer().validate_resume(fixture.evidence))
    assert len(fixture.runner.calls) == calls


def test_wall_time_stop_also_bounds_invalid_only_search(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path, markers="AB", dependencies={"B": ("ctx-a",)}, protected=frozenset("B")
    )
    stopped = fixture.run(clock=lambda: datetime.now(UTC) + timedelta(seconds=20_000))
    assert stopped.stop_reason == "wall-time-exhausted"
    assert stopped.status == "search-incomplete"
    assert stopped.checkpoint.trials == ()
    assert len(fixture.runner.calls) == 1


def test_prepared_target_mismatch_refuses_even_invalid_only_search(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path, markers="AB", dependencies={"B": ("ctx-a",)}, protected=frozenset("B")
    )
    fixture.prepared = replace(
        fixture.prepared,
        experiment=fixture.prepared.experiment.model_copy(
            update={"target": ShellActionTarget(tool_name_equals="different")}
        ),
    )
    with pytest.raises(ResumeMismatchError):
        fixture.run()


def test_invalid_trial_variant_is_strict_and_cannot_carry_observations(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path, markers="AB", dependencies={"B": ("ctx-a",)}, protected=frozenset("B")
    )
    trial = fixture.run().checkpoint.trials[0]
    assert isinstance(trial, SampledInvalidCandidateTrial)
    values = trial.model_dump()
    values["comparison"] = None
    with pytest.raises(ValidationError):
        SampledInvalidCandidateTrial.model_validate(values)


def test_mixed_invalid_and_sampled_neighbors_have_exact_terminal_coverage(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        markers="ABC",
        behavior=lambda _removed, _block: 1,
        dependencies={"C": ("ctx-a",)},
        protected=frozenset("C"),
    )
    result = fixture.run()
    assert result.status == "search-reduced"
    assert result.checkpoint.removed_context_ids == ("ctx-b",)
    assert result.checkpoint.expected_final_neighbors == (("ctx-a",),)
    assert any(
        isinstance(trial, SampledInvalidCandidateTrial) for trial in result.checkpoint.trials
    )
    assert any(isinstance(trial, SampledCandidateTrial) for trial in result.checkpoint.trials)
    assert fixture.run().checkpoint == result.checkpoint


def _workflow(fixture: SearchFixture, **kwargs: Any) -> SampledPhaseOutcome:
    return asyncio.run(
        run_sampled_phases(
            evidence=fixture.evidence,
            prepared=fixture.prepared,
            store=fixture.store,
            state_file=fixture.state_file,
            **kwargs,
        )
    )


def _validate_result(fixture: SearchFixture, outcome: SampledPhaseOutcome) -> None:
    asyncio.run(
        validate_sampled_result(
            result=outcome.result,
            evidence=fixture.evidence,
            prepared=fixture.prepared,
            store=fixture.store,
            state_file=fixture.state_file,
        )
    )


@pytest.mark.parametrize(
    ("samples", "matches", "expected"),
    [(12, 12, "confirmed-preserving"), (8, 0, "loses"), (1, 1, "inconclusive")],
)
def test_fresh_confirmation_reports_actual_outcome_without_reselection(
    tmp_path: Path,
    samples: int,
    matches: int,
    expected: str,
) -> None:
    fixture = _fixture(tmp_path, markers="AB", samples=samples)
    fixture.runner.confirmation_matches = matches
    originals = {
        p: p.read_bytes()
        for p in (tmp_path / "case").rglob("*")
        if p.is_file() and ".runs" not in p.parts
    }
    outcome = _workflow(fixture)
    assert outcome.result.search_result == "search-reduced"
    assert outcome.result.confirmation_result == expected
    confirmation = outcome.result.confirmation
    assert confirmation is not None
    assert confirmation.evidence.original_counts.valid == samples
    assert confirmation.evidence.candidate_counts.matches == matches
    assert confirmation.evidence.candidate_counts.valid == samples
    assert confirmation.evidence.plan.candidate_digest == outcome.result.selected_candidate_digest
    prior_ids = set(outcome.result.calibration_observation_ids)
    prior_ids.update(i for b in outcome.result.checkpoint.blocks for i in b.observation_ids)
    assert not prior_ids.intersection(confirmation.evidence.observation_ids)
    prior_attempts = set(outcome.result.calibration_attempt_ids)
    prior_attempts.update(i for b in outcome.result.checkpoint.blocks for i in b.attempt_ids)
    assert not prior_attempts.intersection(confirmation.evidence.attempt_ids)
    assert outcome.usage.actual_provider_calls == len(fixture.runner.calls)
    _validate_result(fixture, outcome)
    calls = len(fixture.runner.calls)
    assert _workflow(fixture).result == outcome.result
    assert len(fixture.runner.calls) == calls
    with pytest.raises(ResumeMismatchError, match="confirmation"):
        fixture.run()
    assert all(p.read_bytes() == content for p, content in originals.items())
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
    snapshot = write_sampling_snapshot(
        destination=tmp_path / "result", state=state, store=fixture.store
    )
    assert load_sampling_snapshot(snapshot, fixture.store) == state


def test_search_selected_false_positive_is_unconfirmed_by_fresh_holdout(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, markers="AB", samples=8, behavior=lambda _removed, _block: 8)
    fixture.runner.confirmation_matches = 0
    outcome = _workflow(fixture)
    assert outcome.result.checkpoint.removed_context_ids == ("ctx-a", "ctx-b")
    assert outcome.result.search_result == "search-reduced"
    assert outcome.result.confirmation_result == "loses"
    assert outcome.result.confirmation is not None
    assert outcome.result.confirmation.evidence.candidate_counts.no_matches == 8
    _validate_result(fixture, outcome)


@pytest.mark.parametrize("kind", ["comparison", "calls", "safe-errors"])
def test_safe_incomplete_search_can_use_confirmation_capacity(tmp_path: Path, kind: str) -> None:
    kwargs: dict[str, Any] = {"samples": 12, "comparisons": 3}
    verifier = None
    if kind == "calls":
        kwargs = {"samples": 1, "calls": 4}
    if kind == "safe-errors":
        kwargs = {"samples": 12, "behavior": lambda _removed, _block: RunnerErrorCode.STARTUP}
        verifier = _independent_failure
    fixture = _fixture(tmp_path, **kwargs)
    fixture.runner.confirmation_matches = fixture.runner.samples
    outcome = _workflow(fixture, verify_independence=verifier)
    assert outcome.result.search_result == "search-incomplete"
    assert outcome.result.confirmation_result in {"confirmed-preserving", "inconclusive"}
    assert not outcome.result.checkpoint.final_sweep_complete
    _validate_result(fixture, outcome)
    calls = len(fixture.runner.calls)
    assert _workflow(fixture, verify_independence=verifier).result == outcome.result
    assert len(fixture.runner.calls) == calls


def test_ineligible_search_forbids_confirmation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, behavior=lambda _removed, _block: RunnerErrorCode.PROTOCOL)
    outcome = _workflow(fixture)
    assert outcome.result.search_result == "search-incomplete"
    assert outcome.result.confirmation_result == "not-run"
    assert outcome.stop_reason == "ineligible-prior-block"
    assert not any(a.phase == "confirmation" for a in fixture.runner.calls)
    with fixture.state_file.acquire_lock(fixture.store):
        assert fixture.state_file.load(fixture.store).selection is None
    _validate_result(fixture, outcome)


@pytest.mark.parametrize(("independent", "expected"), [(False, "refused"), (True, "incomplete")])
def test_confirmation_errors_are_operational_and_never_behavioral_losses(
    tmp_path: Path,
    independent: bool,
    expected: str,
) -> None:
    fixture = _fixture(tmp_path, markers="AB", samples=1)
    fixture.runner.confirmation_matches = RunnerErrorCode.STARTUP
    verifier = _independent_failure if independent else None
    outcome = _workflow(fixture, verify_independence=verifier)
    assert outcome.result.confirmation_result == expected
    block = outcome.result.confirmation
    assert block is not None
    assert block.evidence.candidate_counts.runner_errors == 1
    assert block.evidence.candidate_counts.valid == 0
    assert block.evidence.candidate_counts.no_matches == 0
    _validate_result(fixture, outcome)
    calls = len(fixture.runner.calls)
    assert _workflow(fixture, verify_independence=verifier).result == outcome.result
    assert len(fixture.runner.calls) == calls


@pytest.mark.parametrize("after", [1, 2])
def test_confirmation_interruption_resumes_same_holdout_without_search_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after: int,
) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    original = sampling_module.observe_once
    count = 0

    async def interrupted(**kwargs: Any):
        nonlocal count
        observation = await original(**kwargs)
        if kwargs["attempt"].phase == "confirmation":
            count += 1
            if count == after:
                raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", interrupted)
        stopped = _workflow(fixture)
    assert stopped.stop_reason == "user-interrupt"
    block = stopped.result.confirmation
    assert block is not None
    assert len(block.evidence.attempts) == after
    calls = len(fixture.runner.calls)
    resumed = _workflow(fixture)
    assert resumed.result.confirmation is not None
    assert resumed.result.confirmation.evidence.plan == block.evidence.plan
    assert resumed.result.confirmation.evidence.attempts[:after] == block.evidence.attempts
    assert len(fixture.runner.calls) == calls + (2 - after)
    assert resumed.result.checkpoint == stopped.result.checkpoint
    assert len({a.attempt_id for a in fixture.runner.calls}) == len(fixture.runner.calls)
    _validate_result(fixture, resumed)


def test_selection_is_durable_before_first_confirmation_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    original = sampling_module._collect_sampling_block_owned

    async def stopped(**kwargs: Any):
        if kwargs["plan"].role == "confirmation":
            raise asyncio.CancelledError
        return await original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "_collect_sampling_block_owned", stopped)
        with pytest.raises(asyncio.CancelledError):
            _workflow(fixture)
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
        assert state.selection is not None
        assert not any(b.plan.role == "confirmation" for b in state.blocks)
    calls = len(fixture.runner.calls)
    outcome = _workflow(fixture)
    assert len(fixture.runner.calls) == calls + 2
    assert outcome.result.confirmation is not None
    assert outcome.result.confirmation.evidence.plan == state.selection.plan
    _validate_result(fixture, outcome)


def test_confirmation_unresolved_dispatch_is_charged_and_never_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    original = sampling_module.observe_once

    async def missing(**kwargs: Any):
        if kwargs["attempt"].phase == "confirmation":
            raise asyncio.CancelledError
        return await original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", missing)
        stopped = _workflow(fixture)
    assert stopped.stop_reason == "unresolved-dispatch"
    assert stopped.usage.pending_provider_calls == 1
    calls = len(fixture.runner.calls)
    resumed = _workflow(fixture)
    assert resumed.stop_reason == "unresolved-dispatch"
    assert resumed.usage.pending_provider_calls == 1
    assert resumed.result == stopped.result
    assert len(fixture.runner.calls) == calls
    _validate_result(fixture, resumed)


def test_result_validation_rejects_omitted_confirmation_and_calibration_membership(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    outcome = _workflow(fixture)
    changes_to_test: tuple[dict[str, Any], ...] = (
        {"confirmation": None},
        {"calibration_observation_ids": ()},
        {"calibration_attempt_ids": ()},
    )
    for changes in changes_to_test:
        forged = replace(outcome, result=outcome.result.model_copy(update=changes))
        with pytest.raises(ResultSnapshotError, match="authoritative"):
            _validate_result(fixture, forged)
    calls = len(fixture.runner.calls)
    assert _workflow(fixture).result == outcome.result
    assert len(fixture.runner.calls) == calls


def _independent_failure(_observation: Observation) -> str:
    return "a" * 64


def test_full_confirmation_block_must_fit_after_calibration_consumption(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, samples=12, calls=24)
    outcome = _workflow(fixture)
    assert outcome.result.search_result == "search-incomplete"
    assert outcome.result.confirmation_result == "incomplete"
    assert outcome.stop_reason == "provider-call-budget"
    assert len(fixture.runner.calls) == 1
    assert outcome.result.confirmation is not None
    assert outcome.result.confirmation.evidence.attempts == ()
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
        assert state.selection is not None
        assert state.blocks == ()
    _validate_result(fixture, outcome)


def test_expired_run_forbids_starting_confirmation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, comparisons=1)
    fixture.run()
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
    calls = len(fixture.runner.calls)
    expired = state.started_at + timedelta(seconds=state.allowance.max_wall_time_seconds + 1)
    outcome = _workflow(fixture, clock=lambda: expired)
    assert outcome.result.confirmation_result == "not-run"
    assert outcome.stop_reason == "wall-time-exhausted"
    assert len(fixture.runner.calls) == calls
    _validate_result(fixture, outcome)


def test_stale_interrupted_confirmation_refuses_more_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    original = sampling_module.observe_once

    async def interrupted(**kwargs: Any):
        observation = await original(**kwargs)
        if kwargs["attempt"].phase == "confirmation":
            raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", interrupted)
        stopped = _workflow(fixture)
    assert stopped.result.confirmation is not None
    expired = stopped.result.confirmation.evidence.plan.expires_at
    calls = len(fixture.runner.calls)
    outcome = _workflow(fixture, clock=lambda: expired)
    assert outcome.stop_reason == "freshness-expired"
    assert outcome.result.confirmation_result == "incomplete"
    assert outcome.result.confirmation == stopped.result.confirmation
    assert len(fixture.runner.calls) == calls
    _validate_result(fixture, outcome)


def test_search_missing_dispatch_forbids_holdout_and_stays_charged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)

    async def missing(**_kwargs: Any):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", missing)
        outcome = _workflow(fixture)
    assert outcome.stop_reason == "unresolved-dispatch"
    assert outcome.result.confirmation_result == "not-run"
    assert outcome.usage.pending_provider_calls == 1
    calls = len(fixture.runner.calls)
    assert _workflow(fixture).stop_reason == "unresolved-dispatch"
    assert len(fixture.runner.calls) == calls


def test_selection_cannot_be_changed_or_followed_by_a_different_holdout(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, markers="AB")
    outcome = _workflow(fixture)
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
        assert state.selection is not None
        with pytest.raises(ValueError, match="cannot be repeated"):
            fixture.state_file.select_candidate(fixture.store, state.selection)
        assert state.checkpoint is not None
        from ithrl.run_state import SampledRunState

        payload = state.model_dump()
        payload["selection"]["checkpoint_digest"] = "a" * 64
        with pytest.raises(ValidationError, match="checkpoint cannot change"):
            SampledRunState.model_validate(payload)
        payload = state.model_dump()
        payload["selection"]["plan"]["candidate_digest"] = "a" * 64
        with pytest.raises(ValidationError, match="selected candidate"):
            SampledRunState.model_validate(payload)
        payload = state.model_dump()
        payload["selection"]["search_stop_reason"] = "user-interrupt"
        with pytest.raises(ValidationError, match="search_stop_reason"):
            SampledRunState.model_validate(payload)
        payload = state.model_dump()
        payload["selection"]["plan"]["block_id"] = "redrawn-holdout"
        with pytest.raises(ValidationError, match="single frozen run holdout"):
            SampledRunState.model_validate(payload)
    _validate_result(fixture, outcome)


def test_confirmation_supports_maximum_length_run_id(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, markers="AB", run_id="r" * 128)
    outcome = _workflow(fixture)
    assert outcome.result.confirmation is not None
    plan = outcome.result.confirmation.evidence.plan
    with fixture.state_file.acquire_lock(fixture.store):
        state = fixture.state_file.load(fixture.store)
        digest = content_digest({"run_id": state.run_id, "role": "confirmation"})
        assert plan.block_id == f"confirmation-{digest}"
        assert len(plan.block_id) == 77
        assert plan.schedule_seed == int(digest[:16], 16)
    _validate_result(fixture, outcome)
    calls = len(fixture.runner.calls)
    assert _workflow(fixture).result == outcome.result
    assert len(fixture.runner.calls) == calls


def test_safe_failure_exhaustion_stays_incomplete_and_cannot_redraw(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, behavior=lambda _removed, _block: RunnerErrorCode.STARTUP)

    def independent(_observation: Observation) -> str:
        return "a" * 64

    result = fixture.run(verify_independence=independent)
    assert result.status == "search-incomplete"
    assert result.stop_reason == "attempt-limit"
    assert result.checkpoint.active_block is not None
    assert result.checkpoint.active_block.eligible
    assert result.checkpoint.active_block.candidate_counts.valid == 0
    calls = len(fixture.runner.calls)
    resumed = fixture.run(verify_independence=independent)
    assert resumed.stop_reason == "attempt-limit"
    assert len(fixture.runner.calls) == calls


def test_expired_partial_block_is_refused_without_redraw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    original = sampling_module.observe_once

    async def interrupted(**kwargs: Any):
        await original(**kwargs)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampling_module, "observe_once", interrupted)
        first = fixture.run()
    block = first.checkpoint.active_block
    assert block is not None
    calls = len(fixture.runner.calls)
    refused = fixture.run(clock=lambda: block.plan.expires_at)
    assert refused.stop_reason == "freshness-expired"
    assert refused.checkpoint.active_block == block
    assert len(fixture.runner.calls) == calls


def test_trial_ceiling_also_bounds_graph_invalid_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.reduce.engine as engine_module

    fixture = _fixture(
        tmp_path, markers="AB", dependencies={"B": ("ctx-a",)}, protected=frozenset("B")
    )
    monkeypatch.setattr(engine_module, "MAX_CANDIDATE_RUNS", 1)
    result = fixture.run()
    assert result.status == "search-incomplete"
    assert result.stop_reason == "trial-limit"
    assert len(result.checkpoint.trials) == 1
    assert len(fixture.runner.calls) == 1


def test_interrupted_resume_never_regresses_saved_trial_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, comparisons=2)
    before = fixture.run().checkpoint
    assert len(before.trials) == 2
    save = fixture.state_file.save_checkpoint

    def interrupted(store: ObservationStore, checkpoint: SampledReductionCheckpoint):
        save(store, checkpoint)
        raise OSError("synthetic interruption after publication")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.state_file, "save_checkpoint", interrupted)
        with pytest.raises(OSError, match="synthetic interruption"):
            fixture.run()
    with fixture.state_file.acquire_lock(fixture.store):
        assert fixture.state_file.load(fixture.store).checkpoint == before
    fixture.extend(100)
    resumed = fixture.run()
    assert resumed.status == "search-reduced"
    assert resumed.checkpoint.blocks[:2] == before.blocks
    attempt_ids = [attempt.attempt_id for attempt in fixture.runner.calls]
    assert len(attempt_ids) == len(set(attempt_ids))


def test_resume_recovers_publication_before_archival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)

    def interrupted(_store: ObservationStore):
        raise OSError("synthetic interruption before archival")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.state_file, "archive_block", interrupted)
        with pytest.raises(OSError, match="synthetic interruption"):
            fixture.run()
    with fixture.state_file.acquire_lock(fixture.store):
        saved = fixture.state_file.load(fixture.store)
        assert saved.checkpoint is not None
        assert len(saved.checkpoint.trials) == 1
        assert not saved.blocks[0].archived
        original = fixture.state_file.evidence(fixture.store)[0]
    resumed = fixture.run()
    assert resumed.status == "search-reduced"
    assert resumed.checkpoint.blocks[0] == original
    with fixture.state_file.acquire_lock(fixture.store):
        assert all(progress.archived for progress in fixture.state_file.load(fixture.store).blocks)
    attempt_ids = [attempt.attempt_id for attempt in fixture.runner.calls]
    assert len(attempt_ids) == len(set(attempt_ids))
