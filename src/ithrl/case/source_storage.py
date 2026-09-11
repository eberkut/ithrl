"""Persist and verify opaque files below a case's source boundary."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from ithrl.case.contracts import CaseFormatError, UnsafeCasePathError
from ithrl.case.filesystem import (
    PublishedFile,
    atomic_write,
    atomic_write_bytes,
    create_private_directory,
    read_safe_bytes,
    read_safe_text,
    require_private_directory,
)
from ithrl.case.source import (
    MAX_SOURCE_ARTIFACT_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_MANIFEST_BYTES,
    SOURCE_MANIFEST_PATH,
    SourceArtifact,
    SourceManifest,
)
from ithrl.context import JsonValue, validate_json_limits

SOURCE_ROOT = Path("source")
SOURCE_MANIFEST = Path(SOURCE_MANIFEST_PATH)


def write_sources(
    root: Path,
    sources: tuple[SourceArtifact, ...],
    published: list[PublishedFile],
) -> None:
    if not sources:
        return
    source_root = create_private_directory(root / SOURCE_ROOT)
    directories = sorted(
        {
            root / parent
            for artifact in sources
            for parent in source_parent_paths(artifact.path)
            if parent != SOURCE_ROOT
        },
        key=_path_depth,
    )
    for directory in directories:
        create_private_directory(directory)
    for artifact in sources:
        published.append(atomic_write_bytes(root / PurePosixPath(artifact.path), artifact.content))
    manifest = SourceManifest(artifacts=tuple(artifact.identity for artifact in sources))
    rendered = _render_manifest(manifest)
    if len(rendered.encode("utf-8")) > MAX_SOURCE_MANIFEST_BYTES:
        raise CaseFormatError("source manifest exceeds the byte limit")
    published.append(atomic_write(source_root / "manifest.json", rendered))


def load_sources(root: Path) -> tuple[SourceArtifact, ...]:
    source_root = root / SOURCE_ROOT
    if not os.path.lexists(source_root):
        return ()
    require_private_directory(source_root, label="source")
    manifest_text = read_safe_text(
        root / SOURCE_MANIFEST,
        max_bytes=MAX_SOURCE_MANIFEST_BYTES,
        label="source manifest",
    )
    manifest = _parse_manifest(manifest_text)
    validate_source_tree(root, manifest)
    return _read_artifacts(root, manifest)


def _parse_manifest(value: str) -> SourceManifest:
    raw = _load_json(value)
    try:
        validate_json_limits(raw)
        manifest = SourceManifest.model_validate(raw)
    except (ValidationError, ValueError) as error:
        raise CaseFormatError("invalid source manifest") from error
    if value != _render_manifest(manifest):
        raise CaseFormatError("source manifest is not in canonical form")
    return manifest


def _read_artifacts(root: Path, manifest: SourceManifest) -> tuple[SourceArtifact, ...]:
    sources: list[SourceArtifact] = []
    total_bytes = 0
    for expected in manifest.artifacts:
        content = read_safe_bytes(
            root / PurePosixPath(expected.path),
            max_bytes=MAX_SOURCE_ARTIFACT_BYTES,
            label="source artifact",
        )
        total_bytes += len(content)
        if total_bytes > MAX_SOURCE_BYTES:
            raise CaseFormatError("source evidence exceeds the total byte limit")
        artifact = SourceArtifact(
            path=expected.path,
            media_type=expected.media_type,
            content=content,
        )
        if artifact.identity != expected:
            raise CaseFormatError("source artifact identity does not match its manifest")
        sources.append(artifact)
    return tuple(sources)


def validate_source_tree(root: Path, manifest: SourceManifest) -> None:
    expected_files, expected_directories = _expected_entries(manifest)
    seen_files, seen_directories = _scan_source_tree(root, expected_files, expected_directories)
    if seen_files != expected_files or seen_directories != expected_directories:
        raise CaseFormatError("source evidence is incomplete")


def _expected_entries(manifest: SourceManifest) -> tuple[set[str], set[str]]:
    files = {SOURCE_MANIFEST_PATH, *(artifact.path for artifact in manifest.artifacts)}
    directories = {SOURCE_ROOT.as_posix()}
    for artifact in manifest.artifacts:
        directories.update(path.as_posix() for path in source_parent_paths(artifact.path))
    return files, directories


def _scan_source_tree(
    root: Path,
    expected_files: set[str],
    expected_directories: set[str],
) -> tuple[set[str], set[str]]:
    pending = [root / SOURCE_ROOT]
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    while pending:
        directory = pending.pop()
        seen_directories.add(directory.relative_to(root).as_posix())
        for entry in _directory_entries(directory):
            relative = Path(entry.path).relative_to(root).as_posix()
            metadata = _entry_metadata(entry)
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeCasePathError("source evidence must not contain symbolic links")
            if stat.S_ISDIR(metadata.st_mode):
                _accept_directory(entry, relative, expected_directories, pending)
            elif stat.S_ISREG(metadata.st_mode):
                _accept_file(relative, expected_files, seen_files)
            else:
                raise UnsafeCasePathError("source evidence entries must be regular files")
    return seen_files, seen_directories


def _directory_entries(directory: Path) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as iterator:
            yield from iterator
    except OSError as error:
        raise CaseFormatError("cannot inspect source evidence") from error


def _entry_metadata(entry: os.DirEntry[str]) -> os.stat_result:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as error:
        raise CaseFormatError("cannot inspect source evidence") from error


def _accept_directory(
    entry: os.DirEntry[str],
    relative: str,
    expected: set[str],
    pending: list[Path],
) -> None:
    if relative not in expected:
        raise CaseFormatError("source directory contains an unexpected entry")
    path = Path(entry.path)
    require_private_directory(path, label="source")
    pending.append(path)


def _accept_file(relative: str, expected: set[str], seen: set[str]) -> None:
    if relative not in expected:
        raise CaseFormatError("source directory contains an unexpected entry")
    seen.add(relative)


def source_parent_paths(path: str) -> tuple[Path, ...]:
    parent = Path(*PurePosixPath(path).parts).parent
    parents: list[Path] = []
    while parent != Path():
        parents.append(parent)
        parent = parent.parent
    return tuple(reversed(parents))


def _render_manifest(manifest: SourceManifest) -> str:
    return _render_json(manifest.model_dump(mode="json"))


def _render_json(value: JsonValue) -> str:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, ValueError) as error:
        raise CaseFormatError("value is not valid JSON") from error


def _load_json(source: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CaseFormatError("source manifest contains a duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(source, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, RecursionError) as error:
        raise CaseFormatError("invalid source manifest") from error


def _path_depth(path: Path) -> int:
    return len(path.parts)
