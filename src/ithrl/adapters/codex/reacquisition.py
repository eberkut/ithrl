"""Project recorded Codex calls across explicit compaction without reconstructing them."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, cast

from ithrl.adapters._jsonl import JsonlDocument
from ithrl.adapters.codex.parser import (
    PARSER,
    _required_string,
    _session_metadata,
    _validate_envelopes,
    _validate_response_items,
)
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.adapters.reacquisition import (
    Anchor,
    Boundary,
    CapturedResult,
    Operation,
    PassiveProjection,
)
from ithrl.context import JsonObject, content_digest

_CALLS = {"function_call": "arguments", "custom_tool_call": "input"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output"}
_KNOWN_RECORDS = {"session_meta", "turn_context", "response_item", "compacted"}
_STARTS = {
    "compaction_start",
    "compaction_started",
    "context_compaction_start",
    "session.compaction_start",
}
_COMPLETES = {
    "compaction_complete",
    "compaction_completed",
    "context_compaction_complete",
    "context_compacted",
    "session.compaction_complete",
}


def project_reacquisition(document: JsonlDocument) -> PassiveProjection:
    """Validate native identities and retain only top-level call/result evidence.

    Replacement history inside a compaction is a snapshot, never a new retrieval.
    Existing experimental parsing and its active compaction policy are untouched.
    """

    if not PARSER.matches(document):
        raise AdapterError(AdapterErrorCode.UNSUPPORTED_FORMAT, "source is not a Codex rollout")
    records = _validate_envelopes(document.records)
    metadata = _session_metadata(records)
    if sum(record.type == "session_meta" for record in records) != 1:
        raise AdapterError(AdapterErrorCode.INVALID_SESSION, "Codex metadata must be unique")
    _validate_response_items(records)
    boundaries, barriers = _compactions(document.records)
    operations: dict[str, Operation] = {}
    workspace = _workspace(records[0].payload)
    unknown = 0
    for record, raw in zip(records, document.records, strict=True):
        anchor = Anchor(record.line, content_digest(raw))
        payload = record.payload
        if record.type == "turn_context":
            workspace = _workspace(payload)
        elif record.type == "compacted" or _compaction_transition(record.type, payload):
            continue
        elif record.type == "response_item":
            item_type = _required_string(payload, "type", record.line, "response item type")
            if item_type in _CALLS:
                call_id = _required_string(payload, "call_id", record.line, "tool call identifier")
                tool = _required_string(payload, "name", record.line, "tool name")
                argument_key = _CALLS[item_type]
                if not isinstance(payload.get(argument_key), str):
                    raise AdapterError(
                        AdapterErrorCode.INVALID_SESSION,
                        "Codex tool call arguments must be a captured string",
                        line=record.line,
                    )
                operations[call_id] = Operation(
                    call=anchor,
                    completion=None,
                    tool=tool,
                    arguments={
                        "type": item_type,
                        "namespace": payload.get("namespace"),
                        argument_key: payload[argument_key],
                    },
                    status="incomplete",
                    workspace_digest=workspace,
                )
            elif item_type in _OUTPUTS:
                call_id = _required_string(payload, "call_id", record.line, "tool call identifier")
                operation = operations[call_id]
                call = cast(JsonObject, document.records[operation.call.line - 1]["payload"])
                status = _status(call, payload)
                results = ()
                if "output" in payload:
                    results = (
                        CapturedResult(
                            "output",
                            payload["output"],
                            _completeness(payload),
                        ),
                    )
                operations[call_id] = replace(
                    operation, completion=anchor, status=status, results=results
                )
            elif item_type not in {"message", "reasoning"}:
                unknown += 1
        elif record.type not in _KNOWN_RECORDS:
            unknown += 1
    return PassiveProjection(
        source=document.source,
        harness="codex",
        producer_version=metadata.format.producer_version,
        record_count=len(records),
        boundaries=tuple(boundaries),
        operations=tuple(operations.values()),
        unknown_records=unknown,
        barriers=tuple(barriers),
        limitations=(
            "native-output-completeness-unverified",
            "resource-state-unknown",
            "usage-not-attributable",
            *(("unverified-compaction-transitions",) if barriers else ()),
        ),
    )


def _workspace(payload: JsonObject) -> str | None:
    value = payload.get("cwd")
    return content_digest(value) if isinstance(value, str) and value else None


def _successful_compaction(payload: JsonObject) -> bool:
    """Recognize the captured summary/history record, not an attempted compact command."""

    history = payload.get("replacement_history")
    return (
        isinstance(payload.get("message"), str)
        and isinstance(history, list)
        and bool(payload.get("message") or history)
        and all(isinstance(item, dict) for item in history)
        and payload.get("status") in (None, "completed", "complete")
        and _successful_flag(payload)
        and payload.get("error") in (None, False)
    )


def _compaction_transition(record_type: str, payload: JsonObject) -> str | None:
    event_type = payload.get("type") if record_type == "event_msg" else record_type
    if not isinstance(event_type, str):
        return None
    if event_type in _STARTS:
        return "start"
    if event_type in _COMPLETES:
        return "complete"
    if event_type.startswith(("compaction_", "context_compaction", "session.compaction_")):
        return "unknown"
    return None


def _compactions(records: tuple[JsonObject, ...]) -> tuple[list[Boundary], list[Anchor]]:
    boundaries: list[Boundary] = []
    barriers: list[Anchor] = []
    pending: Anchor | None = None
    terminal_candidate: int | None = None
    for line, raw in enumerate(records, 1):
        kind = cast(str, raw["type"])
        payload = cast(JsonObject, raw["payload"])
        transition = _compaction_transition(kind, payload)
        if kind != "compacted" and transition is None:
            if kind == "response_item" and payload.get("type") in (*_CALLS, *_OUTPUTS):
                terminal_candidate = None
            continue
        anchor = Anchor(line, content_digest(raw))
        if transition == "start":
            if pending is not None:
                barriers.append(pending)
            pending = anchor
            terminal_candidate = None
        elif kind == "compacted":
            if _successful_compaction(payload):
                boundaries.append(
                    Boundary(f"boundary-{anchor.line}-{anchor.digest}", pending or anchor, anchor)
                )
                terminal_candidate = len(boundaries) - 1
            else:
                barriers.extend((pending, anchor) if pending is not None else (anchor,))
                terminal_candidate = None
            pending = None
        elif (
            transition == "complete"
            and _successful_flag(payload)
            and payload.get("status") in (None, "complete", "completed")
            and payload.get("error") in (None, False)
        ):
            if terminal_candidate is not None:
                boundary = boundaries[terminal_candidate]
                boundaries[terminal_candidate] = replace(boundary, end=anchor)
            else:
                barriers.extend((pending, anchor) if pending is not None else (anchor,))
            pending = None
            terminal_candidate = None
        else:
            if terminal_candidate is not None:
                boundary = boundaries.pop(terminal_candidate)
                barriers.extend((boundary.start, boundary.end))
            barriers.extend((pending, anchor) if pending is not None else (anchor,))
            pending = None
            terminal_candidate = None
    if pending is not None:
        barriers.append(pending)
    return boundaries, barriers


def _status(call: JsonObject, output: JsonObject) -> Literal["completed", "failed", "incomplete"]:
    if call.get("status") in ("failed", "cancelled") or output.get("status") in (
        "failed",
        "cancelled",
    ):
        return "failed"
    if output.get("is_error") is True or output.get("error") not in (None, False):
        return "failed"
    if call.get("name") in {
        "exec_command",
        "write_stdin",
        "functions.exec_command",
        "functions.write_stdin",
    }:
        native_status = _exec_status(output.get("output"))
        if native_status != "completed":
            return native_status or "incomplete"
    if (
        call.get("status") not in (None, "completed", "complete")
        or output.get("status") not in (None, "completed", "complete")
        or "output" not in output
    ):
        return "incomplete"
    return "completed"


def _successful_flag(payload: JsonObject) -> bool:
    return "success" not in payload or payload["success"] is True


def _completeness(payload: JsonObject) -> Literal["captured", "truncated", "unknown"]:
    output = payload.get("output")
    if _has_truncation_warning(output):
        return "truncated"
    containers = (payload, output) if isinstance(output, dict) else (payload,)
    flags = [container["truncated"] for container in containers if "truncated" in container]
    if any(flag is True for flag in flags):
        return "truncated"
    return "unknown" if any(type(flag) is not bool for flag in flags) else "captured"


def _captured_text(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(
            item["text"]
            for item in value
            if isinstance(item, dict)
            and item.get("type") in ("input_text", "text")
            and isinstance(item.get("text"), str)
        )
    return ()


def _exec_status(value: object) -> Literal["completed", "failed", "incomplete"] | None:
    """Read only the native exec header, never status-looking command output."""
    # Native format owner: openai/codex rust-v0.153.4,
    # codex-rs/core/src/tools/context.rs, ExecCommandToolOutput::response_header.
    texts = _captured_text(value)
    if not texts:
        return None
    positions = (texts[0].find("\nOutput:"), texts[0].find("\nFinal output:"))
    end = min((position for position in positions if position >= 0), default=-1)
    if end < 0:
        return None
    lines = texts[0][:end].split("\n")
    if lines[0].startswith("Chunk ID: "):
        lines = lines[1:]
    if not lines or not lines[0].startswith("Wall time: ") or not lines[0].endswith(" seconds"):
        return None
    if len(lines) not in (2, 3) or not lines[1].startswith("Process exited with code "):
        return "incomplete"
    if len(lines) == 3:
        count = lines[2].removeprefix("Original token count: ")
        if (
            not lines[2].startswith("Original token count: ")
            or not count.isascii()
            or not count.isdecimal()
        ):
            return "incomplete"
    code = lines[1].removeprefix("Process exited with code ")
    digits = code.removeprefix("-")
    if not digits or not digits.isascii() or not digits.isdecimal():
        return "incomplete"
    return "failed" if digits.strip("0") else "completed"


def _has_truncation_warning(value: object) -> bool:
    for text in _captured_text(value):
        for line in text.split("\n"):
            prefix = "Warning: truncated output (original token count: "
            if line.startswith(prefix) and line.endswith(")"):
                count = line[len(prefix) : -1]
                if count.isascii() and count.isdecimal():
                    return True
    return False
