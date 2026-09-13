"""Verify source-backed context and decision selectors against opaque evidence."""

from __future__ import annotations

import io
import json
from collections import defaultdict

from ithrl.case.contracts import CaseFormatError
from ithrl.case.source import SourceArtifact
from ithrl.context import ContextGraph, JsonObject, SourceRecordSelector, content_digest
from ithrl.decisions import DecisionEvidence

_MAX_SOURCE_RECORD_BYTES = 8 * 1024 * 1024


def validate_source_references(
    context: ContextGraph,
    decision: DecisionEvidence | None,
    artifacts: tuple[SourceArtifact, ...],
) -> None:
    """Verify every native record selector and its canonical record digest."""

    if decision is not None:
        _validate_decision_boundary(context, decision)
    expected: dict[str, list[tuple[SourceRecordSelector, str]]] = defaultdict(list)
    for node in context.nodes:
        if node.selector is not None:
            expected[node.source].append((node.selector, node.digest))
    if decision is not None:
        expected[decision.selector.source].append(
            (decision.selector.record, decision.selector.record_digest)
        )
    if not expected:
        return

    by_path = {artifact.path: artifact for artifact in artifacts}
    missing = sorted(expected.keys() - by_path.keys())
    if missing:
        raise CaseFormatError("source selector refers to unavailable evidence")
    for path, references in expected.items():
        _validate_artifact_records(by_path[path], references)


def _validate_decision_boundary(
    context: ContextGraph,
    decision: DecisionEvidence,
) -> None:
    for node in context.nodes:
        if node.selector is None:
            continue
        if node.source != decision.selector.source:
            raise CaseFormatError("source-backed decision context must use one source artifact")
        if node.selector.line >= decision.selector.context_before_line:
            raise CaseFormatError("source-backed context must precede the selected decision")


def _validate_artifact_records(
    artifact: SourceArtifact,
    references: list[tuple[SourceRecordSelector, str]],
) -> None:
    by_line: dict[int, list[tuple[SourceRecordSelector, str]]] = defaultdict(list)
    for selector, digest in references:
        by_line[selector.line].append((selector, digest))
    remaining = set(by_line)
    for line_number, raw_line in enumerate(io.BytesIO(artifact.content), start=1):
        selected_records = by_line.get(line_number)
        if selected_records is None:
            continue
        record = _load_record(raw_line)
        digest = content_digest(record)
        for selector, expected_digest in selected_records:
            _validate_record_identity(record, selector)
            if digest != expected_digest:
                raise CaseFormatError("source record digest does not match case metadata")
        remaining.remove(line_number)
        if not remaining:
            return
    raise CaseFormatError("source selector line is unavailable")


def _load_record(raw: bytes) -> JsonObject:
    record = raw.removesuffix(b"\n").removesuffix(b"\r")
    if len(record) > _MAX_SOURCE_RECORD_BYTES:
        raise CaseFormatError("selected source record exceeds the byte limit")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CaseFormatError("selected source record contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise CaseFormatError("selected source record contains an invalid number")

    try:
        value = json.loads(
            record.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except CaseFormatError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as error:
        raise CaseFormatError("selected source record is not valid JSON") from error
    if not isinstance(value, dict):
        raise CaseFormatError("selected source record must be a JSON object")
    return value


def _validate_record_identity(record: JsonObject, selector: SourceRecordSelector) -> None:
    if selector.identity_key is None:
        return
    actual = record.get(selector.identity_key)
    expected = selector.identity_value
    if type(actual) is not type(expected) or actual != expected:
        raise CaseFormatError("source record identity does not match its selector")
