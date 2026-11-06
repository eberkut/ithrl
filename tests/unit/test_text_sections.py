"""Exact paragraph partitioning and immutable, bounded section identities."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from ithrl.context import content_digest
from ithrl.mutation import TextAppend, TextMutation
from ithrl.runner import Candidate
from ithrl.text_sections import (
    MAX_TEXT_SECTION_BYTES,
    MAX_TEXT_SECTIONS,
    TextSectionCatalog,
    TextSectionError,
    TextSectionLimitError,
    TextSectionRemoval,
    catalog_text_sections,
    remove_text_sections,
)


def _catalog(
    text: str,
    *,
    case_digest: str = "a" * 64,
    context_id: str = "ctx-prior",
    context_digest: str = "b" * 64,
    content_index: int = 0,
) -> TextSectionCatalog:
    return catalog_text_sections(
        case_digest=case_digest,
        context_id=context_id,
        context_digest=context_digest,
        content_index=content_index,
        text=text,
    )


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("", []),
        (" \t\r\n \t", [" \t\r\n \t"]),
        ("A\n\nB\n", ["A\n\n", "B\n"]),
        ("\r\n\t\r\nA\r\n\r\nB\r", ["\r\n\t\r\n", "A\r\n\r\n", "B\r"]),
        ("A\r\rB", ["A\r\r", "B"]),
        ("é🙂e\u0301\n \t\n後\n", ["é🙂e\u0301\n \t\n", "後\n"]),
        ("A\u2028\u0085\n\u00a0\nB", ["A\u2028\u0085\n\u00a0\nB"]),
    ],
)
def test_policy_is_an_exact_ordered_partition(text: str, parts: list[str]) -> None:
    catalog = _catalog(text)
    assert [text[s.start : s.end] for s in catalog.sections] == parts
    assert sum(s.utf8_bytes for s in catalog.sections) == len(text.encode())
    assert catalog.original_utf8_bytes == len(text.encode())
    assert _catalog(text) == catalog
    assert TextSectionCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    assert remove_text_sections(text, catalog, catalog.removal(())) == text
    assert (
        remove_text_sections(text, catalog, catalog.removal(tuple(s.id for s in catalog.sections)))
        == ""
    )


@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=300))
def test_arbitrary_unicode_is_never_normalized_or_lost(text: str) -> None:
    catalog = _catalog(text)
    assert "".join(text[s.start : s.end] for s in catalog.sections) == text
    assert remove_text_sections(text, catalog, catalog.removal(())) == text
    ids = tuple(s.id for s in catalog.sections[::2])
    expected = "".join(text[s.start : s.end] for s in catalog.sections[1::2])
    assert remove_text_sections(text, catalog, catalog.removal(ids)) == expected


def test_identity_binds_case_node_block_original_and_policy() -> None:
    text = "Relevant instruction.\n\nArchived background."
    catalog = _catalog(text)
    ids = tuple(s.id for s in catalog.sections)
    for changes in (
        {"case_digest": "c" * 64},
        {"context_id": "ctx-other"},
        {"context_digest": "d" * 64},
        {"content_index": 1},
        {"text": text + "\r\n"},
    ):
        options: dict[str, Any] = {"text": text, **changes}
        other = _catalog(**options)
        assert set(ids).isdisjoint(s.id for s in other.sections)
        with pytest.raises(TextSectionError):
            other.validate_removal(catalog.removal((ids[-1],)))
    changed_policy = catalog.model_copy(update={"policy_digest": "e" * 64})
    with pytest.raises(TextSectionError, match="immutable source"):
        remove_text_sections(text, changed_policy, catalog.removal((ids[-1],)))


def test_selection_refuses_unknown_repeated_reordered_and_forged_ranges() -> None:
    text = "First.\n\nSecond.\n\nThird."
    catalog = _catalog(text)
    ids = tuple(s.id for s in catalog.sections)
    for selected in (("section-" + "f" * 64,), ids[::-1]):
        with pytest.raises(TextSectionError, match="source order"):
            catalog.removal(selected)
    with pytest.raises(ValidationError, match="duplicate"):
        catalog.removal((ids[0], ids[0]))
    forged = catalog.model_copy(
        update={
            "sections": (catalog.sections[0].model_copy(update={"end": 1}), *catalog.sections[1:])
        }
    )
    with pytest.raises(TextSectionError, match="immutable source"):
        remove_text_sections(text, forged, catalog.removal((ids[0],)))
    data = catalog.model_dump()
    data["sections"][1]["start"] -= 1
    data["sections"][1]["utf8_bytes"] += 1
    with pytest.raises(ValidationError, match="nonoverlapping"):
        TextSectionCatalog.model_validate(data)
    with pytest.raises(TextSectionError, match="immutable source"):
        remove_text_sections(text + "changed", catalog, catalog.removal((ids[0],)))


def test_limits_refuse_whole_field_instead_of_truncating_units() -> None:
    boundary = "é" * (MAX_TEXT_SECTION_BYTES // 2)
    assert _catalog(boundary).original_utf8_bytes == MAX_TEXT_SECTION_BYTES
    with pytest.raises(TextSectionLimitError, match="byte limit"):
        _catalog(boundary + "é")
    with pytest.raises(TextSectionError, match="Unicode"):
        _catalog("\ud800")
    assert len(_catalog("A\n\n" * MAX_TEXT_SECTIONS).sections) == MAX_TEXT_SECTIONS
    with pytest.raises(TextSectionLimitError, match="section-count"):
        _catalog("A\n\n" * (MAX_TEXT_SECTIONS + 1))


def test_empty_selection_normalizes_and_new_variant_is_persisted_privately() -> None:
    catalog = _catalog("Keep.\n\nRemove.")
    assert TextMutation(edits=(catalog.removal(()),)) == TextMutation()
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[1].id,)),))
    assert TextMutation.model_validate_json(mutation.model_dump_json()) == mutation
    assert "Keep." not in mutation.model_dump_json()
    assert "Remove." not in repr(catalog)
    with pytest.raises(ValidationError):
        TextMutation(edits=(mutation.edits[0], mutation.edits[0]))
    data = mutation.model_dump()
    data["edits"][0]["section_ids"] = ("synthetic-private-text",)
    with pytest.raises(ValidationError) as caught:
        TextMutation.model_validate(data)
    assert "synthetic-private-text" not in str(caught.value)
    with pytest.raises(ValidationError):
        TextSectionRemoval.model_validate({**mutation.edits[0].model_dump(), "start": 0})


def test_existing_append_wire_shape_and_candidate_digests_are_unchanged() -> None:
    empty = TextMutation()
    append = TextMutation(
        edits=(
            TextAppend(
                context_id="ctx-a",
                content_index=0,
                original_text_digest=content_digest("original text"),
                text="\n\nSynthetic archived note.",
            ),
        )
    )
    assert empty.model_dump() == {"schema_version": 1, "edits": ()}
    assert append.model_dump()["edits"][0] == {
        "operation": "append-text",
        "context_id": "ctx-a",
        "content_index": 0,
        "original_text_digest": "c0ffc0a6d6cfa3c01314c2e20f846820998d1315be1b0d47195b5cb1a01ab391",
        "text": "\n\nSynthetic archived note.",
    }
    expected = (
        "c6e74e3bd04baa99286c57d2d51df3783dddd251dd581bcdf7a5392586a0c469",
        "911246b42f45bc3e1b0b4f01a6daa127e281f230a78df8a63d1bab5dd783a1b2",
    )
    for mutation, digest in zip((empty, append), expected, strict=True):
        candidate = Candidate(
            case_digest="a" * 64,
            execution_plan_digest="b" * 64,
            source_artifact_digests=("c" * 64,),
            retained_context_ids=("ctx-a", "ctx-b"),
            removed_context_ids=(),
            materialization="source-backed",
            mutation=mutation,
        )
        assert candidate.digest == digest
