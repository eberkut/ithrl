"""Discover ordinary GitHub Copilot CLI session event logs."""

from __future__ import annotations

from pathlib import Path

from ithrl.adapters.discovery import DiscoveryLimits, SourceScan, scan_store


def discover_sources(
    *,
    root: Path | None = None,
    limits: DiscoveryLimits | None = None,
    session_id: str | None = None,
) -> SourceScan:
    """Return event logs from the ordinary Copilot CLI session store."""

    store = root if root is not None else Path.home() / ".copilot" / "session-state"
    return scan_store(
        store,
        max_depth=1,
        accepts=lambda path: (
            len(path.parts) == 2
            and path.name == "events.jsonl"
            and (session_id is None or path.parent.name == session_id)
        ),
        limits=limits or DiscoveryLimits(),
    )
