"""Read saved reductions without live state, runtime detection or observation-store access."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.case.filesystem import lstat_regular_file, read_safe_text, validate_existing_root
from ithrl.context import ContextGraphError, JsonValue, content_digest
from ithrl.execution import ResolvedExecutionPlan
from ithrl.experiment import ExperimentDefinition, SampledExperimentDefinition
from ithrl.extraction import validate_experiment_target, validate_extracted_case
from ithrl.mutation import TextMutation
from ithrl.observations import ObservationIdentity
from ithrl.reduce import ResumeMismatchError, SampledInvalidCandidateTrial
from ithrl.results.models import ResultSnapshot, ResultSnapshotError, SampledReductionEvidence
from ithrl.results.storage import load_result_snapshot
from ithrl.results.validation import loads_json, validate_result_case
from ithrl.run_state import SampledRunState
from ithrl.runner import Candidate
from ithrl.sampled_run_metadata import SampledRunMetadata
from ithrl.text_sections import TextSectionRemoval

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_SESSION_BYTES = 1024 * 1024
_SAMPLED_FILES = frozenset(
    {"summary.json", "result.json", "experiment.toml", "retained-context.jsonl", "cli-session.json"}
)
_OFFLINE_LIMITS = (
    "Inspection checks saved evidence and identities without opening the observation database.",
    "Saved observations and historical execution are not independently reverified.",
)


@dataclass(frozen=True, slots=True, repr=False)
class RetainedResult:
    """Validated in-memory input for a content-safe inspection report."""

    evidence: CaseEvidence
    experiment: ExperimentDefinition | SampledExperimentDefinition
    retained_ids: tuple[str, ...]
    execution_plan: ResolvedExecutionPlan | None = None
    deterministic: ResultSnapshot | None = None
    sampled: SampledReductionEvidence | None = None
    recorded_metadata: Mapping[str, JsonValue] = field(
        default_factory=lambda: MappingProxyType(dict[str, JsonValue]())
    )
    validation_limits: tuple[str, ...] = _OFFLINE_LIMITS


def load_retained_result(case_path: Path, result_path: Path) -> RetainedResult:
    """Load and bind a current result snapshot without following recorded external paths.

    No live run, original workspace, native installation or SQLite store is required.
    Result and case validation errors intentionally omit their untrusted input values.
    """

    try:
        evidence = CaseWorkspace.open(case_path.absolute()).load()
        validate_extracted_case(evidence)
        root = validate_existing_root(result_path.absolute())
        summary = _read_object(root / "summary.json")
        if type(summary.get("schema_version")) is not int or summary["schema_version"] != 1:
            raise ValueError("unsupported summary schema")
        if summary.get("kind") == "reduction":
            for entry in root.iterdir():
                _read_private(entry)
            snapshot = load_result_snapshot(root)
            validate_result_case(snapshot, evidence)
            if evidence.decision is not None:
                validate_experiment_target(evidence.decision, snapshot.experiment.target)
            _validate_deterministic_trials(snapshot, evidence)
            return RetainedResult(
                evidence=evidence,
                experiment=snapshot.experiment,
                retained_ids=snapshot.summary.retained_context_ids,
                execution_plan=snapshot.execution_plan,
                deterministic=snapshot,
            )
        if summary.get("kind") != "sampled":
            raise ValueError("unsupported result kind")
        return _load_sampled(root, evidence, summary)
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ResultSnapshotError(
            "cannot inspect a valid reduction snapshot for this case"
        ) from None


def _read_private(path: Path, *, max_bytes: int = _MAX_FILE_BYTES) -> str:
    metadata = lstat_regular_file(path, label="inspection input")
    if metadata.st_nlink != 1 or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ValueError("inspection input must be private and single-link")
    raw = read_safe_text(path, max_bytes=max_bytes, label="inspection input")
    if lstat_regular_file(path, label="inspection input").st_nlink != 1:
        raise ValueError("inspection input acquired an unsafe link")
    return raw


def _read_object(path: Path, *, max_bytes: int = _MAX_FILE_BYTES) -> dict[str, Any]:
    value = loads_json(_read_private(path, max_bytes=max_bytes), label="inspection input")
    if not isinstance(value, dict):
        raise ValueError("inspection input requires an object")
    return value


def _candidate(
    evidence: CaseEvidence,
    identity: ObservationIdentity,
    removed: tuple[str, ...],
    mutation: TextMutation | None = None,
) -> Candidate:
    closure = evidence.context.dependency_closure(removed)
    if set(removed) != closure:
        raise ValueError("removals must be dependency-closed")
    mutation = (
        TextMutation() if mutation is None else TextMutation.model_validate(mutation.model_dump())
    )
    for edit in mutation.edits:
        if not isinstance(edit, TextSectionRemoval) or edit.context_id in closure:
            raise ValueError("saved section edit requires a retained original field")
        evidence.validate_text_sections(edit)
    if evidence.has_request and not mutation.is_empty:
        evidence.materialize(closure, mutation=mutation)
    return Candidate(
        case_digest=evidence.digest,
        execution_plan_digest=identity.execution_plan_digest,
        source_artifact_digests=tuple(a.sha256 for a in evidence.source_artifacts),
        retained_context_ids=tuple(n.id for n in evidence.context.nodes if n.id not in closure),
        removed_context_ids=tuple(n.id for n in evidence.context.nodes if n.id in closure),
        materialization="exact-request" if evidence.has_request else "source-backed",
        mutation=mutation,
    )


def _load_sampled(root: Path, evidence: CaseEvidence, summary: dict[str, Any]) -> RetainedResult:
    if {p.name for p in root.iterdir()} != _SAMPLED_FILES | {"sampling"}:
        raise ValueError("sampled result file set differs")
    sampling = validate_existing_root(root / "sampling")
    if {p.name for p in sampling.iterdir()} != {"sampling-state.json"}:
        raise ValueError("sampling snapshot file set differs")
    result_raw = _read_private(root / "result.json")
    loads_json(result_raw, label="result.json")
    result = SampledReductionEvidence.model_validate_json(result_raw)
    state_raw = _read_private(sampling / "sampling-state.json")
    loads_json(state_raw, label="sampling-state.json")
    state = SampledRunState.model_validate_json(state_raw)
    experiment = SampledExperimentDefinition.from_toml(_read_private(root / "experiment.toml"))
    if evidence.decision is not None:
        validate_experiment_target(evidence.decision, experiment.target)
    session_raw = _read_private(root / "cli-session.json", max_bytes=_MAX_SESSION_BYTES)
    loads_json(session_raw, label="sampled session")
    session = SampledRunMetadata.model_validate_json(session_raw)
    if not session.matches_state(state) or session.experiment != experiment:
        raise ValueError("sampled session differs from saved state")
    _validate_result_state(result, state)
    identity = result.checkpoint.original_identity
    if (
        identity.case_digest != evidence.digest
        or identity.experiment_digest != experiment.digest
        or identity.target_digest != experiment.target.digest
        or experiment.reduction != result.checkpoint.reduction
        or (
            evidence.manifest.harness is not None
            and evidence.manifest.harness != experiment.runner.type
        )
    ):
        raise ValueError("sampled experiment does not belong to the case")
    if _candidate(evidence, identity, ()).digest != result.checkpoint.original_candidate_digest:
        raise ValueError("immutable original candidate does not match the case")
    _validate_sampled_trials(result, evidence, state.run_id)
    selected = _candidate(
        evidence,
        result.checkpoint.best_candidate_identity,
        result.checkpoint.removed_context_ids,
        result.checkpoint.selected_mutation,
    )
    if selected.digest != result.selected_candidate_digest:
        raise ValueError("selected candidate does not match the case")
    retained = _read_private(root / "retained-context.jsonl")
    expected = "".join(
        n.model_dump_json() + "\n"
        for n in evidence.context.nodes
        if n.id in selected.retained_context_ids
    )
    if retained != expected:
        raise ValueError("retained context does not match the case")
    _validate_summary(summary, result, evidence, experiment)
    return RetainedResult(
        evidence=evidence,
        experiment=experiment,
        retained_ids=selected.retained_context_ids,
        sampled=result,
        validation_limits=(
            *_OFFLINE_LIMITS,
            "The saved sampled snapshot does not include its full execution plan.",
        ),
    )


def _validate_result_state(result: SampledReductionEvidence, state: SampledRunState) -> None:
    if (
        state.checkpoint != result.checkpoint
        or state.original_candidate_digest != result.checkpoint.original_candidate_digest
        or state.calibration_observation_ids != result.calibration_observation_ids
        or len(result.calibration_attempt_ids) != len(result.calibration_observation_ids)
        or result.selected_at < state.started_at
        or state.fuzz_progress is not None
    ):
        raise ValueError("sampled result differs from saved state")
    if state.selection is None:
        if result.confirmation is not None:
            raise ValueError("confirmation lacks durable selection")
    elif (
        result.selected_at != state.selection.selected_at
        or result.confirmation is None
        or result.confirmation.evidence.plan != state.selection.plan
    ):
        raise ValueError("sampled result differs from frozen selection")
    blocks = result.checkpoint.blocks
    if result.confirmation is not None:
        blocks = (*blocks, result.confirmation.evidence)
    by_id = {b.plan.block_id: b for b in blocks}
    if set(by_id) != {b.plan.block_id for b in state.blocks}:
        raise ValueError("sampling snapshot has different blocks")
    for progress in state.blocks:
        block = by_id[progress.plan.block_id]
        if progress.plan != block.plan or len(progress.observations) != len(block.attempts):
            raise ValueError("sampling snapshot has different evidence")
        for reference, attempt in zip(progress.observations, block.attempts, strict=True):
            if not reference.matches_attempt(attempt):
                raise ValueError("saved evidence differs from durable references")


def _validate_sampled_trials(
    result: SampledReductionEvidence, evidence: CaseEvidence, run_id: str
) -> None:
    from ithrl.reduce.contracts import validate_section_checkpoint

    try:
        validate_section_checkpoint(evidence, result.checkpoint, run_id=run_id)
    except ResumeMismatchError:
        raise ValueError("section checkpoint differs from immutable source or search") from None
    for trial in result.checkpoint.fine_trials:
        if trial.comparison is None:
            continue
        plan = trial.comparison.evidence.plan
        seed = int(content_digest({"run_id": run_id, "trial_key": trial.trial_key})[:16], 16)
        if plan.block_id != "search-" + trial.trial_key or plan.schedule_seed != seed:
            raise ValueError("section trial does not bind its frozen search block")
    for trial in result.checkpoint.trials:
        _candidate(evidence, result.checkpoint.original_identity, trial.base_removals)
        if not set(trial.requested_removals) <= evidence.context.by_id.keys():
            raise ValueError("trial requests unknown context")
        if isinstance(trial, SampledInvalidCandidateTrial):
            # Runtime reconstruction refusals cannot be re-probed during inspection.
            continue
        effective = evidence.context.dependency_closure(
            (*trial.base_removals, *trial.requested_removals)
        )
        if effective != set(trial.effective_removals):
            raise ValueError("trial removal closure differs from captured graph")
        plan = trial.comparison.evidence.plan
        seed = int(content_digest({"run_id": run_id, "trial_key": trial.trial_key})[:16], 16)
        if plan.block_id != "search-" + trial.trial_key or plan.schedule_seed != seed:
            raise ValueError("trial does not bind its frozen search block")
        if (
            _candidate(evidence, plan.candidate_identity, trial.effective_removals).digest
            != plan.candidate_digest
        ):
            raise ValueError("trial candidate does not match captured context")
    checkpoint = result.checkpoint
    if checkpoint.final_sweep_complete:
        neighbors = tuple(
            (node.id,)
            for node in evidence.context.nodes
            if node.mutable and not node.protected and node.id not in checkpoint.removed_context_ids
        )
        if checkpoint.expected_final_neighbors != neighbors:
            raise ValueError("final-sweep witness does not cover the captured context")


def _validate_deterministic_trials(snapshot: ResultSnapshot, evidence: CaseEvidence) -> None:
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=snapshot.experiment.digest,
        runner_fingerprint=snapshot.execution_plan.runner_fingerprint,
        execution_plan_digest=snapshot.execution_plan.digest,
        target_digest=snapshot.experiment.target.digest,
        algorithm_digest=snapshot.checkpoint.algorithm_digest,
    )
    base: tuple[str, ...] = ()
    for trial in snapshot.checkpoint.trials:
        if trial.base_removals != base or not trial.requested_removals:
            raise ValueError("trial parent does not follow search history")
        _candidate(evidence, identity, trial.base_removals)
        if not set(trial.requested_removals) <= evidence.context.by_id.keys():
            raise ValueError("trial requests unknown context")
        try:
            effective = evidence.context.dependency_closure(
                (*trial.base_removals, *trial.requested_removals)
            )
        except ContextGraphError:
            if trial.classification != "invalid":
                raise ValueError("trial claims unsafe removal") from None
            continue
        if trial.classification == "invalid":
            raise ValueError("deterministic invalid trial is not structurally invalid")
        if (
            effective != set(trial.effective_removals)
            or _candidate(evidence, identity, trial.effective_removals).digest
            != trial.candidate_digest
        ):
            raise ValueError("trial candidate differs from the case")
        if trial.accepts_removal:
            base = trial.effective_removals
    if base != snapshot.checkpoint.removed_context_ids:
        raise ValueError("final retained context differs from trial history")


def _validate_summary(
    summary: dict[str, Any],
    result: SampledReductionEvidence,
    evidence: CaseEvidence,
    experiment: SampledExperimentDefinition,
) -> None:
    removed = result.checkpoint.removed_context_ids
    expected: dict[str, object] = {
        "command": "shrink",
        "plan_digest": result.checkpoint.original_identity.execution_plan_digest,
        "target": experiment.target.type,
        "original_context_count": len(evidence.context.nodes),
        "retained_context_count": len(evidence.context.nodes) - len(removed),
        "removed_context_count": len(removed),
        "search_result": result.search_result,
        "confirmation_result": result.confirmation_result,
        "margin": result.checkpoint.reduction.margin,
        "one_sided_confidence": result.checkpoint.reduction.confirmation.one_sided_confidence,
    }
    checkpoint = result.checkpoint
    sections = checkpoint.reduction.section_search
    if sections is not None:
        coarse_blocks = {
            trial.comparison.evidence.plan.block_id
            for trial in checkpoint.trials
            if not isinstance(trial, SampledInvalidCandidateTrial)
        }
        fine_blocks = {
            trial.comparison.evidence.plan.block_id
            for trial in checkpoint.fine_trials
            if trial.comparison is not None
        }
        if checkpoint.active_block is not None:
            target = fine_blocks if checkpoint.coarse_stopped else coarse_blocks
            target.add(checkpoint.active_block.plan.block_id)
        expected.update(
            section_search_enabled=True,
            max_coarse_comparisons=sections.max_coarse_comparisons,
            max_fine_comparisons=sections.max_fine_comparisons,
            coarse_comparisons_used=len(coarse_blocks),
            fine_comparisons_used=len(fine_blocks),
            coarse_search_stopped=checkpoint.coarse_stopped,
            coarse_candidate_digest=checkpoint.coarse_candidate_digest,
            coarse_stop_reason=checkpoint.coarse_stop_reason,
            coarse_final_sweep_complete=checkpoint.final_sweep_complete,
            fine_search_complete=checkpoint.fine_complete,
            additional_removed_utf8_bytes=checkpoint.additional_removed_utf8_bytes,
            minimum_removed_utf8_bytes=sections.minimum_removed_utf8_bytes,
            additional_reduction_result=checkpoint.additional_reduction_result,
        )
    if any(
        summary.get(key) != value
        or (type(value) in (int, bool) and type(summary.get(key)) is not type(value))
        or (type(value) is float and type(summary.get(key)) not in (int, float))
        for key, value in expected.items()
    ):
        raise ValueError("sampled summary differs from recorded evidence")
