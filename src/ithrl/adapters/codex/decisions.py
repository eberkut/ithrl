"""Enumerate completed decisions from parsed Codex CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ithrl.adapters.codex.context import opaque_prefix, source_selector
from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.behavior import BehaviorObservation, FinalResponseTarget
from ithrl.context import JsonObject, JsonValue, content_digest
from ithrl.decisions import (
    MAX_DECISIONS,
    DecisionCaseFacts,
    DecisionDraft,
    DecisionLimitation,
    DecisionSelector,
    TargetCandidate,
)

_ADAPTER = "codex-session"
_ADAPTER_VERSION = "1"
_HARNESS = "codex"
_SOURCE = "source/rollout.jsonl"
_MAX_BEHAVIOR_BYTES = 64 * 1024
_OUTPUT_TYPES = frozenset({"custom_tool_call_output", "function_call_output"})


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


def enumerate_decisions(session: NativeSession) -> tuple[DecisionDraft, ...]:
    """Return supported decisions whose enclosing Codex task completed."""

    if session.metadata.format.harness != _HARNESS:
        raise ValueError("native session is not a Codex CLI session")

    records = session.records
    decisions = _completed_decision_records(records)
    opaque_context = opaque_prefix(records)
    provider = _provider(records)
    return tuple(
        _build_draft(session, records, opaque_context, provider, decision) for decision in decisions
    )


def _completed_decision_records(records: tuple[JsonObject, ...]) -> tuple[_DecisionRecord, ...]:
    turn_models: dict[str, str] = {}
    open_turns: dict[str, list[int]] = {}
    decisions: list[_DecisionRecord] = []
    latest_model_input_line = 0

    for index, record in enumerate(records):
        record_type = _string(record.get("type"))
        payload = _object(record.get("payload"))
        if record_type == "turn_context":
            _remember_turn_model(payload, turn_models)
            continue
        if record_type == "response_item" and _is_model_input(payload):
            latest_model_input_line = index + 2
            continue
        if record_type != "event_msg":
            continue
        event_type = _string(payload.get("type"))
        turn_id = _string(payload.get("turn_id"))
        if turn_id is None:
            continue
        if event_type == "task_started":
            open_turns.setdefault(turn_id, []).append(index + 2)
            continue
        if event_type != "task_complete" or not (starts := open_turns.get(turn_id)):
            continue
        # Codex task IDs can overlap; a completion closes the latest unmatched start for that ID.
        context_before_line = max(starts.pop(), latest_model_input_line)
        if not starts:
            del open_turns[turn_id]
        decision = _final_response_record(
            record,
            index=index,
            turn_id=turn_id,
            context_before_line=context_before_line,
            model=turn_models.get(turn_id),
        )
        if decision is None:
            continue
        if len(decisions) == MAX_DECISIONS:
            raise AdapterError(
                AdapterErrorCode.TOO_MANY_DECISIONS,
                "session contains too many completed decisions",
            )
        decisions.append(decision)

    return tuple(decisions)


def _remember_turn_model(payload: JsonObject, models: dict[str, str]) -> None:
    turn_id = _string(payload.get("turn_id"))
    model = _bounded_string(payload.get("model"), 256)
    if turn_id is not None and model is not None:
        models[turn_id] = model


def _final_response_record(
    record: JsonObject,
    *,
    index: int,
    turn_id: str,
    context_before_line: int,
    model: str | None,
) -> _DecisionRecord | None:
    payload = _object(record.get("payload"))
    text = _bounded_behavior_text(payload.get("last_agent_message"))
    if text is None:
        return None

    target = FinalResponseTarget(text_equals=text)
    return _DecisionRecord(
        index=index,
        context_before_line=context_before_line,
        turn_id=turn_id,
        kind="codex-task-complete",
        native_id=turn_id,
        observation=BehaviorObservation(final_response=text),
        targets=(_target_candidate(target),),
        model=model,
    )


def _is_model_input(payload: JsonObject) -> bool:
    item_type = _string(payload.get("type"))
    return (
        item_type == "message" and payload.get("role") in {"user", "developer", "system"}
    ) or item_type in _OUTPUT_TYPES


def _bounded_behavior_text(value: JsonValue | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= _MAX_BEHAVIOR_BYTES else None


def _target_candidate(target: FinalResponseTarget) -> TargetCandidate:
    return TargetCandidate(
        ordinal=1,
        target=target,
        evidence_support="supported",
        experimental_support="unvalidated",
    )


def _build_draft(
    session: NativeSession,
    records: tuple[JsonObject, ...],
    opaque_prefix: tuple[bool, ...],
    provider: str | None,
    decision: _DecisionRecord,
) -> DecisionDraft:
    record = records[decision.index]
    limitations: list[DecisionLimitation] = ["experimental-support-unvalidated"]
    if opaque_prefix[decision.context_before_line - 1]:
        limitations.append("opaque-context-frozen")
    return DecisionDraft(
        case=DecisionCaseFacts(
            adapter=_ADAPTER,
            adapter_version=_ADAPTER_VERSION,
            provider=provider,
            model_requested=decision.model,
            source_schema=session.metadata.format.name,
        ),
        selector=_selector(session, record, decision),
        observed_at=cast(str, record["timestamp"]),
        observation=decision.observation,
        targets=decision.targets,
        limitations=tuple(limitations),
    )


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


def _provider(records: tuple[JsonObject, ...]) -> str | None:
    if not records:
        return None
    first = records[0]
    if first.get("type") != "session_meta":
        return None
    return _bounded_string(_object(first.get("payload")).get("model_provider"), 256)


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded_string(value: JsonValue | None, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None
