"""Find Codex rollout sessions in an ordinary local store."""

from __future__ import annotations

import os
from pathlib import Path

from ithrl.adapters.discovery import DiscoveryLimits, SourceScan, scan_store

_STORE_DEPTH = 3


def discover_sources(
    *,
    root: Path | None = None,
    limits: DiscoveryLimits | None = None,
    session_id: str | None = None,
) -> SourceScan:
    """Return recent Codex rollout files from a local session store."""

    selected_limits = limits or DiscoveryLimits()
    return scan_store(
        root if root is not None else _default_store(),
        max_depth=_STORE_DEPTH,
        accepts=lambda path: (
            _is_rollout_path(path)
            and (session_id is None or path.name.endswith(f"-{session_id}.jsonl"))
        ),
        limits=selected_limits,
    )


def _default_store() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def _is_rollout_path(path: Path) -> bool:
    parts = path.parts
    if len(parts) != 4:
        return False
    year, month, day, filename = parts
    return all(
        (
            _is_ascii_number(year, width=4),
            _is_ascii_number(month, width=2),
            _is_ascii_number(day, width=2),
            _is_rollout_filename(filename),
        )
    )


def _is_ascii_number(value: str, *, width: int) -> bool:
    return len(value) == width and value.isascii() and value.isdigit()


def _is_rollout_filename(value: str) -> bool:
    return value.startswith("rollout-") and value != "rollout-.jsonl" and value.endswith(".jsonl")
