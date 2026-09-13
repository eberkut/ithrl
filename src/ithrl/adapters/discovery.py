"""Bounded contracts and filesystem traversal for native session discovery."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.adapters.contracts import SourceFacts
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.behavior import BehaviorType
from ithrl.decisions import DecisionLimitation
from ithrl.path_safety import UnsafePathError, validate_directory_chain
from ithrl.timestamps import validate_rfc3339_timestamp

type HarnessName = Literal["codex", "copilot"]


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    """Resource limits for one read-only default-store scan."""

    max_entries: int = 50_000
    max_candidates: int = 4_096
    max_parse_bytes: int = 512 * 1024 * 1024
    max_sessions: int = 10

    def __post_init__(self) -> None:
        if (
            min(
                self.max_entries,
                self.max_candidates,
                self.max_parse_bytes,
                self.max_sessions,
            )
            < 1
        ):
            raise ValueError("discovery limits must be positive")
        if self.max_candidates > self.max_entries:
            raise ValueError("max_candidates must not exceed max_entries")


@dataclass(frozen=True, slots=True)
class SessionSource:
    """One candidate native session file found under a known store."""

    path: Path
    modified_at_ns: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SourceScan:
    """A bounded set of matching source files from one native store."""

    sources: tuple[SessionSource, ...]
    truncated: bool


class SessionSummary(BaseModel):
    """Native session facts that can be shown without captured content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    harness: HarnessName
    session_id: Annotated[str, Field(min_length=1, max_length=1024)]
    producer_version: Annotated[str, Field(min_length=1, max_length=128)]
    captured_at: Annotated[str, Field(min_length=1, max_length=64)]
    decision_count: Annotated[int, Field(ge=1, le=4096)]
    target_types: Annotated[tuple[BehaviorType, ...], Field(min_length=1, max_length=2)]
    limitations: tuple[DecisionLimitation, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="captured_at")

    @model_validator(mode="after")
    def validate_unique_values(self) -> SessionSummary:
        if len(set(self.target_types)) != len(self.target_types):
            raise ValueError("target_types must not contain duplicates")
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError("limitations must not contain duplicates")
        return self


@dataclass(frozen=True, slots=True)
class DiscoveredSession:
    """A display-safe summary paired with its internal source path."""

    source: SessionSource
    summary: SessionSummary
    source_facts: SourceFacts


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Bounded supported sessions and scan counts that expose no session content."""

    sessions: tuple[DiscoveredSession, ...]
    candidates_seen: int
    rejected_candidates: int
    rejection_codes: tuple[AdapterErrorCode, ...]
    truncated: bool


def scan_store(
    root: Path,
    *,
    max_depth: int,
    accepts: Callable[[Path], bool],
    limits: DiscoveryLimits,
) -> SourceScan:
    """Return matching regular files without following directory links."""

    if max_depth < 0:
        raise ValueError("max_depth must not be negative")
    if not _store_is_available(root):
        return SourceScan(sources=(), truncated=False)

    candidates: list[SessionSource] = []
    matching_entries = 0
    for path, entry_metadata in _walk_store(
        root,
        max_depth=max_depth,
        max_entries=limits.max_entries,
    ):
        if not accepts(path.relative_to(root)):
            continue
        matching_entries += 1
        if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISREG(entry_metadata.st_mode):
            raise AdapterError(
                AdapterErrorCode.UNSAFE_SOURCE,
                "native session candidate must be a regular file, not a link",
            )
        candidates.append(
            SessionSource(
                path=path,
                modified_at_ns=entry_metadata.st_mtime_ns,
                size_bytes=entry_metadata.st_size,
            )
        )
        if len(candidates) == limits.max_candidates * 2:
            candidates.sort(key=_source_sort_key)
            del candidates[limits.max_candidates :]
    candidates.sort(key=_source_sort_key)
    del candidates[limits.max_candidates :]
    return SourceScan(
        sources=tuple(candidates),
        truncated=matching_entries > len(candidates),
    )


def _store_is_available(root: Path) -> bool:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise AdapterError(
            AdapterErrorCode.SOURCE_UNAVAILABLE,
            "native session store is unavailable",
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AdapterError(
            AdapterErrorCode.UNSAFE_SOURCE,
            "native session store must be a directory, not a link",
        )
    try:
        validate_directory_chain(root)
    except UnsafePathError:
        raise AdapterError(
            AdapterErrorCode.UNSAFE_SOURCE,
            "native session store has an unsafe controlling directory",
        ) from None
    return True


def _walk_store(
    root: Path,
    *,
    max_depth: int,
    max_entries: int,
) -> Iterator[tuple[Path, os.stat_result]]:
    pending = [(root, 0)]
    entries_seen = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        raise AdapterError(
                            AdapterErrorCode.DISCOVERY_LIMIT_EXCEEDED,
                            "native session store exceeds the discovery entry limit",
                        )
                    entry_metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_metadata.st_mode):
                        if depth < max_depth and not entry.is_symlink():
                            pending.append((Path(entry.path), depth + 1))
                        continue
                    yield Path(entry.path), entry_metadata
        except AdapterError:
            raise
        except OSError:
            raise AdapterError(
                AdapterErrorCode.SOURCE_UNAVAILABLE,
                "native session store cannot be inspected",
            ) from None


def _source_sort_key(source: SessionSource) -> tuple[int, str]:
    return -source.modified_at_ns, source.path.as_posix()
