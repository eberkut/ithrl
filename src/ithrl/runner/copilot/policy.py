"""Fixed safety policy for GitHub Copilot CLI runner targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ithrl.execution import ContextOperation

type CopilotTarget = Literal["final-response", "shell-action"]

RUNNER_VERSION = "1"
CAPTURE_TYPE = "logical-session"
CONTEXT_OPERATION = "dependency-closed-removal"
MATERIALIZATION = "source-backed"
RECONSTRUCTION_METHOD = "copilot-native-prefix-sdk-v1"
SOURCE_SCHEMA = "copilot-events-jsonl"
PROTOCOL_VERSION = "copilot-sdk-events-v1"
PROVIDER = "github-copilot"
ENDPOINT = "copilot://managed"
POLICY_VERSION = "copilot-sdk-controlled-session-v1"
DISCOVERY_POLICY_VERSION = "copilot-isolated-discovery-v1"


@dataclass(frozen=True, slots=True)
class CopilotTargetPolicy:
    """Target-specific SDK session policy and advertised action guarantees."""

    target: CopilotTarget
    command_flags: tuple[str, ...]
    action_visibility: Literal["not-applicable", "visible"]
    action_denial: Literal["enforced"]
    context_operations: tuple[ContextOperation, ...]


def target_policy(target: CopilotTarget) -> CopilotTargetPolicy:
    """Return the fixed policy for one supported Copilot behavior target."""

    if target == "final-response":
        return CopilotTargetPolicy(
            target=target,
            command_flags=(),
            action_visibility="not-applicable",
            action_denial="enforced",
            context_operations=(CONTEXT_OPERATION, "append-text"),
        )
    if target == "shell-action":
        return CopilotTargetPolicy(
            target=target,
            command_flags=(),
            action_visibility="visible",
            action_denial="enforced",
            context_operations=(CONTEXT_OPERATION,),
        )
    raise ValueError("unsupported Copilot target")


def policy_deviation(target: CopilotTarget) -> str:
    """Describe the controlled surface without claiming historical replay."""

    if target == "final-response":
        return "the model sees no Copilot tools through an explicit availability allowlist"
    return "only Copilot's native bash tool is visible and its shell permission is denied"


__all__ = [
    "CAPTURE_TYPE",
    "CONTEXT_OPERATION",
    "DISCOVERY_POLICY_VERSION",
    "ENDPOINT",
    "MATERIALIZATION",
    "POLICY_VERSION",
    "PROTOCOL_VERSION",
    "PROVIDER",
    "RECONSTRUCTION_METHOD",
    "RUNNER_VERSION",
    "SOURCE_SCHEMA",
    "CopilotTarget",
    "CopilotTargetPolicy",
    "policy_deviation",
    "target_policy",
]
