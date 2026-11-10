"""Reconstruct a Codex app-server turn from preserved rollout evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from enum import StrEnum

from ithrl.adapters import AdapterError
from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.registry import decision_input_node_id
from ithrl.case import CaseFormatError
from ithrl.context import JsonObject, JsonValue
from ithrl.extraction import validate_extracted_case
from ithrl.mutation import (
    EditableText,
    MutationError,
    TextAppend,
    append_message_text,
    message_text,
    replace_message_text,
)
from ithrl.runner.codex.policy import (
    CAPTURE_TYPE,
    MATERIALIZATION,
    SOURCE_SCHEMA,
    TARGET_TYPE,
)
from ithrl.runner.contracts import PreparedAttempt, _validate_attempt_identities
from ithrl.text_sections import TextSectionError, TextSectionRemoval, remove_text_sections

_SOURCE_PATH = "source/rollout.jsonl"
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
    """A user input or continuation of an injected native tool-output checkpoint."""

    items: tuple[JsonObject, ...] = field(repr=False)
    checkpoint_continuation: bool = False

    def __post_init__(self) -> None:
        if type(self.checkpoint_continuation) is not bool or bool(self.items) == (
            self.checkpoint_continuation
        ):
            raise ValueError("Codex turn input requires user items or a checkpoint continuation")


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
    turn_input: CodexTurnInput = field(repr=False)


def materialize_codex_attempt(attempt: PreparedAttempt) -> CodexMaterialization:
    """Re-derive and materialize one selected Codex final-response attempt."""

    if any(
        not isinstance(edit, (TextAppend, TextSectionRemoval))
        for edit in attempt.candidate.mutation.edits
    ):
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            "Codex reconstruction requires a supported text intervention",
        )
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
    if any(isinstance(edit, TextSectionRemoval) for edit in attempt.candidate.mutation.edits):
        try:
            _validate_attempt_identities(attempt)
        except ValueError:
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.CHANGED_EVIDENCE,
                "the Codex candidate does not match its evidence and execution plan",
            ) from None
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
    if any(record.get("type") == "compacted" for record in records[: boundary - 1]):
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            "Codex reconstruction requires an uncompacted source prefix",
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

    try:
        trigger_id = decision_input_node_id(
            session, decision.selector, evidence.context, records=records
        )
    except AdapterError:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "the selected Codex decision has no supported triggering input",
        ) from None
    if trigger_id not in retained:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "the candidate removes the input that triggered the selected decision",
        )

    edit = next(iter(attempt.candidate.mutation.edits), None)
    if edit is not None and not isinstance(edit, (TextAppend, TextSectionRemoval)):
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
            "Codex reconstruction requires a supported text intervention",
        )
    if edit is not None and edit.context_id == trigger_id:
        raise CodexMaterializationError(
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            "mutation cannot change the current decision input",
        )
    applied = edit is None

    history: list[JsonObject] = []
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
                        if isinstance(edit, TextAppend):
                            payload = append_message_text(payload, edit)
                        else:
                            catalog = evidence.validate_text_sections(edit)
                            retained_text = remove_text_sections(
                                message_text(payload, edit.content_index), catalog, edit
                            )
                            payload = replace_message_text(
                                payload, edit.content_index, retained_text
                            )
                    except (MutationError, TextSectionError) as error:
                        raise CodexMaterializationError(
                            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT, str(error)
                        ) from error
                    applied = True
                history.append(copy.deepcopy(payload))
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
    # Compute this before freezing history: a tool-output checkpoint includes the
    # captured output itself, with its original call_id and all native metadata.
    turn_input = _turn_input(trigger, history)
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
        turn_input=turn_input,
    )


def editable_codex_text(attempt: PreparedAttempt) -> tuple[EditableText, ...]:
    """Pure text discovery on the verified original, with the trigger excluded."""

    if attempt.candidate.removed_context_ids or not attempt.candidate.mutation.is_empty:
        raise MutationError("text discovery requires the unchanged original")
    materialize_codex_attempt(attempt)
    return attempt.evidence.editable_text()


def validate_codex_output_limit(
    materialized: CodexMaterialization, token_limit: int | None
) -> None:
    """Refuse tool outputs that native history insertion might silently truncate."""

    # Codex 0.167 core/src/context_manager/history.rs record_item_with_metadata
    # applies the model's truncation policy while injecting history; the override
    # lives in models-manager/src/model_info.rs with_config_overrides.
    # Serialized ASCII JSON bytes conservatively bound UTF-8 text/token storage,
    # including content-list overhead. Never assume a particular model's default.
    for item in materialized.history:
        if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
            continue
        output = item.get("output")
        if not _is_text_tool_output(output):
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "the Codex tool output uses an unsupported content shape",
            )
        size = len(json.dumps(output, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        if token_limit is None or size > token_limit:
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "Codex tool-output reconstruction requires an explicit adequate "
                "tool_output_token_limit",
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
        calls = [
            item
            for item in history
            if item.get("type") in {"custom_tool_call", "function_call"}
            and item.get("call_id") == call_id
        ]
        if len(calls) != 1 or calls[0].get("type") != item_type.removesuffix("_output"):
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.MISSING_TRIGGER,
                "the Codex tool output has no unique retained matching call",
            )
        _required_string(calls[0].get("name"), "Codex tool call has no name")
        output = trigger.get("output")
        if not _is_text_tool_output(output):
            raise CodexMaterializationError(
                CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
                "the Codex tool output uses an unsupported content shape",
            )
        # turn/start.toolOutput identifies a tool by name and synthesizes a new
        # call identity. Inject the preserved output instead, then start an empty
        # turn so Codex continues the captured call/output pair unchanged.
        history.append(copy.deepcopy(trigger))
        return CodexTurnInput(items=(), checkpoint_continuation=True)
    raise CodexMaterializationError(
        CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        "the Codex decision trigger cannot be reconstructed by this runner",
    )


def _is_text_tool_output(value: JsonValue | None) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list)
        and all(
            isinstance(item, dict)
            and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)
            for item in value
        )
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
