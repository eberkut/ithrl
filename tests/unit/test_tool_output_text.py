"""Exact native-text spans are bounded, source-bound and preserve unedited text."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.context import content_digest
from ithrl.mutation import TextMutation
from ithrl.text_sections import MAX_TEXT_SECTION_BYTES
from ithrl.tool_output_text import (
    EditableToolOutputText,
    ToolOutputTextError,
    ToolOutputTextField,
    ToolOutputTextLimitError,
    describe_tool_output_text,
    remove_tool_output_text_span,
)


def _field(original: str, **changes: str) -> ToolOutputTextField:
    return describe_tool_output_text(
        **{
            "case_digest": "a" * 64,
            "context_id": "ctx-result",
            "context_digest": "b" * 64,
            "field_path": "/result/content",
            "text": original,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("text", "start", "end", "expected"),
    [
        ("before recipe after", 7, 13, "before  after"),
        ("é🧪\r\nkeep\n", 1, 4, "ékeep\n"),
        ("\rfirst\r\nsecond\n", 0, 1, "first\r\nsecond\n"),
        (r'{"output":"first\nremove\nlast"}', 18, 26, r'{"output":"first\nlast"}'),
        ("complete", 0, 8, ""),
    ],
)
def test_exact_source_span_preserves_prefix_and_suffix(
    text: str, start: int, end: int, expected: str
) -> None:
    field = _field(text)
    edit = field.removal(text, start=start, end=end)
    assert remove_tool_output_text_span(text, field, edit) == expected
    assert edit.removed_text_digest == content_digest(text[start:end])
    mutation = TextMutation(edits=(edit,))
    assert TextMutation.model_validate_json(mutation.model_dump_json()) == mutation
    assert ToolOutputTextField.model_validate_json(field.model_dump_json()) == field
    assert field.original_codepoints == len(text)
    assert field.original_utf8_bytes == len(text.encode())


@pytest.mark.parametrize(("start", "end"), [(-1, 2), (0, 0), (2, 1), (0, 5)])
def test_authoring_refuses_empty_reversed_or_out_of_bounds_spans(start: int, end: int) -> None:
    with pytest.raises(ToolOutputTextError):
        _field("text").removal("text", start=start, end=end)


@pytest.mark.parametrize(
    "change",
    [
        {"field_id": "tool-output-text-" + "f" * 64},
        {"context_id": "ctx-foreign"},
        {"original_text_digest": "f" * 64},
        {"removed_text_digest": "f" * 64},
        {"start": -1, "end": 0, "removed_text_digest": content_digest("")},
        {"start": 2, "end": 2, "removed_text_digest": content_digest("")},
        {"end": 99},
        {"start": False, "end": True},
        {"start": 0.0, "end": 1.0},
        {"operation": "append-text"},
    ],
)
def test_materialization_revalidates_forged_edit_models(
    change: dict[str, str | int | float | bool],
) -> None:
    field = _field("text")
    edit = field.removal("text", start=0, end=1).model_copy(update=change)
    with pytest.raises(ToolOutputTextError):
        remove_tool_output_text_span("text", field, edit)


@pytest.mark.parametrize(("start", "end"), [(False, True), (0.0, 1.0), ("0", "1")])
def test_authoring_rejects_noninteger_offsets(start: Any, end: Any) -> None:
    with pytest.raises(ToolOutputTextError, match="integer span"):
        _field("text").removal("text", start=start, end=end)


def test_native_field_identity_binds_case_record_path_and_text() -> None:
    field = _field("source")
    for change in (
        {"case_digest": "c" * 64},
        {"context_id": "ctx-other"},
        {"context_digest": "d" * 64},
        {"field_path": "/different/content"},
        {"text": "different"},
    ):
        assert _field("source", **change).field_id != field.field_id
    with pytest.raises(ToolOutputTextError):
        field.removal("changed", start=0, end=1)
    with pytest.raises(ToolOutputTextError):
        remove_tool_output_text_span("changed", field, field.removal("source", start=0, end=1))
    with pytest.raises(ValidationError):
        ToolOutputTextField.model_validate({**field.model_dump(), "field_path": "/forged"})


@pytest.mark.parametrize("text", ["x" * (MAX_TEXT_SECTION_BYTES + 1), "é" * MAX_TEXT_SECTION_BYTES])
def test_whole_field_byte_limit(text: str) -> None:
    with pytest.raises(ToolOutputTextLimitError):
        _field(text)


def test_unicode_refusal_and_private_representations() -> None:
    with pytest.raises(ToolOutputTextError, match="Unicode"):
        _field("\ud800")
    text = "PRIVATE_TOOL_OUTPUT_TEXT"
    site = EditableToolOutputText("ctx-result", "/result/content", text)
    field = _field(text)
    edit = field.removal(text, start=0, end=1)
    assert text not in repr(site)
    assert text not in field.model_dump_json()
    assert text not in repr(edit)


def test_empty_and_maximum_size_fields_have_complete_metadata() -> None:
    assert _field("").original_utf8_bytes == 0
    assert _field("x" * MAX_TEXT_SECTION_BYTES).original_utf8_bytes == MAX_TEXT_SECTION_BYTES
