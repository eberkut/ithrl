"""Append-only local observation storage."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from random import Random
from typing import Annotated, Literal
from uuid import uuid4

from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ithrl.context import content_digest
from ithrl.experiment import (
    MAX_SAMPLING_ATTEMPTS,
    SampledReductionPlan,
    SamplingArmTarget,
    SamplingEligibilityPolicy,
)
from ithrl.path_safety import UnsafePathError, private_file_lock
from ithrl.runner.evidence import AttemptEvidence

_STORE_SCHEMA_VERSION = 2
_METADATA_TABLE = "ithrl_metadata"
_OBSERVATION_COLUMNS = (
    "observation_id",
    "recorded_at",
    "case_digest",
    "candidate_digest",
    "experiment_digest",
    "runner_fingerprint",
    "execution_plan_digest",
    "target_digest",
    "algorithm_digest",
    "role",
    "arm",
    "attempt",
    "outcome",
    "error_category",
    "attempt_evidence",
)


class ObservationStoreError(RuntimeError):
    """Base error for observation persistence."""


class UnsupportedObservationSchemaError(ObservationStoreError):
    """The database schema is absent, malformed, or from another version."""


class ObservationConflictError(ObservationStoreError):
    """An observation conflicts with an existing append-only record."""


class ObservationStoreBusyError(ObservationStoreError):
    """Another sampled run owns dispatch and reconciliation against this store."""


class ObservationRole(StrEnum):
    """The evidence role for an observation."""

    CALIBRATION = "calibration"
    SEARCH = "search"
    CONFIRMATION = "confirmation"
    NEIGHBOR_CONFIRMATION = "neighbor-confirmation"


class ObservationArm(StrEnum):
    """The experimental arm that produced an observation."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"
    SELECTED = "selected"


class ObservationOutcome(StrEnum):
    """The oracle or operational outcome of one runner attempt."""

    MATCH = "match"
    NO_MATCH = "no_match"
    INVALID_OBSERVATION = "invalid_observation"
    RUNNER_ERROR = "runner_error"


class ObservationIdentity(BaseModel):
    """All identities that must match before observations can be reused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_digest: str = Field(min_length=1)
    experiment_digest: str = Field(min_length=1)
    runner_fingerprint: str = Field(min_length=1)
    execution_plan_digest: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)
    algorithm_digest: str = Field(min_length=1)


class Observation(BaseModel):
    """One deterministic or provider runner attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    case_digest: str = Field(min_length=1)
    candidate_digest: str = Field(min_length=1)
    experiment_digest: str = Field(min_length=1)
    runner_fingerprint: str = Field(min_length=1)
    execution_plan_digest: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)
    algorithm_digest: str = Field(min_length=1)
    role: ObservationRole
    arm: ObservationArm
    attempt: int = Field(ge=1)
    outcome: ObservationOutcome
    error_category: Annotated[
        str | None,
        Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ] = None
    attempt_evidence: AttemptEvidence

    @field_validator("recorded_at")
    @classmethod
    def _normalize_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_outcome(self) -> Observation:
        is_error = self.outcome in {
            ObservationOutcome.INVALID_OBSERVATION,
            ObservationOutcome.RUNNER_ERROR,
        }
        if is_error and not self.error_category:
            raise ValueError("error outcomes require error_category")
        if not is_error and self.error_category is not None:
            raise ValueError("valid outcomes must not have error_category")
        completed = self.attempt_evidence.termination_reason == "completed"
        if self.outcome is ObservationOutcome.RUNNER_ERROR and completed:
            raise ValueError("runner errors require incomplete attempt evidence")
        if self.valid and not completed:
            raise ValueError("behavior outcomes require completed attempt evidence")
        if self.attempt_evidence.overload_replacement_safe and (
            self.outcome is not ObservationOutcome.RUNNER_ERROR
            or self.error_category != "provider-overloaded"
        ):
            raise ValueError(
                "overload replacement evidence requires the structured overload category"
            )
        return self

    @property
    def identity(self) -> ObservationIdentity:
        """Return the compatibility identity for reuse queries."""

        return ObservationIdentity(
            case_digest=self.case_digest,
            experiment_digest=self.experiment_digest,
            runner_fingerprint=self.runner_fingerprint,
            execution_plan_digest=self.execution_plan_digest,
            target_digest=self.target_digest,
            algorithm_digest=self.algorithm_digest,
        )

    @property
    def valid(self) -> bool:
        """Whether the attempt produced an oracle verdict."""

        return self.outcome in {ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH}

    @property
    def target_observed(self) -> bool | None:
        """Return the target result, or ``None`` for an operational outcome."""

        if self.outcome is ObservationOutcome.MATCH:
            return True
        if self.outcome is ObservationOutcome.NO_MATCH:
            return False
        return None

    @property
    def safe_provider_overload(self) -> bool:
        return (
            self.outcome is ObservationOutcome.RUNNER_ERROR
            and self.error_category == "provider-overloaded"
            and self.attempt_evidence.overload_replacement_safe
        )

    def allows_fresh_attempt(self, policy: SamplingEligibilityPolicy) -> bool:
        """Apply the frozen policy; this never authorizes replaying an unresolved dispatch."""

        return self.attempt_evidence.automatic_retry_safe or (
            policy == "safe-provider-overload" and self.safe_provider_overload
        )


class AttemptCounts(BaseModel):
    """Attempt and valid-sample counts for one exact experimental arm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: int = Field(ge=0)
    valid_samples: int = Field(ge=0)
    matches: int = Field(ge=0)
    no_matches: int = Field(ge=0)
    invalid_observations: int = Field(ge=0)
    runner_errors: int = Field(ge=0)
    next_attempt: int = Field(ge=1)


type SamplingArm = Literal["baseline", "candidate"]
type SamplingRole = Literal["search", "confirmation"]
type _Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _SamplingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SamplingBlockPlan(_SamplingContract):
    """One frozen strict two-arm block, independent of administrative run allowances.

    The original candidate identity must come from the protected full-context materialization.
    Execution identities remain arm-specific; a budget extension cannot change this plan.
    The recorded order includes replacement slots, which are skipped once an arm is complete.
    """

    block_id: Annotated[str, Field(min_length=1, max_length=128)]
    role: SamplingRole
    reduction: SampledReductionPlan
    original_identity: ObservationIdentity
    candidate_identity: ObservationIdentity
    original_candidate_digest: _Digest
    candidate_digest: _Digest
    frozen_at: datetime
    schedule_seed: Annotated[int, Field(ge=0, le=2**64 - 1)]
    schedule: Annotated[tuple[SamplingArm, ...], Field(max_length=2 * MAX_SAMPLING_ATTEMPTS)]
    provider_calls_per_attempt: Annotated[int, Field(ge=1, le=2)]

    @field_validator("frozen_at")
    @classmethod
    def normalize_frozen_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("frozen_at must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_block(self) -> SamplingBlockPlan:
        original = self.original_identity.model_dump(exclude={"execution_plan_digest"})
        candidate = self.candidate_identity.model_dump(exclude={"execution_plan_digest"})
        if original != candidate:
            raise ValueError("both arms must share case, experiment, runner, target and algorithm")
        if self.schedule != sampling_schedule(
            self.targets_for("baseline").max_attempts,
            self.targets_for("candidate").max_attempts,
            self.schedule_seed,
        ):
            raise ValueError("schedule must match the frozen seeded two-arm order")
        return self

    def targets_for(self, arm: SamplingArm) -> SamplingArmTarget:
        phase = self.reduction.search if self.role == "search" else self.reduction.confirmation
        return phase.original if arm == "baseline" else phase.candidate

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

    @property
    def expires_at(self) -> datetime:
        return self.frozen_at + timedelta(seconds=self.reduction.freshness_seconds)

    @property
    def required_provider_call_budget(self) -> int:
        """Conservative capacity for every frozen attempt, including replacement slots."""

        return len(self.schedule) * self.provider_calls_per_attempt


def sampling_schedule(
    original_max_attempts: int, candidate_max_attempts: int, seed: int
) -> tuple[SamplingArm, ...]:
    """Canonical seeded slot order; this constructs no attempts and dispatches no work."""

    for count in (original_max_attempts, candidate_max_attempts):
        if type(count) is not int or not 1 <= count <= MAX_SAMPLING_ATTEMPTS:
            raise ValueError("schedule counts must be bounded positive integers")
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError("schedule seed must be a bounded nonnegative integer")
    arms: list[SamplingArm] = ["baseline" for _ in range(original_max_attempts)]
    arms.extend("candidate" for _ in range(candidate_max_attempts))
    Random(seed).shuffle(arms)  # noqa: S311 -- reproducible experimental ordering, not secrets.
    return tuple(arms)


class SamplingAttempt(_SamplingContract):
    """Dispatch position and a content-safe observation, without a behavioral error verdict.

    Independence evidence references a separately verified exclusion justification. A digest
    cannot itself prove independence; runtime collection must establish the frozen policy.
    """

    schedule_position: Annotated[int, Field(ge=0, lt=2 * MAX_SAMPLING_ATTEMPTS)]
    observation: Observation = Field(repr=False)
    independence_evidence_digest: _Digest | None = None

    @model_validator(mode="after")
    def validate_exclusion_reference(self) -> SamplingAttempt:
        if self.independence_evidence_digest is not None and self.observation.valid:
            raise ValueError("valid observations cannot carry exclusion evidence")
        return self

    @property
    def replaceable(self) -> bool:
        """Whether the record satisfies the narrow safe-preaccept exclusion contract."""

        observation = self.observation
        return (
            observation.outcome is ObservationOutcome.RUNNER_ERROR
            and observation.error_category in {"preparation", "startup", "transport"}
            and observation.attempt_evidence.retry_safe
            and self.independence_evidence_digest is not None
        )


class SamplingArmCounts(_SamplingContract):
    """Behavior conditional on validity and execution reliability remain separate counts."""

    dispatched: Annotated[int, Field(ge=0)]
    valid: Annotated[int, Field(ge=0)]
    matches: Annotated[int, Field(ge=0)]
    no_matches: Annotated[int, Field(ge=0)]
    invalid_observations: Annotated[int, Field(ge=0)]
    runner_errors: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_counts(self) -> SamplingArmCounts:
        if self.valid != self.matches + self.no_matches:
            raise ValueError("valid count must equal matches plus no_matches")
        if self.dispatched != self.valid + self.invalid_observations + self.runner_errors:
            raise ValueError("dispatched count must include every operational outcome")
        return self


class SamplingBlockEvidence(_SamplingContract):
    """Ordered evidence for a frozen block, including partial or refused collection.

    Counts and eligibility are derived from the referenced observations rather than trusted
    persisted summary flags. Storage publication and continuation ownership are separate duties.
    """

    plan: SamplingBlockPlan
    attempts: Annotated[tuple[SamplingAttempt, ...], Field(max_length=2 * MAX_SAMPLING_ATTEMPTS)]

    @model_validator(mode="after")
    def validate_membership(self) -> SamplingBlockEvidence:
        positions = tuple(item.schedule_position for item in self.attempts)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("attempts must have unique increasing schedule positions")
        if len(set(self.observation_ids)) != len(self.attempts):
            raise ValueError("block observation IDs must be unique")
        if len(set(self.attempt_ids)) != len(self.attempts):
            raise ValueError("block attempt IDs must be unique")
        valid = {"baseline": 0, "candidate": 0}
        previous_attempt: dict[str, int] = {}
        next_position = 0
        previous_time = self.plan.frozen_at
        for item in self.attempts:
            if item.schedule_position >= len(self.plan.schedule):
                raise ValueError("attempt position exceeds the frozen schedule")
            arm = self.plan.schedule[item.schedule_position]
            observation = item.observation
            for skipped_arm in self.plan.schedule[next_position : item.schedule_position]:
                if valid[skipped_arm] != self.plan.targets_for(skipped_arm).required_valid:
                    raise ValueError("only completed arms may skip scheduled slots")
            next_position = item.schedule_position + 1
            if observation.recorded_at < previous_time:
                raise ValueError("observations must follow dispatch time order")
            previous_time = observation.recorded_at
            if arm in previous_attempt and observation.attempt != previous_attempt[arm] + 1:
                raise ValueError("arm-local observation attempts must be consecutive")
            previous_attempt[arm] = observation.attempt
            identity = (
                self.plan.original_identity if arm == "baseline" else self.plan.candidate_identity
            )
            digest = (
                self.plan.original_candidate_digest
                if arm == "baseline"
                else self.plan.candidate_digest
            )
            if (
                observation.identity != identity
                or observation.candidate_digest != digest
                or observation.arm.value != arm
                or observation.role.value != self.plan.role
            ):
                raise ValueError("observation does not belong to the frozen arm and role")
            if valid[arm] == self.plan.targets_for(arm).required_valid:
                raise ValueError("completed arms cannot dispatch additional attempts")
            valid[arm] += int(observation.valid)
        return self

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(item.observation.observation_id for item in self.attempts)

    @property
    def attempt_ids(self) -> tuple[str, ...]:
        return tuple(item.observation.attempt_evidence.attempt_id for item in self.attempts)

    def counts_for(self, arm: SamplingArm) -> SamplingArmCounts:
        observations = tuple(
            item.observation for item in self.attempts if item.observation.arm.value == arm
        )
        matches = sum(item.outcome is ObservationOutcome.MATCH for item in observations)
        no_matches = sum(item.outcome is ObservationOutcome.NO_MATCH for item in observations)
        return SamplingArmCounts(
            dispatched=len(observations),
            valid=matches + no_matches,
            matches=matches,
            no_matches=no_matches,
            invalid_observations=sum(
                item.outcome is ObservationOutcome.INVALID_OBSERVATION for item in observations
            ),
            runner_errors=sum(
                item.outcome is ObservationOutcome.RUNNER_ERROR for item in observations
            ),
        )

    @property
    def original_counts(self) -> SamplingArmCounts:
        return self.counts_for("baseline")

    @property
    def candidate_counts(self) -> SamplingArmCounts:
        return self.counts_for("candidate")

    @property
    def complete(self) -> bool:
        return all(
            self.counts_for(arm).valid == self.plan.targets_for(arm).required_valid
            for arm in ("baseline", "candidate")
        )

    @property
    def next_schedule_position(self) -> int | None:
        """Next frozen slot, skipping only arms with their full valid target."""

        start = 0 if not self.attempts else self.attempts[-1].schedule_position + 1
        finished = {
            arm
            for arm in ("baseline", "candidate")
            if self.counts_for(arm).valid == self.plan.targets_for(arm).required_valid
        }
        return next(
            (
                position
                for position in range(start, len(self.plan.schedule))
                if self.plan.schedule[position] not in finished
            ),
            None,
        )

    @property
    def eligibility_refusal(self) -> str | None:
        for item in self.attempts:
            observation = item.observation
            if not self.plan.frozen_at <= observation.recorded_at < self.plan.expires_at:
                return "stale-observation"
            if not observation.valid:
                if not item.replaceable and not (
                    self.plan.reduction.eligibility_policy == "safe-provider-overload"
                    and observation.safe_provider_overload
                ):
                    return "ineligible-exclusion"
                continue
            accounting = observation.attempt_evidence.accounting
            if accounting is None or not (
                accounting.actions_executed == 0
                and accounting.tool_network_requests == 0
                and accounting.actions_requested == accounting.actions_denied
                and accounting.process_outcome != "unknown"
                and accounting.cleanup_outcome in {"not-required", "verified"}
                and accounting.residual_disposable_state_bytes == 0
                and accounting.residual_disposable_state_entries == 0
                and accounting.source_store_effect == "unchanged"
                and accounting.original_workspace_effect == "unchanged"
            ):
                return "unsafe-attempt"
            if accounting.provider_calls_started > self.plan.provider_calls_per_attempt:
                return "provider-call-bound-exceeded"
        return None

    @property
    def eligible(self) -> bool:
        return self.eligibility_refusal is None

    @property
    def point_difference(self) -> float | None:
        if not self.complete or not self.eligible:
            return None
        original, candidate = self.original_counts, self.candidate_counts
        return candidate.matches / candidate.valid - original.matches / original.valid


class SamplingBudgetAllowance(_SamplingContract):
    """Administrative limits and confirmation capacity, outside frozen statistical identities.

    The overall wall-time limit is separate from frozen per-attempt deadlines and the known
    runner-time confirmation reserve. This record does not authorize execution. Token accounting
    is observed/estimated; current profiles cannot enforce an overall hard input-token guarantee
    before dispatch.
    """

    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_provider_calls: Annotated[int, Field(ge=1)]
    max_wall_time_seconds: Annotated[int, Field(ge=1)]
    max_candidate_comparisons: Annotated[int, Field(ge=1, le=10_000)]
    confirmation_provider_calls: Annotated[int, Field(ge=1)]
    confirmation_runner_seconds: Annotated[int, Field(ge=1)]
    inter_attempt_delay_seconds: Annotated[int, Field(ge=0, le=86_400)] = 0
    max_tokens: Annotated[int | None, Field(ge=1)] = None
    token_accounting: Literal["observed-or-estimated"] = "observed-or-estimated"

    @model_validator(mode="after")
    def validate_confirmation_capacity(self) -> SamplingBudgetAllowance:
        if self.confirmation_provider_calls > self.max_provider_calls:
            raise ValueError("confirmation budget exceeds maximum provider calls")
        if self.confirmation_runner_seconds > self.max_wall_time_seconds:
            raise ValueError("confirmation budget exceeds maximum wall-time seconds")
        return self

    def validate_confirmation_plan(
        self, plan: SamplingBlockPlan, *, attempt_timeout_seconds: int
    ) -> None:
        """Check the declared reserve against every frozen confirmation attempt.

        Collection must separately prove remaining capacity before dispatch; this neither
        reserves resources nor authorizes execution.
        """

        if plan.role != "confirmation":
            raise ValueError("confirmation budget requires a confirmation plan")
        if type(attempt_timeout_seconds) is not int or attempt_timeout_seconds < 1:
            raise ValueError("attempt timeout must be a positive integer")
        if self.confirmation_provider_calls < plan.required_provider_call_budget:
            raise ValueError("confirmation provider-call budget cannot cover every planned attempt")
        if self.confirmation_runner_seconds < len(plan.schedule) * attempt_timeout_seconds:
            raise ValueError("confirmation runner-time budget cannot cover every planned attempt")


class ObservationStore:
    """Append-only SQLite store with private local file permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def acquire_sampling_lock(self) -> Generator[None]:
        """Serialize sampled ownership across reconciliation and progress publication.

        SQLite transactions still protect individual appends. This wider lock belongs to
        sampled run storage and must be held across a reservation's dispatch lifecycle.
        """

        if not os.path.lexists(self.path):
            raise ObservationStoreError("observation database is unavailable")
        self.initialize()
        lock_path = self.path.with_name(f"{self.path.name}.sampling.lock")
        _reject_symlink_components(lock_path)
        try:
            lock = private_file_lock(lock_path)
        except UnsafePathError as error:
            raise ObservationStoreError("sampling lock file is unsafe") from error
        try:
            lock.acquire()
        except Timeout as error:
            raise ObservationStoreBusyError(
                "another sampled run is using the observation store"
            ) from error
        try:
            yield
        finally:
            lock.release()

    def initialize(self) -> None:
        """Create a new store or validate the exact supported schema."""

        _reject_symlink_components(self.path.parent)
        parent_existed = os.path.lexists(self.path.parent)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            _restrict_permissions(self.path.parent, 0o700)
        _reject_symlink_components(self.path)
        if self.path.is_symlink():
            raise ObservationStoreError(
                f"observation database must not be a symbolic link: {self.path}"
            )
        if not os.path.lexists(self.path):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                pass
            except OSError as error:
                raise ObservationStoreError(
                    f"cannot create observation database: {self.path}"
                ) from error
            else:
                os.close(descriptor)
        try:
            metadata = self.path.lstat()
        except OSError as error:
            raise ObservationStoreError(
                f"observation database is unavailable: {self.path}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ObservationStoreError(f"observation database must be a regular file: {self.path}")
        _restrict_permissions(self.path, 0o600)
        try:
            with self._connection() as connection:
                # Serialize schema creation and validation before any writer uses the database.
                connection.execute("BEGIN IMMEDIATE")
                tables = _user_tables(connection)
                if not tables:
                    self._create_schema(connection)
                else:
                    self._validate_schema(connection, tables)
        except sqlite3.Error as error:
            raise ObservationStoreError(
                f"cannot initialize observation database: {self.path}"
            ) from error

    def append(self, observation: Observation) -> None:
        """Append one observation without replacing any existing record."""

        self.append_many((observation,))

    def append_many(self, observations: Iterable[Observation]) -> None:
        """Append observations atomically in one short transaction."""

        batch = tuple(observations)
        if not batch:
            return
        self.initialize()
        try:
            with self._connection() as connection:
                connection.executemany(_INSERT_OBSERVATION, (_to_row(item) for item in batch))
        except sqlite3.IntegrityError as error:
            raise ObservationConflictError(
                "an observation ID or experimental attempt already exists"
            ) from error
        except sqlite3.Error as error:
            raise ObservationStoreError(
                f"cannot append to observation database: {self.path}"
            ) from error

    def record_attempt(
        self,
        *,
        identity: ObservationIdentity,
        candidate_digest: str,
        role: ObservationRole,
        arm: ObservationArm,
        outcome: ObservationOutcome,
        error_category: str | None = None,
        attempt_evidence: AttemptEvidence,
    ) -> Observation:
        """Allocate and append the next attempt for one exact arm atomically."""

        if not candidate_digest:
            raise ValueError("candidate_digest must not be empty")
        self.initialize()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    _SELECT_NEXT_ATTEMPT,
                    (*_identity_parameters(identity), candidate_digest, role.value, arm.value),
                ).fetchone()
                if row is None:
                    raise ObservationStoreError("observation attempt allocation returned no result")
                observation = Observation(
                    **identity.model_dump(),
                    candidate_digest=candidate_digest,
                    role=role,
                    arm=arm,
                    attempt=int(row[0]),
                    outcome=outcome,
                    error_category=error_category,
                    attempt_evidence=attempt_evidence,
                )
                connection.execute(_INSERT_OBSERVATION, _to_row(observation))
        except sqlite3.IntegrityError as error:
            raise ObservationConflictError(
                "an observation ID or experimental attempt already exists"
            ) from error
        except sqlite3.Error as error:
            raise ObservationStoreError(
                f"cannot record an observation in database: {self.path}"
            ) from error
        return observation

    def list_compatible(
        self,
        identity: ObservationIdentity,
        *,
        candidate_digest: str,
        role: ObservationRole,
        arm: ObservationArm,
    ) -> tuple[Observation, ...]:
        """List reusable observations for one exact candidate arm."""

        if not candidate_digest:
            raise ValueError("candidate_digest must not be empty")
        self.initialize()
        return self._select(
            """
            case_digest = ?
            AND experiment_digest = ?
            AND runner_fingerprint = ?
            AND execution_plan_digest = ?
            AND target_digest = ?
            AND algorithm_digest = ?
            AND candidate_digest = ?
            AND role = ?
            AND arm = ?
            """,
            (
                *_identity_parameters(identity),
                candidate_digest,
                role.value,
                arm.value,
            ),
            order_by="attempt",
        )

    def list_for_experiment(self, experiment_digest: str) -> tuple[Observation, ...]:
        """List an experiment for diagnostics, not observation reuse."""

        if not experiment_digest:
            raise ValueError("experiment_digest must not be empty")
        self.initialize()
        return self._select("experiment_digest = ?", (experiment_digest,))

    def get(self, observation_id: str) -> Observation | None:
        """Resolve one durable reference without creating a missing evidence store."""

        if not observation_id:
            raise ValueError("observation_id must not be empty")
        if not os.path.lexists(self.path):
            raise ObservationStoreError("observation database is unavailable")
        self.initialize()
        try:
            observations = self._select("observation_id = ?", (observation_id,))
        except (ValidationError, ValueError) as error:
            raise ObservationStoreError("stored observation is invalid") from error
        return observations[0] if observations else None

    def get_many(self, observation_ids: Iterable[str]) -> tuple[Observation, ...]:
        """Resolve a complete ordered reference set in one consistent read transaction."""

        references = tuple(observation_ids)
        if any(not reference for reference in references) or len(set(references)) != len(
            references
        ):
            raise ValueError("observation references must be nonempty and unique")
        if not os.path.lexists(self.path):
            raise ObservationStoreError("observation database is unavailable")
        self.initialize()
        observations: dict[str, Observation] = {}
        columns = ", ".join(_OBSERVATION_COLUMNS)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN")
                # Keep below older SQLite parameter limits, including on Python 3.12.
                for start in range(0, len(references), 512):
                    batch = references[start : start + 512]
                    placeholders = ", ".join("?" for _ in batch)
                    query = f"SELECT {columns} FROM observations WHERE observation_id IN ({placeholders})"  # noqa: S608 -- identifiers/placeholders are constants; reference IDs are bound.
                    rows = connection.execute(
                        query,
                        batch,
                    ).fetchall()
                    for row in rows:
                        observation = _from_row(row)
                        observations[observation.observation_id] = observation
        except sqlite3.Error as error:
            raise ObservationStoreError("cannot resolve observation references") from error
        except (ValidationError, ValueError) as error:
            raise ObservationStoreError("stored observation is invalid") from error
        if len(observations) != len(references):
            raise ObservationStoreError("observation references include missing evidence")
        return tuple(observations[reference] for reference in references)

    def get_attempt(
        self,
        identity: ObservationIdentity,
        *,
        candidate_digest: str,
        role: ObservationRole,
        arm: ObservationArm,
        attempt: int,
    ) -> Observation | None:
        """Resolve a reserved arm ordinal; callers must verify its prepared-attempt binding."""

        if not candidate_digest:
            raise ValueError("candidate_digest must not be empty")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not os.path.lexists(self.path):
            raise ObservationStoreError("observation database is unavailable")
        self.initialize()
        try:
            observations = self._select(
                """
                case_digest = ? AND experiment_digest = ? AND runner_fingerprint = ?
                AND execution_plan_digest = ? AND target_digest = ? AND algorithm_digest = ?
                AND candidate_digest = ? AND role = ? AND arm = ? AND attempt = ?
                """,
                (
                    *_identity_parameters(identity),
                    candidate_digest,
                    role.value,
                    arm.value,
                    attempt,
                ),
            )
        except (ValidationError, ValueError) as error:
            raise ObservationStoreError("stored observation is invalid") from error
        return observations[0] if observations else None

    def attempt_counts(
        self,
        identity: ObservationIdentity,
        *,
        candidate_digest: str,
        role: ObservationRole,
        arm: ObservationArm,
    ) -> AttemptCounts:
        """Count attempts and valid verdicts for one exact candidate arm."""

        if not candidate_digest:
            raise ValueError("candidate_digest must not be empty")
        self.initialize()
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*),
                           SUM(outcome = 'match'),
                           SUM(outcome = 'no_match'),
                           SUM(outcome = 'invalid_observation'),
                           SUM(outcome = 'runner_error'),
                           COALESCE(MAX(attempt), 0) + 1
                      FROM observations
                     WHERE case_digest = ?
                       AND experiment_digest = ?
                       AND runner_fingerprint = ?
                       AND execution_plan_digest = ?
                       AND target_digest = ?
                       AND algorithm_digest = ?
                       AND candidate_digest = ?
                       AND role = ?
                       AND arm = ?
                    """,
                    (*_identity_parameters(identity), candidate_digest, role.value, arm.value),
                ).fetchone()
        except sqlite3.Error as error:
            raise ObservationStoreError(
                f"cannot query observation database: {self.path}"
            ) from error
        if row is None:
            raise ObservationStoreError("observation attempt query returned no result")
        matches = int(row[1] or 0)
        no_matches = int(row[2] or 0)
        return AttemptCounts(
            attempts=int(row[0]),
            valid_samples=matches + no_matches,
            matches=matches,
            no_matches=no_matches,
            invalid_observations=int(row[3] or 0),
            runner_errors=int(row[4] or 0),
            next_attempt=int(row[5]),
        )

    def _select(
        self,
        where: str,
        parameters: Iterable[str | int],
        *,
        order_by: str = "recorded_at, observation_id",
    ) -> tuple[Observation, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT observation_id, recorded_at, case_digest, candidate_digest,
                           experiment_digest, runner_fingerprint, execution_plan_digest,
                           target_digest,
                           algorithm_digest, role, arm, attempt, outcome, error_category,
                           attempt_evidence
                     FROM observations
                     WHERE {where}
                     ORDER BY {order_by}
                    """,  # noqa: S608 -- SQL fragments are private module constants.
                    tuple(parameters),
                ).fetchall()
        except sqlite3.Error as error:
            raise ObservationStoreError(
                f"cannot query observation database: {self.path}"
            ) from error
        return tuple(_from_row(row) for row in rows)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO ithrl_metadata (key, value) VALUES ('schema_version', ?)",
            (str(_STORE_SCHEMA_VERSION),),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection, tables: frozenset[str]) -> None:
        required = {_METADATA_TABLE, "observations"}
        if not required.issubset(tables):
            raise UnsupportedObservationSchemaError(
                "observation database has no supported schema version metadata"
            )
        row = connection.execute(
            "SELECT value FROM ithrl_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row[0] != str(_STORE_SCHEMA_VERSION):
            actual = "missing" if row is None else repr(row[0])
            raise UnsupportedObservationSchemaError(
                f"observation database schema version {actual} is unsupported; "
                f"expected {_STORE_SCHEMA_VERSION}"
            )
        columns = tuple(item[1] for item in connection.execute("PRAGMA table_info(observations)"))
        if columns != _OBSERVATION_COLUMNS:
            raise UnsupportedObservationSchemaError(
                f"observation database schema version {_STORE_SCHEMA_VERSION} has an "
                "incompatible observations table"
            )
        if _schema_fingerprint(connection) != _expected_schema_fingerprint():
            raise UnsupportedObservationSchemaError(
                f"observation database schema version {_STORE_SCHEMA_VERSION} has modified or "
                "unexpected schema objects"
            )

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )


def _schema_fingerprint(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_schema
         WHERE name NOT LIKE 'sqlite_%'
           AND sql IS NOT NULL
         ORDER BY type, name
        """
    )
    return tuple(
        (str(item[0]), str(item[1]), str(item[2]), " ".join(str(item[3]).split())) for item in rows
    )


@lru_cache(maxsize=1)
def _expected_schema_fingerprint() -> tuple[tuple[str, str, str, str], ...]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(_SCHEMA)
        return _schema_fingerprint(connection)


def _to_row(observation: Observation) -> tuple[str | int | None, ...]:
    return (
        observation.observation_id,
        observation.recorded_at.isoformat(),
        observation.case_digest,
        observation.candidate_digest,
        observation.experiment_digest,
        observation.runner_fingerprint,
        observation.execution_plan_digest,
        observation.target_digest,
        observation.algorithm_digest,
        observation.role.value,
        observation.arm.value,
        observation.attempt,
        observation.outcome.value,
        observation.error_category,
        observation.attempt_evidence.model_dump_json(),
    )


def _identity_parameters(identity: ObservationIdentity) -> tuple[str, str, str, str, str, str]:
    return (
        identity.case_digest,
        identity.experiment_digest,
        identity.runner_fingerprint,
        identity.execution_plan_digest,
        identity.target_digest,
        identity.algorithm_digest,
    )


def _from_row(row: tuple[str | int | None, ...]) -> Observation:
    return Observation(
        observation_id=str(row[0]),
        recorded_at=datetime.fromisoformat(str(row[1])),
        case_digest=str(row[2]),
        candidate_digest=str(row[3]),
        experiment_digest=str(row[4]),
        runner_fingerprint=str(row[5]),
        execution_plan_digest=str(row[6]),
        target_digest=str(row[7]),
        algorithm_digest=str(row[8]),
        role=str(row[9]),
        arm=str(row[10]),
        attempt=int(str(row[11])),
        outcome=str(row[12]),
        error_category=None if row[13] is None else str(row[13]),
        attempt_evidence=AttemptEvidence.model_validate_json(str(row[14])),
    )


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


def _reject_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise ObservationStoreError(
                f"observation database path contains a symbolic link: {current}"
            )


_INSERT_OBSERVATION = """
INSERT INTO observations (
    observation_id, recorded_at, case_digest, candidate_digest,
    experiment_digest, runner_fingerprint, execution_plan_digest, target_digest,
    algorithm_digest, role, arm, attempt, outcome, error_category, attempt_evidence
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_NEXT_ATTEMPT = """
SELECT COALESCE(MAX(attempt), 0) + 1
  FROM observations
 WHERE case_digest = ?
   AND experiment_digest = ?
   AND runner_fingerprint = ?
   AND execution_plan_digest = ?
   AND target_digest = ?
   AND algorithm_digest = ?
   AND candidate_digest = ?
   AND role = ?
   AND arm = ?
"""

_SCHEMA_STATEMENTS = (
    """CREATE TABLE ithrl_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID""",
    """CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    case_digest TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    experiment_digest TEXT NOT NULL,
    runner_fingerprint TEXT NOT NULL,
    execution_plan_digest TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    algorithm_digest TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('calibration', 'search', 'confirmation', 'neighbor-confirmation')
    ),
    arm TEXT NOT NULL CHECK (arm IN ('baseline', 'candidate', 'selected')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('match', 'no_match', 'invalid_observation', 'runner_error')
    ),
    error_category TEXT,
    attempt_evidence TEXT NOT NULL,
    CHECK (
        (outcome IN ('match', 'no_match') AND error_category IS NULL)
        OR
        (outcome IN ('invalid_observation', 'runner_error') AND length(error_category) > 0)
    ),
    UNIQUE (
        case_digest, experiment_digest, runner_fingerprint, execution_plan_digest, target_digest,
        algorithm_digest, candidate_digest, role, arm, attempt
    )
)""",
    """CREATE INDEX observations_compatibility
    ON observations (
        case_digest, experiment_digest, runner_fingerprint, execution_plan_digest, target_digest,
        algorithm_digest, role, candidate_digest, arm
    )""",
    """CREATE INDEX observations_experiment
    ON observations (experiment_digest, recorded_at, observation_id)""",
    """CREATE TRIGGER observations_no_update
BEFORE UPDATE ON observations
BEGIN
    SELECT RAISE(ABORT, 'observations are append-only');
END""",
    """CREATE TRIGGER observations_no_delete
BEFORE DELETE ON observations
BEGIN
    SELECT RAISE(ABORT, 'observations are append-only');
END""",
)

_SCHEMA = ";\n\n".join(_SCHEMA_STATEMENTS) + ";"
