"""Private CLI configuration and immutable sampled-result publication.

Session metadata locates owned state; it never authorizes a provider dispatch. Callers
must reconstruct the prepared execution identity and validate the live state and store.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.case.filesystem import (
    atomic_write,
    lstat_regular_file,
    prepare_private_directory,
    read_safe_text,
    validate_existing_root,
)
from ithrl.experiment import SampledExperimentDefinition
from ithrl.observations import ObservationStore, ObservationStoreError
from ithrl.results import SampledReductionEvidence, write_sampling_snapshot
from ithrl.results.validation import json_object, loads_json
from ithrl.run_state import SampledRunState
from ithrl.sampled_run_metadata import SampledRunMetadata

_SESSION_FILE = "cli-session.json"
_MAX_SESSION_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_RESULT_FILES = frozenset(
    {"summary.json", "result.json", "experiment.toml", "retained-context.jsonl", _SESSION_FILE}
)


class SampledCliSessionError(ValueError):
    """Private sampled CLI metadata or publication is unavailable or inconsistent."""


def save_cli_session(run_root: Path, session: SampledRunMetadata) -> Path:
    """Create immutable private metadata inside the already-owned live run."""

    try:
        validated = SampledRunMetadata.model_validate_json(session.model_dump_json())
        root = validate_existing_root(run_root.absolute())
        if root != validated.run_directory:
            raise SampledCliSessionError("session destination does not match the live run")
        _validate_state_session(_read_state(root / "sampled-state.json"), validated)
        path = root / _SESSION_FILE
        content = validated.model_dump_json(indent=2) + "\n"
        if len(content.encode("utf-8")) > _MAX_SESSION_BYTES:
            raise SampledCliSessionError("sampled CLI session exceeds its byte limit")
        atomic_write(path, content)
        _sync_directory(root)
        return path
    except (OSError, ValueError) as error:
        raise SampledCliSessionError("cannot save private sampled CLI session") from error


def load_cli_session(source: Path) -> SampledRunMetadata:
    """Load a canonical live run or a result pointing to that same live session.

    The result copy must match the still-present live metadata. Neither copy replaces
    validation of the actual run state, case, observation store or execution identity.
    """

    try:
        root = validate_existing_root(source.absolute())
        session = _read_session(root / _SESSION_FILE)
        live = validate_existing_root(session.run_directory)
        validate_existing_root(session.source_case)
        live_state = _read_state(live / "sampled-state.json")
        _validate_state_session(live_state, session)
        if root == live:
            return session
        if root.parent != session.source_case / "results":
            raise SampledCliSessionError("result does not belong to the source case")
        entries = {entry.name: entry for entry in root.iterdir()}
        if set(entries) != _RESULT_FILES | {"sampling"}:
            raise SampledCliSessionError("sampled CLI result has an invalid file set")
        contents = {
            name: _read_private(entries[name], max_bytes=_MAX_RESULT_BYTES)
            for name in _RESULT_FILES
        }
        validate_existing_root(root / "sampling")
        if {entry.name for entry in (root / "sampling").iterdir()} != {"sampling-state.json"}:
            raise SampledCliSessionError("sampling result has an invalid file set")
        snapshot_state = _read_state(root / "sampling" / "sampling-state.json")
        _validate_state_session(snapshot_state, session)
        if (
            snapshot_state.original_candidate_digest != live_state.original_candidate_digest
            or snapshot_state.provider_calls_per_attempt != live_state.provider_calls_per_attempt
        ):
            raise SampledCliSessionError("result snapshot differs from its live execution profile")
        if _read_session(live / _SESSION_FILE) != session:
            raise SampledCliSessionError("result session differs from its live run")
        loads_json(contents["result.json"], label="result.json")
        result = SampledReductionEvidence.model_validate_json(contents["result.json"])
        experiment = SampledExperimentDefinition.from_toml(contents["experiment.toml"])
        if (
            result.checkpoint.original_identity != session.original_identity
            or experiment != session.experiment
        ):
            raise SampledCliSessionError("result identities differ from its session")
        _validate_result_state(result, snapshot_state)
        return session
    except (OSError, ValueError) as error:
        raise SampledCliSessionError("cannot load private sampled CLI session") from error


def load_cli_run_state(run_root: Path) -> SampledRunState:
    """Read canonical live progress without creating a store or authorizing execution.

    Dispatch still requires the owned state/store locks and prepared execution identity.
    Result directories are pointers and cannot substitute for this live state.
    """

    try:
        root = validate_existing_root(run_root.absolute())
        session = _read_session(root / _SESSION_FILE)
        if root != session.run_directory:
            raise SampledCliSessionError("sampled progress requires its canonical live run")
        validate_existing_root(session.source_case)
        state = _read_state(root / "sampled-state.json")
        _validate_state_session(state, session)
        return state
    except (OSError, ValueError) as error:
        raise SampledCliSessionError("cannot load private sampled CLI run state") from error


def save_cli_result(
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    state: SampledRunState,
    store: ObservationStore,
    result: SampledReductionEvidence,
    session: SampledRunMetadata,
    summary: Mapping[str, Any],
) -> Path:
    """Publish a private immutable result after caller-side workflow validation.

    The caller first runs ``validate_sampled_result`` against prepared execution and
    owned state. This publisher checks references and associations, preserving pending
    reservations and partial evidence without granting continuation authority.
    """

    staging: Path | None = None
    try:
        root = validate_existing_root(workspace.root.absolute())
        destination = root / "results" / f"{session.run_id}-{uuid4().hex}"
        if root != session.source_case or load_cli_session(session.run_directory) != session:
            raise SampledCliSessionError("publication session does not match the source case")
        if load_cli_run_state(session.run_directory) != state:
            raise SampledCliSessionError("publication requires the current exact live state")
        _validate_state_session(state, session)
        if (
            store.path.absolute() != session.observation_store
            or evidence.digest != session.original_identity.case_digest
            or state.checkpoint != result.checkpoint
            or result.checkpoint.original_identity != session.original_identity
            or result.calibration_observation_ids != state.calibration_observation_ids
        ):
            raise SampledCliSessionError("publication evidence does not match the exact run")
        _validate_result_state(result, state)
        state.validate_observations(store)
        removed = frozenset(result.checkpoint.removed_context_ids)
        if evidence.context.dependency_closure(removed) != removed:
            raise SampledCliSessionError("retained context does not match a valid removal")
        retained = "".join(
            node.model_dump_json() + "\n"
            for node in evidence.context.nodes
            if node.id not in removed
        )
        contents = {
            "summary.json": json.dumps(
                json_object({**summary, "result_path": str(destination)}, label="summary"),
                allow_nan=False,
                ensure_ascii=True,
            )
            + "\n",
            "result.json": result.model_dump_json(indent=2) + "\n",
            "experiment.toml": session.experiment.to_toml(),
            "retained-context.jsonl": retained,
            _SESSION_FILE: session.model_dump_json(indent=2) + "\n",
        }
        if any(len(content.encode("utf-8")) > _MAX_RESULT_BYTES for content in contents.values()):
            raise SampledCliSessionError("sampled CLI result exceeds its file limit")
        parent = root / "results"
        if parent.exists():
            validate_existing_root(parent)
        else:
            prepare_private_directory(parent)
        staging = Path(tempfile.mkdtemp(prefix=".sampled-", dir=parent))
        publication = prepare_private_directory(staging / "result")
        for name, content in contents.items():
            atomic_write(publication / name, content)
        write_sampling_snapshot(destination=staging / "sampling", state=state, store=store)
        (staging / "sampling").rename(publication / "sampling")
        if os.path.lexists(destination):
            raise SampledCliSessionError("refusing to overwrite a sampled CLI result")
        _sync_directory(publication)
        publication.rename(destination)
        _sync_directory(parent)
        return destination
    except (OSError, ValueError, ObservationStoreError) as error:
        raise SampledCliSessionError("cannot publish private sampled CLI result") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _read_session(path: Path) -> SampledRunMetadata:
    raw = _read_private(path, max_bytes=_MAX_SESSION_BYTES)
    loads_json(raw, label=_SESSION_FILE)
    try:
        return SampledRunMetadata.model_validate_json(raw)
    except ValidationError:
        raise SampledCliSessionError("sampled CLI session metadata is invalid") from None


def _read_state(path: Path) -> SampledRunState:
    raw = _read_private(path, max_bytes=_MAX_RESULT_BYTES)
    loads_json(raw, label="sampling state")
    return SampledRunState.model_validate_json(raw)


def _validate_state_session(state: SampledRunState, session: SampledRunMetadata) -> None:
    if not session.matches_state(state):
        raise SampledCliSessionError("session metadata differs from its owned run state")


def _validate_result_state(result: SampledReductionEvidence, state: SampledRunState) -> None:
    if (
        state.checkpoint != result.checkpoint
        or state.original_candidate_digest != result.checkpoint.original_candidate_digest
        or state.calibration_observation_ids != result.calibration_observation_ids
        or len(result.calibration_attempt_ids) != len(result.calibration_observation_ids)
        or result.selected_at < state.started_at
    ):
        raise SampledCliSessionError("result differs from its sampling snapshot")
    if state.selection is None:
        if result.confirmation is not None:
            raise SampledCliSessionError("result confirmation requires durable selection")
        return
    if (
        result.selected_at != state.selection.selected_at
        or result.confirmation is None
        or result.confirmation.evidence.plan != state.selection.plan
    ):
        raise SampledCliSessionError("result differs from its frozen selection")
    confirmation = tuple(block for block in state.blocks if block.plan.role == "confirmation")
    references = () if not confirmation else confirmation[0].observations
    attempts = result.confirmation.evidence.attempts
    if len(references) != len(attempts) or any(
        not reference.matches_attempt(attempt)
        for reference, attempt in zip(references, attempts, strict=True)
    ):
        raise SampledCliSessionError("result confirmation differs from its durable references")


def _read_private(path: Path, *, max_bytes: int) -> str:
    metadata = lstat_regular_file(path, label=path.name)
    if metadata.st_nlink != 1 or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600):
        raise SampledCliSessionError("sampled CLI files must be private and single-link")
    raw = read_safe_text(path, max_bytes=max_bytes, label=path.name)
    if lstat_regular_file(path, label=path.name).st_nlink != 1:
        raise SampledCliSessionError("sampled CLI file acquired an unsafe link")
    return raw


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
