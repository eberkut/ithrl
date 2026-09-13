from __future__ import annotations

import pytest

from ithrl.case import CaseFormatError, SourceArtifact, source_context
from ithrl.context import ContextGraph, ContextNode, SourceRecordSelector, content_digest


def _graph(*, line: int = 1, identity: int = 0, digest: str = "0" * 64) -> ContextGraph:
    return ContextGraph(
        nodes=(
            ContextNode(
                id="source-record",
                kind="fixture",
                selector=SourceRecordSelector(
                    line=line,
                    identity_key="ordinal",
                    identity_value=identity,
                ),
                mutable=True,
                source="source/session.jsonl",
                digest=digest,
            ),
        )
    )


def _artifact(content: bytes) -> SourceArtifact:
    return SourceArtifact(
        path="source/session.jsonl",
        media_type="application/x-ndjson",
        content=content,
    )


def test_source_record_reference_matches_exact_native_record() -> None:
    record = {"ordinal": 0, "value": "fixture"}
    artifact = _artifact(b'{"ordinal":0,"value":"fixture"}\n')

    source_context.validate_source_references(
        _graph(digest=content_digest(record)),
        None,
        (artifact,),
    )


def test_source_record_reference_requires_artifact_and_line() -> None:
    with pytest.raises(CaseFormatError, match="unavailable evidence"):
        source_context.validate_source_references(_graph(), None, ())
    with pytest.raises(CaseFormatError, match="line is unavailable"):
        source_context.validate_source_references(_graph(line=2), None, (_artifact(b"{}\n"),))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b'{"ordinal":0,"ordinal":1}\n', "duplicate key"),
        (b'{"ordinal":NaN}\n', "invalid number"),
        (b"[]\n", "JSON object"),
        (b"not-json\n", "valid JSON"),
    ],
)
def test_invalid_selected_source_record_is_refused(content: bytes, message: str) -> None:
    with pytest.raises(CaseFormatError, match=message):
        source_context.validate_source_references(_graph(), None, (_artifact(content),))


def test_selected_source_record_has_an_independent_line_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_context, "_MAX_SOURCE_RECORD_BYTES", 16)

    with pytest.raises(CaseFormatError, match="byte limit"):
        source_context.validate_source_references(
            _graph(),
            None,
            (_artifact(b'{"ordinal":0,"padding":"value"}\n'),),
        )


@pytest.mark.parametrize("delimiter", [b"\n", b"\r\n"])
def test_selected_source_record_limit_excludes_jsonl_delimiter(
    monkeypatch: pytest.MonkeyPatch,
    delimiter: bytes,
) -> None:
    raw = b'{"ordinal":0,"value":"fixture"}'
    record = {"ordinal": 0, "value": "fixture"}
    monkeypatch.setattr(source_context, "_MAX_SOURCE_RECORD_BYTES", len(raw))

    source_context.validate_source_references(
        _graph(digest=content_digest(record)),
        None,
        (_artifact(raw + delimiter),),
    )
