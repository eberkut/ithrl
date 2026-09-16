"""Append-only local observation storage."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ObservationStore:
    """Append-only SQLite store with private local file permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

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
                identity.case_digest,
                identity.experiment_digest,
                identity.runner_fingerprint,
                identity.execution_plan_digest,
                identity.target_digest,
                identity.algorithm_digest,
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
        parameters: Iterable[str],
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
