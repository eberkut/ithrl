"""Reconstruct GitHub Copilot semantic sessions from preserved native evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Never

from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.registry import parse_session_bytes
from ithrl.case import CaseFormatError
from ithrl.context import ContextNode, JsonObject, JsonValue, content_digest
from ithrl.extraction import validate_extracted_case
from ithrl.runner.contracts import PreparedAttempt
from ithrl.runner.copilot.policy import CAPTURE_TYPE, MATERIALIZATION, SOURCE_SCHEMA

_SOURCE_PATH = "source/events.jsonl"
_MAX_SEMANTIC_SESSION_BYTES = 64 * 1024 * 1024

# These records describe harness lifecycle or duplicate model telemetry. They are
# not conversation items in Copilot's semantic session interchange format.
_OMITTED_RECORD_TYPES = frozenset(
    {
        "assistant.turn_end",
        "assistant.turn_start",
        "model.captured_assignment_context",
        "model.message",
        "model.messages_snapshot",
        "model.model_call_started",
        "model.model_call_success",
        "model.response",
        "model.turn_ended",
        "model.turn_started",
        "session.task_complete",
    }
)


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
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CopilotMaterialization:
    """Private Copilot CLI inputs derived from one verified candidate."""

    semantic_session: bytes = field(repr=False)
    trigger: str = field(repr=False)


@dataclass(slots=True)
class _ToolSequence:
    turn_id: str
    name: str
    arguments: JsonObject = field(repr=False)
    started: bool = False
    completed: bool = False


@dataclass(slots=True)
class _SemanticBuilder:
    records: list[JsonObject]
    tools: dict[str, _ToolSequence] = field(default_factory=dict)
    retained_user_turns: set[str] = field(default_factory=set)
    open_turns: dict[str, int] = field(default_factory=dict)

    def add(self, record: JsonObject) -> None:
        record_type = _required_native_string(record.get("type"))
        if record_type == "user.message":
            self._add_user(record)
        elif record_type == "assistant.turn_start":
            self._start_turn(record)
        elif record_type == "assistant.turn_end":
            self._end_turn(record)
        elif record_type == "assistant.message":
            self._add_assistant(record)
        elif record_type == "tool.execution_start":
            self._start_tool(record)
        elif record_type == "tool.execution_complete":
            self._complete_tool(record)
        elif record_type not in _OMITTED_RECORD_TYPES:
            _unsupported("the retained Copilot context contains an unsupported record")

    def finish(self) -> tuple[JsonObject, ...]:
        if any(not tool.completed for tool in self.tools.values()):
            _unsupported("the retained Copilot context contains incomplete tool work")
        if self.open_turns:
            _unsupported("the retained Copilot context contains an incomplete assistant turn")
        return tuple(self.records)

    def _add_user(self, record: JsonObject) -> None:
        if any(not tool.completed for tool in self.tools.values()):
            _unsupported("the retained Copilot context contains incomplete tool work")
        data = _object(record.get("data"))
        content = _historical_user_content(data)
        turn_id = _required_data_string(data, "turnId", "user turn identity is missing")
        self.records.append(_semantic_message(record, role="user", content=content))
        self.retained_user_turns.add(turn_id)

    def _start_turn(self, record: JsonObject) -> None:
        turn_id = _record_turn_id(record)
        self.open_turns[turn_id] = self.open_turns.get(turn_id, 0) + 1

    def _end_turn(self, record: JsonObject) -> None:
        turn_id = _record_turn_id(record)
        depth = self.open_turns.get(turn_id, 0)
        if depth < 1:
            _unsupported("the retained Copilot context has an unmatched assistant turn")
        if depth == 1:
            del self.open_turns[turn_id]
        else:
            self.open_turns[turn_id] = depth - 1

    def _add_assistant(self, record: JsonObject) -> None:
        if any(not tool.completed for tool in self.tools.values()):
            _unsupported("the retained Copilot context contains incomplete tool work")
        data = _object(record.get("data"))
        turn_id = _required_data_string(data, "turnId", "assistant turn identity is missing")
        if turn_id not in self.retained_user_turns or not self.open_turns.get(turn_id, 0):
            _unsupported(
                "the retained Copilot context contains an assistant message without a user"
            )
        content = data.get("content")
        if not isinstance(content, str):
            _unsupported("a retained Copilot assistant message has unsupported content")
        self.records.append(_semantic_message(record, role="assistant", content=content))
        requests = data.get("toolRequests")
        if requests is None:
            return
        if not isinstance(requests, list):
            _unsupported("a retained Copilot assistant message has unsupported tool requests")
        self._add_tool_requests(record, turn_id, requests)

    def _add_tool_requests(
        self,
        record: JsonObject,
        turn_id: str,
        requests: list[JsonValue],
    ) -> None:
        for request in requests:
            if not isinstance(request, dict):
                _unsupported("a retained Copilot assistant message has unsupported tool requests")
            self._add_tool_request(record, turn_id, request)

    def _add_tool_request(
        self,
        record: JsonObject,
        turn_id: str,
        request: JsonObject,
    ) -> None:
        tool_call_id = _required_data_string(request, "toolCallId", "tool call identity is missing")
        if tool_call_id in self.tools:
            _unsupported("the retained Copilot context repeats a tool call identity")
        name = _required_data_string(request, "name", "tool call name is missing")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            _unsupported("a retained Copilot tool call has unsupported arguments")
        self.records.append(
            {
                "type": "tool-call",
                "id": tool_call_id,
                "timestamp": _record_timestamp(record),
                "name": name,
                "arguments": arguments,
            }
        )
        self.tools[tool_call_id] = _ToolSequence(
            turn_id=turn_id,
            name=name,
            arguments=arguments,
        )

    def _start_tool(self, record: JsonObject) -> None:
        data = _object(record.get("data"))
        tool_call_id = _required_data_string(data, "toolCallId", "tool start identity is missing")
        tool = self.tools.get(tool_call_id)
        if tool is None or tool.started:
            _unsupported("the retained Copilot context has an invalid tool start")
        if _required_data_string(data, "turnId", "tool turn identity is missing") != tool.turn_id:
            _unsupported("the retained Copilot tool sequence crosses assistant turns")
        if (
            _required_data_string(data, "toolName", "tool start name is missing") != tool.name
            or data.get("arguments") != tool.arguments
        ):
            _unsupported("the retained Copilot tool start does not match its request")
        tool.started = True

    def _complete_tool(self, record: JsonObject) -> None:
        data = _object(record.get("data"))
        tool_call_id = _required_data_string(data, "toolCallId", "tool result identity is missing")
        tool = self.tools.get(tool_call_id)
        if tool is None or not tool.started or tool.completed:
            _unsupported("the retained Copilot context has an invalid tool result")
        if _required_data_string(data, "turnId", "tool turn identity is missing") != tool.turn_id:
            _unsupported("the retained Copilot tool sequence crosses assistant turns")
        success = data.get("success")
        if success is not True:
            _unsupported("a retained Copilot tool result uses an unsupported failure shape")
        result = data.get("result")
        if not isinstance(result, dict) or set(result) != {"content"}:
            _unsupported("a retained Copilot tool result uses an unsupported content shape")
        content = _required_content(result.get("content"), "tool result")
        self.records.append(
            {
                "type": "tool-result",
                "id": _record_id(record),
                "timestamp": _record_timestamp(record),
                "toolCallId": tool_call_id,
                "success": True,
                "content": content,
            }
        )
        tool.completed = True


def materialize_copilot_attempt(attempt: PreparedAttempt) -> CopilotMaterialization:
    """Re-derive one selected Copilot attempt as semantic session input."""

    _validate_compatible_attempt(attempt)
    _validate_preserved_evidence(attempt)
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


def _validate_preserved_evidence(attempt: PreparedAttempt) -> None:
    try:
        validate_extracted_case(attempt.evidence)
    except CaseFormatError as error:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.CHANGED_EVIDENCE,
            "the preserved Copilot evidence no longer matches the case",
        ) from error


def _source_session(attempt: PreparedAttempt) -> NativeSession:
    evidence = attempt.evidence
    decision = evidence.decision
    if decision is None:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case has no selected Copilot decision",
        )
    source = next(
        (
            artifact
            for artifact in evidence.source_artifacts
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
    evidence = attempt.evidence
    decision = evidence.decision
    if decision is None:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case has no selected Copilot decision",
        )
    records = session.records
    boundary = decision.selector.context_before_line
    trigger_record, trigger = _decision_trigger(records, boundary, decision.selector.turn_id)

    retained = frozenset(attempt.candidate.retained_context_ids)
    selected = _selected_context(evidence.context.nodes, records, boundary, retained)
    if evidence.context.nodes[-1].id not in retained or selected[-1][1] is not trigger_record:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.MISSING_TRIGGER,
            "the candidate removes the user message that triggered the selected decision",
        )

    semantic_session = _semantic_history(selected)
    return CopilotMaterialization(semantic_session=semantic_session, trigger=trigger)


def _decision_trigger(
    records: tuple[JsonObject, ...],
    boundary: int,
    turn_id: str,
) -> tuple[JsonObject, str]:
    if boundary < 3 or boundary > len(records):
        _ambiguous("the selected Copilot decision has an invalid context boundary")
    turn_start = records[boundary - 1]
    if (
        turn_start.get("type") != "assistant.turn_start"
        or _object(turn_start.get("data")).get("turnId") != turn_id
    ):
        _ambiguous("the selected Copilot decision has no matching assistant turn boundary")
    trigger = records[boundary - 2]
    if trigger.get("type") != "user.message":
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.MISSING_TRIGGER,
            "the selected Copilot decision is not directly triggered by a user message",
        )
    trigger_data = _object(trigger.get("data"))
    if trigger_data.get("turnId") != turn_id:
        _ambiguous("the selected Copilot trigger and assistant turn do not match")
    return trigger, _required_trigger(trigger_data.get("content"))


def _semantic_history(selected: list[tuple[str, JsonObject]]) -> bytes:
    _, session_start = selected[0]
    if session_start.get("type") != "session.start":
        _ambiguous("the Copilot context does not begin with its session record")
    builder = _SemanticBuilder(records=[_semantic_header(session_start)])
    for _, record in selected[1:-1]:
        builder.add(record)
    return _encode_semantic_session(builder.finish())


def _selected_context(
    nodes: tuple[ContextNode, ...],
    records: tuple[JsonObject, ...],
    boundary: int,
    retained: frozenset[str],
) -> list[tuple[str, JsonObject]]:
    if len(nodes) != boundary - 1:
        _ambiguous("the Copilot context graph does not match the selected boundary")
    selected: list[tuple[str, JsonObject]] = []
    for line, node in enumerate(nodes, start=1):
        record = records[line - 1]
        _validate_context_node(node, record, line)
        if node.id in retained:
            selected.append((node.id, record))
    if not selected:
        _ambiguous("the Copilot candidate has no retained session context")
    return selected


def _validate_context_node(node: ContextNode, record: JsonObject, line: int) -> None:
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


def _semantic_header(record: JsonObject) -> JsonObject:
    data = _object(record.get("data"))
    return {
        "type": "session",
        "version": 1,
        "externalId": _required_data_string(data, "sessionId", "session identity is missing"),
        "createdAt": _required_data_string(data, "startTime", "session start time is missing"),
    }


def _semantic_message(record: JsonObject, *, role: str, content: str) -> JsonObject:
    return {
        "type": "message",
        "id": _record_id(record),
        "timestamp": _record_timestamp(record),
        "role": role,
        "content": [{"type": "text", "text": content}],
    }


def _encode_semantic_session(records: tuple[JsonObject, ...]) -> bytes:
    encoded = b"".join(
        json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )
    if len(encoded) > _MAX_SEMANTIC_SESSION_BYTES:
        _unsupported("the reconstructed Copilot semantic session exceeds the byte limit")
    return encoded


def _record_id(record: JsonObject) -> str:
    return _required_native_string(record.get("id"))


def _record_timestamp(record: JsonObject) -> str:
    return _required_native_string(record.get("timestamp"))


def _record_turn_id(record: JsonObject) -> str:
    return _required_data_string(
        _object(record.get("data")),
        "turnId",
        "assistant turn identity is missing",
    )


def _required_data_string(document: JsonObject, field_name: str, message: str) -> str:
    value = document.get(field_name)
    if not isinstance(value, str) or not value:
        _unsupported(message)
    return value


def _required_native_string(value: JsonValue | None) -> str:
    if not isinstance(value, str) or not value:
        _ambiguous("a Copilot source record has an invalid identity")
    return value


def _required_content(value: JsonValue | None, kind: str) -> str:
    if not isinstance(value, str):
        _unsupported(f"a retained Copilot {kind} has unsupported content")
    return value


def _historical_user_content(data: JsonObject) -> str:
    if "transformedContent" in data:
        return _required_content(data.get("transformedContent"), "transformed user message")
    return _required_content(data.get("content"), "message")


def _required_trigger(value: JsonValue | None) -> str:
    if not isinstance(value, str) or not value:
        raise CopilotMaterializationError(
            CopilotMaterializationErrorCode.MISSING_TRIGGER,
            "the selected Copilot decision has no supported user trigger",
        )
    return value


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _unsupported(message: str) -> Never:
    raise CopilotMaterializationError(
        CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        message,
    )


def _ambiguous(message: str) -> Never:
    raise CopilotMaterializationError(
        CopilotMaterializationErrorCode.AMBIGUOUS_BOUNDARY,
        message,
    )
