"""Compile editable context graphs from parsed Codex CLI sessions."""

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
from ithrl.mutation import EditableText, editable_message_text

_HARNESS = "codex"
_SOURCE = "source/rollout.jsonl"
_CALL_TYPES = frozenset({"custom_tool_call", "function_call"})
_OUTPUT_TYPES = frozenset({"custom_tool_call_output", "function_call_output"})
_STRUCTURAL_RECORD_TYPES = frozenset({"session_meta", "turn_context", "world_state"})
_MODEL_INPUT_TYPES = frozenset({"message", "custom_tool_call_output", "function_call_output"})
_OPAQUE_RECORD_TYPES = frozenset(
    {
        "inter_agent_communication_metadata",
        "token_usage_record",
        "world_state",
    }
)


def compile_context(session: NativeSession, selector: DecisionSelector) -> ContextGraph:
    """Build context preceding one selector produced from the parsed session."""

    records = session.records
    _validate_selector(session, selector, records)
    context_count = selector.context_before_line - 1
    if context_count > MAX_CONTEXT_NODES:
        raise AdapterError(
            AdapterErrorCode.CONTEXT_TOO_LARGE,
            "selected decision has too many preceding context records",
        )
    return ContextGraph(nodes=_context_nodes(records[:context_count]))


def editable_text(
    session: NativeSession, selector: DecisionSelector, context: ContextGraph
) -> tuple[EditableText, ...]:
    """Discover ordinary prior-user text while preserving the selected turn input."""

    records = session.records
    trigger_id = _trigger_node_id(context.nodes, records, selector.context_before_line)
    return tuple(
        site
        for node in context.nodes
        if node.id != trigger_id and node.selector is not None
        for record in (records[node.selector.line - 1],)
        if record.get("type") == "response_item"
        for site in editable_message_text(node, _object(record.get("payload")))
    )


def _trigger_node_id(
    nodes: tuple[ContextNode, ...], records: tuple[JsonObject, ...], boundary: int
) -> str:
    """Identify the last native model input before the selected decision boundary."""

    for node in reversed(nodes):
        selector = node.selector
        if selector is None or selector.line >= boundary:
            continue
        record = records[selector.line - 1]
        if record.get("type") != "response_item":
            continue
        payload = _object(record.get("payload"))
        item_type = _string(payload.get("type"))
        if item_type not in _MODEL_INPUT_TYPES:
            continue
        if item_type != "message" or payload.get("role") in {"user", "developer", "system"}:
            return node.id
    raise AdapterError(
        AdapterErrorCode.INVALID_SESSION,
        "the selected Codex decision has no supported triggering input",
    )


def source_selector(index: int, record: JsonObject) -> SourceRecordSelector:
    """Return the stable native selector for one Codex JSONL record."""

    ordinal = record.get("ordinal")
    if type(ordinal) is int:
        return SourceRecordSelector(
            line=index + 1,
            identity_key="ordinal",
            identity_value=ordinal,
        )
    return SourceRecordSelector(line=index + 1)


def opaque_prefix(records: tuple[JsonObject, ...]) -> tuple[bool, ...]:
    """Track whether each source prefix contains frozen opaque context."""

    prefixes = [False]
    for record in records:
        prefixes.append(prefixes[-1] or _is_opaque_record(record))
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
    calls: dict[str, str] = {}
    for index, record in enumerate(records):
        record_type = cast(str, record["type"])
        payload = _object(record.get("payload"))
        kind = _context_kind(record_type, payload)
        digest = content_digest(record)
        selector = source_selector(index, record)
        node_id = stable_source_context_id(
            source=_SOURCE,
            selector=selector,
            kind=kind,
            digest=digest,
        )
        nodes.append(
            ContextNode(
                id=node_id,
                kind=kind,
                selector=selector,
                mutable=_is_mutable(record_type, payload),
                protected=_is_protected(record_type, payload),
                depends_on=_dependencies(record_type, payload, calls),
                source=_SOURCE,
                digest=digest,
            )
        )
        _remember_call(record_type, payload, node_id, calls)
    return tuple(nodes)


def _context_kind(record_type: str, payload: JsonObject) -> str:
    if record_type == "response_item" and (item_type := _string(payload.get("type"))):
        return f"response_item.{item_type}"
    if record_type == "event_msg" and (event_type := _string(payload.get("type"))):
        return f"event_msg.{event_type}"
    return record_type


def _dependencies(
    record_type: str,
    payload: JsonObject,
    calls: dict[str, str],
) -> tuple[str, ...]:
    item_type = _string(payload.get("type"))
    call_id = _string(payload.get("call_id"))
    if record_type == "response_item" and item_type in _OUTPUT_TYPES and call_id in calls:
        return (calls[call_id],)
    return ()


def _remember_call(
    record_type: str,
    payload: JsonObject,
    node_id: str,
    calls: dict[str, str],
) -> None:
    item_type = _string(payload.get("type"))
    call_id = _string(payload.get("call_id"))
    if record_type == "response_item" and item_type in _CALL_TYPES and call_id is not None:
        calls[call_id] = node_id


def _is_mutable(record_type: str, payload: JsonObject) -> bool:
    if record_type != "response_item":
        return False
    item_type = _string(payload.get("type"))
    if item_type == "message":
        return payload.get("role") in {"user", "assistant"}
    return item_type in _CALL_TYPES or item_type in _OUTPUT_TYPES


def _is_protected(record_type: str, payload: JsonObject) -> bool:
    if record_type in _STRUCTURAL_RECORD_TYPES:
        return True
    return record_type == "event_msg" and payload.get("type") in {
        "task_started",
        "task_complete",
    }


def _is_opaque(kind: str) -> bool:
    if kind in _OPAQUE_RECORD_TYPES:
        return True
    if kind.startswith("event_msg."):
        return kind not in {"event_msg.task_started", "event_msg.task_complete"}
    if kind.startswith("response_item."):
        return kind.removeprefix("response_item.") not in {
            "custom_tool_call",
            "custom_tool_call_output",
            "function_call",
            "function_call_output",
            "message",
        }
    return kind not in {"session_meta", "turn_context"}


def _is_opaque_record(record: JsonObject) -> bool:
    record_type = cast(str, record["type"])
    return _is_opaque(_context_kind(record_type, _object(record.get("payload"))))


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None
