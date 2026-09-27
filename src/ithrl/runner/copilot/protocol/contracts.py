"""Data contracts and limits for GitHub Copilot CLI JSONL output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ithrl.runner.contracts import TokenUsage, ToolCall

type ExpectedOutcome = Literal["final-response", "shell-action"]

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_EVENTS = 256
MAX_OUTPUT_LINE_BYTES = 1024 * 1024
MAX_EVENT_DEPTH = 32
MAX_EVENT_ITEMS = 8192
MAX_ID_LENGTH = 512
MAX_MODEL_LENGTH = 256

SUPPORTED_EVENTS = frozenset(
    {
        "assistant.idle",
        "assistant.message",
        "assistant.turn_end",
        "assistant.turn_start",
        "assistant.usage",
        "model.call_finished",
        "model.call_start",
        "result",
        "session.background_tasks_changed",
        "session.info",
        "session.mcp_servers_loaded",
        "session.skills_loaded",
        "session.tools_updated",
        "tool.execution_complete",
        "tool.execution_start",
        "user.message",
    }
)
STARTUP_EVENTS = frozenset(
    {
        "session.info",
        "session.mcp_servers_loaded",
        "session.skills_loaded",
        "session.tools_updated",
    }
)


class CopilotProtocolError(ValueError):
    """A content-safe refusal to interpret Copilot CLI output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CopilotProtocolOutcome:
    """Normalized facts proved by one complete Copilot CLI event stream."""

    type: ExpectedOutcome
    model_returned: str
    provider_calls_started: int
    provider_calls_completed: int
    final_text: str | None = field(default=None, repr=False)
    tool_call: ToolCall | None = field(default=None, repr=False)
    usage: TokenUsage | None = None
    actions_requested: int = 0
    actions_denied: int = 0
    actions_executed: int = 0
