"""Fixed safety policy for GitHub Copilot CLI runner targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type CopilotTarget = Literal["final-response", "shell-action"]

RUNNER_VERSION = "1"
CAPTURE_TYPE = "logical-session"
CONTEXT_OPERATION = "dependency-closed-removal"
MATERIALIZATION = "source-backed"
RECONSTRUCTION_METHOD = "copilot-semantic-session-import-v1"
SOURCE_SCHEMA = "copilot-events-jsonl"
PROTOCOL_VERSION = "copilot-jsonl-v1"
PROVIDER = "github-copilot"
ENDPOINT = "copilot://managed"
POLICY_VERSION = "copilot-controlled-session-v1"
DISCOVERY_POLICY_VERSION = "copilot-isolated-discovery-v1"

# Copilot treats --available-tools as an allowlist. A name which cannot identify a
# built-in or configured tool therefore gives the final-response profile an empty
# tool surface. Capability detection verifies the allowlist option before this
# profile is advertised.
NO_TOOLS_SENTINEL = "ithrl-no-tools"

_COMMON_FLAGS = (
    "--no-auto-update",
    "--no-custom-instructions",
    "--disable-builtin-mcps",
    "--no-remote",
    "--no-remote-export",
    "--no-experimental",
    "--no-ask-user",
    "--no-bash-env",
    "--dynamic-retrieval=skills=off",
    "--output-format=json",
    "--stream=off",
    "--log-level=none",
)


@dataclass(frozen=True, slots=True)
class CopilotTargetPolicy:
    """Target-specific command flags and advertised action guarantees."""

    target: CopilotTarget
    command_flags: tuple[str, ...]
    action_visibility: Literal["not-applicable", "visible"]
    action_denial: Literal["enforced"]


def target_policy(target: CopilotTarget) -> CopilotTargetPolicy:
    """Return the fixed policy for one supported Copilot behavior target."""

    if target == "final-response":
        return CopilotTargetPolicy(
            target=target,
            command_flags=(
                *_COMMON_FLAGS,
                f"--available-tools={NO_TOOLS_SENTINEL}",
                "--allow-all-tools",
                "--disallow-temp-dir",
            ),
            action_visibility="not-applicable",
            action_denial="enforced",
        )
    if target == "shell-action":
        return CopilotTargetPolicy(
            target=target,
            command_flags=(
                *_COMMON_FLAGS,
                "--available-tools=bash",
                "--allow-all-tools",
                "--deny-tool=shell",
                "--disallow-temp-dir",
            ),
            action_visibility="visible",
            action_denial="enforced",
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
    "NO_TOOLS_SENTINEL",
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
