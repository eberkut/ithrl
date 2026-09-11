from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ithrl.adapters import AdapterError, AdapterErrorCode, ParsingLimits
from ithrl.adapters import _jsonl as jsonl_module
from ithrl.adapters._jsonl import read_jsonl


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _assert_error(
    path: Path,
    code: AdapterErrorCode,
    *,
    limits: ParsingLimits | None = None,
) -> AdapterError:
    with pytest.raises(AdapterError) as captured:
        read_jsonl(path, limits=limits or ParsingLimits())
    assert captured.value.code is code
    assert path.name not in str(captured.value)
    return captured.value


def test_reader_preserves_exact_bytes_and_record_order(tmp_path: Path) -> None:
    source = _write(tmp_path / "session.jsonl", b'{"position":1}\r\n{"position":2}\r\n')

    document = read_jsonl(source, limits=ParsingLimits())

    assert document.raw == source.read_bytes()
    assert document.records == ({"position": 1}, {"position": 2})
    assert document.source.size_bytes == len(document.raw)
    assert len(document.source.sha256) == 64


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", AdapterErrorCode.INVALID_JSONL),
        (b"{}\n\n", AdapterErrorCode.INVALID_JSONL),
        (b"\xff\n", AdapterErrorCode.INVALID_UTF8),
        (b'{"key":1,"key":2}\n', AdapterErrorCode.DUPLICATE_KEY),
        (b"[]\n", AdapterErrorCode.INVALID_JSONL),
        (b'{"number":NaN}\n', AdapterErrorCode.INVALID_JSONL),
        (b'{"unfinished":true\n', AdapterErrorCode.INVALID_JSONL),
    ],
)
def test_reader_rejects_invalid_jsonl(
    tmp_path: Path,
    content: bytes,
    code: AdapterErrorCode,
) -> None:
    source = _write(tmp_path / "private-source-name.jsonl", content)

    error = _assert_error(source, code)

    assert "private-source-name" not in str(error)


def test_reader_enforces_byte_line_and_record_limits(tmp_path: Path) -> None:
    source = _write(tmp_path / "session.jsonl", b"{}\n{}\n{}\n")

    _assert_error(
        source,
        AdapterErrorCode.SOURCE_TOO_LARGE,
        limits=ParsingLimits(max_bytes=5, max_line_bytes=2, max_records=3),
    )
    _assert_error(
        source,
        AdapterErrorCode.LINE_TOO_LARGE,
        limits=ParsingLimits(max_bytes=64, max_line_bytes=1, max_records=3),
    )
    _assert_error(
        source,
        AdapterErrorCode.TOO_MANY_RECORDS,
        limits=ParsingLimits(max_bytes=64, max_line_bytes=2, max_records=2),
    )


def test_reader_enforces_json_depth_limit(tmp_path: Path) -> None:
    value: dict[str, Any] = {}
    current = value
    for _ in range(130):
        child: dict[str, Any] = {}
        current["nested"] = child
        current = child
    source = _write(tmp_path / "deep.jsonl", f"{json.dumps(value)}\n".encode())

    _assert_error(source, AdapterErrorCode.JSON_LIMIT_EXCEEDED)


def test_reader_rejects_links_and_non_regular_files(tmp_path: Path) -> None:
    target = _write(tmp_path / "target.jsonl", b"{}\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)

    _assert_error(link, AdapterErrorCode.UNSAFE_SOURCE)
    _assert_error(tmp_path, AdapterErrorCode.UNSAFE_SOURCE)


def test_reader_reports_missing_source_without_disclosing_its_path(tmp_path: Path) -> None:
    source = tmp_path / "private-missing-session.jsonl"

    error = _assert_error(source, AdapterErrorCode.SOURCE_UNAVAILABLE)

    assert "private-missing-session" not in str(error)
    assert error.__suppress_context__


def test_reader_suppresses_private_parse_error_context(tmp_path: Path) -> None:
    source = _write(tmp_path / "session.jsonl", b'{"private":"unfinished"\n')

    error = _assert_error(source, AdapterErrorCode.INVALID_JSONL)

    assert error.__cause__ is None
    assert error.__suppress_context__


def test_reader_stops_at_record_limit_for_newline_dense_input(tmp_path: Path) -> None:
    source = _write(tmp_path / "many-records.jsonl", b"{}\n" * 10_000)

    _assert_error(
        source,
        AdapterErrorCode.TOO_MANY_RECORDS,
        limits=ParsingLimits(max_bytes=64 * 1024, max_line_bytes=16, max_records=100),
    )


def test_reader_applies_json_item_limit_across_records(tmp_path: Path) -> None:
    source = _write(tmp_path / "many-items.jsonl", b'{"items":[1,2]}\n' * 2)

    _assert_error(
        source,
        AdapterErrorCode.JSON_LIMIT_EXCEEDED,
        limits=ParsingLimits(
            max_bytes=1024,
            max_line_bytes=128,
            max_records=10,
            max_json_items=5,
        ),
    )


def test_reader_maps_oversized_integer_to_stable_error(tmp_path: Path) -> None:
    source = _write(tmp_path / "large-integer.jsonl", b'{"number":' + b"9" * 5_000 + b"}\n")

    error = _assert_error(source, AdapterErrorCode.INVALID_JSONL)

    assert error.line == 1
    assert error.__suppress_context__


def test_reader_refuses_a_source_that_changes_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "session.jsonl", b"{}\n")
    original_signature = jsonl_module._signature
    calls = 0

    def changed_signature(metadata: Any) -> tuple[int, int, int, int, int]:
        nonlocal calls
        calls += 1
        signature = original_signature(metadata)
        if calls == 6:
            return (*signature[:4], signature[4] + 1)
        return signature

    monkeypatch.setattr(jsonl_module, "_signature", changed_signature)

    _assert_error(source, AdapterErrorCode.SOURCE_CHANGED)


def test_parsing_does_not_change_source_content_or_metadata(tmp_path: Path) -> None:
    source = _write(tmp_path / "session.jsonl", b'{"fixture":true}\n')
    before = source.stat()
    before_content = source.read_bytes()

    read_jsonl(source, limits=ParsingLimits())

    after = source.stat()
    assert source.read_bytes() == before_content
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
