"""Exact deletions bound to one verified native tool output text field."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import content_digest, decode_pointer
from ithrl.text_sections import MAX_TEXT_SECTION_BYTES

type _Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type _ContextId = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
type _FieldId = Annotated[str, Field(pattern=r"^tool-output-text-[0-9a-f]{64}$")]


class ToolOutputTextError(ValueError):
    """A native text field or span does not match its immutable binding."""


class ToolOutputTextLimitError(ToolOutputTextError):
    """A complete tool output text field exceeds the supported byte limit."""


@dataclass(frozen=True, slots=True)
class EditableToolOutputText:
    """An adapter-discovered native string; discovery alone grants no execution support."""

    context_id: str
    field_path: str
    original_text: str = field(repr=False)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ToolOutputTextSpanRemoval(_Contract):
    """Delete exact source codepoints, without decoding or replacing native text."""

    operation: Literal["remove-tool-output-text-span"] = "remove-tool-output-text-span"
    context_id: _ContextId
    field_id: _FieldId
    original_text_digest: _Digest
    start: Annotated[int, Field(ge=0, le=MAX_TEXT_SECTION_BYTES)]
    end: Annotated[int, Field(gt=0, le=MAX_TEXT_SECTION_BYTES)]
    removed_text_digest: _Digest

    @model_validator(mode="after")
    def validate_span(self) -> ToolOutputTextSpanRemoval:
        if self.end <= self.start:
            raise ValueError("tool output text removal requires a nonempty forward span")
        return self


class ToolOutputTextField(_Contract):
    """Metadata identifying a single complete string in immutable native evidence."""

    field_id: _FieldId
    case_digest: _Digest
    context_id: _ContextId
    context_digest: _Digest
    field_path: Annotated[str, Field(min_length=1, max_length=256)]
    original_text_digest: _Digest
    original_codepoints: Annotated[int, Field(ge=0, le=MAX_TEXT_SECTION_BYTES)]
    original_utf8_bytes: Annotated[int, Field(ge=0, le=MAX_TEXT_SECTION_BYTES)]

    @model_validator(mode="after")
    def validate_field(self) -> ToolOutputTextField:
        decode_pointer(self.field_path)
        if self.original_utf8_bytes < self.original_codepoints:
            raise ValueError(
                "tool output text byte size cannot be smaller than its codepoint count"
            )
        if self.field_id != "tool-output-text-" + content_digest(
            self.model_dump(mode="json", exclude={"field_id"})
        ):
            raise ValueError("tool output text field identity does not match its source binding")
        return self

    def removal(self, text: str, *, start: int, end: int) -> ToolOutputTextSpanRemoval:
        """Describe one range after verifying the complete source field."""

        _validate_text(text, self)
        _validate_span(start, end, len(text))
        return ToolOutputTextSpanRemoval(
            context_id=self.context_id,
            field_id=self.field_id,
            original_text_digest=self.original_text_digest,
            start=start,
            end=end,
            removed_text_digest=content_digest(text[start:end]),
        )


def describe_tool_output_text(
    *, case_digest: str, context_id: str, context_digest: str, field_path: str, text: str
) -> ToolOutputTextField:
    """Bind metadata to an adapter-enumerated field, without native operations."""

    if len(text) > MAX_TEXT_SECTION_BYTES:
        raise ToolOutputTextLimitError("tool output text exceeds the UTF-8 byte limit")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ToolOutputTextError("tool output text is not valid Unicode") from None
    if size > MAX_TEXT_SECTION_BYTES:
        raise ToolOutputTextLimitError("tool output text exceeds the UTF-8 byte limit")
    binding = {
        "case_digest": case_digest,
        "context_id": context_id,
        "context_digest": context_digest,
        "field_path": field_path,
        "original_text_digest": content_digest(text),
        "original_codepoints": len(text),
        "original_utf8_bytes": size,
    }
    return ToolOutputTextField.model_validate(
        {"field_id": "tool-output-text-" + content_digest(binding), **binding}
    )


def _validate_text(text: str, descriptor: ToolOutputTextField) -> None:
    actual = describe_tool_output_text(
        case_digest=descriptor.case_digest,
        context_id=descriptor.context_id,
        context_digest=descriptor.context_digest,
        field_path=descriptor.field_path,
        text=text,
    )
    if actual != descriptor:
        raise ToolOutputTextError("tool output text does not match its original field")


def _validate_span(start: int, end: int, length: int) -> None:
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= length:
        raise ToolOutputTextError(
            "tool output text removal requires an integer span within its original field"
        )


def validate_tool_output_text_removal(
    text: str, descriptor: ToolOutputTextField, edit: ToolOutputTextSpanRemoval
) -> None:
    """Validate the complete binding without constructing a retained text copy."""

    _validate_text(text, descriptor)
    _validate_span(edit.start, edit.end, len(text))
    if (
        edit.operation != "remove-tool-output-text-span"
        or edit.context_id != descriptor.context_id
        or edit.field_id != descriptor.field_id
        or edit.original_text_digest != descriptor.original_text_digest
        or content_digest(text[edit.start : edit.end]) != edit.removed_text_digest
    ):
        raise ToolOutputTextError(
            "tool output text removal does not match its original field and span"
        )


def remove_tool_output_text_span(
    text: str, descriptor: ToolOutputTextField, edit: ToolOutputTextSpanRemoval
) -> str:
    """Splice one verified source substring; preserve every retained codepoint."""

    validate_tool_output_text_removal(text, descriptor, edit)
    return text[: edit.start] + text[edit.end :]
