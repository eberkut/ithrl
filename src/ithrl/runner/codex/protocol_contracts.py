"""Bounds, accepted messages, and outcomes for the Codex app-server exchange."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024
MAX_PROTOCOL_MESSAGES = 100_000
MAX_CLIENT_PROTOCOL_MESSAGES = 6
MAX_PROTOCOL_DEPTH = 128
MAX_PROTOCOL_ITEMS = 1_000_000
MAX_WORKER_REQUEST_BYTES = 64 * 1024 * 1024
SANDBOX_ROOT = "/tmp/ithrl-state"  # noqa: S108 - private Bubblewrap tmpfs
SANDBOX_WORKSPACE = f"{SANDBOX_ROOT}/workspace"

type ProtocolStatus = Literal["completed", "error"]
type ProviderAcceptance = Literal["not-started", "accepted", "unknown"]

ACTION_RESPONSES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/tool/call": {
        "contentItems": [{"type": "inputText", "text": "Tool execution is disabled."}],
        "success": False,
    },
    "mcpServer/elicitation/request": {
        "action": "decline",
        "content": None,
        "_meta": None,
    },
}
ACTION_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "commandExecution",
        "dynamicToolCall",
        "fileChange",
        "hookPrompt",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "sleep",
        "subAgentActivity",
        "webSearch",
    }
)
PASSIVE_ITEM_TYPES = frozenset(
    {
        "agentMessage",
        "contextCompaction",
        "enteredReviewMode",
        "exitedReviewMode",
        "functionCallOutput",
        "plan",
        "reasoning",
        "userMessage",
    }
)
PASSIVE_NOTIFICATIONS = {
    "item/agentMessage/delta": ("delta",),
    "item/plan/delta": ("delta",),
    "item/reasoning/summaryPartAdded": ("summaryIndex",),
    "item/reasoning/summaryTextDelta": ("delta", "summaryIndex"),
    "item/reasoning/textDelta": ("contentIndex", "delta"),
}
THREAD_PASSIVE_NOTIFICATIONS = {
    "model/verification": "verifications",
    "thread/status/changed": "status",
}
ACTION_REQUEST_METHODS = frozenset(ACTION_RESPONSES)
SUPPORTED_NOTIFICATION_METHODS = frozenset(
    {
        *PASSIVE_NOTIFICATIONS,
        *THREAD_PASSIVE_NOTIFICATIONS,
        "account/rateLimits/updated",
        "account/updated",
        "item/completed",
        "item/started",
        "model/rerouted",
        "remoteControl/status/changed",
        "thread/started",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/started",
        "warning",
    }
)


class CodexProtocolError(RuntimeError):
    """A content-safe app-server protocol failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProtocolUsage:
    """Token counts reported for the reconstructed turn."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


@dataclass(frozen=True, slots=True)
class ProtocolOutcome:
    """Content-safe control facts plus protected final response content."""

    status: ProtocolStatus
    error_code: str | None
    final_text: str | None = field(repr=False)
    model_returned: str | None
    service_tier: str | None
    usage: ProtocolUsage | None
    provider_acceptance: ProviderAcceptance
    provider_calls_started: int
    provider_calls_completed: int
    actions_requested: int
    actions_denied: int

    def __post_init__(self) -> None:
        if self.status == "completed" and (self.error_code is not None or self.final_text is None):
            raise ValueError("completed Codex outcome requires one final response")
        if self.status == "error" and (self.error_code is None or self.final_text is not None):
            raise ValueError("failed Codex outcome requires an error code and no final response")
        counts = (
            self.provider_calls_started,
            self.provider_calls_completed,
            self.actions_requested,
            self.actions_denied,
        )
        if min(counts) < 0:
            raise ValueError("Codex protocol counts cannot be negative")
        if self.provider_calls_completed > self.provider_calls_started:
            raise ValueError("completed provider calls cannot exceed started calls")
        if self.actions_denied > self.actions_requested:
            raise ValueError("denied actions cannot exceed requested actions")


def validate_protocol_json(value: object) -> None:
    """Reject values that exceed the protocol's JSON shape and resource bounds."""

    pending = [(value, 0)]
    items = 0
    while pending:
        current, depth = pending.pop()
        if depth > MAX_PROTOCOL_DEPTH:
            raise CodexProtocolError("json-depth", "Codex protocol JSON is too deeply nested")
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise CodexProtocolError("invalid-json", "Codex protocol JSON has invalid keys")
            items += len(current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            items += len(current)
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise CodexProtocolError("invalid-json", "Codex protocol JSON has invalid values")
        elif current is not None and not isinstance(current, (bool, int, str)):
            raise CodexProtocolError("invalid-json", "Codex protocol JSON has invalid values")
        if items > MAX_PROTOCOL_ITEMS:
            raise CodexProtocolError("json-items", "Codex protocol JSON has too many items")
