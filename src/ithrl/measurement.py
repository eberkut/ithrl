"""Run one candidate and record its structural observation."""

from __future__ import annotations

from ithrl.behavior import (
    BehaviorObservation,
    BehaviorTarget,
    ShellAction,
    evaluate_target,
)
from ithrl.context import JsonObject, content_digest
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.runner import Runner, RunResult


class RunnerExecutionError(RuntimeError):
    """A runner failed before producing a structural observation."""

    def __init__(self, message: str, observation: Observation | None = None) -> None:
        super().__init__(message)
        self.observation = observation


async def observe_once(
    *,
    runner: Runner,
    target: BehaviorTarget,
    identity: ObservationIdentity,
    store: ObservationStore,
    request: JsonObject,
    role: ObservationRole,
    arm: ObservationArm,
) -> Observation:
    """Run once, evaluate the result, and save the observation."""

    candidate_digest = content_digest(request)
    try:
        result = await runner.run(request)
        observed = evaluate_target(target, normalize_run_result(result))
    except Exception as error:
        failed_observation = store.record_attempt(
            identity=identity,
            candidate_digest=candidate_digest,
            role=role,
            arm=arm,
            outcome=ObservationOutcome.RUNNER_ERROR,
            error_category=type(error).__name__,
        )
        raise RunnerExecutionError(
            f"runner failed with {type(error).__name__}", failed_observation
        ) from error

    return store.record_attempt(
        identity=identity,
        candidate_digest=candidate_digest,
        role=role,
        arm=arm,
        outcome=(ObservationOutcome.MATCH if observed else ObservationOutcome.NO_MATCH),
    )


def normalize_run_result(result: RunResult) -> BehaviorObservation:
    """Convert runner output to the standard behavior format."""

    actions: list[ShellAction] = []
    for call in result.tool_calls:
        command = call.arguments.get("command", call.arguments.get("cmd", ""))
        actions.append(
            ShellAction(
                tool_name=call.name,
                command=command if isinstance(command, str) else "",
            )
        )
    return BehaviorObservation(final_response=result.final_text, shell_actions=tuple(actions))
