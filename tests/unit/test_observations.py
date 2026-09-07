from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationConflictError,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
    ObservationStoreError,
    UnsupportedObservationSchemaError,
)


def _observation(
    *,
    attempt: int,
    outcome: ObservationOutcome = ObservationOutcome.MATCH,
    error_category: str | None = None,
    **changes: object,
) -> Observation:
    values: dict[str, object] = {
        "case_digest": "case",
        "candidate_digest": "candidate",
        "experiment_digest": "experiment",
        "runner_fingerprint": "fake-v1",
        "target_digest": "target",
        "algorithm_digest": "deterministic-ddmin-v1",
        "role": ObservationRole.SEARCH,
        "arm": ObservationArm.CANDIDATE,
        "attempt": attempt,
        "outcome": outcome,
        "error_category": error_category,
    }
    values.update(changes)
    return Observation.model_validate(values)


def _identity(**changes: str) -> ObservationIdentity:
    values = {
        "case_digest": "case",
        "experiment_digest": "experiment",
        "runner_fingerprint": "fake-v1",
        "target_digest": "target",
        "algorithm_digest": "deterministic-ddmin-v1",
    }
    values.update(changes)
    return ObservationIdentity(**values)


def _reusable(
    store: ObservationStore, identity: ObservationIdentity | None = None
) -> tuple[Observation, ...]:
    return store.list_compatible(
        identity or _identity(),
        candidate_digest="candidate",
        role=ObservationRole.SEARCH,
        arm=ObservationArm.CANDIDATE,
    )


def test_store_appends_repeated_candidate_observations(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "private" / "observations.sqlite")

    store.append_many(
        (
            _observation(attempt=1),
            _observation(attempt=2, outcome=ObservationOutcome.NO_MATCH),
        )
    )

    observations = _reusable(store)
    assert [item.target_observed for item in observations] == [True, False]
    assert [item.valid for item in observations] == [True, True]
    assert [item.attempt for item in observations] == [1, 2]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_digest", "other-case"),
        ("experiment_digest", "other-experiment"),
        ("runner_fingerprint", "other-runner"),
        ("target_digest", "other-target"),
        ("algorithm_digest", "other-algorithm"),
    ],
)
def test_compatible_query_requires_every_reuse_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(_observation(attempt=1).model_copy(update={field: value}))

    assert _reusable(store) == ()


def test_compatible_query_can_filter_role_candidate_and_arm(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append_many(
        (
            _observation(attempt=1),
            _observation(
                attempt=1,
                candidate_digest="original",
                role=ObservationRole.CALIBRATION,
                arm=ObservationArm.BASELINE,
            ),
        )
    )

    observations = store.list_compatible(
        _identity(),
        role=ObservationRole.CALIBRATION,
        candidate_digest="original",
        arm=ObservationArm.BASELINE,
    )

    assert len(observations) == 1
    assert observations[0].candidate_digest == "original"


def test_attempt_counts_distinguish_valid_and_operational_outcomes(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append_many(
        (
            _observation(attempt=1),
            _observation(attempt=2, outcome=ObservationOutcome.NO_MATCH),
            _observation(
                attempt=3,
                outcome=ObservationOutcome.INVALID_OBSERVATION,
                error_category="oracle-contract",
            ),
            _observation(
                attempt=4,
                outcome=ObservationOutcome.RUNNER_ERROR,
                error_category="transport",
            ),
        )
    )

    counts = store.attempt_counts(
        _identity(),
        candidate_digest="candidate",
        role=ObservationRole.SEARCH,
        arm=ObservationArm.CANDIDATE,
    )

    assert counts.attempts == 4
    assert counts.valid_samples == 2
    assert counts.matches == 1
    assert counts.no_matches == 1
    assert counts.invalid_observations == 1
    assert counts.runner_errors == 1
    assert counts.next_attempt == 5


def test_attempt_count_is_scoped_to_exact_arm(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(_observation(attempt=4))

    counts = store.attempt_counts(
        _identity(),
        candidate_digest="candidate",
        role=ObservationRole.SEARCH,
        arm=ObservationArm.BASELINE,
    )

    assert counts.attempts == 0
    assert counts.next_attempt == 1


def test_record_attempt_allocates_same_arm_attempts_atomically(tmp_path: Path) -> None:
    path = tmp_path / "observations.sqlite"
    workers = 8
    ready = Barrier(workers)

    def record(_index: int) -> Observation:
        ready.wait()
        return ObservationStore(path).record_attempt(
            identity=_identity(),
            candidate_digest="candidate",
            role=ObservationRole.SEARCH,
            arm=ObservationArm.CANDIDATE,
            outcome=ObservationOutcome.MATCH,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        observations = tuple(executor.map(record, range(workers)))

    assert sorted(item.attempt for item in observations) == list(range(1, workers + 1))
    assert len(_reusable(ObservationStore(path))) == workers


@pytest.mark.parametrize("field", ["role", "arm", "outcome"])
def test_observation_rejects_unknown_versioned_values(field: str) -> None:
    values: dict[str, object] = {
        "case_digest": "case",
        "candidate_digest": "candidate",
        "experiment_digest": "experiment",
        "runner_fingerprint": "runner",
        "target_digest": "target",
        "algorithm_digest": "algorithm",
        "role": ObservationRole.SEARCH,
        "arm": ObservationArm.CANDIDATE,
        "attempt": 1,
        "outcome": ObservationOutcome.MATCH,
    }
    values[field] = "future-value"

    with pytest.raises(ValidationError):
        Observation.model_validate(values)


def test_observation_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        _observation(attempt=1, unexpected="value")


def test_observation_requires_aware_timestamp_and_normalizes_to_utc() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        _observation(attempt=1, recorded_at=datetime(2026, 1, 1))  # noqa: DTZ001

    observation = _observation(
        attempt=1,
        recorded_at=datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
    )
    assert observation.recorded_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert observation.recorded_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("outcome", "error_category"),
    [
        (ObservationOutcome.MATCH, "unexpected"),
        (ObservationOutcome.NO_MATCH, "unexpected"),
        (ObservationOutcome.INVALID_OBSERVATION, None),
        (ObservationOutcome.RUNNER_ERROR, None),
    ],
)
def test_observation_enforces_outcome_error_consistency(
    outcome: ObservationOutcome, error_category: str | None
) -> None:
    with pytest.raises(ValidationError):
        _observation(attempt=1, outcome=outcome, error_category=error_category)


def test_duplicate_attempt_is_rejected_without_replacing_first(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    first = _observation(attempt=1)
    store.append(first)

    with pytest.raises(ObservationConflictError):
        store.append(_observation(attempt=1, outcome=ObservationOutcome.NO_MATCH))

    assert _reusable(store) == (first,)


def test_batch_append_is_atomic_on_conflict(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")

    with pytest.raises(ObservationConflictError):
        store.append_many((_observation(attempt=1), _observation(attempt=1)))

    assert _reusable(store) == ()


def test_existing_unversioned_store_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "observations.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE observations (observation_id TEXT PRIMARY KEY)")

    with pytest.raises(UnsupportedObservationSchemaError, match="schema version metadata"):
        ObservationStore(path).initialize()


def test_concurrent_first_initialization_uses_one_complete_schema(tmp_path: Path) -> None:
    path = tmp_path / "observations.sqlite"
    workers = 8
    ready = Barrier(workers)

    def initialize(_index: int) -> None:
        ready.wait()
        ObservationStore(path).initialize()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tuple(executor.map(initialize, range(workers)))

    ObservationStore(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT value FROM ithrl_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("1",)


def test_sqlite_initialization_errors_use_the_store_error_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")

    def fail_connect(_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr("ithrl.observations.sqlite3.connect", fail_connect)

    with pytest.raises(ObservationStoreError, match="cannot initialize") as raised:
        store.initialize()

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_unsupported_schema_version_fails_closed(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE ithrl_metadata SET value = '999' WHERE key = 'schema_version'")

    with pytest.raises(UnsupportedObservationSchemaError, match="999"):
        store.initialize()


def test_incomplete_current_schema_fails_closed(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP INDEX observations_compatibility")

    with pytest.raises(UnsupportedObservationSchemaError, match="schema objects"):
        store.initialize()


def test_modified_append_only_trigger_fails_closed(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP TRIGGER observations_no_update")
        connection.execute(
            """
            CREATE TRIGGER observations_no_update
            BEFORE UPDATE ON observations
            BEGIN
                SELECT 1;
            END
            """
        )

    with pytest.raises(UnsupportedObservationSchemaError, match="schema objects"):
        store.initialize()


def test_observation_rows_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(_observation(attempt=1))

    with closing(sqlite3.connect(store.path)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE observations SET outcome = 'no_match'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM observations")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_store_uses_private_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o755)
    store = ObservationStore(directory / "observations.sqlite")
    store.initialize()

    assert store.path.parent.stat().st_mode & 0o777 == 0o755
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_store_makes_a_new_parent_private(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "new-private" / "observations.sqlite")

    store.initialize()

    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_store_file_is_private_if_schema_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ObservationStore(tmp_path / "private" / "observations.sqlite")

    def fail_schema(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("schema creation failed")

    monkeypatch.setattr(store, "_create_schema", fail_schema)
    with pytest.raises(RuntimeError, match="schema creation failed"):
        store.initialize()

    assert store.path.stat().st_mode & 0o777 == 0o600


def test_store_refuses_a_symbolic_link_database(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.touch()
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(target)

    with pytest.raises(ObservationStoreError, match="path contains a symbolic link"):
        ObservationStore(linked).initialize()


def test_store_refuses_a_symbolic_link_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ObservationStoreError, match="path contains a symbolic link"):
        ObservationStore(linked / "observations.sqlite").initialize()


def test_store_refuses_a_symbolic_link_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ObservationStoreError, match="path contains a symbolic link"):
        ObservationStore(linked / "nested" / "observations.sqlite").initialize()
