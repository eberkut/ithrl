"""Immutable text interventions on captured context, separate from source evidence."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ithrl.context import MAX_JSON_ITEMS, ContextNode, JsonObject, content_digest, decode_pointer

MAX_MUTATION_TEXT_BYTES = 64 * 1024


class MutationError(ValueError):
    """A text intervention cannot be applied to the declared original field."""


@dataclass(frozen=True, slots=True)
class EditableText:
    """One original user-text location, discovered by its owning materializer."""

    context_id: str
    content_index: int
    original_text: str = field(repr=False)


class TextAppend(BaseModel):
    """Append literal text to one original content block.

    This describes an edit, not permission to execute it. The materializer must
    verify the original text digest and that the node/block is an editable prior
    user text field, excluding the protected trigger and instructions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    operation: Literal["append-text"] = "append-text"
    context_id: Annotated[
        str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    ]
    content_index: Annotated[int, Field(ge=0, lt=MAX_JSON_ITEMS)]
    original_text_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    text: Annotated[str, Field(max_length=MAX_MUTATION_TEXT_BYTES, repr=False)]

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if size > MAX_MUTATION_TEXT_BYTES:
            raise ValueError("mutation text exceeds the UTF-8 byte limit")
        return value


class TextMutation(BaseModel):
    """One bounded text append, or the canonical empty intervention.

    Empty appends normalize away. Whitespace, Unicode and line endings remain
    exact: they can affect behavior. Multiple operations are deliberately not
    supported by this first one-field mutation contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    edits: Annotated[tuple[TextAppend, ...], Field(max_length=1)] = ()

    @field_validator("edits")
    @classmethod
    def normalize_empty(cls, value: tuple[TextAppend, ...]) -> tuple[TextAppend, ...]:
        return tuple(edit for edit in value if edit.text)

    @property
    def is_empty(self) -> bool:
        return not self.edits

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


def message_text(message: JsonObject, content_index: int) -> str:
    """Read only an ordinary user-text block, never instructions or tool records."""

    if content_index < 0:
        raise MutationError("mutation requires a nonnegative content index")
    if message.get("role") != "user" or message.get("type") not in (None, "message"):
        raise MutationError("mutation requires an ordinary user message")
    content = message.get("content")
    if isinstance(content, str) and content_index == 0:
        return content
    if isinstance(content, list) and content_index < len(content):
        block = content[content_index]
        if isinstance(block, dict) and block.get("type") == "input_text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    raise MutationError("mutation requires an existing input-text block")


def append_message_text(message: JsonObject, edit: TextAppend) -> JsonObject:
    """Return a copy with exactly the declared original text extended."""

    original = message_text(message, edit.content_index)
    if content_digest(original) != edit.original_text_digest:
        raise MutationError("mutation original text digest does not match")
    result = copy.deepcopy(message)
    content = result["content"]
    if isinstance(content, str):
        result["content"] = original + edit.text
    elif isinstance(content, list):
        block = content[edit.content_index]
        if not isinstance(block, dict):
            raise MutationError("mutation requires an existing input-text block")
        block["text"] = original + edit.text
    else:
        raise MutationError("mutation requires ordinary user text")
    return result


def request_text_item(request: JsonObject, node: ContextNode) -> tuple[int, JsonObject]:
    """Locate an editable prior message in an exact request; the last input is fixed."""

    if not node.mutable or node.protected or node.path is None:
        raise MutationError("mutation requires an editable request context node")
    tokens = decode_pointer(node.path)
    if (
        len(tokens) != 2
        or tokens[0] != "input"
        or not tokens[1].isascii()
        or not tokens[1].isdigit()
    ):
        raise MutationError("mutation requires an original input-message location")
    index = int(tokens[1])
    inputs = request.get("input")
    if str(index) != tokens[1] or not isinstance(inputs, list) or index >= len(inputs) - 1:
        raise MutationError("mutation cannot change the current decision input")
    item = inputs[index]
    if not isinstance(item, dict):
        raise MutationError("mutation requires an ordinary user message")
    return index, item


def editable_message_text(node: ContextNode, message: JsonObject) -> tuple[EditableText, ...]:
    """List supported blocks in a known prior message, preserving opaque content."""

    if not node.mutable or node.protected:
        return ()
    content = message.get("content")
    count = len(content) if isinstance(content, list) else 1
    found = []
    for index in range(count):
        try:
            text = message_text(message, index)
        except MutationError:
            continue
        found.append(EditableText(node.id, index, text))
    return tuple(found)
