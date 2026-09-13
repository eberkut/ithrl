from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ithrl.adapters.discovery import DiscoveryLimits
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.adapters.registry import (
    discover_sessions,
    parse_session,
    reopen_discovered_session,
    resolve_session,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex-session" / "rollout.jsonl"
COPILOT_FIXTURE = FIXTURES / "copilot-session" / "events.jsonl"
CODEX_SESSION_ID = "00000000-0000-4000-8000-000000000001"
COPILOT_SESSION_ID = "20000000-0000-4000-8000-000000000001"


def _codex_session(
    root: Path,
    name: str = "rollout-fixture.jsonl",
    *,
    modified_at_ns: int = 1,
) -> Path:
    destination = root / "2026" / "01" / "01" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CODEX_FIXTURE, destination)
    os.utime(destination, ns=(modified_at_ns, modified_at_ns))
    return destination


def _copilot_session(
    root: Path,
    name: str = "fixture-session",
    *,
    modified_at_ns: int = 1,
) -> Path:
    destination = root / name / "events.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(COPILOT_FIXTURE, destination)
    os.utime(destination, ns=(modified_at_ns, modified_at_ns))
    return destination


def test_discovery_orders_supported_sessions_globally(tmp_path: Path) -> None:
    codex_root = tmp_path / "codex"
    copilot_root = tmp_path / "copilot"
    _codex_session(codex_root, modified_at_ns=20)
    _copilot_session(copilot_root, modified_at_ns=20)

    result = discover_sessions(
        roots={"codex": codex_root, "copilot": copilot_root},
    )

    assert tuple(session.summary.harness for session in result.sessions) == (
        "codex",
        "copilot",
    )
    assert result.candidates_seen == 2
    assert result.rejected_candidates == 0
    assert result.truncated is False


def test_invalid_candidates_do_not_consume_the_session_limit(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    invalid = root / "2026" / "01" / "01" / "rollout-invalid.jsonl"
    invalid.parent.mkdir(parents=True)
    invalid.write_text('{"private":"do not disclose"}\n', encoding="utf-8")
    os.utime(invalid, ns=(20, 20))
    _codex_session(root, modified_at_ns=10)

    result = discover_sessions(
        harness="codex",
        roots={"codex": root},
        limits=DiscoveryLimits(max_entries=20, max_candidates=2, max_sessions=1),
    )

    assert len(result.sessions) == 1
    assert result.sessions[0].summary.session_id == CODEX_SESSION_ID
    assert result.candidates_seen == 2
    assert result.rejected_candidates == 1
    assert result.truncated is False


def test_discovery_reports_truncation_after_reaching_session_limit(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    first = _codex_session(root, "rollout-first.jsonl", modified_at_ns=20)
    _codex_session(root, "rollout-second.jsonl", modified_at_ns=10)

    result = discover_sessions(
        harness="codex",
        roots={"codex": root},
        limits=DiscoveryLimits(max_entries=20, max_candidates=2, max_sessions=1),
    )

    assert len(result.sessions) == 1
    assert result.sessions[0].source.path == first
    assert result.candidates_seen == 1
    assert result.rejected_candidates == 0
    assert result.truncated is True


def test_discovery_stops_before_exceeding_the_parse_byte_budget(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _codex_session(root, "rollout-first.jsonl", modified_at_ns=2)
    _codex_session(root, "rollout-second.jsonl", modified_at_ns=1)

    result = discover_sessions(
        harness="codex",
        roots={"codex": root},
        limits=DiscoveryLimits(
            max_entries=20,
            max_candidates=2,
            max_parse_bytes=CODEX_FIXTURE.stat().st_size,
            max_sessions=2,
        ),
    )

    assert len(result.sessions) == 1
    assert result.candidates_seen == 1
    assert result.rejected_candidates == 0
    assert result.truncated is True


def test_invalid_candidates_are_charged_to_the_parse_byte_budget(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    first = root / "2026" / "01" / "01" / "rollout-first.jsonl"
    first.parent.mkdir(parents=True)
    first.write_bytes(b'{"unsupported":true}\n')
    second = first.with_name("rollout-second.jsonl")
    second.write_bytes(b'{"unsupported":false}\n')
    os.utime(first, ns=(2, 2))
    os.utime(second, ns=(1, 1))

    result = discover_sessions(
        harness="codex",
        roots={"codex": root},
        limits=DiscoveryLimits(
            max_entries=20,
            max_candidates=2,
            max_parse_bytes=first.stat().st_size,
            max_sessions=2,
        ),
    )

    assert result.sessions == ()
    assert result.candidates_seen == 1
    assert result.rejected_candidates == 1
    assert result.truncated is True


def test_resolution_applies_the_parse_byte_budget(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    first = _codex_session(root, f"rollout-first-{CODEX_SESSION_ID}.jsonl")

    with pytest.raises(AdapterError) as caught:
        resolve_session(
            harness="codex",
            session_id=CODEX_SESSION_ID,
            root=root,
            limits=DiscoveryLimits(
                max_entries=20,
                max_candidates=2,
                max_parse_bytes=first.stat().st_size - 1,
                max_sessions=2,
            ),
        )

    assert caught.value.code is AdapterErrorCode.DISCOVERY_LIMIT_EXCEEDED


def test_resolution_filters_by_stable_id_before_candidate_limit(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    expected = _codex_session(
        root,
        f"rollout-oldest-{CODEX_SESSION_ID}.jsonl",
        modified_at_ns=1,
    )
    _codex_session(root, "rollout-newer-a.jsonl", modified_at_ns=3)
    _codex_session(root, "rollout-newer-b.jsonl", modified_at_ns=2)

    session = resolve_session(
        harness="codex",
        session_id=CODEX_SESSION_ID,
        root=root,
        limits=DiscoveryLimits(max_entries=20, max_candidates=1),
    )

    assert session.source.sha256 == parse_session(expected).source.sha256


def test_resolution_preserves_a_matching_source_error(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    source = _codex_session(root, f"rollout-fixture-{CODEX_SESSION_ID}.jsonl")
    records = [json.loads(line) for line in source.read_text().splitlines()]
    records[0]["payload"]["cli_version"] = "0.153.5"
    source.write_text("".join(json.dumps(record) + "\n" for record in records))

    with pytest.raises(AdapterError) as caught:
        resolve_session(harness="codex", session_id=CODEX_SESSION_ID, root=root)

    assert caught.value.code is AdapterErrorCode.UNSUPPORTED_VERSION


def test_missing_stores_return_an_empty_result(tmp_path: Path) -> None:
    result = discover_sessions(
        roots={
            "codex": tmp_path / "missing-codex",
            "copilot": tmp_path / "missing-copilot",
        },
    )

    assert result.sessions == ()
    assert result.candidates_seen == 0
    assert result.rejected_candidates == 0
    assert result.truncated is False


def test_resolve_session_refuses_duplicate_identifiers(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _codex_session(root, f"rollout-first-{CODEX_SESSION_ID}.jsonl")
    _codex_session(root, f"rollout-second-{CODEX_SESSION_ID}.jsonl")

    with pytest.raises(AdapterError) as caught:
        resolve_session(harness="codex", session_id=CODEX_SESSION_ID, root=root)

    assert caught.value.code is AdapterErrorCode.AMBIGUOUS_SESSION
    assert CODEX_SESSION_ID not in str(caught.value)
    assert str(root) not in str(caught.value)


def test_harness_selection_isolated_to_its_store(tmp_path: Path) -> None:
    codex_root = tmp_path / "codex"
    copilot_root = tmp_path / "copilot"
    _codex_session(codex_root)
    _copilot_session(copilot_root, COPILOT_SESSION_ID)

    result = discover_sessions(
        harness="copilot",
        roots={"codex": codex_root, "copilot": copilot_root},
    )
    session = resolve_session(
        harness="copilot",
        session_id=COPILOT_SESSION_ID,
        root=copilot_root,
    )

    assert tuple(item.summary.harness for item in result.sessions) == ("copilot",)
    assert session.metadata.format.harness == "copilot"


def test_discovery_output_and_errors_do_not_expose_captured_content(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _codex_session(root)
    private_marker = "PRIVATE-MARKER-DO-NOT-REPORT"
    invalid = root / "2026" / "01" / "01" / "rollout-private.jsonl"
    invalid.write_text(f'{{"private":"{private_marker}"}}\n', encoding="utf-8")

    result = discover_sessions(harness="codex", roots={"codex": root})

    assert "Fixture complete" not in repr(result)
    assert "fixture-command" not in repr(result)
    assert private_marker not in repr(result)

    with pytest.raises(AdapterError) as caught:
        resolve_session(
            harness="codex",
            session_id="missing-private-session",
            root=root,
        )
    assert caught.value.code is AdapterErrorCode.SESSION_NOT_FOUND
    assert private_marker not in str(caught.value)
    assert str(root) not in str(caught.value)


def test_reopen_refuses_source_changed_after_discovery(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    source = _codex_session(root)
    discovered = discover_sessions(harness="codex", roots={"codex": root}).sessions[0]
    private_marker = "PRIVATE-CHANGED-SOURCE"
    source.write_bytes(
        source.read_bytes()
        + b'{"ordinal":13,"timestamp":"2026-01-01T00:00:13.000Z",'
        + f'"type":"fixture.changed","payload":{{"private":"{private_marker}"}}}}\n'.encode()
    )

    with pytest.raises(AdapterError) as caught:
        reopen_discovered_session(discovered)

    assert caught.value.code is AdapterErrorCode.SOURCE_CHANGED
    assert private_marker not in str(caught.value)
    assert str(source) not in str(caught.value)


def test_explicit_path_parsing_does_not_require_store_discovery(tmp_path: Path) -> None:
    explicit = tmp_path / "arbitrary-name.jsonl"
    shutil.copyfile(CODEX_FIXTURE, explicit)

    session = parse_session(explicit)
    discovery = discover_sessions(
        harness="codex",
        roots={"codex": tmp_path / "missing-store"},
    )

    assert session.metadata.session_id == CODEX_SESSION_ID
    assert discovery.sessions == ()
