"""Narrow passive evidence projections; these are not execution/session contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ithrl.adapters.contracts import SourceFacts
from ithrl.context import JsonValue


@dataclass(frozen=True, slots=True)
class Anchor:
    line: int
    digest: str


@dataclass(frozen=True, slots=True)
class Boundary:
    id: str
    start: Anchor
    end: Anchor


@dataclass(frozen=True, slots=True)
class CapturedResult:
    scope: str
    value: JsonValue = field(repr=False)
    completeness: Literal["captured", "truncated", "unknown"] = "captured"


@dataclass(frozen=True, slots=True)
class Operation:
    call: Anchor
    completion: Anchor | None
    tool: str = field(repr=False)
    arguments: JsonValue = field(repr=False)
    results: tuple[CapturedResult, ...] = ()
    status: Literal["completed", "failed", "incomplete"] = "completed"
    category: Literal["file-read", "search", "unclassified"] = "unclassified"
    workspace_digest: str | None = None


@dataclass(frozen=True, slots=True)
class PassiveProjection:
    source: SourceFacts
    harness: Literal["codex", "copilot"]
    producer_version: str = field(repr=False)
    record_count: int
    boundaries: tuple[Boundary, ...]
    operations: tuple[Operation, ...]
    unknown_records: int = 0
    limitations: tuple[str, ...] = ()
    barriers: tuple[Anchor, ...] = ()
