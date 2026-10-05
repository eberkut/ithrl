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
    SampledReductionEvidence,
    SearchResult,
)
from ithrl.results.storage import (
    load_calibration_snapshot,
    load_result_snapshot,
    load_sampling_snapshot,
    write_calibration_snapshot,
    write_result_snapshot,
    write_sampling_snapshot,
)
from ithrl.results.summary import calibration_summary_from_observations
from ithrl.results.validation import validate_sampled_result

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
    "SampledReductionEvidence",
    "SearchResult",
    "calibration_summary_from_observations",
    "load_calibration_snapshot",
    "load_result_snapshot",
    "load_sampling_snapshot",
    "validate_sampled_result",
    "write_calibration_snapshot",
    "write_result_snapshot",
    "write_sampling_snapshot",
]
