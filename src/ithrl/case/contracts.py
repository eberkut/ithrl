"""Immutable case metadata and errors."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ithrl.timestamps import validate_rfc3339_timestamp


class CaseError(ValueError):
    """Base error for case workspace operations."""


class CaseExistsError(CaseError):
    """A case cannot be created because its target already exists."""


class UnsafeCasePathError(CaseError):
    """A case path or workspace entry is unsafe."""


class CaseFormatError(CaseError):
    """Persisted case evidence is malformed or internally inconsistent."""


class CaptureFacts(BaseModel):
    """Classification and completeness of captured evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    request: Literal[
        "provider-request",
        "logical-session",
        "reconstructed",
        "transcript",
        "unknown",
    ]
    completeness: Annotated[str, Field(min_length=1)]


class ReplayFacts(BaseModel):
    """State and environment requirements known at capture time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    state: Annotated[str, Field(min_length=1)]
    environment: Annotated[str, Field(min_length=1)]


class ProvenanceFacts(BaseModel):
    """Source identity without copied credentials or transport headers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source: Annotated[str, Field(min_length=1)]
    inference_call_id: Annotated[str | None, Field(min_length=1)] = None
    trace_schema: Annotated[str, Field(min_length=1)]


class CaseManifest(BaseModel):
    """Versioned immutable facts about an extracted decision case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    adapter: Annotated[str, Field(min_length=1)]
    adapter_version: Annotated[str, Field(min_length=1)]
    captured_at: Annotated[str, Field(min_length=1)]
    harness: Annotated[str | None, Field(min_length=1)] = None
    harness_version: Annotated[str | None, Field(min_length=1)] = None
    source_schema: Annotated[str | None, Field(min_length=1)] = None
    provider: Annotated[str | None, Field(min_length=1)] = None
    endpoint: Annotated[str | None, Field(min_length=1)] = None
    model_requested: Annotated[str | None, Field(min_length=1)] = None
    model_returned: Annotated[str | None, Field(min_length=1)] = None
    capture: CaptureFacts
    replay: ReplayFacts
    provenance: ProvenanceFacts

    @field_validator("captured_at")
    @classmethod
    def require_timestamp_offset(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="captured_at")
