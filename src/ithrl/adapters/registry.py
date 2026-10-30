"""Composition root for built-in native session parsers."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from ithrl.adapters._jsonl import JsonlDocument, read_jsonl, read_jsonl_bytes
from ithrl.adapters.codex import PARSER as CODEX_PARSER
from ithrl.adapters.codex.context import compile_context as compile_codex_context
from ithrl.adapters.codex.context import editable_text as editable_codex_text
from ithrl.adapters.codex.decisions import enumerate_decisions as enumerate_codex_decisions
from ithrl.adapters.codex.discovery import discover_sources as discover_codex_sources
from ithrl.adapters.contracts import NativeSession, ParsingLimits
from ithrl.adapters.copilot import PARSER as COPILOT_PARSER
from ithrl.adapters.copilot.context import compile_context as compile_copilot_context
from ithrl.adapters.copilot.context import editable_text as editable_copilot_text
from ithrl.adapters.copilot.decisions import enumerate_decisions as enumerate_copilot_decisions
from ithrl.adapters.copilot.discovery import discover_sources as discover_copilot_sources
from ithrl.adapters.discovery import (
    DiscoveredSession,
    DiscoveryLimits,
    DiscoveryResult,
    HarnessName,
    SessionSource,
    SessionSummary,
    SourceScan,
)
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import ContextGraph
from ithrl.decisions import DecisionDraft, DecisionSelector, DecisionSummary, SelectedDecision
from ithrl.mutation import EditableText


class NativeSessionParser(Protocol):
    """One harness-specific detector and parser."""

    format_name: str

    def matches(self, document: JsonlDocument) -> bool: ...

    def parse(self, document: JsonlDocument) -> NativeSession: ...


class DecisionEnumerator(Protocol):
    """Interpret completed decisions in one validated native format."""

    def __call__(self, session: NativeSession) -> tuple[DecisionDraft, ...]: ...


class ContextCompiler(Protocol):
    """Compile context for one selected native decision."""

    def __call__(self, session: NativeSession, selector: DecisionSelector) -> ContextGraph: ...


class SessionDiscoverer(Protocol):
    """Find candidate files in one harness's ordinary local store."""

    def __call__(
        self,
        *,
        root: Path | None = None,
        limits: DiscoveryLimits | None = None,
        session_id: str | None = None,
    ) -> SourceScan: ...


class TextDiscoverer(Protocol):
    """Interpret editable text in one verified native decision context."""

    def __call__(
        self, session: NativeSession, selector: DecisionSelector, context: ContextGraph
    ) -> tuple[EditableText, ...]: ...


@dataclass(frozen=True, slots=True)
class _AdapterRegistration:
    harness: HarnessName
    parser: NativeSessionParser
    discoverer: SessionDiscoverer
    decision_enumerator: DecisionEnumerator
    context_compiler: ContextCompiler
    text_discoverer: TextDiscoverer | None = None


@dataclass(frozen=True, slots=True)
class _DiscoveryCollection:
    sessions: tuple[DiscoveredSession, ...]
    candidates_seen: int
    rejected_candidates: int
    rejection_codes: tuple[AdapterErrorCode, ...]


_BUILTIN_ADAPTERS = (
    _AdapterRegistration(
        harness="codex",
        parser=CODEX_PARSER,
        discoverer=discover_codex_sources,
        decision_enumerator=enumerate_codex_decisions,
        context_compiler=compile_codex_context,
        text_discoverer=editable_codex_text,
    ),
    _AdapterRegistration(
        harness="copilot",
        parser=COPILOT_PARSER,
        discoverer=discover_copilot_sources,
        decision_enumerator=enumerate_copilot_decisions,
        context_compiler=compile_copilot_context,
        text_discoverer=editable_copilot_text,
    ),
)
_ADAPTERS_BY_HARNESS = {adapter.harness: adapter for adapter in _BUILTIN_ADAPTERS}
_ADAPTERS_BY_FORMAT = {adapter.parser.format_name: adapter for adapter in _BUILTIN_ADAPTERS}
_BUILTIN_PARSERS = tuple(adapter.parser for adapter in _BUILTIN_ADAPTERS)


def editable_session_text(
    session: NativeSession, selector: DecisionSelector, context: ContextGraph
) -> tuple[EditableText, ...]:
    """Discover text through the native adapter after source and decision validation."""

    adapter = _ADAPTERS_BY_FORMAT.get(session.metadata.format.name)
    if adapter is None or adapter.text_discoverer is None:
        return ()
    return adapter.text_discoverer(session, selector, context)


def discover_sessions(
    *,
    harness: HarnessName | None = None,
    roots: Mapping[HarnessName, Path] | None = None,
    limits: DiscoveryLimits | None = None,
) -> DiscoveryResult:
    """Find recent supported sessions without exposing their captured content."""

    selected_limits = limits or DiscoveryLimits()
    scans = _discover_source_scans(harness, roots, selected_limits)
    candidates = _ordered_sources(scans)
    collection = _collect_sessions(candidates, selected_limits)
    return DiscoveryResult(
        sessions=collection.sessions,
        candidates_seen=collection.candidates_seen,
        rejected_candidates=collection.rejected_candidates,
        rejection_codes=collection.rejection_codes,
        truncated=(
            any(scan.truncated for _, scan in scans) or collection.candidates_seen < len(candidates)
        ),
    )


def _discover_source_scans(
    harness: HarnessName | None,
    roots: Mapping[HarnessName, Path] | None,
    limits: DiscoveryLimits,
) -> tuple[tuple[HarnessName, SourceScan], ...]:
    harnesses = (harness,) if harness is not None else tuple(_ADAPTERS_BY_HARNESS)
    return tuple(
        (
            selected_harness,
            _ADAPTERS_BY_HARNESS[selected_harness].discoverer(
                root=roots.get(selected_harness) if roots is not None else None,
                limits=limits,
            ),
        )
        for selected_harness in harnesses
    )


def _ordered_sources(
    scans: tuple[tuple[HarnessName, SourceScan], ...],
) -> tuple[tuple[SessionSource, HarnessName], ...]:
    return tuple(
        sorted(
            ((candidate, harness) for harness, scan in scans for candidate in scan.sources),
            key=_discovered_source_sort_key,
        )
    )


def _collect_sessions(
    candidates: tuple[tuple[SessionSource, HarnessName], ...],
    limits: DiscoveryLimits,
) -> _DiscoveryCollection:
    sessions: list[DiscoveredSession] = []
    rejected = 0
    rejection_codes: dict[AdapterErrorCode, None] = {}
    considered = 0
    parsed_bytes = 0
    for source, expected_harness in candidates:
        if len(sessions) == limits.max_sessions:
            break
        remaining_bytes = limits.max_parse_bytes - parsed_bytes
        if remaining_bytes < 1:
            break
        considered += 1
        session: NativeSession | None = None
        try:
            session = parse_session(
                source.path,
                limits=_parsing_limits(max_bytes=remaining_bytes),
            )
            parsed_bytes += session.source.size_bytes
            summary = _summarize_session(session, expected_harness=expected_harness)
        except AdapterError as error:
            if session is None:
                parsed_bytes += error.bytes_read
            rejected += 1
            rejection_codes.setdefault(error.code, None)
            continue
        sessions.append(
            DiscoveredSession(
                source=source,
                summary=summary,
                source_facts=session.source,
            )
        )
    return _DiscoveryCollection(
        sessions=tuple(sessions),
        candidates_seen=considered,
        rejected_candidates=rejected,
        rejection_codes=tuple(rejection_codes),
    )


def _discovered_source_sort_key(
    item: tuple[SessionSource, HarnessName],
) -> tuple[int, str]:
    source, _ = item
    return -source.modified_at_ns, source.path.as_posix()


def resolve_session(
    *,
    harness: HarnessName,
    session_id: str,
    root: Path | None = None,
    limits: DiscoveryLimits | None = None,
) -> NativeSession:
    """Resolve one stable native session identifier within a known harness store."""

    if not session_id or len(session_id) > 1024:
        raise AdapterError(
            AdapterErrorCode.SESSION_NOT_FOUND,
            "session was not found in the selected harness store",
        )
    selected_limits = limits or DiscoveryLimits()
    scan = _ADAPTERS_BY_HARNESS[harness].discoverer(
        root=root,
        limits=selected_limits,
        session_id=session_id,
    )
    if not scan.sources:
        raise AdapterError(
            AdapterErrorCode.SESSION_NOT_FOUND,
            "session was not found in the selected harness store",
        )
    if len(scan.sources) != 1 or scan.truncated:
        raise AdapterError(
            AdapterErrorCode.AMBIGUOUS_SESSION,
            "session identifier matches more than one source file",
        )
    source = scan.sources[0]
    if source.size_bytes > selected_limits.max_parse_bytes:
        raise AdapterError(
            AdapterErrorCode.DISCOVERY_LIMIT_EXCEEDED,
            "session resolution exceeds the parse byte limit",
        )
    session = parse_session(
        source.path,
        limits=_parsing_limits(max_bytes=selected_limits.max_parse_bytes),
    )
    if session.metadata.format.harness != harness or session.metadata.session_id != session_id:
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session identifier does not match its native store entry",
        )
    return session


def _parsing_limits(*, max_bytes: int) -> ParsingLimits:
    defaults = ParsingLimits()
    candidate_max_bytes = min(defaults.max_bytes, max_bytes)
    return ParsingLimits(
        max_bytes=candidate_max_bytes,
        max_line_bytes=min(defaults.max_line_bytes, candidate_max_bytes),
        max_records=defaults.max_records,
        max_json_items=defaults.max_json_items,
    )


def reopen_discovered_session(discovered: DiscoveredSession) -> NativeSession:
    """Reopen a listed session and refuse evidence changed after discovery."""

    try:
        session = parse_session(discovered.source.path)
    except AdapterError:
        raise AdapterError(
            AdapterErrorCode.SOURCE_CHANGED,
            "native session changed after it was listed",
        ) from None
    if session.source != discovered.source_facts or (
        _summarize_session(session, expected_harness=discovered.summary.harness)
        != discovered.summary
    ):
        raise AdapterError(
            AdapterErrorCode.SOURCE_CHANGED,
            "native session changed after it was listed",
        )
    return session


def parse_session(
    path: str | os.PathLike[str],
    *,
    limits: ParsingLimits | None = None,
) -> NativeSession:
    """Detect and parse one supported native session JSONL file."""

    document = read_jsonl(path, limits=limits or ParsingLimits())
    try:
        return _parse_document(document, _BUILTIN_PARSERS)
    except AdapterError as error:
        error.bytes_read = document.source.size_bytes
        raise


def parse_session_bytes(
    source: bytes,
    *,
    limits: ParsingLimits | None = None,
) -> NativeSession:
    """Detect and parse exact native session bytes retained by a case."""

    document = read_jsonl_bytes(source, limits=limits or ParsingLimits())
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
    try:
        return matches[0].parse(document)
    except AdapterError:
        raise
    except (ValidationError, ValueError):
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session metadata is outside the supported bounds",
        ) from None


def enumerate_decisions(session: NativeSession) -> tuple[DecisionSummary, ...]:
    """List completed decisions without exposing their captured content."""

    return tuple(draft.summarize() for draft in _decision_drafts(session))


def _summarize_session(
    session: NativeSession,
    *,
    expected_harness: HarnessName,
) -> SessionSummary:
    if session.metadata.format.harness != expected_harness:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_FORMAT,
            "session source does not match its native store layout",
        )
    decisions = enumerate_decisions(session)
    if not decisions:
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session contains no supported completed decisions",
        )
    target_types = tuple(
        dict.fromkeys(target.type for decision in decisions for target in decision.targets)
    )
    limitations = tuple(
        dict.fromkeys(limitation for decision in decisions for limitation in decision.limitations)
    )
    return SessionSummary(
        harness=expected_harness,
        session_id=session.metadata.session_id,
        producer_version=session.metadata.format.producer_version,
        captured_at=session.metadata.captured_at,
        decision_count=len(decisions),
        target_types=target_types,
        limitations=limitations,
    )


def select_decision(
    session: NativeSession,
    *,
    decision_id: str,
    target_id: str,
) -> SelectedDecision:
    """Select one completed decision and behavior target by stable ID."""

    drafts = _decision_drafts(session)
    matches = tuple(draft for draft in drafts if draft.decision_id == decision_id)
    if len(matches) != 1:
        raise AdapterError(
            AdapterErrorCode.DECISION_NOT_FOUND,
            "completed decision was not found in the selected session",
        )
    draft = matches[0]
    if target_id not in {candidate.target_id for candidate in draft.targets}:
        raise AdapterError(
            AdapterErrorCode.TARGET_NOT_FOUND,
            "behavior target was not found for the selected decision",
        )
    compiler = _ADAPTERS_BY_FORMAT[session.metadata.format.name].context_compiler
    try:
        context = compiler(session, draft.selector)
    except AdapterError:
        raise
    except (ValidationError, ValueError):
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session context cannot be compiled safely",
        ) from None
    return draft.select(target_id, context=context)


def _decision_drafts(session: NativeSession) -> tuple[DecisionDraft, ...]:
    adapter = _ADAPTERS_BY_FORMAT.get(session.metadata.format.name)
    if adapter is None:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_FORMAT,
            "session format does not support decision enumeration",
        )
    try:
        return adapter.decision_enumerator(session)
    except AdapterError:
        raise
    except (ValidationError, ValueError):
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session decision metadata is not supported",
        ) from None
