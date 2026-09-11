"""Composition root for built-in native session parsers."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from ithrl.adapters._jsonl import JsonlDocument, read_jsonl
from ithrl.adapters.codex import PARSER as CODEX_PARSER
from ithrl.adapters.contracts import NativeSession, ParsingLimits
from ithrl.adapters.copilot import PARSER as COPILOT_PARSER
from ithrl.adapters.errors import AdapterError, AdapterErrorCode


class NativeSessionParser(Protocol):
    """One harness-specific detector and parser."""

    format_name: str

    def matches(self, document: JsonlDocument) -> bool: ...

    def parse(self, document: JsonlDocument) -> NativeSession: ...


_BUILTIN_PARSERS: tuple[NativeSessionParser, ...] = (CODEX_PARSER, COPILOT_PARSER)


def parse_session(
    path: str | os.PathLike[str],
    *,
    limits: ParsingLimits | None = None,
) -> NativeSession:
    """Detect and parse one supported native session JSONL file."""

    document = read_jsonl(path, limits=limits or ParsingLimits())
    return _parse_document(document, _BUILTIN_PARSERS)


def _parse_document(
    document: JsonlDocument,
    parsers: Sequence[NativeSessionParser],
) -> NativeSession:
    matches = tuple(parser for parser in parsers if parser.matches(document))
    if not matches:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_FORMAT,
            "session source does not match a supported native format",
        )
    if len(matches) > 1:
        raise AdapterError(
            AdapterErrorCode.AMBIGUOUS_FORMAT,
            "session source matches more than one native format",
        )
    return matches[0].parse(document)
