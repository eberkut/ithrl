"""Runner contracts and deterministic test runner."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ithrl.context import content_digest


class ToolCall(BaseModel):
    """A custom tool call returned by the model but not executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Structured output from one runner attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_calls: tuple[ToolCall, ...] = ()
    final_text: str | None = None
    model_requested: str | None = None
    model_returned: str | None = None
    provider: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Runner(Protocol):
    """Execute one prepared request without continuing the tool loop."""

    @property
    def fingerprint(self) -> str: ...

    async def run(self, request: Mapping[str, Any]) -> RunResult: ...


class FakeRunner:
    """Deterministic runner whose target depends on planted input markers."""

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
        self._marker_sets = (required_markers, *alternative_marker_sets)
        self._tool_name = tool_name
        self._command = command
        self._final_text = final_text

    @property
    def fingerprint(self) -> str:
        return "fake-v2:" + content_digest(
            {
                "marker_sets": [sorted(marker_set) for marker_set in self._marker_sets],
                "tool_name": self._tool_name,
                "command": self._command,
                "final_text": self._final_text,
            }
        )

    async def run(self, request: Mapping[str, Any]) -> RunResult:
        markers = _collect_strings(request.get("input", []))
        matched = any(marker_set <= markers for marker_set in self._marker_sets)
        model = request.get("model")
        model_name = model if isinstance(model, str) else None
        if matched and self._tool_name is not None:
            arguments: dict[str, Any] = {}
            if self._command is not None:
                arguments["command"] = self._command
            return RunResult(
                tool_calls=(ToolCall(name=self._tool_name, arguments=arguments),),
                model_requested=model_name,
                model_returned=model_name,
                provider="fake",
            )
        if matched:
            return RunResult(
                final_text=self._final_text,
                model_requested=model_name,
                model_returned=model_name,
                provider="fake",
            )
        return RunResult(
            final_text="Target behavior not emitted.",
            model_requested=model_name,
            model_returned=model_name,
            provider="fake",
        )


def _collect_strings(value: object) -> set[str]:
    strings: set[str] = set()
    if isinstance(value, str):
        strings.add(value)
    elif isinstance(value, Mapping):
        for child in value.values():
            strings.update(_collect_strings(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            strings.update(_collect_strings(child))
    return strings
