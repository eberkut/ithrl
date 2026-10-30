"""Parse bounded GitHub Copilot CLI JSONL outcomes."""

from ithrl.runner.copilot.protocol.contracts import (
    MAX_OUTPUT_LINE_BYTES,
    CopilotProtocolError,
    CopilotProtocolOutcome,
    ExpectedOutcome,
)
from ithrl.runner.copilot.protocol.parser import parse_output

__all__ = [
    "MAX_OUTPUT_LINE_BYTES",
    "CopilotProtocolError",
    "CopilotProtocolOutcome",
    "ExpectedOutcome",
    "parse_output",
]
