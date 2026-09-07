"""Persisted result snapshots and their public API."""

from ithrl.results.models import (
    CalibrationResult,
    ConfirmationResult,
    ResultSnapshot,
    ResultSnapshotError,
    ResultStatus,
    ResultSummary,
    SearchResult,
)
from ithrl.results.storage import load_result_snapshot, write_result_snapshot

__all__ = [
    "CalibrationResult",
    "ConfirmationResult",
    "ResultSnapshot",
    "ResultSnapshotError",
    "ResultStatus",
    "ResultSummary",
    "SearchResult",
    "load_result_snapshot",
    "write_result_snapshot",
]
