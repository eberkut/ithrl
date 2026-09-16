"""Private, atomic result snapshot storage."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ithrl.context import JsonObject
from ithrl.execution import ResolvedExecutionPlan
from ithrl.reduce import ReductionCheckpoint
from ithrl.results.models import ResultSnapshot, ResultSnapshotError, ResultSummary
from ithrl.results.validation import (
    json_object,
    loads_json,
    validate_contents,
    validation_message,
)

_SNAPSHOT_FILES = frozenset(
    {
        "summary.json",
        "retained-context.jsonl",
        "experiment.toml",
        "provenance.json",
        "execution-plan.json",
        "checkpoint.json",
    }
)
_MAX_RESULT_FILE_BYTES = 64 * 1024 * 1024


def write_result_snapshot(
    *,
    destination: Path,
    summary: ResultSummary,
    request: Mapping[str, Any] | None,
    retained_context_lines: tuple[str, ...],
    experiment_source: Path,
    provenance: Mapping[str, Any],
    execution_plan: ResolvedExecutionPlan | Mapping[str, Any],
    checkpoint: ReductionCheckpoint | Mapping[str, Any],
) -> Path:
    """Atomically create a complete private result directory without overwriting."""

    destination = _absolute_path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"result destination already exists: {destination}")
    _prepare_private_parent(destination.parent)
    experiment_text = _read_regular_text(experiment_source, label="experiment source")
    validated = validate_contents(
        root=destination,
        summary=summary,
        request=request,
        retained_context_lines=retained_context_lines,
        experiment_source=experiment_text,
        provenance=provenance,
        execution_plan=execution_plan,
        checkpoint=checkpoint,
    )

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    if os.name == "posix":
        staging.chmod(0o700)
    try:
        _write_json(staging / "summary.json", validated.summary.model_dump(mode="json"))
        if validated.reduced_request is not None:
            _write_json(staging / "request.json", validated.reduced_request)
        _write_text(
            staging / "retained-context.jsonl",
            "".join(f"{line.rstrip()}\n" for line in validated.retained_context_lines),
        )
        _write_text(staging / "experiment.toml", validated.experiment_source)
        _write_json(staging / "provenance.json", validated.provenance.model_dump(mode="json"))
        _write_json(
            staging / "execution-plan.json",
            validated.execution_plan.model_dump(mode="json"),
        )
        _write_json(
            staging / "checkpoint.json",
            validated.checkpoint.model_dump(mode="json"),
        )
        _restrict_tree(staging)
        staging.rename(destination)
        _sync_directory(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def load_result_snapshot(root: Path) -> ResultSnapshot:
    """Load and cross-check a complete private result snapshot for safe resumption."""

    root = _absolute_path(root)
    _require_private_directory(root)
    entries = {entry.name: entry for entry in root.iterdir()}
    if "summary.json" not in entries:
        raise ResultSnapshotError("result snapshot is missing: summary.json")
    _require_private_file(entries["summary.json"], label="summary.json")
    summary_data = _read_json_object(entries["summary.json"])
    try:
        summary = ResultSummary.model_validate_json(json.dumps(summary_data))
    except ValidationError as error:
        raise ResultSnapshotError(f"invalid summary.json: {validation_message(error)}") from error
    expected_files = _SNAPSHOT_FILES | (
        {"request.json"} if summary.candidate_materialization == "exact-request" else set()
    )
    missing = sorted(expected_files - entries.keys())
    unexpected = sorted(entries.keys() - expected_files)
    if missing:
        raise ResultSnapshotError(f"result snapshot is missing: {', '.join(missing)}")
    if unexpected:
        raise ResultSnapshotError(
            f"result snapshot contains unexpected entries: {', '.join(unexpected)}"
        )
    for name, path in entries.items():
        _require_private_file(path, label=name)

    request = (
        _read_json_object(entries["request.json"])
        if summary.candidate_materialization == "exact-request"
        else None
    )
    retained_lines = _read_context_lines(entries["retained-context.jsonl"])
    experiment_source = _read_regular_text(entries["experiment.toml"], label="experiment.toml")
    provenance = _read_json_object(entries["provenance.json"])
    execution_plan = _read_json_object(entries["execution-plan.json"])
    checkpoint = _read_json_object(entries["checkpoint.json"])
    return validate_contents(
        root=root,
        summary=summary,
        request=request,
        retained_context_lines=retained_lines,
        experiment_source=experiment_source,
        provenance=provenance,
        execution_plan=execution_plan,
        checkpoint=checkpoint,
    )


def _read_context_lines(path: Path) -> tuple[str, ...]:
    text = _read_regular_text(path, label="retained-context.jsonl")
    if not text:
        return ()
    if not text.endswith("\n"):
        raise ResultSnapshotError("retained-context.jsonl must end with a newline")
    return tuple(text.splitlines())


def _read_json_object(path: Path) -> JsonObject:
    label = path.name
    value = loads_json(_read_regular_text(path, label=label), label=label)
    if not isinstance(value, dict):
        raise ResultSnapshotError(f"{label} must contain a JSON object")
    return json_object(value, label=label)


def _read_regular_text(path: Path, *, label: str) -> str:
    _reject_unsafe_path(_absolute_path(path))
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ResultSnapshotError(f"{label} is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ResultSnapshotError(f"{label} must be a regular file, not a link: {path}")
    if metadata.st_size > _MAX_RESULT_FILE_BYTES:
        raise ResultSnapshotError(f"{label} exceeds the {_MAX_RESULT_FILE_BYTES}-byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ResultSnapshotError(f"cannot read {label}: {error}") from error


def _write_json(path: Path, value: object) -> None:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ResultSnapshotError(f"{path.name} is not JSON-compatible: {error}") from error
    _write_text(path, f"{rendered}\n")


def _write_text(path: Path, value: str) -> None:
    if len(value.encode("utf-8")) > _MAX_RESULT_FILE_BYTES:
        raise ResultSnapshotError(f"{path.name} exceeds the {_MAX_RESULT_FILE_BYTES}-byte limit")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        path.chmod(0o600)


def _prepare_private_parent(path: Path) -> None:
    _reject_unsafe_path(path)
    if path.is_symlink():
        raise ResultSnapshotError(f"result parent must not be a symbolic link: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_unsafe_path(path)
    if not path.is_dir() or path.is_symlink():
        raise ResultSnapshotError(f"result parent must be a directory: {path}")
    if os.name == "posix":
        path.chmod(0o700)


def _require_private_directory(path: Path) -> None:
    _reject_unsafe_path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ResultSnapshotError(f"result snapshot is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ResultSnapshotError(f"result snapshot must be a directory, not a link: {path}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ResultSnapshotError("result snapshot directory must have mode 0700")


def _require_private_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ResultSnapshotError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ResultSnapshotError(f"{label} must be a regular file, not a link")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ResultSnapshotError(f"{label} must have mode 0600")


def _restrict_tree(root: Path) -> None:
    if os.name != "posix":
        return
    root.chmod(0o700)
    for path in root.iterdir():
        path.chmod(0o600)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _reject_unsafe_path(path: Path) -> None:
    if ".." in path.parts:
        raise ResultSnapshotError("result snapshot path cannot contain '..'")
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise ResultSnapshotError(f"result snapshot path contains a symbolic link: {current}")
