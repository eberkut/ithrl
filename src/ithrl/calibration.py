"""Bounded full-context calibration for sampled runner profiles."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.context import content_digest
from ithrl.experiment import (
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledExperimentDefinition,
    SamplingEligibilityPolicy,
)
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.path_safety import UnsafePathError, private_file_lock
from ithrl.results import (
    CalibrationOutcome,
    CalibrationSnapshot,
    CalibrationStopReason,
    calibration_summary_from_observations,
    load_calibration_snapshot,
    write_calibration_snapshot,
)
from ithrl.runner import Candidate, PreparedAttempt, prepare_candidate
from ithrl.workflow import PreparedExperiment


class CalibrationBusyError(RuntimeError):
    """Another caller holds the observation store's calibration lock."""


class CalibrationInFlightError(RuntimeError):
    """A possibly dispatched attempt has no durable observation."""


class CalibrationWallTimeBudgetError(RuntimeError):
    """Command admission cannot fit the remaining calibration before its wall-time limit."""


class _InFlightAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    identity: ObservationIdentity
    candidate_digest: str = Field(min_length=1)
    attempt: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class SampledCalibrationResult:
    """A content-safe classification backed by exact baseline observations."""

    outcome: CalibrationOutcome
    stop_reason: CalibrationStopReason
    observations: tuple[Observation, ...]

    @property
    def matches(self) -> int:
        return sum(item.outcome is ObservationOutcome.MATCH for item in self.observations)

    @property
    def no_matches(self) -> int:
        return sum(item.outcome is ObservationOutcome.NO_MATCH for item in self.observations)

    @property
    def valid_samples(self) -> int:
        return self.matches + self.no_matches


def run_sampled_calibration(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    before_attempt: Callable[[], None] | None = None,
    eligibility_policy: SamplingEligibilityPolicy | None = None,
) -> SampledCalibrationResult:
    """Run or resume calibration, checking optional command admission before each attempt.

    Standalone calibration retains its frozen attempt limits. A composing command can
    additionally enforce its overall resource allowance without changing that plan.
    """

    experiment = _sampled_experiment(prepared)
    eligibility_policy = _eligibility_policy(experiment, eligibility_policy)
    with _calibration_lock(store):
        _reconcile_inflight(store)
        candidate = prepare_candidate(evidence, prepared.execution_plan)
        try:
            return asyncio.run(
                _run_sampled_calibration(
                    evidence=evidence,
                    prepared=prepared,
                    store=store,
                    candidate=candidate,
                    experiment=experiment,
                    before_attempt=before_attempt,
                    eligibility_policy=eligibility_policy,
                )
            )
        except (KeyboardInterrupt, asyncio.CancelledError, CalibrationWallTimeBudgetError) as error:
            observations = _compatible(
                prepared, store, candidate.digest, experiment, eligibility_policy=eligibility_policy
            )
            classified = _classify(
                observations,
                experiment,
                interrupted=not isinstance(error, CalibrationWallTimeBudgetError),
                wall_time_stopped=isinstance(error, CalibrationWallTimeBudgetError),
                eligibility_policy=eligibility_policy,
            )
            if classified is None:
                raise RuntimeError(
                    "interrupted calibration has no terminal classification"
                ) from None
            return classified


def save_sampled_calibration_result(
    *,
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    result: SampledCalibrationResult,
) -> CalibrationSnapshot:
    """Persist a private, cross-validated snapshot of one calibration block."""

    with _calibration_lock(store):
        _reconcile_inflight(store)
        return _save_sampled_calibration_result(
            workspace=workspace,
            evidence=evidence,
            prepared=prepared,
            store=store,
            result=result,
        )


def _save_sampled_calibration_result(
    *,
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    result: SampledCalibrationResult,
) -> CalibrationSnapshot:
    experiment = _sampled_experiment(prepared)
    if workspace.load().digest != evidence.digest:
        raise ValueError("calibration result case does not match its workspace")
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    current = _compatible(prepared, store, candidate.digest, experiment)
    if current != result.observations:
        raise ValueError("calibration observations changed before result persistence")
    classified = _classify(
        current,
        experiment,
        interrupted=result.stop_reason == "user-interrupt",
        wall_time_stopped=result.stop_reason == "wall-time-budget",
    )
    if classified != result:
        raise ValueError("calibration outcome does not match its observations")

    request = evidence.materialize(frozenset()) if evidence.has_request else None
    retained_nodes = evidence.context.nodes
    provenance = evidence.manifest.provenance.model_dump(mode="json")
    run_id = f"calibration-{candidate.digest[:12]}-{uuid4().hex[:8]}"
    base = {
        "run_id": run_id,
        "outcome": result.outcome,
        "stop_reason": result.stop_reason,
        "case_digest": evidence.digest,
        "experiment_digest": experiment.digest,
        "target_digest": experiment.target.digest,
        "execution_plan_digest": prepared.execution_plan.digest,
        "candidate_digest": candidate.digest,
        "candidate_materialization": candidate.materialization,
        "source_artifact_digests": candidate.source_artifact_digests,
        "request_digest": content_digest(request) if request is not None else None,
        "retained_context_digest": content_digest(
            [node.model_dump(mode="json") for node in retained_nodes]
        ),
        "provenance_digest": content_digest(provenance),
        "runner_fingerprint": prepared.runner.fingerprint,
        "algorithm_digest": prepared.identity.algorithm_digest,
        "original_context_ids": tuple(node.id for node in retained_nodes),
        "retained_context_ids": candidate.retained_context_ids,
        "source_case": str(workspace.root.absolute()),
    }
    summary = calibration_summary_from_observations(
        base=base,
        observations=current,
        expected_policy_digest=prepared.safety_policy.digest,
        max_provider_calls_per_attempt=(
            prepared.execution_plan.capability.provider_calls_per_attempt
        ),
    )
    destination = workspace.root / "results" / run_id
    write_calibration_snapshot(
        destination=destination,
        summary=summary,
        request=request,
        retained_context_lines=tuple(node.model_dump_json() for node in retained_nodes),
        experiment_source=experiment.to_toml(),
        provenance=provenance,
        execution_plan=prepared.execution_plan,
    )
    return load_calibration_snapshot(destination)


async def _run_sampled_calibration(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    candidate: Candidate,
    experiment: SampledExperimentDefinition,
    before_attempt: Callable[[], None] | None = None,
    eligibility_policy: SamplingEligibilityPolicy | None = None,
) -> SampledCalibrationResult:
    # The candidate is always the unchanged full-context baseline.
    while True:
        observations = _compatible(
            prepared, store, candidate.digest, experiment, eligibility_policy=eligibility_policy
        )
        classified = _classify(
            observations, experiment, interrupted=False, eligibility_policy=eligibility_policy
        )
        if classified is not None:
            return classified
        if before_attempt is not None:
            before_attempt()
        reservation = _InFlightAttempt(
            identity=prepared.identity,
            candidate_digest=candidate.digest,
            attempt=len(observations) + 1,
        )
        _reserve_attempt(store, reservation)
        try:
            await observe_once(
                runner=prepared.runner,
                identity=prepared.identity,
                store=store,
                eligibility_policy=_eligibility_policy(experiment, eligibility_policy),
                attempt=PreparedAttempt(
                    evidence=evidence,
                    candidate=candidate,
                    target=experiment.target,
                    execution_plan=prepared.execution_plan,
                    safety_policy=prepared.safety_policy,
                    resource_limits=prepared.resource_limits,
                    phase="calibration",
                    arm="baseline",
                ),
            )
        except RunnerExecutionError as error:
            if error.observation is None:
                raise
            # The store owns the attempt number and exact evidence. Re-read it before
            # deciding whether the next attempt is permitted.
        finally:
            _reconcile_inflight(store)


def _eligibility_policy(
    experiment: SampledExperimentDefinition,
    override: SamplingEligibilityPolicy | None,
) -> SamplingEligibilityPolicy:
    return override or (
        experiment.reduction.eligibility_policy
        if experiment.reduction is not None
        else "independent-safe-preaccept-only"
    )


def _classify(
    observations: tuple[Observation, ...],
    experiment: SampledExperimentDefinition,
    *,
    interrupted: bool,
    wall_time_stopped: bool = False,
    eligibility_policy: SamplingEligibilityPolicy | None = None,
) -> SampledCalibrationResult | None:
    phase = experiment.calibration
    if sum(item.valid for item in observations) >= phase.required_valid:
        matches = sum(item.outcome is ObservationOutcome.MATCH for item in observations)
        if matches >= phase.minimum_matches:
            outcome: CalibrationOutcome = "measurable"
        elif matches == 0:
            outcome = "not-measurable"
        else:
            outcome = "inconclusive"
        return SampledCalibrationResult(outcome, "valid-sample-limit", observations)
    unsafe = next(
        (
            item
            for item in reversed(observations)
            if not item.allows_fresh_attempt(_eligibility_policy(experiment, eligibility_policy))
        ),
        None,
    )
    if unsafe is not None and unsafe.error_category in {"safety", "contract"}:
        return _stopped_for_error(observations, unsafe)
    if interrupted:
        return SampledCalibrationResult("incomplete", "user-interrupt", observations)
    if unsafe is not None:
        return _stopped_for_error(observations, unsafe)
    if len(observations) >= phase.max_attempts:
        return SampledCalibrationResult("incomplete", "attempt-limit", observations)
    if wall_time_stopped:
        return SampledCalibrationResult("incomplete", "wall-time-budget", observations)
    return None


def _stopped_for_error(
    observations: tuple[Observation, ...], error: Observation
) -> SampledCalibrationResult:
    if error.error_category in {"safety", "contract"}:
        return SampledCalibrationResult("refused", "safety-refusal", observations)
    return SampledCalibrationResult("incomplete", "unsafe-retry", observations)


def _sampled_experiment(prepared: PreparedExperiment) -> SampledExperimentDefinition:
    experiment = prepared.experiment
    if not isinstance(experiment, SampledExperimentDefinition):
        raise ValueError("sampled calibration requires a sampled experiment")
    if prepared.execution_plan.experiment_digest != experiment.digest:
        raise ValueError("sampled calibration plan does not match the experiment")
    profile = prepared.runner.capability_profile
    if profile.deterministic and not isinstance(experiment.runner, FakeRunnerConfig):
        raise ValueError("deterministic calibration requires the credential-free fake runner")
    if profile.digest != prepared.execution_plan.capability.digest:
        raise ValueError("sampled calibration runner profile changed after preflight")
    if (
        isinstance(experiment.runner, NativeRunnerConfig)
        and prepared.runner.capability_profile.digest
        != experiment.runner.required_capability_digest
    ):
        raise ValueError("sampled calibration runner profile changed after preflight")
    return experiment


def _compatible(
    prepared: PreparedExperiment,
    store: ObservationStore,
    candidate_digest: str,
    experiment: SampledExperimentDefinition,
    *,
    eligibility_policy: SamplingEligibilityPolicy | None = None,
) -> tuple[Observation, ...]:
    observations = store.list_compatible(
        prepared.identity,
        candidate_digest=candidate_digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
    )
    if len(observations) > experiment.calibration.max_attempts or any(
        item.attempt != index for index, item in enumerate(observations, start=1)
    ):
        raise ValueError("stored calibration attempts exceed or break the configured sequence")
    valid = 0
    for index, item in enumerate(observations):
        if valid >= experiment.calibration.required_valid:
            raise ValueError("stored calibration attempts continue after a complete sample block")
        if index and not observations[index - 1].allows_fresh_attempt(
            _eligibility_policy(experiment, eligibility_policy)
        ):
            raise ValueError("stored calibration attempts continue after an unsafe attempt")
        valid += item.valid
    return observations


@contextmanager
def _calibration_lock(store: ObservationStore) -> Generator[None]:
    """Serialize budget decisions with execution and snapshot persistence."""

    store.initialize()
    lock_path = store.path.with_name(f"{store.path.name}.calibration.lock")
    try:
        lock = private_file_lock(lock_path)
    except UnsafePathError as error:
        raise ValueError("calibration lock file is unsafe") from error
    try:
        lock.acquire()
    except Timeout as error:
        raise CalibrationBusyError("another calibration is using the observation store") from error
    try:
        yield
    finally:
        lock.release()


def _inflight_path(store: ObservationStore) -> Path:
    return store.path.with_name(f"{store.path.name}.calibration.inflight")


def _reserve_attempt(store: ObservationStore, reservation: _InFlightAttempt) -> None:
    """Durably reserve one attempt before the runner can dispatch a provider call."""

    path = _inflight_path(store)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise CalibrationInFlightError("a calibration attempt is unresolved") from error
    with os.fdopen(descriptor, "wb") as marker:
        marker.write(reservation.model_dump_json().encode("utf-8"))
        marker.flush()
        os.fsync(marker.fileno())
    _sync_directory(path.parent)


def _read_inflight(store: ObservationStore) -> _InFlightAttempt | None:
    """Read a bounded private dispatch marker without clearing or redispatching it."""

    path = _inflight_path(store)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CalibrationInFlightError("calibration attempt marker is unavailable") from error
    try:
        with os.fdopen(descriptor, "rb") as marker:
            metadata = os.fstat(marker.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
            ):
                raise CalibrationInFlightError("calibration attempt marker is unsafe")
            raw = marker.read(65_537)
            if len(raw) > 65_536:
                raise CalibrationInFlightError("calibration attempt marker exceeds its limit")
            reservation = _InFlightAttempt.model_validate_json(raw)
    except (ValidationError, ValueError) as error:
        raise CalibrationInFlightError("calibration attempt marker is invalid") from error
    return reservation


def _reconcile_inflight(store: ObservationStore) -> None:
    """Clear a reservation only when its exact observation is durable."""
    reservation = _read_inflight(store)
    if reservation is None:
        return
    path = _inflight_path(store)
    observations = store.list_compatible(
        reservation.identity,
        candidate_digest=reservation.candidate_digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
    )
    if not any(item.attempt == reservation.attempt for item in observations):
        raise CalibrationInFlightError(
            "a calibration attempt may have reached a provider without a saved observation"
        )
    path.unlink()
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
