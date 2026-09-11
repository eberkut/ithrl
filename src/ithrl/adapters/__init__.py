"""Read and validate native coding-agent session evidence."""

from ithrl.adapters.contracts import (
    NativeSession,
    ParsingLimits,
    SessionFormat,
    SessionMetadata,
    SourceFacts,
)
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.adapters.registry import parse_session

__all__ = [
    "AdapterError",
    "AdapterErrorCode",
    "NativeSession",
    "ParsingLimits",
    "SessionFormat",
    "SessionMetadata",
    "SourceFacts",
    "parse_session",
]
