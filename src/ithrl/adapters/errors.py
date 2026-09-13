"""Stable native-session errors that do not expose captured content."""

from __future__ import annotations

from enum import StrEnum


class AdapterErrorCode(StrEnum):
    """Machine-readable reasons native session evidence cannot be used."""

    SOURCE_UNAVAILABLE = "source-unavailable"
    UNSAFE_SOURCE = "unsafe-source"
    SOURCE_TOO_LARGE = "source-too-large"
    LINE_TOO_LARGE = "line-too-large"
    TOO_MANY_RECORDS = "too-many-records"
    INVALID_UTF8 = "invalid-utf8"
    INVALID_JSONL = "invalid-jsonl"
    DUPLICATE_KEY = "duplicate-key"
    JSON_LIMIT_EXCEEDED = "json-limit-exceeded"
    SOURCE_CHANGED = "source-changed"
    UNSUPPORTED_FORMAT = "unsupported-format"
    AMBIGUOUS_FORMAT = "ambiguous-format"
    UNSUPPORTED_VERSION = "unsupported-version"
    INVALID_SESSION = "invalid-session"
    CONTEXT_TOO_LARGE = "context-too-large"
    DECISION_NOT_FOUND = "decision-not-found"
    TARGET_NOT_FOUND = "target-not-found"
    TOO_MANY_DECISIONS = "too-many-decisions"
    DISCOVERY_LIMIT_EXCEEDED = "discovery-limit-exceeded"
    SESSION_NOT_FOUND = "session-not-found"
    AMBIGUOUS_SESSION = "ambiguous-session"
    NO_SESSIONS = "no-sessions"


class AdapterError(ValueError):
    """Native session evidence cannot be read, validated, or selected safely."""

    __slots__ = ("bytes_read", "code", "line")

    def __init__(
        self,
        code: AdapterErrorCode,
        message: str,
        *,
        line: int | None = None,
        bytes_read: int = 0,
    ) -> None:
        self.code = code
        self.line = line
        self.bytes_read = bytes_read
        location = f" at line {line}" if line is not None else ""
        super().__init__(f"{message}{location}")
