"""Pure, source-bound paragraph units for prospective text removal.

The catalog describes immutable text; it does not grant native execution support.
Only unit identities are persisted in an edit, never arbitrary caller-supplied ranges.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import MAX_JSON_ITEMS, content_digest

MAX_TEXT_SECTION_BYTES = 1024 * 1024
MAX_TEXT_SECTIONS = 1024
TEXT_SECTION_POLICY = "paragraph-v1"
TEXT_SECTION_POLICY_DIGEST = content_digest(
    {
        "policy": TEXT_SECTION_POLICY,
        "offsets": "unicode-codepoint-half-open",
        "newlines": ["LF", "CRLF", "CR"],
        "blank_line_content": "ASCII-space-tab-only",
        "partition": "nonblank-run-with-following-blanks;leading-blanks-separate",
        "empty": "no-sections;preserve-field",
        "max_utf8_bytes": MAX_TEXT_SECTION_BYTES,
        "max_sections": MAX_TEXT_SECTIONS,
    }
)

type _Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type _ContextId = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
type _SectionId = Annotated[str, Field(pattern=r"^section-[0-9a-f]{64}$")]


class TextSectionError(ValueError):
    """A section catalog or selection does not match its immutable text."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class TextSectionRemoval(_Contract):
    """One field's ordered selection of original paragraph units to remove."""

    operation: Literal["remove-text-sections"] = "remove-text-sections"
    context_id: _ContextId
    content_index: Annotated[int, Field(ge=0, lt=MAX_JSON_ITEMS)]
    original_text_digest: _Digest
    policy_digest: _Digest
    section_ids: Annotated[tuple[_SectionId, ...], Field(max_length=MAX_TEXT_SECTIONS)]

    @model_validator(mode="after")
    def validate_unique(self) -> TextSectionRemoval:
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("section removals must not contain duplicate units")
        return self


class TextSection(_Contract):
    """One complete original span; byte size is distinct from codepoint offsets."""

    id: _SectionId
    start: Annotated[int, Field(ge=0, le=MAX_TEXT_SECTION_BYTES)]
    end: Annotated[int, Field(gt=0, le=MAX_TEXT_SECTION_BYTES)]
    utf8_bytes: Annotated[int, Field(gt=0, le=MAX_TEXT_SECTION_BYTES)]

    @model_validator(mode="after")
    def validate_range(self) -> TextSection:
        if self.end <= self.start or self.utf8_bytes < self.end - self.start:
            raise ValueError("section requires a nonempty Unicode span")
        return self


class TextSectionCatalog(_Contract):
    """Metadata-only partition bound to one original case, node and content block."""

    case_digest: _Digest
    context_id: _ContextId
    context_digest: _Digest
    content_index: Annotated[int, Field(ge=0, lt=MAX_JSON_ITEMS)]
    original_text_digest: _Digest
    policy_digest: _Digest = TEXT_SECTION_POLICY_DIGEST
    original_utf8_bytes: Annotated[int, Field(ge=0, le=MAX_TEXT_SECTION_BYTES)]
    sections: Annotated[tuple[TextSection, ...], Field(max_length=MAX_TEXT_SECTIONS)]

    @model_validator(mode="after")
    def validate_partition(self) -> TextSectionCatalog:
        if self.policy_digest != TEXT_SECTION_POLICY_DIGEST:
            raise ValueError("unsupported text-section policy")
        end = 0
        seen: set[str] = set()
        for section in self.sections:
            if section.start != end or section.id in seen:
                raise ValueError("section catalog must be an ordered nonoverlapping partition")
            end = section.end
            seen.add(section.id)
        if sum(section.utf8_bytes for section in self.sections) != self.original_utf8_bytes:
            raise ValueError("section byte sizes must cover the original field")
        return self

    def removal(self, section_ids: tuple[str, ...]) -> TextSectionRemoval:
        """Construct a selection in original source order, refusing unknown units."""

        edit = TextSectionRemoval(
            context_id=self.context_id,
            content_index=self.content_index,
            original_text_digest=self.original_text_digest,
            policy_digest=self.policy_digest,
            section_ids=section_ids,
        )
        self.validate_removal(edit)
        return edit

    def validate_removal(self, edit: TextSectionRemoval) -> None:
        """Refuse stale fields, foreign units, duplicates and noncanonical ordering."""

        if (
            edit.context_id != self.context_id
            or edit.content_index != self.content_index
            or edit.original_text_digest != self.original_text_digest
            or edit.policy_digest != self.policy_digest
        ):
            raise TextSectionError("section removal does not match the original field and policy")
        selected = set(edit.section_ids)
        expected = tuple(section.id for section in self.sections if section.id in selected)
        if edit.section_ids != expected:
            raise TextSectionError("section removal requires known units in original source order")


def _paragraph_ranges(text: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    start = 0
    previous_blank = False
    for line in re.finditer(r"([^\r\n]*)(?:\r\n|\r|\n|$)", text):
        if line.start() == line.end():
            continue
        blank = not line.group(1).strip(" \t")
        if not blank and previous_blank:
            ranges.append((start, line.start()))
            start = line.start()
            if len(ranges) >= MAX_TEXT_SECTIONS:
                raise TextSectionError("text field exceeds the section-count limit")
        previous_blank = blank
    if text:
        ranges.append((start, len(text)))
    return tuple(ranges)


def catalog_text_sections(
    *,
    case_digest: str,
    context_id: str,
    context_digest: str,
    content_index: int,
    text: str,
) -> TextSectionCatalog:
    """Partition exact text without reading a harness, filesystem or provider."""

    if len(text) > MAX_TEXT_SECTION_BYTES:
        raise TextSectionError("text field exceeds the UTF-8 byte limit")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise TextSectionError("text field is not valid Unicode") from None
    if size > MAX_TEXT_SECTION_BYTES:
        raise TextSectionError("text field exceeds the UTF-8 byte limit")
    binding = {
        "case_digest": case_digest,
        "context_id": context_id,
        "context_digest": context_digest,
        "content_index": content_index,
        "original_text_digest": content_digest(text),
        "policy_digest": TEXT_SECTION_POLICY_DIGEST,
    }
    sections = tuple(
        TextSection(
            id="section-"
            + content_digest(
                {
                    **binding,
                    "start": start,
                    "end": end,
                    "text_digest": content_digest(text[start:end]),
                }
            ),
            start=start,
            end=end,
            utf8_bytes=len(text[start:end].encode("utf-8")),
        )
        for start, end in _paragraph_ranges(text)
    )
    return TextSectionCatalog(**binding, original_utf8_bytes=size, sections=sections)


def remove_text_sections(text: str, catalog: TextSectionCatalog, edit: TextSectionRemoval) -> str:
    """Return exact retained slices after re-deriving the complete original catalog."""

    expected = catalog_text_sections(
        case_digest=catalog.case_digest,
        context_id=catalog.context_id,
        context_digest=catalog.context_digest,
        content_index=catalog.content_index,
        text=text,
    )
    if catalog != expected:
        raise TextSectionError("section catalog does not match immutable source text")
    catalog.validate_removal(edit)
    removed = set(edit.section_ids)
    return "".join(text[s.start : s.end] for s in catalog.sections if s.id not in removed)
