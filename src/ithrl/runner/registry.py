"""Application-level runner construction without harness-specific branches."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ithrl.case import CaseEvidence
from ithrl.execution import PreflightIssue
from ithrl.experiment import FakeRunnerConfig, RunnerConfig, UnavailableRunnerConfig
from ithrl.runner.contracts import Runner
from ithrl.runner.fake import FakeRunner

type RunnerFactory = Callable[[RunnerConfig, CaseEvidence], Runner | PreflightIssue]


@dataclass(frozen=True, slots=True)
class RunnerResolution:
    """A constructed runner or the reason no compatible runner is registered."""

    runner: Runner | None
    issue: PreflightIssue | None

    def __post_init__(self) -> None:
        if (self.runner is None) == (self.issue is None):
            raise ValueError("runner resolution requires exactly one outcome")


class RunnerRegistry:
    """Resolve experiment runner types at the application composition boundary."""

    def __init__(self, factories: Mapping[str, RunnerFactory]) -> None:
        if not factories or any(not runner_type for runner_type in factories):
            raise ValueError("runner registry requires named factories")
        self._factories = dict(factories)

    def resolve(self, config: RunnerConfig, evidence: CaseEvidence) -> RunnerResolution:
        """Construct the configured runner without starting external work."""

        if isinstance(config, UnavailableRunnerConfig):
            code = (
                "runner-unavailable"
                if config.harness == evidence.manifest.harness
                else "runner-evidence-mismatch"
            )
            message = (
                "the experiment has no compatible implemented runner"
                if code == "runner-unavailable"
                else "experiment runner does not match the captured harness"
            )
            return RunnerResolution(
                runner=None,
                issue=PreflightIssue(code=code, message=message),
            )
        factory = self._factories.get(config.type)
        if factory is None:
            return RunnerResolution(
                runner=None,
                issue=PreflightIssue(
                    code="runner-unavailable",
                    message="the experiment has no compatible implemented runner",
                ),
            )
        result = factory(config, evidence)
        if isinstance(result, PreflightIssue):
            return RunnerResolution(runner=None, issue=result)
        runner = result
        if runner.capability_profile.runner_type != config.type:
            raise ValueError("runner factory returned the wrong runner type")
        return RunnerResolution(runner=runner, issue=None)


def built_in_runner_registry() -> RunnerRegistry:
    """Return the runner factories included in the installed package."""

    return RunnerRegistry({"fake": _create_fake_runner})


def _create_fake_runner(config: RunnerConfig, _evidence: CaseEvidence) -> Runner:
    if not isinstance(config, FakeRunnerConfig):
        raise TypeError("fake runner factory requires FakeRunnerConfig")
    return FakeRunner(
        required_markers=frozenset(config.required_markers),
        alternative_marker_sets=tuple(
            frozenset(marker_set) for marker_set in config.alternative_marker_sets
        ),
        tool_name=config.tool_name,
        command=config.command,
        final_text=config.final_text,
    )
