from __future__ import annotations

import os
from pathlib import Path

import pytest

from ithrl.adapters.copilot.discovery import discover_sources
from ithrl.adapters.discovery import DiscoveryLimits
from ithrl.adapters.errors import AdapterError, AdapterErrorCode


def _session_log(root: Path, session_id: str) -> Path:
    session = root / session_id
    session.mkdir(parents=True)
    log = session / "events.jsonl"
    log.write_text("{}\n")
    return log


def test_discovery_matches_only_session_event_logs(tmp_path: Path) -> None:
    expected = _session_log(tmp_path, "session-a")
    (tmp_path / "events.jsonl").write_text("{}\n")
    (tmp_path / "session-a" / "other.jsonl").write_text("{}\n")
    nested = tmp_path / "container" / "session-b"
    nested.mkdir(parents=True)
    (nested / "events.jsonl").write_text("{}\n")

    sources = discover_sources(root=tmp_path).sources

    assert tuple(source.path for source in sources) == (expected,)


def test_discovery_orders_newest_first_then_by_path(tmp_path: Path) -> None:
    older = _session_log(tmp_path, "older")
    alpha = _session_log(tmp_path, "alpha")
    beta = _session_log(tmp_path, "beta")
    os.utime(older, ns=(1, 1))
    os.utime(alpha, ns=(2, 2))
    os.utime(beta, ns=(2, 2))

    sources = discover_sources(root=tmp_path).sources

    assert tuple(source.path for source in sources) == (alpha, beta, older)
    assert tuple(source.modified_at_ns for source in sources) == (2, 2, 1)


def test_missing_store_returns_no_sources(tmp_path: Path) -> None:
    assert discover_sources(root=tmp_path / "missing").sources == ()


def test_default_store_uses_the_ordinary_copilot_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _session_log(tmp_path / ".copilot" / "session-state", "session-a")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sources = discover_sources().sources

    assert tuple(source.path for source in sources) == (expected,)


def test_root_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "store"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(AdapterError) as captured:
        discover_sources(root=link)

    assert captured.value.code is AdapterErrorCode.UNSAFE_SOURCE


def test_candidate_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n")
    session = tmp_path / "session-a"
    session.mkdir()
    (session / "events.jsonl").symlink_to(target)

    with pytest.raises(AdapterError) as captured:
        discover_sources(root=tmp_path)

    assert captured.value.code is AdapterErrorCode.UNSAFE_SOURCE


def test_linked_session_directory_is_not_followed(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _session_log(external, "session-a")
    store = tmp_path / "store"
    store.mkdir()
    (store / "linked-session").symlink_to(
        external / "session-a",
        target_is_directory=True,
    )

    assert discover_sources(root=store).sources == ()


def test_entry_limit_is_enforced(tmp_path: Path) -> None:
    (tmp_path / "irrelevant-a").write_text("")
    (tmp_path / "irrelevant-b").write_text("")

    with pytest.raises(AdapterError) as captured:
        discover_sources(
            root=tmp_path,
            limits=DiscoveryLimits(max_entries=1, max_candidates=1),
        )

    assert captured.value.code is AdapterErrorCode.DISCOVERY_LIMIT_EXCEEDED


def test_candidate_limit_retains_a_bounded_scan(tmp_path: Path) -> None:
    first = _session_log(tmp_path, "session-a")
    second = _session_log(tmp_path, "session-b")
    os.utime(first, ns=(2, 2))
    os.utime(second, ns=(1, 1))

    scan = discover_sources(
        root=tmp_path,
        limits=DiscoveryLimits(max_entries=10, max_candidates=1),
    )

    assert tuple(source.path for source in scan.sources) == (first,)
    assert scan.truncated is True


def test_session_id_filter_is_applied_before_candidate_limit(tmp_path: Path) -> None:
    expected = _session_log(tmp_path, "wanted")
    _session_log(tmp_path, "other-a")
    _session_log(tmp_path, "other-b")

    scan = discover_sources(
        root=tmp_path,
        session_id="wanted",
        limits=DiscoveryLimits(max_entries=20, max_candidates=1),
    )

    assert tuple(source.path for source in scan.sources) == (expected,)
    assert scan.truncated is False
