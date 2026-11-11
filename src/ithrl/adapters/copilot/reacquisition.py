"""Project recorded Copilot compaction and tool evidence without a native runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ithrl.adapters._jsonl import JsonlDocument
from ithrl.adapters.copilot.parser import (
    PARSER,
    _invalid,
    _parse_metadata,
    _Record,
    _required_string,
    _validate_envelopes,
)
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.adapters.reacquisition import (
    Anchor,
    Boundary,
    CapturedResult,
    Operation,
    PassiveProjection,
)
from ithrl.context import JsonValue, content_digest


@dataclass(slots=True)
class _Call:
    request: Anchor
    tool: str = field(repr=False)
    arguments: JsonValue = field(repr=False)
    turn_id: str | None = field(repr=False)
    started: bool = False
    completion: Anchor | None = None
    results: tuple[CapturedResult, ...] = ()
    status: Literal["completed", "failed", "incomplete"] = "incomplete"


def project_reacquisition(document: JsonlDocument) -> PassiveProjection:
    """Validate the passive profile; missing turn IDs never replace native call IDs.

    This profile uses the same envelopes and metadata as experimental parsing but
    validates tool request/start/completion identities independently of turn nesting.
    Known turn IDs must agree. Experimental parsing remains stricter and unchanged.
    """

    if not PARSER.matches(document):
        raise AdapterError(AdapterErrorCode.UNSUPPORTED_FORMAT, "source is not a Copilot event log")
    records = _validate_envelopes(document.records)
    metadata = _parse_metadata(records)
    calls: dict[str, _Call] = {}
    boundaries: list[Boundary] = []
    barriers: list[Anchor] = []
    pending_compactions: dict[str, Anchor] = {}
    seen_compactions: set[str] = set()
    limitations: set[str] = {
        "tool-relations-validated-independently-of-turn-nesting",
        "captured-output-completeness-unverified",
    }
    context = records[0].data.get("context")
    cwd = context.get("cwd") if isinstance(context, dict) else None
    workspace_digest = (
        content_digest(cwd)
        if isinstance(cwd, str) and cwd.startswith("/") and "\x00" not in cwd
        else None
    )
    limitations.add(
        "workspace-identity-from-session-start-only"
        if workspace_digest is not None
        else "workspace-identity-unavailable"
    )
    unknown_records = 0
    for record in records:
        anchor = Anchor(line=record.line, digest=content_digest(document.records[record.line - 1]))
        if record.record_type == "assistant.message":
            _requests(record, anchor, calls, limitations)
        elif record.record_type == "tool.execution_start":
            call = _related_call(record, calls, limitations)
            if call.started or call.completion is not None:
                _invalid("tool execution starts more than once", record.line)
            tool = _required_string(
                record.data.get("toolName"), record.line, "tool name is missing"
            )
            if "arguments" not in record.data:
                _invalid("tool arguments are missing", record.line)
            if tool != call.tool or content_digest(record.data["arguments"]) != content_digest(
                call.arguments
            ):
                _invalid("tool start differs from its assistant request", record.line)
            call.started = True
        elif record.record_type == "tool.execution_complete":
            call = _related_call(record, calls, limitations)
            if not call.started or call.completion is not None:
                _invalid("tool completion requires one unmatched execution start", record.line)
            success = record.data.get("success")
            if type(success) is not bool:
                _invalid("tool completion success must be a boolean", record.line)
            call.completion = anchor
            call.status = "completed" if success else "failed"
            call.results = _results(record)
        elif record.record_type.startswith("session.compaction_"):
            _compaction(
                record,
                anchor,
                pending_compactions,
                seen_compactions,
                boundaries,
                barriers,
                limitations,
            )
        elif record.record_type not in _KNOWN_OPAQUE_RECORDS:
            unknown_records += 1
    if pending_compactions:
        barriers.extend(pending_compactions.values())
        limitations.add("incomplete-compaction-attempts")
    operations = tuple(
        Operation(
            call=call.request,
            completion=call.completion,
            tool=call.tool,
            arguments=call.arguments,
            results=call.results,
            status=call.status,
            category=_category(call, workspace_digest),
            workspace_digest=workspace_digest,
        )
        for call in calls.values()
    )
    return PassiveProjection(
        source=document.source,
        harness="copilot",
        producer_version=metadata.format.producer_version,
        record_count=len(records),
        boundaries=tuple(boundaries),
        operations=operations,
        barriers=tuple(sorted(barriers, key=lambda item: item.line)),
        unknown_records=unknown_records,
        limitations=tuple(sorted(limitations)),
    )


def _category(call: _Call, workspace_digest: str | None) -> Literal["file-read", "unclassified"]:
    path = call.arguments.get("path") if isinstance(call.arguments, dict) else None
    view_range = call.arguments.get("view_range") if isinstance(call.arguments, dict) else None
    if view_range is not None and (
        not isinstance(view_range, list)
        or len(view_range) != 2
        or type(view_range[0]) is not int
        or type(view_range[1]) is not int
        or view_range[0] < 1
        or (view_range[1] != -1 and view_range[1] < view_range[0])
    ):
        return "unclassified"
    if (
        call.tool == "view"
        and isinstance(path, str)
        and path
        and "\x00" not in path
        and (path.startswith("/") or workspace_digest is not None)
    ):
        return "file-read"
    return "unclassified"


def _optional_turn(record: _Record, limitations: set[str]) -> str | None:
    turn = record.data.get("turnId")
    if turn is None:
        limitations.add("missing-turn-ids-native-call-identities-used")
        return None
    return _required_string(turn, record.line, "turn ID must be a non-empty string")


def _requests(
    record: _Record, anchor: Anchor, calls: dict[str, _Call], limitations: set[str]
) -> None:
    requests = record.data.get("toolRequests")
    if requests is None:
        return
    if not isinstance(requests, list):
        _invalid("assistant tool requests must be an array", record.line)
    if not requests:
        return
    turn = _optional_turn(record, limitations)
    for request in requests:
        if not isinstance(request, dict):
            _invalid("assistant tool request must be an object", record.line)
        call_id = _required_string(
            request.get("toolCallId"), record.line, "tool call ID is missing"
        )
        tool = _required_string(request.get("name"), record.line, "tool name is missing")
        if call_id in calls:
            _invalid("assistant tool call IDs must be unique", record.line)
        if "arguments" not in request:
            _invalid("assistant tool arguments are missing", record.line)
        calls[call_id] = _Call(anchor, tool, request["arguments"], turn)


def _related_call(record: _Record, calls: dict[str, _Call], limitations: set[str]) -> _Call:
    call_id = _required_string(
        record.data.get("toolCallId"), record.line, "tool call ID is missing"
    )
    call = calls.get(call_id)
    if call is None:
        _invalid("tool execution requires its preceding assistant request", record.line)
    turn = _optional_turn(record, limitations)
    if turn is not None and call.turn_id is not None and turn != call.turn_id:
        _invalid("tool execution and assistant request use different turns", record.line)
    if call.turn_id is None:
        call.turn_id = turn
    return call


def _results(record: _Record) -> tuple[CapturedResult, ...]:
    data = record.data
    result = data.get("result")
    if not isinstance(result, dict):
        return ()
    for container in (data, result):
        if "truncated" in container and type(container["truncated"]) is not bool:
            _invalid("tool result truncated flag must be a boolean", record.line)
    truncated = data.get("truncated") is True or result.get("truncated") is True
    return tuple(
        CapturedResult(
            scope=scope, value=result[scope], completeness="truncated" if truncated else "captured"
        )
        for scope in ("content", "detailedContent")
        if isinstance(result.get(scope), str)
    )


def _compaction(
    record: _Record,
    anchor: Anchor,
    pending: dict[str, Anchor],
    seen: set[str],
    boundaries: list[Boundary],
    barriers: list[Anchor],
    limitations: set[str],
) -> None:
    attempt = record.data.get("compactionAttemptId")
    if not isinstance(attempt, str) or not attempt:
        barriers.append(anchor)
        limitations.add("compaction-without-attempt-identity")
        return
    if record.record_type == "session.compaction_start":
        if attempt in seen:
            _invalid("compaction attempt IDs must be unique", record.line)
        seen.add(attempt)
        if pending:
            _invalid("overlapping compaction attempts are unsupported", record.line)
        pending[attempt] = anchor
        return
    start = pending.pop(attempt, None)
    if record.record_type != "session.compaction_complete" or start is None:
        barriers.append(anchor)
        if start is not None:
            barriers.append(start)
        limitations.add("unpaired-or-unknown-compaction-events")
        return
    if record.data.get("success") is True and record.data.get("outcome") in (None, "applied"):
        boundaries.append(
            Boundary(id=f"boundary-{anchor.line}-{anchor.digest}", start=start, end=anchor)
        )
    else:
        barriers.extend((start, anchor))
        limitations.add("failed-or-unapplied-compaction-attempts")


# These records are preserved by the JSONL snapshot but never interpreted as new tool calls.
_KNOWN_OPAQUE_RECORDS = frozenset(
    {
        "session.start",
        "session.info",
        "session.resume",
        "session.shutdown",
        "session.task_complete",
        "session.model_change",
        "session.error",
        "session.warning",
        "user.message",
        "assistant.turn_start",
        "assistant.turn_end",
        "assistant.usage",
        "model.model_call_start",
        "model.model_call_success",
        "model.model_call_failure",
        "model.message",
        "model.response",
        "model.messages_snapshot",
        "skill.invoked",
    }
)
