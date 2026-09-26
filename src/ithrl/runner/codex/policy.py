"""Fixed capability and safety facts for the Codex final-response runner."""

from __future__ import annotations

DISABLED_FEATURES = frozenset(
    {
        "apps",
        "browser_use",
        "code_mode_host",
        "computer_use",
        "goals",
        "hooks",
        "image_generation",
        "memories",
        "multi_agent",
        "multi_agent_v2",
        "plugins",
        "remote_plugin",
        "request_permissions_tool",
        "shell_tool",
        "skill_mcp_dependency_install",
        "skill_search",
        "sleep_tool",
        "tool_suggest",
        "unbounded_connection_retries",
        "view_image",
        "workspace_dependencies",
    }
)
# Codex may force its unified-exec backend on. Disabling ``shell_tool`` prevents that
# backend from being registered as a model tool, which is the safety-relevant fact.

POLICY_VERSION = "codex-final-response-no-actions-v1"
RUNNER_VERSION = "1"
CAPTURE_TYPE = "logical-session"
CONTEXT_OPERATION = "dependency-closed-removal"
MATERIALIZATION = "source-backed"
RECONSTRUCTION_METHOD = "codex-app-server-inject-items-v1"
SOURCE_SCHEMA = "codex-rollout-jsonl"
TARGET_TYPE = "final-response"


def configuration_deviation(public_digest: str) -> str:
    """Return the capability fact that binds one resolved provider configuration."""

    return f"provider configuration {public_digest}"


def runtime_deviation(runtime_identity: str) -> str:
    """Return the capability fact that binds the probed local executables."""

    return f"runtime executables {runtime_identity}"


__all__ = [
    "CAPTURE_TYPE",
    "CONTEXT_OPERATION",
    "DISABLED_FEATURES",
    "MATERIALIZATION",
    "POLICY_VERSION",
    "RECONSTRUCTION_METHOD",
    "RUNNER_VERSION",
    "SOURCE_SCHEMA",
    "TARGET_TYPE",
    "configuration_deviation",
    "runtime_deviation",
]
