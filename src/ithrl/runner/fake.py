"""Deterministic credential-free runner used by tests and examples."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ithrl.context import JsonObject, content_digest
from ithrl.execution import CapabilityProfile
from ithrl.runner.contracts import (
    PreparedAttempt,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    ToolCall,
)


class FakeRunner:
    """Emit configured output when all required context markers are present."""

    def __init__(
        self,
        *,
        required_markers: frozenset[str],
        tool_name: str | None = None,
        command: str | None = None,
        final_text: str | None = None,
        alternative_marker_sets: tuple[frozenset[str], ...] = (),
    ) -> None:
        if not required_markers:
            raise ValueError("required_markers must not be empty")
        if any(not marker_set for marker_set in alternative_marker_sets):
            raise ValueError("alternative_marker_sets cannot contain an empty set")
        if tool_name == "":
            raise ValueError("tool_name must not be empty")
        if (tool_name is None) == (final_text is None):
            raise ValueError("fake runner must emit exactly one behavior family")
        if command is not None and tool_name is None:
            raise ValueError("command requires tool_name")
        self.required_markers = required_markers
        self.alternative_marker_sets = alternative_marker_sets
        self.tool_name = tool_name
        self.command = command
        self.final_text = final_text

    @property
    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile.deterministic_fake(runner_version="3")

    @property
    def fingerprint(self) -> str:
        payload = {
            "runner": "fake",
            "version": "3",
            "profile": self.capability_profile.digest,
            "required_markers": sorted(self.required_markers),
            "alternative_marker_sets": [
                sorted(marker_set) for marker_set in self.alternative_marker_sets
            ],
            "tool_name": self.tool_name,
            "command": self.command,
            "final_text": self.final_text,
        }
        return f"fake-v3:{content_digest(payload)}"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        if attempt.candidate.materialization != "exact-request":
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "fake runner requires an exact provider request",
            )
        request = attempt.evidence.materialize(attempt.candidate.removed_context_ids)
        strings = set(_collect_strings(request.get("input", [])))
        marker_sets = (self.required_markers, *self.alternative_marker_sets)
        matched = any(required <= strings for required in marker_sets)
        arguments: JsonObject = {"command": self.command} if self.command is not None else {}
        tool_calls = (
            (ToolCall(name=self.tool_name, arguments=arguments),)
            if matched and self.tool_name is not None
            else ()
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            tool_calls=tool_calls,
            final_text=self.final_text if matched else "Target behavior not emitted.",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider="fake",
            accounting=RunAccounting(
                actions_requested=len(tool_calls),
                actions_denied=len(tool_calls),
            ),
        )


def _collect_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _collect_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _collect_strings(nested)
