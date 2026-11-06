"""Provider-free interpretation of retained reduction evidence."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ithrl.case import CaseEvidence
from ithrl.context import JsonValue, decode_pointer
from ithrl.experiment import SampledReductionPlan
from ithrl.mutation import TextMutation
from ithrl.observations import SamplingArmCounts, SamplingBlockEvidence
from ithrl.reduce import (
    CandidateTrial,
    SampledCandidateTrial,
    SampledConfirmation,
    SampledInvalidCandidateTrial,
    classify_confirmation,
)
from ithrl.results.inspection import RetainedResult, load_retained_result
from ithrl.runner import Candidate
from ithrl.text_sections import (
    TextSectionCatalog,
    TextSectionError,
    TextSectionRemoval,
    catalog_text_sections,
    remove_text_sections,
)

MAX_INSPECTION_TEXT_BYTES = 65_536
MAX_INSPECTION_SECTION_UNITS = 4096
type TrialOutcome = Literal[
    "deterministic-target-observed",
    "deterministic-target-not-observed",
    "exploratory-screen-admitted",
    "exploratory-screen-rejected",
    "invalid",
    "incomplete",
    "ineligible",
]
type RemovalEvidence = Literal[
    "protected",
    "frozen",
    "single-removal-tested",
    "incomplete-or-ineligible",
    "invalid-removal",
    "joint-or-historical-only",
    "untested",
]
_MACHINE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z", re.ASCII)
_HISTORICAL_LIMIT = (
    "This measures behavior under the recorded reconstructed context and runner policy; "
    "it does not prove exact historical replay."
)


class _InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmationInspection(_InspectionModel):
    status: str
    margin: float | None = None
    one_sided_confidence: float | None = None
    original: SamplingArmCounts | None = None
    candidate: SamplingArmCounts | None = None
    difference: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None


class TrialInspection(_InspectionModel):
    index: int
    phase: Literal["chunk", "final-sweep"]
    outcome: TrialOutcome
    parent_digest: str
    candidate_digest: str | None
    baseline_digest: str
    base_removals: tuple[str, ...]
    requested_removals: tuple[str, ...]
    effective_removals: tuple[str, ...]
    final_parent: bool
    single_removal: bool
    original: SamplingArmCounts | None = None
    candidate: SamplingArmCounts | None = None


class ActiveComparisonInspection(_InspectionModel):
    """A partial block has a candidate but no completed removal-trial attribution."""

    candidate_digest: str
    baseline_digest: str
    outcome: Literal["incomplete", "ineligible"]
    original: SamplingArmCounts
    candidate: SamplingArmCounts


class TextSectionFieldInspection(_InspectionModel):
    """An available structural catalog is not evidence of section-level behavior."""

    content_index: int
    status: Literal["available-untested", "exploratory-tested", "selected", "unavailable"]
    catalog: TextSectionCatalog | None = None
    unavailable_reason: Literal["section-policy-limit", "inspection-section-limit"] | None = None
    removed_section_ids: tuple[str, ...] = ()
    retained_utf8_bytes: int | None = None
    trial_indexes: tuple[int, ...] = ()


class SectionTrialInspection(_InspectionModel):
    """A one-field exploratory trial, distinct from whole-record removal evidence."""

    index: int
    phase: Literal["chunk", "final-sweep"]
    context_id: str
    content_index: int
    base_section_ids: tuple[str, ...]
    requested_section_ids: tuple[str, ...]
    removed_section_ids: tuple[str, ...]
    removed_utf8_bytes: int
    outcome: TrialOutcome
    candidate_digest: str | None
    baseline_digest: str
    original: SamplingArmCounts | None = None
    candidate: SamplingArmCounts | None = None


class SectionSearchInspection(_InspectionModel):
    """Structural reduction and the frozen criterion, separate from confirmation."""

    coarse_stopped: bool
    coarse_candidate_digest: str | None
    coarse_stop_reason: str | None
    coarse_final_sweep_complete: bool
    fine_complete: bool
    max_coarse_comparisons: int
    max_fine_comparisons: int
    additional_removed_utf8_bytes: int
    minimum_removed_utf8_bytes: int
    additional_reduction_result: Literal[
        "criterion-met", "below-threshold", "no-retained-fields", "not-assessed"
    ]
    selected_context_id: str | None = None
    selected_content_index: int | None = None
    removed_section_ids: tuple[str, ...] = ()


class RetainedNodeInspection(_InspectionModel):
    id: str
    kind: str
    mutable: bool
    protected: bool
    depends_on: tuple[str, ...]
    required_by: tuple[str, ...]
    canonical_json_bytes: int
    trial_indexes: tuple[int, ...]
    final_single_removal_trial_indexes: tuple[int, ...]
    removal_evidence: RemovalEvidence
    text_section_fields: tuple[TextSectionFieldInspection, ...] = ()
    text: str | None = Field(default=None, repr=False)
    text_scope: Literal["source-record", "retained-text-field"] = "source-record"
    text_content_index: int | None = None
    text_truncated: bool = False


class RetainedInspection(_InspectionModel):
    schema_version: Literal[1] = 1
    command: Literal["inspect"] = "inspect"
    kind: Literal["retained"] = "retained"
    mode: Literal["deterministic", "sampled"]
    case_digest: str
    candidate_digest: str
    original_candidate_digest: str
    original_context_count: int
    retained_context_count: int
    removed_context_count: int
    original_canonical_json_bytes: int
    retained_canonical_json_bytes: int
    byte_measure: Literal["canonical-json-utf8-not-provider-tokens"] = (
        "canonical-json-utf8-not-provider-tokens"
    )
    canonical_json_scope: Literal["source-records-before-section-edits"] = (
        "source-records-before-section-edits"
    )
    target_type: Literal["final-response", "shell-action"]
    reconstruction_method: str | None
    safety_policy_digest: str | None
    action_execution: Literal["denied"] | None
    search_result: str
    final_sweep_complete: bool
    failed_candidate_observation_count: int | None = Field(default=None, ge=0)
    confirmation: ConfirmationInspection
    nodes: tuple[RetainedNodeInspection, ...]
    trials: tuple[TrialInspection, ...]
    section_search: SectionSearchInspection | None = None
    section_trials: tuple[SectionTrialInspection, ...] = ()
    active_comparison: ActiveComparisonInspection | None = None
    limitations: tuple[str, ...]
    text_requested: bool = False
    text_node_id: str | None = None
    text_bytes: int = 0
    text_truncated: bool = False
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0


def inspect_retained(
    case_path: Path,
    result_path: Path,
    *,
    show_text: bool = False,
    max_text_bytes: int = 4096,
    text_node_id: str | None = None,
) -> RetainedInspection:
    """Validate saved evidence and derive a report without performing new experiments."""

    return build_retained_inspection(
        load_retained_result(case_path, result_path),
        show_text=show_text,
        max_text_bytes=max_text_bytes,
        text_node_id=text_node_id,
    )


def build_retained_inspection(
    loaded: RetainedResult,
    *,
    show_text: bool = False,
    max_text_bytes: int = 4096,
    text_node_id: str | None = None,
) -> RetainedInspection:
    """Interpret an already-validated snapshot; text display has one global byte allowance."""

    if type(max_text_bytes) is not int or not 0 <= max_text_bytes <= MAX_INSPECTION_TEXT_BYTES:
        raise ValueError("inspection text allowance must be between 0 and 65536 bytes")
    evidence = loaded.evidence
    retained = frozenset(loaded.retained_ids)
    if text_node_id is not None:
        if not show_text:
            raise ValueError("targeted text requires explicit text display")
        if text_node_id not in retained:
            raise ValueError("text selector must identify a retained context unit")
    removed = frozenset(n.id for n in evidence.context.nodes if n.id not in retained)
    trials = _trials(loaded, removed)
    section_trials = _section_trials(loaded)
    selected_mutation = (
        TextMutation() if loaded.sampled is None else loaded.sampled.checkpoint.selected_mutation
    )
    values = _context_values(evidence)
    sizes = {key: len(value.encode("utf-8")) for key, value in values.items()}
    nodes, text_bytes, truncated = _nodes(
        evidence,
        retained,
        trials,
        values,
        sizes,
        show_text,
        max_text_bytes,
        text_node_id,
        selected_mutation,
        section_trials,
    )
    deterministic = loaded.deterministic
    sampled = loaded.sampled
    if deterministic is not None:
        search = deterministic.summary.search_result
        complete = deterministic.checkpoint.final_sweep_complete
        candidate_digest = deterministic.summary.candidate_digest
        confirmation = ConfirmationInspection(status=deterministic.summary.confirmation_result)
    elif sampled is not None:
        search = sampled.search_result
        complete = sampled.checkpoint.final_sweep_complete
        candidate_digest = sampled.selected_candidate_digest
        confirmation = _confirmation(
            sampled.confirmation_result, sampled.confirmation, sampled.checkpoint.reduction
        )
    else:
        raise ValueError("inspection requires a reduction snapshot")
    plan = loaded.execution_plan
    reconstruction = (
        plan.capability.reconstruction_method
        if plan is not None
        else loaded.recorded_metadata.get("reconstruction_method")
    )
    policy_digest = plan.safety_policy.digest if plan is not None else None
    failed_count = (
        len(deterministic.checkpoint.failed_candidate_observation_ids)
        if deterministic is not None
        else None
    )
    limitations = (
        _HISTORICAL_LIMIT,
        "Trial outcomes apply to their complete removals and tested search parent; "
        "joint removals do not explain each retained item.",
        "Available untested text sections are source-bound structural units, not tested removals. "
        "Whole-record trial outcomes do not establish section necessity or usefulness.",
        *loaded.validation_limits,
    )
    if failed_count:
        limitations += (
            "Failed deterministic candidate runs lack recorded removal-set details; "
            "they cannot be attributed to retained units or treated as behavioral losses.",
        )
    if sampled is not None:
        limitations += (
            "Exploratory screens do not establish confirmed loss, necessity or sampled minimality.",
        )
    if not selected_mutation.is_empty:
        limitations += (
            "Canonical JSON byte counts describe source records before section edits. "
            "Additional removed UTF-8 bytes count only the selected editable field; "
            "neither measure establishes provider token or monetary savings.",
            "Text display for an edited node contains its retained text field only; "
            "other native fields are not projected by this offline report.",
        )
    return RetainedInspection(
        mode="deterministic" if deterministic is not None else "sampled",
        case_digest=evidence.digest,
        candidate_digest=candidate_digest,
        original_candidate_digest=_original_candidate_digest(loaded),
        original_context_count=len(evidence.context.nodes),
        retained_context_count=len(retained),
        removed_context_count=len(removed),
        original_canonical_json_bytes=sum(sizes.values()),
        retained_canonical_json_bytes=sum(sizes[node_id] for node_id in retained),
        target_type=loaded.experiment.target.type,
        reconstruction_method=_metadata_label(reconstruction),
        safety_policy_digest=policy_digest,
        action_execution=plan.safety_policy.action_execution if plan is not None else None,
        search_result=search,
        final_sweep_complete=complete,
        failed_candidate_observation_count=failed_count,
        confirmation=confirmation,
        nodes=nodes,
        trials=trials,
        section_search=_section_search(loaded),
        section_trials=section_trials,
        active_comparison=_active_comparison(loaded),
        limitations=limitations,
        text_requested=show_text,
        text_node_id=text_node_id,
        text_bytes=text_bytes,
        text_truncated=truncated,
    )


def _original_candidate_digest(loaded: RetainedResult) -> str:
    evidence = loaded.evidence
    if loaded.execution_plan is not None:
        plan_digest = loaded.execution_plan.digest
    elif loaded.sampled is not None:
        return loaded.sampled.checkpoint.original_candidate_digest
    else:
        raise ValueError("inspection requires recorded candidate identity")
    return Candidate(
        case_digest=evidence.digest,
        execution_plan_digest=plan_digest,
        source_artifact_digests=tuple(a.sha256 for a in evidence.source_artifacts),
        retained_context_ids=tuple(n.id for n in evidence.context.nodes),
        removed_context_ids=(),
        materialization="exact-request" if evidence.has_request else "source-backed",
    ).digest


def _trials(loaded: RetainedResult, removed: frozenset[str]) -> tuple[TrialInspection, ...]:
    if loaded.deterministic is not None:
        source = loaded.deterministic.checkpoint.trials
    elif loaded.sampled is not None:
        source = loaded.sampled.checkpoint.trials
    else:
        return ()
    baseline = _original_candidate_digest(loaded)
    section_edited = (
        loaded.sampled is not None and not loaded.sampled.checkpoint.selected_mutation.is_empty
    )
    parents: dict[tuple[str, ...], str] = {(): baseline}
    output = []
    for index, trial in enumerate(source, start=1):
        block = trial.comparison.evidence if isinstance(trial, SampledCandidateTrial) else None
        candidate_digest = block.plan.candidate_digest if block is not None else None
        original_counts = candidate_counts = None
        outcome: TrialOutcome
        if isinstance(trial, CandidateTrial):
            candidate_digest = trial.candidate_digest
            outcome = (
                "invalid"
                if trial.classification == "invalid"
                else "deterministic-target-observed"
                if trial.target_observed
                else "deterministic-target-not-observed"
            )
        elif isinstance(trial, SampledInvalidCandidateTrial):
            outcome = "invalid"
        else:
            block = trial.comparison.evidence
            original_counts, candidate_counts = block.original_counts, block.candidate_counts
            outcome = (
                "ineligible"
                if not block.eligible
                else "incomplete"
                if not block.complete
                else "exploratory-screen-admitted"
                if trial.accepts_removal
                else "exploratory-screen-rejected"
            )
        delta = set(trial.effective_removals) - set(trial.base_removals)
        output.append(
            TrialInspection(
                index=index,
                phase=trial.phase,
                outcome=outcome,
                parent_digest=parents[trial.base_removals],
                candidate_digest=candidate_digest,
                baseline_digest=baseline,
                base_removals=trial.base_removals,
                requested_removals=trial.requested_removals,
                effective_removals=trial.effective_removals,
                final_parent=not section_edited and set(trial.base_removals) == removed,
                single_removal=len(delta) == 1,
                original=original_counts,
                candidate=candidate_counts,
            )
        )
        if trial.accepts_removal and candidate_digest is not None:
            parents[trial.effective_removals] = candidate_digest
    return tuple(output)


def _section_trials(loaded: RetainedResult) -> tuple[SectionTrialInspection, ...]:
    if loaded.sampled is None:
        return ()
    checkpoint = loaded.sampled.checkpoint
    output = []
    for index, trial in enumerate(checkpoint.fine_trials, start=1):
        block = None if trial.comparison is None else trial.comparison.evidence
        edit = trial.mutation.edits[0]
        if not isinstance(edit, TextSectionRemoval):
            raise ValueError("section trial requires a source-bound removal")
        outcome: TrialOutcome = (
            "invalid"
            if block is None
            else "ineligible"
            if not block.eligible
            else "incomplete"
            if not block.complete
            else "exploratory-screen-admitted"
            if trial.accepts_removal
            else "exploratory-screen-rejected"
        )
        output.append(
            SectionTrialInspection(
                index=index,
                phase=trial.phase,
                context_id=trial.field.context_id,
                content_index=trial.field.content_index,
                base_section_ids=trial.base_section_ids,
                requested_section_ids=trial.requested_section_ids,
                removed_section_ids=edit.section_ids,
                removed_utf8_bytes=trial.removed_utf8_bytes,
                outcome=outcome,
                candidate_digest=None if block is None else block.plan.candidate_digest,
                baseline_digest=checkpoint.original_candidate_digest,
                original=None if block is None else block.original_counts,
                candidate=None if block is None else block.candidate_counts,
            )
        )
    return tuple(output)


def _section_search(loaded: RetainedResult) -> SectionSearchInspection | None:
    if loaded.sampled is None:
        return None
    checkpoint = loaded.sampled.checkpoint
    plan = checkpoint.reduction.section_search
    if plan is None:
        return None
    edit = checkpoint.selected_mutation.edits[0] if checkpoint.selected_mutation.edits else None
    return SectionSearchInspection(
        coarse_stopped=checkpoint.coarse_stopped,
        coarse_candidate_digest=checkpoint.coarse_candidate_digest,
        coarse_stop_reason=checkpoint.coarse_stop_reason,
        coarse_final_sweep_complete=checkpoint.final_sweep_complete,
        fine_complete=checkpoint.fine_complete,
        max_coarse_comparisons=plan.max_coarse_comparisons,
        max_fine_comparisons=plan.max_fine_comparisons,
        additional_removed_utf8_bytes=checkpoint.additional_removed_utf8_bytes,
        minimum_removed_utf8_bytes=plan.minimum_removed_utf8_bytes,
        additional_reduction_result=checkpoint.additional_reduction_result,
        selected_context_id=edit.context_id if isinstance(edit, TextSectionRemoval) else None,
        selected_content_index=edit.content_index if isinstance(edit, TextSectionRemoval) else None,
        removed_section_ids=edit.section_ids if isinstance(edit, TextSectionRemoval) else (),
    )


def _nodes(
    evidence: CaseEvidence,
    retained: frozenset[str],
    trials: tuple[TrialInspection, ...],
    values: Mapping[str, str],
    sizes: Mapping[str, int],
    show_text: bool,
    allowance: int,
    text_node_id: str | None,
    selected_mutation: TextMutation,
    section_trials: tuple[SectionTrialInspection, ...],
) -> tuple[tuple[RetainedNodeInspection, ...], int, bool]:
    section_fields = _section_fields(evidence, retained, selected_mutation, section_trials)
    selected = selected_mutation.edits[0] if selected_mutation.edits else None
    edited_text = None
    if isinstance(selected, TextSectionRemoval):
        catalog = evidence.validate_text_sections(selected)
        site = next(
            site
            for site in evidence.editable_text()
            if (site.context_id, site.content_index)
            == (selected.context_id, selected.content_index)
        )
        edited_text = remove_text_sections(site.original_text, catalog, selected)
    required_by: dict[str, list[str]] = {node.id: [] for node in evidence.context.nodes}
    for node in evidence.context.nodes:
        if node.id in retained:
            for dependency in node.depends_on:
                required_by[dependency].append(node.id)
    nodes = []
    remaining = allowance
    truncated = False
    related: dict[str, list[TrialInspection]] = {node_id: [] for node_id in retained}
    for trial in trials:
        affected = set(trial.requested_removals) | (
            set(trial.effective_removals) - set(trial.base_removals)
        )
        for node_id in retained & affected:
            related[node_id].append(trial)
    for node in evidence.context.nodes:
        if node.id not in retained:
            continue
        node_trials = related[node.id]
        relevant = tuple(trial.index for trial in node_trials)
        singles = tuple(
            trial.index
            for trial in node_trials
            if trial.final_parent
            and trial.single_removal
            and trial.outcome not in {"incomplete", "ineligible", "invalid"}
            and node.id in set(trial.effective_removals) - set(trial.base_removals)
        )
        classification: RemovalEvidence
        if node.protected:
            classification = "protected"
        elif not node.mutable:
            classification = "frozen"
        elif singles:
            classification = "single-removal-tested"
        elif any(
            trial.final_parent and trial.outcome in {"incomplete", "ineligible"}
            for trial in node_trials
        ):
            classification = "incomplete-or-ineligible"
        elif any(trial.final_parent and trial.outcome == "invalid" for trial in node_trials):
            classification = "invalid-removal"
        elif relevant:
            classification = "joint-or-historical-only"
        else:
            classification = "untested"
        text, cut = None, False
        text_scope: Literal["source-record", "retained-text-field"] = "source-record"
        text_content_index = None
        if show_text and (text_node_id is None or text_node_id == node.id):
            value = values[node.id]
            if isinstance(selected, TextSectionRemoval) and selected.context_id == node.id:
                if edited_text is None:
                    raise ValueError("selected retained text is unavailable")
                value = edited_text
                text_scope = "retained-text-field"
                text_content_index = selected.content_index
            text, cut = _bounded_text(value, remaining)
            remaining -= len(text.encode("utf-8"))
            truncated |= cut
        nodes.append(
            RetainedNodeInspection(
                id=node.id,
                kind=_metadata_label(node.kind) or "unavailable",
                mutable=node.mutable,
                protected=node.protected,
                depends_on=node.depends_on,
                required_by=tuple(required_by[node.id]),
                canonical_json_bytes=sizes[node.id],
                trial_indexes=relevant,
                final_single_removal_trial_indexes=singles,
                removal_evidence=classification,
                text_section_fields=section_fields.get(node.id, ()),
                text=text,
                text_scope=text_scope,
                text_content_index=text_content_index,
                text_truncated=cut,
            )
        )
    return tuple(nodes), allowance - remaining, truncated


def _section_fields(
    evidence: CaseEvidence,
    retained: frozenset[str],
    selected_mutation: TextMutation | None = None,
    section_trials: tuple[SectionTrialInspection, ...] = (),
) -> dict[str, tuple[TextSectionFieldInspection, ...]]:
    """Bound metadata without truncating a source-bound field's section catalog."""

    fields: dict[str, list[TextSectionFieldInspection]] = {}
    nodes_by_id = evidence.context.by_id
    remaining = MAX_INSPECTION_SECTION_UNITS
    selected = (
        selected_mutation.edits[0]
        if selected_mutation is not None and selected_mutation.edits
        else None
    )
    selected_ids = set(selected.section_ids) if isinstance(selected, TextSectionRemoval) else set()
    trial_indexes: dict[tuple[str, int], list[int]] = {}
    for trial in section_trials:
        trial_indexes.setdefault((trial.context_id, trial.content_index), []).append(trial.index)
    for site in evidence.editable_text():
        if site.context_id not in retained:
            continue
        try:
            catalog = catalog_text_sections(
                case_digest=evidence.digest,
                context_id=site.context_id,
                context_digest=nodes_by_id[site.context_id].digest,
                content_index=site.content_index,
                text=site.original_text,
            )
        except TextSectionError:
            inspection = TextSectionFieldInspection(
                content_index=site.content_index,
                status="unavailable",
                unavailable_reason="section-policy-limit",
            )
        else:
            if len(catalog.sections) > remaining:
                inspection = TextSectionFieldInspection(
                    content_index=site.content_index,
                    status="unavailable",
                    unavailable_reason="inspection-section-limit",
                )
            else:
                remaining -= len(catalog.sections)
                inspection = TextSectionFieldInspection(
                    content_index=site.content_index,
                    status="available-untested",
                    catalog=catalog,
                    retained_utf8_bytes=catalog.original_utf8_bytes,
                )
                related = tuple(trial_indexes.get((site.context_id, site.content_index), ()))
                if related:
                    inspection = inspection.model_copy(
                        update={"status": "exploratory-tested", "trial_indexes": related}
                    )
                if isinstance(selected, TextSectionRemoval) and (
                    selected.context_id,
                    selected.content_index,
                ) == (site.context_id, site.content_index):
                    catalog.validate_removal(selected)
                    removed_bytes = sum(
                        section.utf8_bytes
                        for section in catalog.sections
                        if section.id in selected_ids
                    )
                    inspection = inspection.model_copy(
                        update={
                            "status": "selected",
                            "removed_section_ids": selected.section_ids,
                            "retained_utf8_bytes": catalog.original_utf8_bytes - removed_bytes,
                        }
                    )
        fields.setdefault(site.context_id, []).append(inspection)
    return {node_id: tuple(values) for node_id, values in fields.items()}


def _confirmation(
    status: str, confirmation: SampledConfirmation | None, policy: SampledReductionPlan
) -> ConfirmationInspection:
    if confirmation is None:
        return ConfirmationInspection(
            status=status,
            margin=policy.margin,
            one_sided_confidence=policy.confirmation.one_sided_confidence,
        )
    evidence = confirmation.evidence
    original, candidate = evidence.original_counts, evidence.candidate_counts
    classification = classify_confirmation(evidence, policy=policy)
    interval = classification.interval if classification else None
    return ConfirmationInspection(
        status=status,
        margin=policy.margin,
        one_sided_confidence=policy.confirmation.one_sided_confidence,
        original=original,
        candidate=candidate,
        difference=(candidate.matches / candidate.valid - original.matches / original.valid)
        if candidate.valid and original.valid
        else None,
        lower_bound=interval.lower if interval is not None else None,
        upper_bound=interval.upper if interval is not None else None,
    )


def _active_comparison(loaded: RetainedResult) -> ActiveComparisonInspection | None:
    if loaded.sampled is None or loaded.sampled.checkpoint.active_block is None:
        return None
    evidence: SamplingBlockEvidence = loaded.sampled.checkpoint.active_block
    return ActiveComparisonInspection(
        candidate_digest=evidence.plan.candidate_digest,
        baseline_digest=evidence.plan.original_candidate_digest,
        outcome="incomplete" if evidence.eligible else "ineligible",
        original=evidence.original_counts,
        candidate=evidence.candidate_counts,
    )


def _metadata_label(value: object) -> str | None:
    return value if isinstance(value, str) and _MACHINE_LABEL.fullmatch(value) else None


def _context_values(evidence: CaseEvidence) -> dict[str, str]:
    values = {}
    request = evidence.request if evidence.has_request else None
    selectors: dict[str, dict[int, str]] = {}
    for node in evidence.context.nodes:
        if node.path is not None and request is not None:
            value: JsonValue = request
            for token in decode_pointer(node.path):
                if isinstance(value, list):
                    value = value[int(token)]
                elif isinstance(value, dict):
                    value = value[token]
                else:
                    raise ValueError("context pointer has no value")
            values[node.id] = _canonical_text(value)
        elif node.selector is not None:
            selectors.setdefault(node.source, {})[node.selector.line] = node.id
    for artifact in evidence.source_artifacts:
        needed = selectors.get(artifact.path, {})
        if not needed:
            continue
        for line, raw in enumerate(io.BytesIO(artifact.content), start=1):
            node_id = needed.get(line)
            if node_id is not None:
                values[node_id] = _canonical_text(json.loads(raw))
    if set(values) != {node.id for node in evidence.context.nodes}:
        raise ValueError("context content is unavailable")
    return values


def _canonical_text(value: JsonValue) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _bounded_text(value: str, allowance: int) -> tuple[str, bool]:
    parts: list[str] = []
    used = 0
    for char in value:
        if char in "[]" or not char.isprintable():
            escaped = f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}"
        else:
            escaped = char
        count = len(escaped.encode("utf-8"))
        if used + count > allowance:
            return "".join(parts), True
        parts.append(escaped)
        used += count
    return "".join(parts), False
