"""Compile editable context graphs from parsed GitHub Copilot CLI sessions."""

from __future__ import annotations

from typing import cast

from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import (
    MAX_CONTEXT_NODES,
    ContextGraph,
    ContextNode,
    JsonObject,
    JsonValue,
    SourceRecordSelector,
    content_digest,
    stable_source_context_id,
)
from ithrl.decisions import DecisionSelector

_HARNESS = "copilot"
_SOURCE = "source/events.jsonl"


def compile_context(session: NativeSession, selector: DecisionSelector) -> ContextGraph:
    """Build context preceding one selector produced from this session."""

    records = session.records
    _validate_selector(session, selector, records)
    context_count = selector.context_before_line - 1
    if context_count > MAX_CONTEXT_NODES:
        raise AdapterError(
            AdapterErrorCode.CONTEXT_TOO_LARGE,
            "selected decision has too many preceding context records",
        )
    return ContextGraph(nodes=_context_nodes(records[:context_count]))


def source_selector(index: int, record: JsonObject) -> SourceRecordSelector:
    """Return the stable native selector for one Copilot JSONL record."""

    return SourceRecordSelector(
        line=index + 1,
        identity_key="id",
        identity_value=cast(str, record["id"]),
    )


def opaque_prefix(records: tuple[JsonObject, ...]) -> tuple[bool, ...]:
    """Track whether each source prefix contains frozen opaque context."""

    prefixes = [False]
    for record in records:
        record_type = cast(str, record["type"])
        prefixes.append(prefixes[-1] or _is_opaque(record_type))
    return tuple(prefixes)


def _validate_selector(
    session: NativeSession,
    selector: DecisionSelector,
    records: tuple[JsonObject, ...],
) -> None:
    index = selector.record.line - 1
    if (
        selector.harness != _HARNESS
        or selector.session_id != session.metadata.session_id
        or selector.source != _SOURCE
        or index >= session.record_count
    ):
        raise AdapterError(
            AdapterErrorCode.DECISION_NOT_FOUND,
            "decision selector does not match the selected session",
        )
    record = records[index]
    if selector.record != source_selector(
        index, record
    ) or selector.record_digest != content_digest(record):
        raise AdapterError(
            AdapterErrorCode.DECISION_NOT_FOUND,
            "decision selector does not match the selected session",
        )


def _context_nodes(records: tuple[JsonObject, ...]) -> tuple[ContextNode, ...]:
    nodes: list[ContextNode] = []
    tool_requests: dict[str, str] = {}
    tool_starts: dict[str, str] = {}

    for index, record in enumerate(records):
        record_type = cast(str, record["type"])
        digest = content_digest(record)
        selector = source_selector(index, record)
        node_id = stable_source_context_id(
            source=_SOURCE,
            selector=selector,
            kind=record_type,
            digest=digest,
        )
        nodes.append(
            ContextNode(
                id=node_id,
                kind=record_type,
                selector=selector,
                mutable=_is_mutable(record_type),
                protected=_is_protected(record_type),
                depends_on=_dependencies(record_type, record, tool_requests, tool_starts),
                source=_SOURCE,
                digest=digest,
            )
        )
        _remember_tool_relationship(record_type, record, node_id, tool_requests, tool_starts)

    return tuple(nodes)


def _dependencies(
    record_type: str,
    record: JsonObject,
    requests: dict[str, str],
    starts: dict[str, str],
) -> tuple[str, ...]:
    tool_call_id = _string(_object(record.get("data")).get("toolCallId"))
    if tool_call_id is None:
        return ()
    if record_type == "tool.execution_start" and tool_call_id in requests:
        return (requests[tool_call_id],)
    if record_type == "tool.execution_complete" and tool_call_id in starts:
        return (starts[tool_call_id],)
    return ()


def _remember_tool_relationship(
    record_type: str,
    record: JsonObject,
    node_id: str,
    requests: dict[str, str],
    starts: dict[str, str],
) -> None:
    data = _object(record.get("data"))
    if record_type == "assistant.message":
        tool_requests = data.get("toolRequests")
        if not isinstance(tool_requests, list):
            return
        for request in tool_requests:
            if isinstance(request, dict) and (tool_call_id := _string(request.get("toolCallId"))):
                requests[tool_call_id] = node_id
    elif record_type == "tool.execution_start":
        if tool_call_id := _string(data.get("toolCallId")):
            starts[tool_call_id] = node_id


def _is_mutable(record_type: str) -> bool:
    return record_type in {
        "assistant.message",
        "model.message",
        "skill.invoked",
        "tool.execution_complete",
        "tool.execution_start",
        "user.message",
    }


def _is_protected(record_type: str) -> bool:
    return record_type in {
        "assistant.turn_end",
        "assistant.turn_start",
        "session.start",
        "session.task_complete",
    }


def _is_opaque(record_type: str) -> bool:
    return not _is_mutable(record_type) and not _is_protected(record_type)


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None
