"""Harness-independent contracts for parsed native session evidence."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ithrl.context import JsonObject
from ithrl.timestamps import validate_rfc3339_timestamp


@dataclass(frozen=True, slots=True)
class ParsingLimits:
    """Resource limits applied before native session evidence is accepted."""

    max_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 8 * 1024 * 1024
    max_records: int = 250_000
    max_json_items: int = 1_000_000

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_line_bytes, self.max_records, self.max_json_items) < 1:
            raise ValueError("parsing limits must be positive")
        if self.max_line_bytes > self.max_bytes:
            raise ValueError("max_line_bytes must not exceed max_bytes")


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SessionFormat(_ImmutableContract):
    """Versioned identity of one native persisted session format."""

    schema_version: Literal[1] = 1
    harness: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    producer_version: Annotated[str, Field(min_length=1, max_length=128)]
    record_schema_version: int | str | None = None


class SessionMetadata(_ImmutableContract):
    """Shared facts needed to identify parsed native evidence."""

    schema_version: Literal[1] = 1
    format: SessionFormat
    session_id: Annotated[str, Field(min_length=1, max_length=1024)]
    captured_at: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="captured_at")


class SourceFacts(_ImmutableContract):
    """Content identity of the exact native source bytes that were parsed."""

    schema_version: Literal[1] = 1
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=1)]


class NativeSession:
    """Parsed metadata with an unchanged native JSONL snapshot."""

    __slots__ = ("_native_bytes", "_records", "metadata", "source")

    def __init__(
        self,
        *,
        metadata: SessionMetadata,
        source: SourceFacts,
        native_bytes: bytes,
        records: tuple[JsonObject, ...],
    ) -> None:
        if not native_bytes or not records:
            raise ValueError("native session evidence must not be empty")
        if source.size_bytes != len(native_bytes):
            raise ValueError("native session byte count does not match source facts")
        if source.sha256 != hashlib.sha256(native_bytes).hexdigest():
            raise ValueError("native session digest does not match source facts")
        self.metadata = metadata
        self.source = source
        self._native_bytes = native_bytes
        self._records = copy.deepcopy(records)

    @property
    def native_bytes(self) -> bytes:
        """Return the exact bytes read from the native session file."""

        return self._native_bytes

    @property
    def records(self) -> tuple[JsonObject, ...]:
        """Return defensive copies of native records in source order."""

        return copy.deepcopy(self._records)

    @property
    def record_count(self) -> int:
        return len(self._records)
