"""GitHub Copilot execution support."""

from ithrl.runner.copilot.capability import DetectedCopilotCapability, detect_copilot_capability
from ithrl.runner.copilot.configuration import (
    CopilotConfiguration,
    resolve_copilot_configuration,
)
from ithrl.runner.copilot.isolation import CopilotRuntime
from ithrl.runner.copilot.runner import CopilotRunner

__all__ = [
    "CopilotConfiguration",
    "CopilotRunner",
    "CopilotRuntime",
    "DetectedCopilotCapability",
    "detect_copilot_capability",
    "resolve_copilot_configuration",
]
