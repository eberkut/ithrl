"""Reconstruct a Codex app-server turn from preserved rollout evidence."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum

from ithrl.adapters.contracts import NativeSession
from ithrl.case import CaseFormatError
from ithrl.context import ContextNode, JsonObject, JsonValue
from ithrl.extraction import validate_extracted_case
from ithrl.mutation import EditableText, MutationError, append_message_text
from ithrl.runner.codex.policy import (
    CAPTURE_TYPE,
    MATERIALIZATION,
    SOURCE_SCHEMA,
    TARGET_TYPE,
)
from ithrl.runner.contracts import PreparedAttempt

_SOURCE_PATH = "source/rollout.jsonl"
_MODEL_INPUT_TYPES = frozenset({"message", "custom_tool_call_output", "function_call_output"})
_IGNORED_RECORD_TYPES = frozenset(
    {
        "event_msg",
        "inter_agent_communication_metadata",
        "session_meta",
        "token_usage_record",
        "turn_context",
        "world_state",
    }
)


class CodexMaterializationErrorCode(StrEnum):
    """Stable failure categories for source-backed Codex reconstruction."""

    INCOMPATIBLE_CASE = "incompatible-case"
    CHANGED_EVIDENCE = "changed-evidence"
    UNSUPPORTED_CONTEXT = "unsupported-context"
    MISSING_TRIGGER = "missing-trigger"
    AMBIGUOUS_BOUNDARY = "ambiguous-boundary"


class CodexMaterializationError(RuntimeError):
    """A Codex candidate cannot be reconstructed without changing its meaning."""

    def __init__(self, code: CodexMaterializationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodexTurnInput:
    """The new input that starts the reconstructed decision turn."""

    items: tuple[JsonObject, ...] = field(repr=False)
    tool_output: JsonObject | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if bool(self.items) == (self.tool_output is not None):
            raise ValueError("Codex turn input requires user items or one tool output")


@dataclass(frozen=True, slots=True)
class CodexMaterialization:
    """Private app-server inputs derived from one verified Codex candidate."""

    model: str
    provider: str
    effort: str | None = field(repr=False)
    personality: str | None = field(repr=False)
    base_instructions: str | None = field(repr=False)
    developer_instructions: str | None = field(repr=False)
    history: tuple[JsonObject, ...] = field(repr=False)
    history_context_ids: tuple[str, ...] = field(repr=False)
    turn_input: CodexTurnInput = field(repr=False)


def materialize_codex_attempt(attempt: PreparedAttempt) -> CodexMaterialization:
    """Re-derive and materialize one selected Codex final-response attempt."""

    evidence = attempt.evidence
    manifest = evidence.manifest
    decision = evidence.decision
    if (
        attempt.candidate.materialization != MATERIALIZATION
        or manifest.harness != "codex"
        or manifest.source_schema != SOURCE_SCHEMA
        or manifest.capture.request != CAPTURE_TYPE
        or decision is None
        or decision.selector.kind != "codex-task-complete"
        or attempt.target.type != TARGET_TYPE
    ):
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case is not a supported Codex final-response decision",
        )

    try:
        session = validate_extracted_case(evidence)
    except CaseFormatError as error:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.CHANGED_EVIDENCE,
            "the preserved Codex evidence no longer matches the case",
        ) from error

    if session is None or decision.selector.source != _SOURCE_PATH:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case has no supported Codex source artifact",
        )
    return _materialize_session(attempt, session)


def _materialize_session(
    attempt: PreparedAttempt,
    session: NativeSession,
) -> CodexMaterialization:
    evidence = attempt.evidence
    decision = evidence.decision
    if decision is None:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.INCOMPATIBLE_CASE,
            "the case has no selected Codex decision",
        )
    records = session.records
    boundary = decision.selector.context_before_line
    if boundary < 2 or boundary > len(records) + 1:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.AMBIGUOUS_BOUNDARY,
            "the selected Codex decision has an invalid context boundary",
        )

    retained = frozenset(attempt.candidate.retained_context_ids)
    selected: list[tuple[str, JsonObject]] = []
    for node in evidence.context.nodes:
        selector = node.selector
        if selector is None or selector.line >= boundary:
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.AMBIGUOUS_BOUNDARY,
                "the Codex context graph does not match the selected boundary",
            )
        if node.id in retained:
            selected.append((node.id, records[selector.line - 1]))

    trigger_id = _trigger_node_id(evidence.context.nodes, records, boundary)
    if trigger_id not in retained:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "the candidate removes the input that triggered the selected decision",
        )

    edit = next(iter(attempt.candidate.mutation.edits), None)
    if edit is not None and edit.context_id == trigger_id:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "mutation cannot change the current decision input",
        )
    applied = edit is None

    history: list[JsonObject] = []
    history_context_ids: list[str] = []
    trigger: JsonObject | None = None
    for node_id, record in selected:
        record_type = _string(record.get("type"))
        if record_type == "response_item":
            payload = _object(record.get("payload"))
            if node_id == trigger_id:
                trigger = payload
            else:
                if edit is not None and node_id == edit.context_id:
                    try:
                        payload = append_message_text(payload, edit)
                    except MutationError as error:
                        raise CodexMaterializationError(
                            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT, str(error)
                        ) from error
                    applied = True
                history.append(copy.deepcopy(payload))
                history_context_ids.append(node_id)
            continue
        if record_type not in _IGNORED_RECORD_TYPES:
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "the retained Codex context contains an unsupported record",
            )

    if not applied:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            "mutation does not address a retained prior user message",
        )
    if trigger is None:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "the selected Codex decision has no supported triggering input",
        )

    metadata = _object(records[0].get("payload"))
    turn_context = _selected_turn_context(records[: boundary - 1], decision.selector.turn_id)
    model = _required_string(turn_context.get("model"), "the selected Codex turn has no model")
    provider = _required_string(
        metadata.get("model_provider"),
        "the selected Codex session has no model provider",
    )
    return CodexMaterialization(
        model=model,
        provider=provider,
        effort=_optional_turn_setting(turn_context, "effort"),
        personality=_optional_turn_setting(turn_context, "personality"),
        base_instructions=_optional_instruction(metadata, "base_instructions", allow_document=True),
        developer_instructions=_optional_instruction(
            turn_context,
            "developer_instructions",
            allow_document=False,
        ),
        history=tuple(history),
        history_context_ids=tuple(history_context_ids),
        turn_input=_turn_input(trigger, history),
    )


def editable_codex_text(attempt: PreparedAttempt) -> tuple[EditableText, ...]:
    """Pure text discovery on the verified original, with the trigger excluded."""

    if attempt.candidate.removed_context_ids or not attempt.candidate.mutation.is_empty:
        raise MutationError("text discovery requires the unchanged original")
    materialize_codex_attempt(attempt)
    return attempt.evidence.editable_text()


def _trigger_node_id(
    nodes: tuple[ContextNode, ...], records: tuple[JsonObject, ...], boundary: int
) -> str:
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
    raise CodexMaterializationError(
        CodexMaterializationErrorCode.MISSING_TRIGGER,
        "the selected Codex decision has no supported triggering input",
    )


def _selected_turn_context(
    records: tuple[JsonObject, ...],
    turn_id: str,
) -> JsonObject:
    matches = [
        _object(record.get("payload"))
        for record in records
        if record.get("type") == "turn_context"
        and _object(record.get("payload")).get("turn_id") == turn_id
    ]
    if not matches:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.AMBIGUOUS_BOUNDARY,
            "the selected Codex turn has no matching context record",
        )
    return matches[-1]


def _turn_input(trigger: JsonObject, history: list[JsonObject]) -> CodexTurnInput:
    item_type = _string(trigger.get("type"))
    if item_type == "message" and trigger.get("role") == "user":
        return CodexTurnInput(items=_user_inputs(trigger.get("content")))
    if item_type in {"custom_tool_call_output", "function_call_output"}:
        call_id = _required_string(trigger.get("call_id"), "Codex tool output has no call identity")
        call = next(
            (
                item
                for item in reversed(history)
                if item.get("type") in {"custom_tool_call", "function_call"}
                and item.get("call_id") == call_id
            ),
            None,
        )
        if call is None:
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.MISSING_TRIGGER,
                "the Codex tool output has no retained matching call",
            )
        output = trigger.get("output")
        if not isinstance(output, (str, list)):
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "the Codex tool output uses an unsupported content shape",
            )
        return CodexTurnInput(
            items=(),
            tool_output={
                "name": _required_string(call.get("name"), "Codex tool call has no name"),
                "namespace": None,
                "output": copy.deepcopy(output),
            },
        )
    raise CodexMaterializationError(
        CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        "the Codex decision trigger cannot be reconstructed by this runner",
    )


def _user_inputs(value: JsonValue | None) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or not value:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            "the Codex user input has an unsupported content shape",
        )
    inputs: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "the Codex user input contains an unsupported content item",
            )
        inputs.append(
            {
                "type": "text",
                "text": _required_string(item.get("text"), "Codex user input has empty text"),
            }
        )
    return tuple(inputs)


def _optional_instruction(
    document: JsonObject,
    field_name: str,
    *,
    allow_document: bool,
) -> str | None:
    if field_name not in document or document[field_name] is None:
        return None
    value = document[field_name]
    if isinstance(value, str) and value:
        return value
    if allow_document and isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text:
            return text
    raise CodexMaterializationError(
        CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        "the selected Codex instructions use an unsupported shape",
    )


def _optional_turn_setting(document: JsonObject, field_name: str) -> str | None:
    if field_name not in document or document[field_name] is None:
        return None
    value = document[field_name]
    if isinstance(value, str) and value:
        return value
    raise CodexMaterializationError(
        CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        "the selected Codex turn uses an unsupported model setting",
    )


def _required_string(value: JsonValue | None, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            message,
        )
    return value


def _object(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None
