from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    CandidateTrial,
    DeterministicReducer,
    ReductionCheckpoint,
    ResumeMismatchError,
)
from ithrl.reduce.contracts import trial_key
from ithrl.reduce.search import _Search
from ithrl.runner import (
    FakeRunner,
    PreparedAttempt,
    RunAccounting,
    Runner,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    ToolCall,
)


@dataclass(frozen=True)
class _FrozenSearchTrial:
    trial_key: str
    effective_removals: tuple[str, ...] = ()
    accepts_removal: bool = False
    allows_search_continuation: bool = True


@pytest.mark.parametrize("continuable", [False, True])
def test_search_reuses_first_frozen_trial_without_replay_or_checkpoint(
    continuable: bool,
) -> None:
    first = _FrozenSearchTrial(
        trial_key("chunk", (), ("A",)), allows_search_continuation=continuable
    )
    conflicting = _FrozenSearchTrial(first.trial_key)
    prior = deque([conflicting])

    async def unexpected_classify(**kwargs: Any) -> _FrozenSearchTrial:
        pytest.fail("a frozen decision must not be classified again")

    def unexpected_checkpoint(*args: Any) -> None:
        pytest.fail("a frozen decision must not publish another checkpoint")

    search = _Search(
        evidence=None,  # type: ignore[arg-type]
        removable=["A"],
        classify=unexpected_classify,
        prior_trials=prior,
        publish=True,
        publish_checkpoint=unexpected_checkpoint,
        trials=[first, conflicting],
    )
    actual = asyncio.run(search._run_trial(frozenset(), ("A",), "chunk"))
    assert actual is (first if continuable else None)
    assert list(prior) == [conflicting]
    assert search.trials == [first, conflicting]


def test_search_indexes_replayed_and_new_trials_in_checkpoint_order() -> None:
    replayed = _FrozenSearchTrial(trial_key("chunk", (), ("A",)))
    prior = deque([replayed])
    classified: list[tuple[str, ...]] = []
    checkpoints: list[tuple[_FrozenSearchTrial, ...]] = []

    async def classify(**kwargs: Any) -> _FrozenSearchTrial:
        requested = tuple(kwargs["requested_removals"])
        classified.append(requested)
        if prior:
            return prior.popleft()
        return _FrozenSearchTrial(trial_key("chunk", (), requested))

    def checkpoint(removed: Any, trials: Any, complete: bool) -> None:
        checkpoints.append(tuple(trials))

    search = _Search(
        evidence=None,  # type: ignore[arg-type]
        removable=["A", "B"],
        classify=classify,
        prior_trials=prior,
        publish=True,
        publish_checkpoint=checkpoint,
        trials=[],
    )

    async def exercise() -> None:
        assert await search._run_trial(frozenset(), ("A",), "chunk") is replayed
        assert not checkpoints
        new = await search._run_trial(frozenset(), ("B",), "chunk")
        assert await search._run_trial(frozenset(), ("A",), "chunk") is replayed
        assert await search._run_trial(frozenset(), ("B",), "chunk") is new

    asyncio.run(exercise())
    assert classified == [("A",), ("B",)]
    assert not prior
    assert checkpoints == [tuple(search.trials)]


@pytest.fixture(scope="module")
def recorded_trial(tmp_path_factory: pytest.TempPathFactory) -> str:
    evidence = _evidence()
    store = ObservationStore(tmp_path_factory.mktemp("trial-contract") / "observations.sqlite")
    result = asyncio.run(_reducer(evidence, store, maximum=1).reduce(evidence))
    return result.checkpoint.trials[0].model_dump_json()


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"base_removals": ["ctx-b", "ctx-a"]}, "base_removals must contain sorted unique"),
        (
            {"requested_removals": ["ctx-a", "ctx-a"]},
            "requested_removals must not contain duplicates",
        ),
        (
            {"effective_removals": ["ctx-b", "ctx-a"]},
            "effective_removals must contain sorted unique",
        ),
        ({"base_removals": ["ctx-z"]}, "effective_removals must include base_removals"),
        ({"trial_key": "f" * 64}, "trial_key does not match"),
        ({"candidate_digest": None}, "valid trials require a candidate observation"),
        ({"target_observed": None}, "valid trials require a candidate observation"),
        ({"observation_id": None}, "valid trials require a candidate observation"),
        ({"observation_attempt": None}, "valid trials require a candidate observation"),
        ({"runner_executed": False}, "observed trials must record runner execution"),
    ],
)
def test_serialized_observed_trial_rejects_inconsistent_removals_and_evidence(
    recorded_trial: str, update: dict[str, object], message: str
) -> None:
    payload = json.loads(recorded_trial)
    payload.update(update)
    with pytest.raises(ValidationError, match=message):
        CandidateTrial.model_validate_json(json.dumps(payload))


def test_serialized_trial_verdict_must_agree_with_the_recorded_observation(
    recorded_trial: str,
) -> None:
    payload = json.loads(recorded_trial)
    payload["classification"] = "loses" if payload["target_observed"] else "preserves"
    with pytest.raises(ValidationError, match="classification must match target_observed"):
        CandidateTrial.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "update",
    [
        {"candidate_digest": "f" * 64},
        {"target_observed": False},
        {"observation_id": "invented"},
        {"observation_attempt": 1},
        {"runner_executed": True},
        {"effective_removals": ["ctx-z"]},
    ],
)
def test_serialized_invalid_trial_cannot_claim_execution_or_removal(
    recorded_trial: str, update: dict[str, object]
) -> None:
    payload = json.loads(recorded_trial)
    payload.update(
        classification="invalid",
        candidate_digest=None,
        target_observed=None,
        observation_id=None,
        observation_attempt=None,
        runner_executed=False,
        effective_removals=payload["base_removals"],
    )
    CandidateTrial.model_validate_json(json.dumps(payload))
    payload.update(update)
    with pytest.raises(ValidationError, match="invalid trials cannot"):
        CandidateTrial.model_validate_json(json.dumps(payload))


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
    runner: Runner | None = None,
    alternatives: tuple[frozenset[str], ...] = (),
    maximum: int | None = None,
    resume: ReductionCheckpoint | None = None,
    identity_changes: dict[str, Any] | None = None,
) -> DeterministicReducer:
    if runner is None:
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
    if identity_changes is not None:
        identity = ObservationIdentity.model_validate({**identity.model_dump(), **identity_changes})
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


@pytest.mark.parametrize(
    ("maximum", "identity_changes", "message"),
    [
        (0, None, "max_candidate_runs"),
        (None, {"algorithm_digest": "f" * 64}, "another reduction algorithm"),
        (None, {"runner_fingerprint": "changed"}, "does not match the execution plan"),
    ],
)
def test_reducer_refuses_invalid_budget_or_execution_identity(
    tmp_path: Path, maximum: int | None, identity_changes: dict[str, Any] | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _reducer(
            _evidence(),
            ObservationStore(tmp_path / "observations.sqlite"),
            maximum=maximum,
            identity_changes=identity_changes,
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


def test_unchanged_chunks_are_not_repeated_at_finer_granularity(tmp_path: Path) -> None:
    evidence = _evidence("BCDEF")  # Required marker A is absent, so every candidate loses.
    result = asyncio.run(
        _reducer(evidence, ObservationStore(tmp_path / "observations.sqlite")).reduce(evidence)
    )

    assert result.status == "search-1-minimal"
    assert result.removed_context_ids == ()
    assert len(result.trials) == len({trial.trial_key for trial in result.trials})
    assert result.candidate_runs_used == len(result.trials)


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
    first = _reducer(evidence, store, runner=runner, maximum=1)
    with pytest.raises(RunnerExecutionError):
        asyncio.run(first.reduce(evidence))

    checkpoint = first.checkpoint
    assert checkpoint.candidate_runs_used == 1
    assert len(checkpoint.failed_candidate_observation_ids) == 1

    no_budget = _reducer(evidence, store, runner=runner, maximum=1, resume=checkpoint)
    partial = asyncio.run(no_budget.reduce(evidence))
    assert partial.status == "search-incomplete"
    assert runner.calls == 1

    continued = _reducer(evidence, store, runner=runner, maximum=2, resume=checkpoint)
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
    first = _reducer(evidence, store, runner=runner, maximum=1)
    with pytest.raises(RunnerExecutionError, match="safety"):
        asyncio.run(first.reduce(evidence))
    checkpoint = first.checkpoint

    resumed = _reducer(evidence, store, runner=runner, maximum=2, resume=checkpoint)
    with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
        asyncio.run(resumed.reduce(evidence))

    assert resumed.checkpoint == checkpoint
    assert runner.calls == 1
    experiment_digest = _experiment(ShellActionTarget(tool_name_equals="exec_command")).digest
    assert len(store.list_for_experiment(experiment_digest)) == 1
