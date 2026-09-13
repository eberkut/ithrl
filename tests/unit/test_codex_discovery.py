from __future__ import annotations

import os
from pathlib import Path

import pytest

from ithrl.adapters.codex.discovery import discover_sources
from ithrl.adapters.discovery import DiscoveryLimits
from ithrl.adapters.errors import AdapterError, AdapterErrorCode


def _rollout(root: Path, relative: str, *, modified_at_ns: int = 1) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, ns=(modified_at_ns, modified_at_ns))
    return path


def test_discovers_only_fixed_rollout_layout_in_deterministic_order(tmp_path: Path) -> None:
    older = _rollout(tmp_path, "2026/01/01/rollout-older.jsonl", modified_at_ns=10)
    tied_a = _rollout(tmp_path, "2026/01/02/rollout-a.jsonl", modified_at_ns=20)
    tied_b = _rollout(tmp_path, "2026/01/02/rollout-b.jsonl", modified_at_ns=20)

    _rollout(tmp_path, "2026/01/rollout-shallow.jsonl")
    _rollout(tmp_path, "2026/01/01/deeper/rollout-deep.jsonl")
    _rollout(tmp_path, "26/01/01/rollout-short-year.jsonl")
    _rollout(tmp_path, "2026/1/01/rollout-short-month.jsonl")
    _rollout(tmp_path, "2026/01/1/rollout-short-day.jsonl")
    _rollout(tmp_path, "2026/01/01/session.jsonl")
    _rollout(tmp_path, "2026/01/01/rollout-no-jsonl.txt")
    _rollout(tmp_path, "2026/01/01/rollout-.jsonl")

    scan = discover_sources(root=tmp_path)

    assert [source.path for source in scan.sources] == [tied_a, tied_b, older]
    assert [source.modified_at_ns for source in scan.sources] == [20, 20, 10]
    assert [source.size_bytes for source in scan.sources] == [3, 3, 3]
    assert scan.truncated is False


def test_missing_store_has_no_sessions(tmp_path: Path) -> None:
    assert discover_sources(root=tmp_path / "missing").sources == ()


def test_default_store_uses_home_codex_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _rollout(tmp_path / ".codex/sessions", "2026/01/01/rollout-one.jsonl")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert [source.path for source in discover_sources().sources] == [expected]


def test_default_store_honors_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "custom-codex"
    expected = _rollout(codex_home / "sessions", "2026/01/01/rollout-one.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert [source.path for source in discover_sources().sources] == [expected]


def test_explicit_root_takes_precedence_over_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit"
    expected = _rollout(explicit, "2026/01/01/rollout-one.jsonl")
    _rollout(tmp_path / "configured/sessions", "2026/01/01/rollout-other.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "configured"))

    assert [source.path for source in discover_sources(root=explicit).sources] == [expected]


def test_does_not_follow_symlink_directories(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside"
    _rollout(outside, "01/01/rollout-outside.jsonl")
    (root / "2026").symlink_to(outside, target_is_directory=True)

    assert discover_sources(root=root).sources == ()


def test_rejects_symlink_root(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(store, target_is_directory=True)

    with pytest.raises(AdapterError, match="must be a directory, not a link") as caught:
        discover_sources(root=linked_store)

    assert caught.value.code is AdapterErrorCode.UNSAFE_SOURCE


def test_rejects_matching_symlink_candidate(tmp_path: Path) -> None:
    target = _rollout(tmp_path, "target.jsonl")
    candidate = tmp_path / "2026/01/01/rollout-linked.jsonl"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(target)

    with pytest.raises(AdapterError, match="candidate must be a regular file") as caught:
        discover_sources(root=tmp_path)

    assert caught.value.code is AdapterErrorCode.UNSAFE_SOURCE


def test_applies_entry_limit_and_retains_newest_candidates(tmp_path: Path) -> None:
    newest = _rollout(tmp_path, "2026/01/01/rollout-newest.jsonl", modified_at_ns=30)
    _rollout(tmp_path, "2026/01/01/rollout-middle.jsonl", modified_at_ns=20)
    _rollout(tmp_path, "2026/01/01/rollout-oldest.jsonl", modified_at_ns=10)

    scan = discover_sources(
        root=tmp_path,
        limits=DiscoveryLimits(max_entries=20, max_candidates=2, max_sessions=3),
    )

    assert scan.sources[0].path == newest
    assert len(scan.sources) == 2
    assert scan.truncated is True

    with pytest.raises(AdapterError, match="entry limit") as entries:
        discover_sources(
            root=tmp_path,
            limits=DiscoveryLimits(max_entries=3, max_candidates=3, max_sessions=3),
        )
    assert entries.value.code is AdapterErrorCode.DISCOVERY_LIMIT_EXCEEDED


def test_session_id_filter_is_applied_before_candidate_limit(tmp_path: Path) -> None:
    wanted = "00000000-0000-4000-8000-000000000001"
    expected = _rollout(
        tmp_path,
        f"2026/01/01/rollout-fixture-{wanted}.jsonl",
        modified_at_ns=1,
    )
    _rollout(tmp_path, "2026/01/01/rollout-newer-a.jsonl", modified_at_ns=3)
    _rollout(tmp_path, "2026/01/01/rollout-newer-b.jsonl", modified_at_ns=2)

    scan = discover_sources(
        root=tmp_path,
        session_id=wanted,
        limits=DiscoveryLimits(max_entries=20, max_candidates=1),
    )

    assert tuple(source.path for source in scan.sources) == (expected,)
    assert scan.truncated is False
