"""Result model and cross-file validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ithrl.case import ProvenanceFacts
from ithrl.context import (
    MAX_CONTEXT_NODES,
    ContextGraph,
    ContextGraphError,
    ContextNode,
    JsonObject,
    JsonValue,
    content_digest,
    validate_json_limits,
)
from ithrl.execution import ResolvedExecutionPlan
from ithrl.experiment import (
    ExperimentDefinition,
    ExperimentFormatError,
    NativeRunnerConfig,
    SampledExperimentDefinition,
)
from ithrl.reduce import CandidateTrial, ReductionCheckpoint, SampledConfirmation, SampledReducer
from ithrl.results.models import (
    CalibrationSnapshot,
    CalibrationSummary,
    ResultSnapshot,
    ResultSnapshotError,
    ResultSummary,
    SampledReductionEvidence,
)
from ithrl.runner import Candidate

if TYPE_CHECKING:
    from ithrl.case import CaseEvidence
    from ithrl.observations import ObservationStore
    from ithrl.run_state import SampledRunStateFile
    from ithrl.workflow import PreparedExperiment


async def validate_sampled_result(
    *,
    result: SampledReductionEvidence,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    state_file: SampledRunStateFile,
) -> None:
    """Independently validate sampled claims against original content and owned authority.

    Validation replays only the candidate generator's recorded decisions, never a runner.
    The existing private sampling snapshot stores the selected checkpoint, frozen holdout
    and both fresh arms; this validator binds a result to that exact durable record.
    """

    from ithrl.observations import SamplingBlockEvidence
    from ithrl.workflow import _sampled_result

    with state_file.acquire_lock(store):
        state = state_file.load(store)
        reducer = SampledReducer(prepared=prepared, state_file=state_file, observation_store=store)
        await reducer._load_and_validate(evidence, allow_selected=True)
        selection = state.selection
        confirmation = None
        if selection is not None:
            plan = selection.plan
            blocks = state_file.evidence(store)
            search = tuple(b for b in blocks if b.plan.role == "search")
            if any(not b.eligible for b in search):
                raise ResultSnapshotError("ineligible search evidence forbids confirmation")
            block = next(
                (b for b in blocks if b.plan.role == "confirmation"),
                SamplingBlockEvidence(plan=plan, attempts=()),
            )
            confirmation = SampledConfirmation.from_evidence(block)
        elif any(b.plan.role == "confirmation" for b in state.blocks):
            raise ResultSnapshotError("confirmation requires a durable prior selection")
        expected = _sampled_result(
            state,
            store,
            selection.selected_at if selection is not None else result.selected_at,
            confirmation=confirmation,
        )
        if result != expected:
            raise ResultSnapshotError("sampled result differs from authoritative run evidence")


def validate_contents(
    *,
    root: Path,
    summary: ResultSummary,
    request: Mapping[str, Any] | None,
    retained_context_lines: tuple[str, ...],
    experiment_source: str,
    provenance: Mapping[str, Any],
    execution_plan: ResolvedExecutionPlan | Mapping[str, Any],
    checkpoint: ReductionCheckpoint | Mapping[str, Any],
) -> ResultSnapshot:
    """Validate and assemble all files in one result snapshot."""

    try:
        request_data = json_object(request, label="request") if request is not None else None
        checkpoint_model = (
            checkpoint
            if isinstance(checkpoint, ReductionCheckpoint)
            else ReductionCheckpoint.model_validate(checkpoint)
        )
        provenance_model = ProvenanceFacts.model_validate(provenance)
        plan_data = (
            execution_plan.model_dump(mode="python")
            if isinstance(execution_plan, ResolvedExecutionPlan)
            else execution_plan
        )
        plan_model = ResolvedExecutionPlan.model_validate(plan_data)
        experiment = ExperimentDefinition.from_toml(experiment_source)
        context_lines, context_nodes, context_digest = validate_context_lines(
            retained_context_lines
        )
        context_ids = tuple(node.id for node in context_nodes)
    except (ValidationError, ExperimentFormatError, ValueError) as error:
        if isinstance(error, ResultSnapshotError):
            raise
        raise ResultSnapshotError(f"invalid result snapshot content: {error}") from error

    _validate_content_identities(
        summary=summary,
        request_data=request_data,
        context_digest=context_digest,
        context_ids=context_ids,
        experiment=experiment,
        provenance=provenance_model,
        execution_plan=plan_model,
        checkpoint=checkpoint_model,
    )
    validate_checkpoint(summary, checkpoint_model, plan_model, context_nodes)

    return ResultSnapshot(
        root=root,
        summary=summary,
        reduced_request=request_data,
        retained_context_lines=context_lines,
        experiment_source=experiment_source,
        experiment=experiment,
        provenance=provenance_model,
        execution_plan=plan_model,
        checkpoint=checkpoint_model,
    )


def validate_calibration_contents(
    *,
    root: Path,
    summary: CalibrationSummary,
    request: Mapping[str, Any] | None,
    retained_context_lines: tuple[str, ...],
    experiment_source: str,
    provenance: Mapping[str, Any],
    execution_plan: ResolvedExecutionPlan | Mapping[str, Any],
) -> CalibrationSnapshot:
    """Validate calibration files and the full-context candidate identity."""

    try:
        request_data = json_object(request, label="request") if request is not None else None
        provenance_model = ProvenanceFacts.model_validate(provenance)
        plan_data = (
            execution_plan.model_dump(mode="python")
            if isinstance(execution_plan, ResolvedExecutionPlan)
            else execution_plan
        )
        plan_model = ResolvedExecutionPlan.model_validate(plan_data)
        experiment = SampledExperimentDefinition.from_toml(experiment_source)
        context_lines, context_nodes, context_digest = validate_context_lines(
            retained_context_lines
        )
    except (ValidationError, ExperimentFormatError, ValueError) as error:
        if isinstance(error, ResultSnapshotError):
            raise
        raise ResultSnapshotError(f"invalid calibration snapshot content: {error}") from error

    _validate_plan_identity(summary, experiment, plan_model)
    _validate_candidate_identity(summary, request_data, plan_model)
    _validate_calibration_outcome(summary, experiment)
    if summary.outcome != "refused" and summary.provider_calls_may_have_started > (
        summary.observations_recorded * plan_model.capability.provider_calls_per_attempt
    ):
        raise ResultSnapshotError("calibration provider-call bound exceeds the execution plan")
    if context_digest != summary.retained_context_digest:
        raise ResultSnapshotError(
            "summary retained_context_digest does not match retained-context.jsonl"
        )
    if tuple(node.id for node in context_nodes) != summary.retained_context_ids:
        raise ResultSnapshotError(
            "retained-context.jsonl IDs or order do not match summary retained_context_ids"
        )
    if content_digest(provenance_model.model_dump(mode="json")) != summary.provenance_digest:
        raise ResultSnapshotError("summary provenance_digest does not match provenance.json")
    if summary.effective_policy_digest not in {None, plan_model.safety_policy.digest}:
        raise ResultSnapshotError("summary effective policy does not match execution plan")
    return CalibrationSnapshot(
        root=root,
        summary=summary,
        request=request_data,
        retained_context_lines=context_lines,
        experiment_source=experiment_source,
        experiment=experiment,
        provenance=provenance_model,
        execution_plan=plan_model,
    )


def _validate_calibration_outcome(
    summary: CalibrationSummary, experiment: SampledExperimentDefinition
) -> None:
    phase = experiment.calibration
    valid = summary.matches + summary.no_matches
    if summary.observations_recorded > phase.max_attempts:
        raise ResultSnapshotError("calibration observations exceed max_attempts")
    enough_valid = valid >= phase.required_valid
    if summary.outcome == "measurable" and not (
        enough_valid and summary.matches >= phase.minimum_matches
    ):
        raise ResultSnapshotError("measurable outcome requires enough valid matches")
    if summary.outcome == "not-measurable" and not (enough_valid and summary.matches == 0):
        raise ResultSnapshotError("not-measurable outcome requires enough valid non-matches")
    if summary.outcome == "inconclusive" and not (
        enough_valid and 0 < summary.matches < phase.minimum_matches
    ):
        raise ResultSnapshotError("inconclusive outcome requires a partial valid match block")
    if summary.outcome == "incomplete" and enough_valid:
        raise ResultSnapshotError("incomplete outcome conflicts with enough valid observations")
    if (
        summary.stop_reason == "attempt-limit"
        and summary.observations_recorded != phase.max_attempts
    ):
        raise ResultSnapshotError("attempt-limit stop reason requires max_attempts observations")
    if (
        summary.stop_reason == "wall-time-budget"
        and summary.observations_recorded >= phase.max_attempts
    ):
        raise ResultSnapshotError("wall-time-budget stop reason requires remaining attempts")
    if summary.stop_reason == "safety-refusal" and summary.terminal_error_category not in {
        "safety",
        "contract",
    }:
        raise ResultSnapshotError("safety-refusal stop reason requires a safety error")
    if summary.stop_reason == "unsafe-retry" and (
        summary.terminal_error_category is None
        or summary.terminal_error_category in {"safety", "contract"}
    ):
        raise ResultSnapshotError("unsafe-retry stop reason requires a non-safety error")


def _validate_content_identities(
    *,
    summary: ResultSummary,
    request_data: JsonObject | None,
    context_digest: str,
    context_ids: tuple[str, ...],
    experiment: ExperimentDefinition,
    provenance: ProvenanceFacts,
    execution_plan: ResolvedExecutionPlan,
    checkpoint: ReductionCheckpoint,
) -> None:
    """Cross-check every persisted digest before a result is trusted or resumed."""

    _validate_plan_identity(summary, experiment, execution_plan)
    _validate_candidate_identity(summary, request_data, execution_plan)
    _validate_file_identities(summary, context_digest, context_ids, provenance, checkpoint)


def _validate_plan_identity(
    summary: ResultSummary | CalibrationSummary,
    experiment: ExperimentDefinition | SampledExperimentDefinition,
    execution_plan: ResolvedExecutionPlan,
) -> None:
    if experiment.digest != summary.experiment_digest:
        raise ResultSnapshotError("summary experiment_digest does not match experiment.toml")
    if execution_plan.digest != summary.execution_plan_digest:
        raise ResultSnapshotError(
            "summary execution_plan_digest does not match execution-plan.json"
        )
    if execution_plan.case_digest != summary.case_digest:
        raise ResultSnapshotError("execution plan and summary case identities differ")
    if execution_plan.experiment_digest != summary.experiment_digest:
        raise ResultSnapshotError("execution plan and summary experiment identities differ")
    if (
        execution_plan.target_digest != experiment.target.digest
        or execution_plan.target_type != experiment.target.type
    ):
        raise ResultSnapshotError("execution plan target does not match experiment.toml")
    if execution_plan.target_digest != summary.target_digest:
        raise ResultSnapshotError("execution plan and summary target identities differ")
    if execution_plan.runner_fingerprint != summary.runner_fingerprint:
        raise ResultSnapshotError("execution plan and summary runner identities differ")
    if isinstance(experiment, SampledExperimentDefinition):
        if experiment.runner.type != execution_plan.capability.runner_type:
            raise ResultSnapshotError("experiment runner type does not match execution plan")
        if (
            isinstance(experiment.runner, NativeRunnerConfig)
            and experiment.runner.required_capability_digest != execution_plan.capability.digest
        ):
            raise ResultSnapshotError("experiment capability does not match execution plan")


def _validate_candidate_identity(
    summary: ResultSummary | CalibrationSummary,
    request_data: JsonObject | None,
    execution_plan: ResolvedExecutionPlan,
) -> None:
    try:
        candidate = Candidate(
            case_digest=summary.case_digest,
            execution_plan_digest=summary.execution_plan_digest,
            source_artifact_digests=summary.source_artifact_digests,
            retained_context_ids=summary.retained_context_ids,
            removed_context_ids=(
                summary.removed_context_ids if isinstance(summary, ResultSummary) else ()
            ),
            materialization=summary.candidate_materialization,
        )
    except ValidationError as error:
        raise ResultSnapshotError(
            f"summary contains an invalid candidate: {validation_message(error)}"
        ) from error
    if candidate.digest != summary.candidate_digest:
        raise ResultSnapshotError("summary candidate_digest does not match its context partition")
    if candidate.materialization != execution_plan.candidate_materialization:
        raise ResultSnapshotError("summary candidate materialization differs from execution plan")
    request_digest = (
        summary.reduced_request_digest
        if isinstance(summary, ResultSummary)
        else summary.request_digest
    )
    if summary.candidate_materialization == "exact-request":
        if request_data is None or content_digest(request_data) != request_digest:
            name = (
                "reduced_request_digest" if isinstance(summary, ResultSummary) else "request_digest"
            )
            raise ResultSnapshotError(f"summary {name} does not match request.json")
    elif request_data is not None or request_digest is not None:
        raise ResultSnapshotError("source-backed results cannot contain a materialized request")


def _validate_file_identities(
    summary: ResultSummary,
    context_digest: str,
    context_ids: tuple[str, ...],
    provenance: ProvenanceFacts,
    checkpoint: ReductionCheckpoint,
) -> None:
    if context_digest != summary.retained_context_digest:
        raise ResultSnapshotError(
            "summary retained_context_digest does not match retained-context.jsonl"
        )
    if content_digest(provenance.model_dump(mode="json")) != summary.provenance_digest:
        raise ResultSnapshotError("summary provenance_digest does not match provenance.json")
    if content_digest(checkpoint.model_dump(mode="json")) != summary.checkpoint_digest:
        raise ResultSnapshotError("summary checkpoint_digest does not match checkpoint.json")
    if context_ids != summary.retained_context_ids:
        raise ResultSnapshotError(
            "retained-context.jsonl IDs or order do not match summary retained_context_ids"
        )


def validate_checkpoint(
    summary: ResultSummary,
    checkpoint: ReductionCheckpoint,
    execution_plan: ResolvedExecutionPlan,
    retained_nodes: tuple[ContextNode, ...],
) -> None:
    _validate_checkpoint_structure(summary, checkpoint, execution_plan)
    if summary.search_result == "search-1-minimal":
        _validate_final_sweep(checkpoint, retained_nodes)
    _validate_checkpoint_observations(summary, checkpoint)


def _validate_checkpoint_structure(
    summary: ResultSummary,
    checkpoint: ReductionCheckpoint,
    execution_plan: ResolvedExecutionPlan,
) -> None:
    if len(set(checkpoint.removed_context_ids)) != len(checkpoint.removed_context_ids):
        raise ResultSnapshotError("checkpoint removed_context_ids must not contain duplicates")
    trial_keys = tuple(trial.trial_key for trial in checkpoint.trials)
    if len(set(trial_keys)) != len(trial_keys):
        raise ResultSnapshotError("checkpoint trial keys must not contain duplicates")
    identities = (
        checkpoint.case_digest,
        checkpoint.experiment_digest,
        checkpoint.runner_fingerprint,
        checkpoint.execution_plan_digest,
        checkpoint.target_digest,
        checkpoint.algorithm_digest,
    )
    expected_identities = (
        summary.case_digest,
        summary.experiment_digest,
        execution_plan.runner_fingerprint,
        execution_plan.digest,
        execution_plan.target_digest,
        summary.algorithm_digest,
    )
    if identities != expected_identities:
        raise ResultSnapshotError(
            "checkpoint identities do not match the summary and execution plan"
        )
    if checkpoint.candidate_runs_used != summary.candidate_runs_used:
        raise ResultSnapshotError(
            "checkpoint candidate_runs_used does not match summary candidate_runs_used"
        )
    if set(checkpoint.removed_context_ids) != set(summary.removed_context_ids):
        raise ResultSnapshotError(
            "checkpoint removed_context_ids do not match summary removed_context_ids"
        )
    if summary.search_result == "search-1-minimal" and not checkpoint.final_sweep_complete:
        raise ResultSnapshotError(
            "search-1-minimal requires a checkpoint with a complete final sweep"
        )
    if summary.search_result != "search-1-minimal" and checkpoint.final_sweep_complete:
        raise ResultSnapshotError("a complete checkpoint final sweep requires search-1-minimal")


def _validate_final_sweep(
    checkpoint: ReductionCheckpoint,
    retained_nodes: tuple[ContextNode, ...],
) -> None:
    final_base = checkpoint.removed_context_ids
    try:
        retained_graph = ContextGraph(nodes=retained_nodes)
    except ValidationError as error:
        raise ResultSnapshotError(
            "retained-context.jsonl does not form a valid context graph: "
            f"{validation_message(error)}"
        ) from error
    required = {node.id for node in retained_nodes if node.mutable and not node.protected}
    terminal_trials = {
        trial.requested_removals[0]: trial
        for trial in checkpoint.trials
        if trial.phase == "final-sweep"
        and trial.base_removals == final_base
        and len(trial.requested_removals) == 1
    }
    if terminal_trials.keys() != required:
        raise ResultSnapshotError(
            "search-1-minimal requires a losing or invalid final-sweep trial "
            "for every retained reducible context item"
        )
    for node_id, trial in terminal_trials.items():
        _validate_final_sweep_trial(retained_graph, final_base, node_id, trial)


def _validate_final_sweep_trial(
    retained_graph: ContextGraph,
    final_base: tuple[str, ...],
    node_id: str,
    trial: CandidateTrial,
) -> None:
    try:
        closure = retained_graph.dependency_closure((node_id,))
    except ContextGraphError:
        if trial.classification != "invalid":
            raise ResultSnapshotError(
                f"final-sweep trial for {node_id!r} must be invalid"
            ) from None
        return

    expected_removals = tuple(sorted((*final_base, *closure)))
    if trial.classification != "loses" or trial.effective_removals != expected_removals:
        raise ResultSnapshotError(
            f"final-sweep trial for {node_id!r} does not prove a losing removal"
        )


def _validate_checkpoint_observations(
    summary: ResultSummary,
    checkpoint: ReductionCheckpoint,
) -> None:
    checkpoint_observation_ids = tuple(
        trial.observation_id for trial in checkpoint.trials if trial.observation_id is not None
    )
    if len(set(checkpoint_observation_ids)) != len(checkpoint_observation_ids):
        raise ResultSnapshotError("checkpoint observation IDs must not contain duplicates")
    failed_observation_ids = checkpoint.failed_candidate_observation_ids
    if set(checkpoint_observation_ids) & set(failed_observation_ids):
        raise ResultSnapshotError(
            "checkpoint successful and failed observation IDs must be disjoint"
        )
    phase_observation_ids = {
        *summary.calibration_observation_ids,
        *summary.confirmation_observation_ids,
    }
    if (set(checkpoint_observation_ids) | set(failed_observation_ids)) & phase_observation_ids:
        raise ResultSnapshotError("checkpoint and phase observation IDs must be disjoint")


def validate_context_lines(
    lines: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[ContextNode, ...], str]:
    if len(lines) > MAX_CONTEXT_NODES:
        raise ResultSnapshotError(
            f"retained-context.jsonl exceeds the {MAX_CONTEXT_NODES}-record limit"
        )
    normalized: list[str] = []
    nodes: list[ContextNode] = []
    context_data: list[JsonObject] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ResultSnapshotError(
                f"retained-context.jsonl line {line_number} must not be blank"
            )
        try:
            data = loads_json(line, label=f"retained-context.jsonl line {line_number}")
            if not isinstance(data, dict):
                raise ResultSnapshotError(
                    f"retained-context.jsonl line {line_number} must be a JSON object"
                )
            node = ContextNode.model_validate(data)
        except ValidationError as error:
            raise ResultSnapshotError(
                f"invalid retained-context.jsonl line {line_number}: {validation_message(error)}"
            ) from error
        normalized.append(node.model_dump_json())
        nodes.append(node)
        context_data.append(node.model_dump(mode="json"))
    if len({node.id for node in nodes}) != len(nodes):
        raise ResultSnapshotError("retained-context.jsonl contains duplicate context IDs")
    return tuple(normalized), tuple(nodes), content_digest(context_data)


def loads_json(source: str, *, label: str) -> object:
    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ResultSnapshotError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ResultSnapshotError(f"{label} contains non-finite number {value}")

    try:
        value = json.loads(
            source,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
        validate_json_limits(value)
        return value
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as error:
        if isinstance(error, ResultSnapshotError):
            raise
        raise ResultSnapshotError(f"malformed {label}: {error}") from error


def json_object(value: Mapping[str, Any], *, label: str) -> JsonObject:
    try:
        normalized = _normalize_json(value, label=label, path="$", top_level=True)
    except RecursionError as error:
        raise ResultSnapshotError(f"{label} exceeds the supported JSON nesting limit") from error
    if not isinstance(normalized, dict):
        raise ResultSnapshotError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(normalized, allow_nan=False, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ResultSnapshotError(f"{label} is not a JSON-compatible object: {error}") from error
    if not isinstance(decoded, dict):
        raise ResultSnapshotError(f"{label} must be a JSON object")
    return decoded


def _normalize_json(
    value: object,
    *,
    label: str,
    path: str,
    top_level: bool = False,
) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        if top_level:
            raise ResultSnapshotError(f"{label} must be a JSON object")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResultSnapshotError(f"{label} contains a non-finite number at {path}")
        if top_level:
            raise ResultSnapshotError(f"{label} must be a JSON object")
        return value
    if isinstance(value, dict):
        normalized: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultSnapshotError(f"{label} contains a non-string key at {path}")
            normalized[key] = _normalize_json(item, label=label, path=f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        if top_level:
            raise ResultSnapshotError(f"{label} must be a JSON object")
        return [
            _normalize_json(item, label=label, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ResultSnapshotError(
        f"{label} contains unsupported {type(value).__name__} value at {path}"
    )


def validation_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
        for issue in error.errors(include_url=False, include_input=False)
    )
