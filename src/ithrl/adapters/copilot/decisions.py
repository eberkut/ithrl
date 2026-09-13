"""Enumerate completed decisions from parsed GitHub Copilot CLI sessions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import cast

from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.copilot.context import opaque_prefix, source_selector
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.behavior import (
    BehaviorObservation,
    FinalResponseTarget,
    ShellAction,
    ShellActionTarget,
)
from ithrl.context import JsonObject, JsonValue, content_digest
from ithrl.decisions import (
    MAX_DECISIONS,
    DecisionCaseFacts,
    DecisionDraft,
    DecisionLimitation,
    DecisionSelector,
    TargetCandidate,
)

_ADAPTER = "copilot-session"
_ADAPTER_VERSION = "1"
_HARNESS = "copilot"
_SOURCE = "source/events.jsonl"
_SHELL_TOOL_NAMES = frozenset({"bash", "shell"})
_MAX_BEHAVIOR_BYTES = 64 * 1024
_MAX_SHELL_ACTIONS = 32


@dataclass(frozen=True, slots=True)
class _DecisionRecord:
    index: int
    context_before_line: int
    turn_id: str
    kind: str
    native_id: str
    observation: BehaviorObservation
    targets: tuple[TargetCandidate, ...]
    model: str | None


@dataclass(frozen=True, slots=True)
class _TurnInterval:
    turn_id: str
    start: int
    stop: int


def enumerate_decisions(session: NativeSession) -> tuple[DecisionDraft, ...]:
    """Return supported decisions whose enclosing assistant turn completed."""

    if session.metadata.format.harness != _HARNESS:
        raise ValueError("native session is not a GitHub Copilot CLI session")

    records = session.records
    completed = _completed_decision_records(records)
    opaque_context = opaque_prefix(records)
    return tuple(_build_draft(session, records, opaque_context, decision) for decision in completed)


def _completed_decision_records(
    records: tuple[JsonObject, ...],
) -> tuple[_DecisionRecord, ...]:
    completed_turns = _completed_turns(records)
    starts = {turn.start: turn for turn in completed_turns}
    stops = {turn.stop: turn for turn in completed_turns}
    active_turns: dict[str, list[_TurnInterval]] = {}
    decisions: list[_DecisionRecord] = []
    for index, record in enumerate(records):
        if turn := starts.get(index):
            active_turns.setdefault(turn.turn_id, []).append(turn)
        decision = _decision_from_record(index, record, active_turns)
        if decision is not None:
            if len(decisions) == MAX_DECISIONS:
                raise AdapterError(
                    AdapterErrorCode.TOO_MANY_DECISIONS,
                    "session contains too many completed decisions",
                )
            decisions.append(decision)
        _close_turn(index, stops, active_turns)
    return tuple(decisions)


def _decision_from_record(
    index: int,
    record: JsonObject,
    active_turns: dict[str, list[_TurnInterval]],
) -> _DecisionRecord | None:
    if record.get("type") != "assistant.message":
        return None
    data = _object(record.get("data"))
    turn_id = _bounded_string(data.get("turnId"), 1024)
    turns = active_turns.get(turn_id) if turn_id is not None else None
    turn = turns[-1] if turns else None
    if turn is None:
        return None
    return _assistant_message_decision(index, turn.start + 1, data)


def _assistant_message_decision(
    index: int,
    context_before_line: int,
    data: JsonObject,
) -> _DecisionRecord | None:
    turn_id = _bounded_string(data.get("turnId"), 1024)
    native_id = _bounded_string(data.get("messageId"), 1024)
    if turn_id is None or native_id is None:
        return None

    actions = _shell_actions(data.get("toolRequests"))
    final_response = _final_response(data)
    if not actions and final_response is None:
        return None
    targets = (
        (_target_candidate(FinalResponseTarget(text_equals=final_response), ordinal=1),)
        if final_response is not None
        else _shell_targets(actions)
    )
    return _DecisionRecord(
        index=index,
        context_before_line=context_before_line,
        turn_id=turn_id,
        kind="copilot-assistant-message",
        native_id=native_id,
        observation=BehaviorObservation(
            final_response=final_response,
            shell_actions=actions,
        ),
        targets=targets,
        model=_bounded_string(data.get("model"), 256),
    )


def _final_response(data: JsonObject) -> str | None:
    tool_requests = data.get("toolRequests")
    if (
        data.get("phase") != "final_answer"
        or data.get("parentToolCallId") is not None
        or not isinstance(tool_requests, list)
        or tool_requests
    ):
        return None
    return _behavior_text(data.get("content"))


def _shell_targets(actions: tuple[ShellAction, ...]) -> tuple[TargetCandidate, ...]:
    counts = Counter((action.tool_name, action.command) for action in actions)
    return tuple(
        _target_candidate(
            ShellActionTarget(
                tool_name_equals=tool_name,
                command_equals=command,
                count=count,
            ),
            ordinal=ordinal,
        )
        for ordinal, ((tool_name, command), count) in enumerate(counts.items(), start=1)
    )


def _close_turn(
    index: int,
    stops: dict[int, _TurnInterval],
    active_turns: dict[str, list[_TurnInterval]],
) -> None:
    turn = stops.get(index)
    if turn is None:
        return
    turns = active_turns[turn.turn_id]
    turns.pop()
    if not turns:
        del active_turns[turn.turn_id]


def _completed_turns(records: tuple[JsonObject, ...]) -> tuple[_TurnInterval, ...]:
    open_turns: dict[str, list[int]] = {}
    completed: list[_TurnInterval] = []
    for index, record in enumerate(records):
        record_type = record.get("type")
        if record_type not in {"assistant.turn_start", "assistant.turn_end"}:
            continue
        turn_id = _bounded_string(_object(record.get("data")).get("turnId"), 1024)
        if turn_id is None:
            continue
        if record_type == "assistant.turn_start":
            open_turns.setdefault(turn_id, []).append(index)
        elif starts := open_turns.get(turn_id):
            completed.append(_TurnInterval(turn_id=turn_id, start=starts.pop(), stop=index))
            if not starts:
                del open_turns[turn_id]
    return tuple(completed)


def _shell_actions(value: JsonValue | None) -> tuple[ShellAction, ...]:
    if not isinstance(value, list):
        return ()

    actions: list[ShellAction] = []
    total_bytes = 0
    for request in value:
        if not isinstance(request, dict):
            continue
        name = _bounded_string(request.get("name"), 256)
        arguments = _object(request.get("arguments"))
        command = _behavior_text(arguments.get("command"))
        if name is None or name not in _SHELL_TOOL_NAMES or command is None:
            continue
        total_bytes += len(command.encode("utf-8"))
        if total_bytes > _MAX_BEHAVIOR_BYTES or len(actions) == _MAX_SHELL_ACTIONS:
            return ()
        actions.append(ShellAction(tool_name=name, command=command))
    return tuple(actions)


def _target_candidate(
    target: FinalResponseTarget | ShellActionTarget,
    *,
    ordinal: int,
) -> TargetCandidate:
    return TargetCandidate(
        ordinal=ordinal,
        target=target,
        evidence_support="supported",
        experimental_support="unvalidated",
    )


def _build_draft(
    session: NativeSession,
    records: tuple[JsonObject, ...],
    opaque_prefix: tuple[bool, ...],
    decision: _DecisionRecord,
) -> DecisionDraft:
    record = records[decision.index]
    selector = _selector(session, record, decision)
    return DecisionDraft(
        case=DecisionCaseFacts(
            adapter=_ADAPTER,
            adapter_version=_ADAPTER_VERSION,
            model_requested=decision.model,
            source_schema=session.metadata.format.name,
        ),
        selector=selector,
        observed_at=cast(str, record["timestamp"]),
        observation=decision.observation,
        targets=decision.targets,
        limitations=_limitations(opaque_prefix[decision.context_before_line - 1]),
    )


def _limitations(has_opaque_context: bool) -> tuple[DecisionLimitation, ...]:
    limitations: list[DecisionLimitation] = ["experimental-support-unvalidated"]
    if has_opaque_context:
        limitations.append("opaque-context-frozen")
    return tuple(limitations)


def _selector(
    session: NativeSession,
    record: JsonObject,
    decision: _DecisionRecord,
) -> DecisionSelector:
    return DecisionSelector(
        harness=_HARNESS,
        session_id=session.metadata.session_id,
        turn_id=decision.turn_id,
        kind=decision.kind,
        native_id=decision.native_id,
        source=_SOURCE,
        context_before_line=decision.context_before_line,
        record=source_selector(decision.index, record),
        record_digest=content_digest(record),
    )


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _bounded_string(value: JsonValue | None, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _behavior_text(value: JsonValue | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= _MAX_BEHAVIOR_BYTES else None
