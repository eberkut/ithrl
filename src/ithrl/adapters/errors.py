"""Stable, content-safe errors for native session parsing."""

from __future__ import annotations

from enum import StrEnum


class AdapterErrorCode(StrEnum):
    """Machine-readable reasons a native session cannot be parsed."""

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


class AdapterError(ValueError):
    """A native session cannot be read or validated safely."""

    __slots__ = ("code", "line")

    def __init__(
        self,
        code: AdapterErrorCode,
        message: str,
        *,
        line: int | None = None,
    ) -> None:
        self.code = code
        self.line = line
        location = f" at line {line}" if line is not None else ""
        super().__init__(f"{message}{location}")
