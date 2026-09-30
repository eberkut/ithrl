from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ithrl.behavior import ShellActionTarget
from ithrl.case import (
    CaptureFacts,
    CaseEvidence,
    CaseManifest,
    ProvenanceFacts,
    ReplayFacts,
)
from ithrl.context import ContextGraph, ContextNode, JsonObject, JsonValue, content_digest
from ithrl.execution import (
    CapabilityProfile,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    FakeRunnerConfig,
    StructuralOracleConfig,
)
from ithrl.measurement import RunnerExecutionError
from ithrl.observations import ObservationIdentity, ObservationStore
from ithrl.reduce import (
    ALGORITHM_DIGEST,
    DeterministicReducer,
    ReductionCheckpoint,
    ResumeMismatchError,
)
from ithrl.runner import (
    FakeRunner,
    PreparedAttempt,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    ToolCall,
)


def _evidence(markers: str = "ABCDEFG") -> CaseEvidence:
    input_items: list[JsonValue] = [{"role": "user", "content": marker} for marker in markers]
    request: JsonObject = {
        "model": "fake",
        "input": input_items,
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object"},
            }
        ],
    }
    nodes = tuple(
        ContextNode(
            id=f"ctx-{marker.lower()}",
            kind="message",
            path=f"/input/{index}",
            mutable=True,
            source="request.json",
            digest=content_digest(item),
        )
        for index, (marker, item) in enumerate(zip(markers, input_items, strict=True))
    )
    manifest = CaseManifest(
        adapter="synthetic",
        adapter_version="1",
        captured_at=datetime.now(UTC).isoformat(),
        provider="fake",
        endpoint="local",
        model_requested="fake",
        model_returned="fake",
        capture=CaptureFacts(request="provider-request", completeness="complete"),
        replay=ReplayFacts(state="self-contained", environment="local"),
        provenance=ProvenanceFacts(
            source="request.json",
            inference_call_id="synthetic-1",
            trace_schema="synthetic-v1",
        ),
    )
    return CaseEvidence(
        manifest=manifest,
        request=request,
        context=ContextGraph(nodes=nodes),
        default_experiment='schema_version = 1\nkind = "deterministic"\n',
    )


def _reducer(
    evidence: CaseEvidence,
    store: ObservationStore,
    *,
    alternatives: tuple[frozenset[str], ...] = (),
    maximum: int | None = None,
    resume: ReductionCheckpoint | None = None,
) -> DeterministicReducer:
    runner = FakeRunner(
        required_markers=frozenset({"A", "C"}),
        alternative_marker_sets=alternatives,
        tool_name="exec_command",
    )
    target = ShellActionTarget(tool_name_equals="exec_command")
    experiment = _experiment(
        target,
        alternatives=alternatives,
    )
    policy = SafetyPolicy.credential_free()
    limits = ResourceLimits.credential_free()
    report = resolve_execution_plan(
        runner.capability_profile,
        experiment,
        evidence=evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint=runner.fingerprint,
        execution_plan_digest=report.plan.digest,
        target_digest=target.digest,
        algorithm_digest=ALGORITHM_DIGEST,
    )
    return DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=maximum,
        resume=resume,
    )


def _experiment(
    target: ShellActionTarget,
    *,
    alternatives: tuple[frozenset[str], ...] = (),
) -> ExperimentDefinition:
    return ExperimentDefinition(
        target=target,
        oracle=StructuralOracleConfig(),
        runner=FakeRunnerConfig(
            required_markers=("A", "C"),
            alternative_marker_sets=tuple(tuple(sorted(items)) for items in alternatives),
            tool_name="exec_command",
        ),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )


def test_reducer_finds_a_dependency_valid_one_minimal_reproducer(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")

    result = asyncio.run(_reducer(evidence, store).reduce(evidence))

    assert result.status == "search-1-minimal"
    assert result.retained_context_ids == ("ctx-a", "ctx-c")
    assert result.reduced_request is not None
    assert result.reduced_request["input"] == [
        {"role": "user", "content": "A"},
        {"role": "user", "content": "C"},
    ]
    assert result.final_sweep_complete is True
    assert result.candidate_runs_used == len(
        [trial for trial in result.trials if trial.runner_executed]
    )
    assert (
        len(
            store.list_for_experiment(
                _experiment(ShellActionTarget(tool_name_equals="exec_command")).digest
            )
        )
        == result.candidate_runs_used
    )


def test_reducer_reports_only_one_minimality_when_alternatives_exist(tmp_path: Path) -> None:
    evidence = _evidence("ABCDE")
    result = asyncio.run(
        _reducer(
            evidence,
            ObservationStore(tmp_path / "observations.sqlite"),
            alternatives=(frozenset({"D", "E"}),),
        ).reduce(evidence)
    )

    assert result.status == "search-1-minimal"
    assert set(result.retained_context_ids) in ({"ctx-a", "ctx-c"}, {"ctx-d", "ctx-e"})


def test_budget_stop_resumes_without_reexecuting_prior_candidates(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    partial = asyncio.run(_reducer(evidence, store, maximum=1).reduce(evidence))
    experiment_digest = _experiment(ShellActionTarget(tool_name_equals="exec_command")).digest
    observations_after_partial = len(store.list_for_experiment(experiment_digest))

    completed = asyncio.run(
        _reducer(evidence, store, maximum=30, resume=partial.checkpoint).reduce(evidence)
    )

    assert partial.status == "search-incomplete"
    assert completed.status == "search-1-minimal"
    assert completed.trials[0].reused is True
    assert len(store.list_for_experiment(experiment_digest)) - observations_after_partial == (
        completed.candidate_runs_used - partial.candidate_runs_used
    )


def test_reducer_refuses_a_checkpoint_with_another_case_identity(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    partial = asyncio.run(_reducer(evidence, store, maximum=1).reduce(evidence))
    changed = _evidence("ABCDEF")

    with pytest.raises(ResumeMismatchError, match="identities"):
        asyncio.run(_reducer(changed, store, maximum=30, resume=partial.checkpoint).reduce(changed))


def test_reducer_refuses_checkpoint_verdict_not_backed_by_observation(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    partial = asyncio.run(_reducer(evidence, store, maximum=1).reduce(evidence))
    original_trial = partial.checkpoint.trials[0]
    changed_trial = original_trial.model_copy(
        update={
            "target_observed": not original_trial.target_observed,
            "classification": (
                "loses" if original_trial.classification == "preserves" else "preserves"
            ),
        }
    )
    changed_checkpoint = partial.checkpoint.model_copy(update={"trials": (changed_trial,)})

    with pytest.raises(ResumeMismatchError, match="append-only observation"):
        asyncio.run(
            _reducer(evidence, store, maximum=30, resume=changed_checkpoint).reduce(evidence)
        )


def test_checkpoint_rejects_a_candidate_count_not_bound_to_trials(tmp_path: Path) -> None:
    evidence = _evidence()
    partial = asyncio.run(
        _reducer(
            evidence,
            ObservationStore(tmp_path / "observations.sqlite"),
            maximum=1,
        ).reduce(evidence)
    )
    payload = partial.checkpoint.model_dump()
    payload["candidate_runs_used"] = 2

    with pytest.raises(ValidationError, match="executed candidate trials"):
        ReductionCheckpoint.model_validate(payload)


def test_resumed_reducer_exposes_the_saved_checkpoint_before_execution(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    partial = asyncio.run(_reducer(evidence, store, maximum=1).reduce(evidence))

    resumed = _reducer(evidence, store, maximum=30, resume=partial.checkpoint)

    assert resumed.checkpoint == partial.checkpoint


def test_resume_rejects_out_of_order_trials_before_any_new_attempt(tmp_path: Path) -> None:
    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    partial = asyncio.run(_reducer(evidence, store, maximum=3).reduce(evidence))
    checkpoint = partial.checkpoint.model_copy(
        update={"trials": tuple(reversed(partial.checkpoint.trials))}
    )
    experiment_digest = _experiment(ShellActionTarget(tool_name_equals="exec_command")).digest
    observations_before = store.list_for_experiment(experiment_digest)

    with pytest.raises(ResumeMismatchError, match="deterministic search order"):
        asyncio.run(_reducer(evidence, store, maximum=30, resume=checkpoint).reduce(evidence))

    assert store.list_for_experiment(experiment_digest) == observations_before


def test_failed_candidate_attempt_counts_against_resume_budget(tmp_path: Path) -> None:
    class FailOnceRunner:
        calls = 0
        fingerprint = "fail-once-v1"

        @property
        def capability_profile(self) -> CapabilityProfile:
            return CapabilityProfile.deterministic_fake(runner_version="fail-once-v1")

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            self.calls += 1
            if self.calls == 1:
                raise RunnerError(
                    RunnerErrorCode.STARTUP,
                    "temporary failure before dispatch",
                    accounting=RunAccounting(),
                )
            return RunResult(
                attempt_id=attempt.attempt_id,
                effective_policy_digest=attempt.safety_policy.digest,
                model_requested=attempt.execution_plan.model_requested,
                model_returned=attempt.execution_plan.model_requested,
                provider="fake",
                tool_calls=(ToolCall(name="exec_command"),),
                accounting=RunAccounting(actions_requested=1, actions_denied=1),
            )

    evidence = _evidence()
    store = ObservationStore(tmp_path / "observations.sqlite")
    runner = FailOnceRunner()
    target = ShellActionTarget(tool_name_equals="exec_command")
    experiment = _experiment(target)
    policy = SafetyPolicy.credential_free()
    limits = ResourceLimits.credential_free()
    report = resolve_execution_plan(
        runner.capability_profile,
        experiment,
        evidence=evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint=runner.fingerprint,
        execution_plan_digest=report.plan.digest,
        target_digest=target.digest,
        algorithm_digest=ALGORITHM_DIGEST,
    )

    first = DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=1,
    )
    with pytest.raises(RunnerExecutionError):
        asyncio.run(first.reduce(evidence))

    checkpoint = first.checkpoint
    assert checkpoint.candidate_runs_used == 1
    assert len(checkpoint.failed_candidate_observation_ids) == 1

    no_budget = DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=1,
        resume=checkpoint,
    )
    partial = asyncio.run(no_budget.reduce(evidence))
    assert partial.status == "search-incomplete"
    assert runner.calls == 1

    continued = DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=2,
        resume=checkpoint,
    )
    resumed = asyncio.run(continued.reduce(evidence))
    assert resumed.candidate_runs_used == 2
    assert runner.calls == 2


def test_blocked_retry_does_not_change_candidate_budget_or_checkpoint(tmp_path: Path) -> None:
    class UnsafeRunner:
        calls = 0
        fingerprint = "unsafe-v1"
        capability_profile = CapabilityProfile.deterministic_fake(runner_version="unsafe-v1")

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            del attempt
            self.calls += 1
            raise RunnerError(
                RunnerErrorCode.SAFETY,
                "runner executed a forbidden action",
                accounting=RunAccounting(actions_requested=1, actions_executed=1),
            )

    evidence = _evidence()
    store = ObservationStore(tmp_path / "unsafe-observations.sqlite")
    runner = UnsafeRunner()
    target = ShellActionTarget(tool_name_equals="exec_command")
    experiment = _experiment(target)
    policy = SafetyPolicy.credential_free()
    limits = ResourceLimits.credential_free()
    report = resolve_execution_plan(
        runner.capability_profile,
        experiment,
        evidence=evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint=runner.fingerprint,
        execution_plan_digest=report.plan.digest,
        target_digest=target.digest,
        algorithm_digest=ALGORITHM_DIGEST,
    )

    first = DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=1,
    )
    with pytest.raises(RunnerExecutionError, match="safety"):
        asyncio.run(first.reduce(evidence))
    checkpoint = first.checkpoint

    resumed = DeterministicReducer(
        runner=runner,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        identity=identity,
        observation_store=store,
        max_candidate_runs=2,
        resume=checkpoint,
    )
    with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
        asyncio.run(resumed.reduce(evidence))

    assert resumed.checkpoint == checkpoint
    assert runner.calls == 1
    assert len(store.list_for_experiment(experiment.digest)) == 1
