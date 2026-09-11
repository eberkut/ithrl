"""Opaque source artifacts retained by a case."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice, pairwise
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SOURCE_ARTIFACTS = 64
MAX_SOURCE_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_PATH_BYTES = 1024
MAX_SOURCE_COMPONENT_BYTES = 255
MAX_SOURCE_MANIFEST_BYTES = 1024 * 1024
SOURCE_MANIFEST_PATH = "source/manifest.json"

_MEDIA_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"|?*')


class SourceArtifactIdentity(BaseModel):
    """Stable identity of one opaque source artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    path: Annotated[str, Field(min_length=1, max_length=MAX_SOURCE_PATH_BYTES)]
    media_type: Annotated[str, Field(min_length=3, max_length=255)]
    size_bytes: Annotated[int, Field(ge=1, le=MAX_SOURCE_ARTIFACT_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_source_path(value)

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        if not _MEDIA_TYPE_PATTERN.fullmatch(value):
            raise ValueError("media_type must be an unparameterized Internet media type")
        return value.lower()


class SourceManifest(BaseModel):
    """Versioned index of every opaque file below the source boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifacts: Annotated[
        tuple[SourceArtifactIdentity, ...], Field(min_length=1, max_length=MAX_SOURCE_ARTIFACTS)
    ]

    @model_validator(mode="after")
    def require_canonical_artifact_order(self) -> SourceManifest:
        paths = tuple(artifact.path for artifact in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("source manifest contains duplicate artifact paths")
        if paths != tuple(sorted(paths)):
            raise ValueError("source manifest artifacts must be ordered by path")
        _validate_non_overlapping_paths(paths)
        if sum(artifact.size_bytes for artifact in self.artifacts) > MAX_SOURCE_BYTES:
            raise ValueError("source manifest exceeds the total byte limit")
        return self


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    """One opaque source file with exact immutable content."""

    path: str
    media_type: str
    content: bytes = field(repr=False)
    _identity: SourceArtifactIdentity = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("source artifact content must be bytes")
        size_bytes = len(self.content)
        if size_bytes == 0:
            raise ValueError("source artifact content must not be empty")
        if size_bytes > MAX_SOURCE_ARTIFACT_BYTES:
            raise ValueError("source artifact exceeds the per-file byte limit")
        identity = SourceArtifactIdentity(
            path=self.path,
            media_type=self.media_type,
            size_bytes=size_bytes,
            sha256=hashlib.sha256(self.content).hexdigest(),
        )
        object.__setattr__(self, "path", identity.path)
        object.__setattr__(self, "media_type", identity.media_type)
        object.__setattr__(self, "_identity", identity)

    @property
    def identity(self) -> SourceArtifactIdentity:
        return self._identity

    @property
    def size_bytes(self) -> int:
        return self._identity.size_bytes

    @property
    def sha256(self) -> str:
        return self._identity.sha256


def prepare_source_artifacts(
    artifacts: Iterable[SourceArtifact],
) -> tuple[SourceArtifact, ...]:
    """Validate and order source artifacts for deterministic persistence."""

    collected = tuple(islice(artifacts, MAX_SOURCE_ARTIFACTS + 1))
    if len(collected) > MAX_SOURCE_ARTIFACTS:
        raise ValueError("case exceeds the source artifact count limit")
    if any(not isinstance(artifact, SourceArtifact) for artifact in collected):
        raise TypeError("source artifacts must use SourceArtifact")
    ordered = tuple(sorted(collected, key=_artifact_path))
    paths = tuple(artifact.path for artifact in ordered)
    if len(paths) != len(set(paths)):
        raise ValueError("case contains duplicate source artifact paths")
    _validate_non_overlapping_paths(paths)
    if sum(artifact.identity.size_bytes for artifact in ordered) > MAX_SOURCE_BYTES:
        raise ValueError("case exceeds the total source byte limit")
    return ordered


def _artifact_path(artifact: SourceArtifact) -> str:
    return artifact.path


def _validate_non_overlapping_paths(paths: tuple[str, ...]) -> None:
    portable_paths = tuple(sorted(_portable_path_key(path) for path in paths))
    if len(portable_paths) != len(set(portable_paths)):
        raise ValueError("source artifact paths must not alias across supported platforms")
    for parent, child in pairwise(portable_paths):
        if child.startswith(f"{parent}/"):
            raise ValueError("source artifact paths must not overlap")


def validate_source_path(value: str) -> str:
    """Require a portable relative path contained by the source directory."""

    parts = value.split("/")
    _validate_path_characters(value)
    _validate_path_structure(value, parts)
    _validate_path_lengths(value, parts)
    _validate_portable_components(parts)
    if _portable_path_key(value) == _portable_path_key(SOURCE_MANIFEST_PATH):
        raise ValueError("source artifact path is reserved")
    return value


def _validate_path_characters(value: str) -> None:
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("source artifact path contains unsupported characters")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("source artifact path must use canonical Unicode")


def _validate_path_structure(value: str, parts: list[str]) -> None:
    if (
        PurePosixPath(value).is_absolute()
        or len(parts) < 2
        or parts[0] != "source"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("source artifact path must be a safe path below 'source/'")


def _validate_path_lengths(value: str, parts: list[str]) -> None:
    if len(value.encode("utf-8")) > MAX_SOURCE_PATH_BYTES:
        raise ValueError("source artifact path is too long")
    if any(len(part.encode("utf-8")) > MAX_SOURCE_COMPONENT_BYTES for part in parts):
        raise ValueError("source artifact path component is too long")


def _validate_portable_components(parts: list[str]) -> None:
    if any(not _is_portable_component(part) for part in parts):
        raise ValueError("source artifact path is not portable across supported platforms")


def _is_portable_component(value: str) -> bool:
    stem = value.split(".", maxsplit=1)[0].casefold()
    return (
        not any(character in _WINDOWS_RESERVED_CHARACTERS for character in value)
        and not value.endswith((" ", "."))
        and stem not in _WINDOWS_RESERVED_NAMES
    )


def _portable_path_key(value: str) -> str:
    return "/".join(part.casefold() for part in value.split("/"))
