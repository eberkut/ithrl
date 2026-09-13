"""Bounded reader for untrusted native JSONL evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ithrl.adapters.contracts import ParsingLimits, SourceFacts
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import JsonObject, validate_json_limits
from ithrl.path_safety import UnsafePathError, validate_directory_chain


@dataclass(frozen=True, slots=True)
class JsonlDocument:
    raw: bytes
    records: tuple[JsonObject, ...]
    source: SourceFacts


type _SourceSignature = tuple[int, int, int, int, int]


class _DuplicateKeyError(ValueError):
    pass


def read_jsonl(path: str | os.PathLike[str], *, limits: ParsingLimits) -> JsonlDocument:
    """Read one regular JSONL file without following its final path component."""

    source_path = Path(path)
    try:
        validate_directory_chain(source_path.parent)
    except UnsafePathError:
        raise AdapterError(
            AdapterErrorCode.UNSAFE_SOURCE,
            "session source has an unsafe controlling directory",
        ) from None
    initial = _lstat(source_path)
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise AdapterError(AdapterErrorCode.UNSAFE_SOURCE, "session source must be a regular file")
    if initial.st_size > limits.max_bytes:
        raise AdapterError(
            AdapterErrorCode.SOURCE_TOO_LARGE, "session source exceeds the byte limit"
        )

    descriptor = _open_read_only(source_path)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise AdapterError(
                AdapterErrorCode.SOURCE_CHANGED,
                "session source changed while opening",
            )
        if _signature(initial) != _signature(opened):
            raise AdapterError(
                AdapterErrorCode.SOURCE_CHANGED, "session source changed while opening"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(min(opened.st_size, limits.max_bytes) + 1)
            finished = os.fstat(handle.fileno())
    except OSError:
        raise AdapterError(
            AdapterErrorCode.SOURCE_UNAVAILABLE, "cannot read session source"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) > limits.max_bytes:
        raise AdapterError(
            AdapterErrorCode.SOURCE_TOO_LARGE,
            "session source exceeds the byte limit",
            bytes_read=len(raw),
        )
    try:
        current = _lstat(source_path, changed=True)
    except AdapterError as error:
        error.bytes_read = len(raw)
        raise
    if _signature(opened) != _signature(finished) or _signature(opened) != _signature(current):
        raise AdapterError(
            AdapterErrorCode.SOURCE_CHANGED,
            "session source changed while reading",
            bytes_read=len(raw),
        )

    try:
        return read_jsonl_bytes(raw, limits=limits)
    except AdapterError as error:
        error.bytes_read = len(raw)
        raise


def read_jsonl_bytes(raw: bytes, *, limits: ParsingLimits) -> JsonlDocument:
    """Parse one in-memory JSONL snapshot under the native evidence limits."""

    if len(raw) > limits.max_bytes:
        raise AdapterError(
            AdapterErrorCode.SOURCE_TOO_LARGE, "session source exceeds the byte limit"
        )
    records = _parse_lines(raw, limits)
    return JsonlDocument(
        raw=raw,
        records=records,
        source=SourceFacts(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)),
    )


def _lstat(path: Path, *, changed: bool = False) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        if changed:
            raise AdapterError(
                AdapterErrorCode.SOURCE_CHANGED, "session source changed while reading"
            ) from None
        raise AdapterError(
            AdapterErrorCode.SOURCE_UNAVAILABLE, "session source is unavailable"
        ) from None


def _open_read_only(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        return os.open(path, flags)
    except OSError:
        raise AdapterError(
            AdapterErrorCode.SOURCE_UNAVAILABLE, "cannot open session source"
        ) from None


def _signature(metadata: os.stat_result) -> _SourceSignature:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _parse_lines(raw: bytes, limits: ParsingLimits) -> tuple[JsonObject, ...]:
    records: list[JsonObject] = []
    json_items = 0
    start = 0
    while start < len(raw):
        end = raw.find(b"\n", start)
        next_start = len(raw) if end < 0 else end + 1
        end = len(raw) if end < 0 else end
        line_number = len(records) + 1
        if line_number > limits.max_records:
            raise AdapterError(
                AdapterErrorCode.TOO_MANY_RECORDS, "session source exceeds the record limit"
            )
        if end - start > limits.max_line_bytes:
            raise AdapterError(
                AdapterErrorCode.LINE_TOO_LARGE,
                "session record exceeds the line limit",
                line=line_number,
            )

        line = raw[start:end].removesuffix(b"\r")
        if not line:
            raise AdapterError(
                AdapterErrorCode.INVALID_JSONL,
                "session source contains a blank record",
                line=line_number,
            )
        record = _parse_record(line, line_number)
        json_items += _count_json_items(record)
        if json_items > limits.max_json_items:
            raise AdapterError(
                AdapterErrorCode.JSON_LIMIT_EXCEEDED,
                "session source exceeds the JSON item limit",
                line=line_number,
            )
        records.append(record)
        start = next_start
    if not records:
        raise AdapterError(AdapterErrorCode.INVALID_JSONL, "session source contains no records")
    return tuple(records)


def _parse_record(raw: bytes, line_number: int) -> JsonObject:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise AdapterError(
            AdapterErrorCode.INVALID_UTF8,
            "session source is not valid UTF-8",
            line=line_number,
        ) from None

    try:
        value: Any = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyError:
        raise AdapterError(
            AdapterErrorCode.DUPLICATE_KEY,
            "session record contains a duplicate object key",
            line=line_number,
        ) from None
    except (ValueError, RecursionError):
        raise AdapterError(
            AdapterErrorCode.INVALID_JSONL,
            "session record is not valid JSON",
            line=line_number,
        ) from None
    if not isinstance(value, dict):
        raise AdapterError(
            AdapterErrorCode.INVALID_JSONL,
            "session record must be a JSON object",
            line=line_number,
        )
    try:
        validate_json_limits(value)
    except ValueError:
        raise AdapterError(
            AdapterErrorCode.JSON_LIMIT_EXCEEDED,
            "session record exceeds JSON structure limits",
            line=line_number,
        ) from None
    return value


def _count_json_items(value: JsonObject) -> int:
    items = 0
    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            items += len(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            items += len(current)
            pending.extend(current)
    return items


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError
