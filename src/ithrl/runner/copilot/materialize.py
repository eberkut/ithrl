"""Reconstruct GitHub Copilot native prefixes from preserved source evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Never

from ithrl.adapters import AdapterError
from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.registry import parse_session_bytes
from ithrl.case import CaseFormatError
from ithrl.context import ContextNode, JsonObject, JsonValue, content_digest
from ithrl.extraction import validate_extracted_case
from ithrl.mutation import MutationError, TextAppend, append_message_text
from ithrl.runner.contracts import PreparedAttempt, _validate_attempt_identities
from ithrl.runner.copilot.policy import CAPTURE_TYPE, MATERIALIZATION, SOURCE_SCHEMA

_SOURCE_PATH = "source/events.jsonl"
_MAX_NATIVE_SESSION_BYTES = 64 * 1024 * 1024


class CopilotMaterializationErrorCode(StrEnum):
    """Stable failure categories for source-backed Copilot reconstruction."""

    INCOMPATIBLE_CASE = "incompatible-case"
    CHANGED_EVIDENCE = "changed-evidence"
    UNSUPPORTED_CONTEXT = "unsupported-context"
    MISSING_TRIGGER = "missing-trigger"
    AMBIGUOUS_BOUNDARY = "ambiguous-boundary"


class CopilotMaterializationError(RuntimeError):
    """A Copilot candidate cannot be reconstructed without changing its meaning."""

    def __init__(self, code: CopilotMaterializationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CopilotMaterialization:
    """Private native restore input; None marks genuinely uncaptured system guidance."""

    native_session: bytes = field(repr=False)
    session_id: str
    system_message: str | None = field(repr=False)


def materialize_copilot_attempt(attempt: PreparedAttempt) -> CopilotMaterialization:
    """Overlay one candidate on the immutable prefix through its native turn start."""

    _validate_compatible_attempt(attempt)
    try:
        validate_extracted_case(attempt.evidence)
    except CaseFormatError:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.CHANGED_EVIDENCE,
            "the preserved Copilot evidence no longer matches the case",
        ) from None
    try:
        _validate_attempt_identities(attempt)
    except ValueError:
        _unsupported("the Copilot candidate does not match its evidence and execution plan")
    return _materialize_session(attempt, _source_session(attempt))


def _validate_compatible_attempt(attempt: PreparedAttempt) -> None:
    evidence = attempt.evidence
    manifest = evidence.manifest
    decision = evidence.decision
    if (
        attempt.candidate.materialization != MATERIALIZATION
        or manifest.harness != "copilot"
        or manifest.source_schema != SOURCE_SCHEMA
        or manifest.capture.request != CAPTURE_TYPE
        or decision is None
        or decision.selector.kind != "copilot-assistant-message"
        or attempt.target.type not in {"final-response", "shell-action"}
    ):
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case is not a supported Copilot decision",
        )
    if not attempt.candidate.mutation.is_empty and attempt.candidate.removed_context_ids:
        _unsupported("text mutation requires the unchanged original context")


def _source_session(attempt: PreparedAttempt) -> NativeSession:
    decision = attempt.evidence.decision
    if decision is None:
        _ambiguous("the Copilot case has no selected decision")
    source = next(
        (
            artifact
            for artifact in attempt.evidence.source_artifacts
            if artifact.path == decision.selector.source == _SOURCE_PATH
        ),
        None,
    )
    if source is None:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case has no supported Copilot source artifact",
        )
    return parse_session_bytes(source.content)


def _materialize_session(
    attempt: PreparedAttempt,
    session: NativeSession,
) -> CopilotMaterialization:
    decision = attempt.evidence.decision
    if decision is None:
        _ambiguous("the Copilot case has no selected decision")
    records = session.records
    boundary = decision.selector.context_before_line
    retained = frozenset(attempt.candidate.retained_context_ids)
    selected = _selected_context(attempt.evidence.context.nodes, records, boundary, retained)
    fixed_input = _trigger_node_id(
        attempt.evidence.context.nodes, records, boundary, decision.selector.turn_id
    )
    if fixed_input not in retained:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.MISSING_TRIGGER,
            "the candidate removes the native input for the selected decision",
        )
    edit = next(iter(attempt.candidate.mutation.edits), None)
    if edit is not None:
        if edit.context_id == fixed_input:
            raise CopilotMaterializationError(
                CopilotMaterializationErrorCode.MISSING_TRIGGER,
                "mutation cannot change the current decision input",
            )
        eligible = {site.context_id for site in attempt.evidence.editable_text()}
        if edit.context_id not in eligible:
            _unsupported("mutation requires an editable prior user message")
    raw_lines = session.native_bytes.splitlines(keepends=True)
    output = [
        _append_record(raw_lines[line - 1], record, edit)
        if edit is not None and node_id == edit.context_id
        else raw_lines[line - 1]
        for line, node_id, record in selected
    ]
    # The existing model-turn boundary is retained; no user input or target output is invented.
    output.append(raw_lines[boundary - 1])
    native_session = b"".join(output)
    if len(native_session) > _MAX_NATIVE_SESSION_BYTES:
        _unsupported("the reconstructed Copilot native prefix exceeds the byte limit")
    try:
        candidate_session = parse_session_bytes(native_session)
    except AdapterError:
        _unsupported("the retained Copilot context has invalid native relationships")
    _validate_complete_tool_work(candidate_session.records)
    return CopilotMaterialization(
        native_session=native_session,
        session_id=session.metadata.session_id,
        system_message=_captured_system_message(candidate_session.records),
    )


def _trigger_node_id(
    nodes: tuple[ContextNode, ...],
    records: tuple[JsonObject, ...],
    boundary: int,
    turn_id: str,
) -> str:
    """Require the fixed native input independently of editable-text discovery."""

    if (
        boundary < 2
        or boundary > len(records)
        or records[boundary - 1].get("type") != "assistant.turn_start"
        or _object(records[boundary - 1].get("data")).get("turnId") != turn_id
    ):
        _ambiguous("the selected Copilot decision has no supported native input boundary")
    for index in range(boundary - 2, -1, -1):
        if records[index].get("type") in {"user.message", "tool.execution_complete"}:
            return nodes[index].id
    return _ambiguous("the selected Copilot decision has no supported native input boundary")


def _selected_context(
    nodes: tuple[ContextNode, ...],
    records: tuple[JsonObject, ...],
    boundary: int,
    retained: frozenset[str],
) -> list[tuple[int, str, JsonObject]]:
    if len(nodes) != boundary - 1:
        _ambiguous("the Copilot context graph does not match the selected boundary")
    selected = []
    for line, node in enumerate(nodes, start=1):
        record = records[line - 1]
        selector = node.selector
        if (
            selector is None
            or selector.line != line
            or selector.identity_key != "id"
            or selector.identity_value != record.get("id")
            or node.source != _SOURCE_PATH
            or node.digest != content_digest(record)
        ):
            _ambiguous("the Copilot context graph does not match its source records")
        if node.id in retained:
            selected.append((line, node.id, record))
    return selected


def _append_record(raw: bytes, record: JsonObject, edit: TextAppend) -> bytes:
    data = _object(record.get("data"))
    effective_field = "transformedContent" if "transformedContent" in data else "content"
    try:
        changed = append_message_text({"role": "user", "content": data.get(effective_field)}, edit)
    except MutationError:
        _unsupported("mutation does not match the effective prior user text")
    data[effective_field] = changed["content"]
    ending = b"\r\n" if raw.endswith(b"\r\n") else b"\n" if raw.endswith(b"\n") else b""
    try:
        encoded = json.dumps(
            record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (UnicodeError, ValueError):
        _unsupported("mutation cannot be encoded as a native Copilot record")
    return encoded + ending


def _validate_complete_tool_work(records: tuple[JsonObject, ...]) -> None:
    requests: set[str] = set()
    starts: set[str] = set()
    completions: set[str] = set()
    for record in records:
        data = _object(record.get("data"))
        if record.get("type") == "assistant.message":
            tools = data.get("toolRequests")
            if isinstance(tools, list):
                requests.update(
                    str(request["toolCallId"]) for request in tools if isinstance(request, dict)
                )
        elif record.get("type") == "tool.execution_start":
            starts.add(str(data["toolCallId"]))
        elif record.get("type") == "tool.execution_complete":
            completions.add(str(data["toolCallId"]))
    if requests != starts or requests != completions:
        _unsupported("the retained Copilot context contains incomplete tool work")


def _captured_system_message(records: tuple[JsonObject, ...]) -> str | None:
    for record in reversed(records):
        if record.get("type") != "system.message":
            continue
        data = _object(record.get("data"))
        if data.get("role", "system") != "system":
            continue
        content = data.get("content")
        if not isinstance(content, str):
            _unsupported("the captured Copilot system message has unsupported content")
        return content
    return None


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _unsupported(message: str) -> Never:
    raise CopilotMaterializationError(
        CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        message,
    ) from None


def _ambiguous(message: str) -> Never:
    raise CopilotMaterializationError(
        CopilotMaterializationErrorCode.AMBIGUOUS_BOUNDARY,
        message,
    )
