"""Persisted result snapshots and their public API."""

from ithrl.results.models import (
    CalibrationOutcome,
    CalibrationResult,
    CalibrationSnapshot,
    CalibrationStopReason,
    CalibrationSummary,
    ConfirmationResult,
    ResultSnapshot,
    ResultSnapshotError,
    ResultStatus,
    ResultSummary,
    SearchResult,
)
from ithrl.results.storage import (
    load_calibration_snapshot,
    load_result_snapshot,
    write_calibration_snapshot,
    write_result_snapshot,
)
from ithrl.results.summary import calibration_summary_from_observations

__all__ = [
    "CalibrationOutcome",
    "CalibrationResult",
    "CalibrationSnapshot",
    "CalibrationStopReason",
    "CalibrationSummary",
    "ConfirmationResult",
    "ResultSnapshot",
    "ResultSnapshotError",
    "ResultStatus",
    "ResultSummary",
    "SearchResult",
    "calibration_summary_from_observations",
    "load_calibration_snapshot",
    "load_result_snapshot",
    "write_calibration_snapshot",
    "write_result_snapshot",
]
