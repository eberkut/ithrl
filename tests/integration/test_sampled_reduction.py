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
from ithrl.workflow import PreparedExperiment, prepare_experiment

_PROVIDER = "synthetic-provider"
_ENDPOINT = "https://provider.example.invalid"
type Behavior = Callable[[frozenset[str], int], int | RunnerErrorCode]


class ScreenRunner:
    """Emit repeatable arm counts with synthetic usage; never invoke a native harness."""

    def __init__(self, behavior: Behavior, samples: int) -> None:
        self.behavior = behavior
        self.samples = samples
        self.calls: list[PreparedAttempt] = []
        self.counts: Counter[tuple[str, str]] = Counter()
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
        key = (attempt.arm, attempt.candidate.digest)
        index = self.counts[key]
        self.counts[key] += 1
        removed = frozenset(attempt.candidate.removed_context_ids)
        matches = self.samples if not removed else self.behavior(removed, index // self.samples)
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
        run_id="sampled-search",
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
                run_id="sampled-search",
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
