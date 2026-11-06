"""Persisted contracts and identities for deterministic reduction."""

from __future__ import annotations

from collections.abc import Generator
from fractions import Fraction
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import MAX_CONTEXT_NODES, JsonObject, content_digest
from ithrl.experiment import SampledReductionPlan
from ithrl.mutation import TextMutation
from ithrl.observations import ObservationIdentity, SamplingBlockEvidence
from ithrl.text_sections import TextSectionCatalog, TextSectionRemoval

if TYPE_CHECKING:
    from ithrl.case import CaseEvidence
from ithrl.statistics import (
    BinomialSample,
    FixedSampleClassification,
    FixedSampleOutcome,
    classify_fixed_sample,
)

ALGORITHM_NAME = "hierarchical-ddmin"
ALGORITHM_VERSION = "2"
ALGORITHM_DIGEST = content_digest({"name": ALGORITHM_NAME, "version": ALGORITHM_VERSION})
DEFAULT_MAX_CANDIDATE_RUNS = 100
MAX_CANDIDATE_RUNS = 10_000

type SearchClassification = Literal["preserves", "loses", "invalid"]
type SearchPhase = Literal["chunk", "final-sweep"]
type SearchStatus = Literal["search-1-minimal", "search-incomplete"]


class ReductionError(RuntimeError):
    """Base error for deterministic reduction."""


class ResumeMismatchError(ReductionError):
    """A checkpoint does not belong to the requested reduction."""


class CandidateTrial(BaseModel):
    """One candidate classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_key: str = Field(min_length=1)
    candidate_digest: str | None
    base_removals: tuple[str, ...]
    requested_removals: tuple[str, ...]
    effective_removals: tuple[str, ...]
    target_observed: bool | None
    classification: SearchClassification
    phase: SearchPhase
    runner_executed: bool
    observation_id: str | None = None
    observation_attempt: int | None = Field(default=None, ge=1)
    reused: bool = False

    @model_validator(mode="after")
    def validate_classification(self) -> CandidateTrial:
        self._validate_removals()
        if self.classification == "invalid":
            self._validate_invalid()
        else:
            self._validate_observed()
        return self

    def _validate_removals(self) -> None:
        if self.base_removals != tuple(sorted(set(self.base_removals))):
            raise ValueError("base_removals must contain sorted unique node IDs")
        if len(set(self.requested_removals)) != len(self.requested_removals):
            raise ValueError("requested_removals must not contain duplicates")
        if self.effective_removals != tuple(sorted(set(self.effective_removals))):
            raise ValueError("effective_removals must contain sorted unique node IDs")
        if not set(self.base_removals) <= set(self.effective_removals):
            raise ValueError("effective_removals must include base_removals")
        if self.trial_key != trial_key(self.phase, self.base_removals, self.requested_removals):
            raise ValueError("trial_key does not match the trial inputs")

    def _validate_observed(self) -> None:
        if any(
            value is None
            for value in (
                self.candidate_digest,
                self.target_observed,
                self.observation_id,
                self.observation_attempt,
            )
        ):
            raise ValueError("valid trials require a candidate observation")
        if (self.classification == "preserves") != self.target_observed:
            raise ValueError("classification must match target_observed")
        if not self.runner_executed:
            raise ValueError("observed trials must record runner execution")

    def _validate_invalid(self) -> None:
        if any(
            value is not None
            for value in (
                self.candidate_digest,
                self.target_observed,
                self.observation_id,
                self.observation_attempt,
            )
        ):
            raise ValueError("invalid trials cannot have an observation")
        if self.runner_executed:
            raise ValueError("invalid trials cannot execute the runner")
        if self.effective_removals != self.base_removals:
            raise ValueError("invalid trials cannot change effective removals")

    @property
    def accepts_removal(self) -> bool:
        return self.classification == "preserves"

    @property
    def allows_search_continuation(self) -> bool:
        return True


class ReductionCheckpoint(BaseModel):
    """Complete deterministic search state needed for exact continuation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    case_digest: str = Field(min_length=1)
    experiment_digest: str = Field(min_length=1)
    runner_fingerprint: str = Field(min_length=1)
    execution_plan_digest: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)
    algorithm_digest: str = Field(pattern=r"^[0-9a-f]{64}$", default=ALGORITHM_DIGEST)
    removed_context_ids: tuple[str, ...]
    trials: tuple[CandidateTrial, ...] = Field(max_length=MAX_CANDIDATE_RUNS)
    failed_candidate_observation_ids: tuple[str, ...] = Field(
        default=(), max_length=MAX_CANDIDATE_RUNS
    )
    candidate_runs_used: int = Field(ge=0, le=MAX_CANDIDATE_RUNS)
    final_sweep_complete: bool = False

    @model_validator(mode="after")
    def validate_run_count(self) -> ReductionCheckpoint:
        if self.removed_context_ids != tuple(sorted(set(self.removed_context_ids))):
            raise ValueError("removed_context_ids must contain sorted unique node IDs")
        trial_keys = tuple(trial.trial_key for trial in self.trials)
        if len(set(trial_keys)) != len(trial_keys):
            raise ValueError("checkpoint trial keys must not contain duplicates")
        if len(set(self.failed_candidate_observation_ids)) != len(
            self.failed_candidate_observation_ids
        ):
            raise ValueError("failed candidate observation IDs must not contain duplicates")
        executed = sum(trial.runner_executed for trial in self.trials) + len(
            self.failed_candidate_observation_ids
        )
        if self.candidate_runs_used != executed:
            raise ValueError("candidate_runs_used must match executed candidate trials")
        return self


class ReductionResult(BaseModel):
    """Best candidate found by the search that keeps dependencies valid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SearchStatus
    case_digest: str
    experiment_digest: str
    original_request: JsonObject | None
    reduced_request: JsonObject | None
    removed_context_ids: tuple[str, ...]
    retained_context_ids: tuple[str, ...]
    trials: tuple[CandidateTrial, ...]
    candidate_runs_used: int
    final_sweep_complete: bool
    checkpoint: ReductionCheckpoint


def validate_checkpoint_identity(
    checkpoint: ReductionCheckpoint,
    *,
    case_digest: str,
    identity: ObservationIdentity,
) -> None:
    """Reject a checkpoint that belongs to another reduction."""

    expected = (
        case_digest,
        identity.experiment_digest,
        identity.runner_fingerprint,
        identity.execution_plan_digest,
        identity.target_digest,
        ALGORITHM_DIGEST,
    )
    actual = (
        checkpoint.case_digest,
        checkpoint.experiment_digest,
        checkpoint.runner_fingerprint,
        checkpoint.execution_plan_digest,
        checkpoint.target_digest,
        checkpoint.algorithm_digest,
    )
    if actual != expected:
        raise ResumeMismatchError(
            "checkpoint identities do not match the case, experiment, execution plan, target, "
            "and algorithm"
        )


def trial_key(
    phase: SearchPhase,
    base_removals: tuple[str, ...] | frozenset[str],
    requested_removals: tuple[str, ...],
) -> str:
    """Return the stable identity for a deterministic candidate trial."""

    return content_digest(
        {
            "phase": phase,
            "base_removals": sorted(base_removals),
            "requested_removals": list(requested_removals),
        }
    )


type SampledScreenAction = Literal["admit", "retain", "incomplete"]
type _SampledNodeId = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]


class SampledComparison(BaseModel):
    """A frozen exploratory screen, never a preservation classification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence: SamplingBlockEvidence
    action: SampledScreenAction | None

    @classmethod
    def from_evidence(cls, evidence: SamplingBlockEvidence) -> SampledComparison:
        """Derive the frozen exploratory action without assigning a confidence claim."""

        return cls(evidence=evidence, action=_sampled_screen_action(evidence))

    @model_validator(mode="after")
    def validate_action(self) -> SampledComparison:
        if self.evidence.plan.role != "search":
            raise ValueError("sampled comparisons require search evidence")
        expected = _sampled_screen_action(self.evidence)
        if self.action != expected:
            raise ValueError("screen action does not match complete eligible evidence")
        return self

    @property
    def operational_refusal(self) -> str | None:
        """Expose operational refusal separately from the exploratory screen action."""

        return self.evidence.eligibility_refusal


class _SampledRemovalTrial(BaseModel):
    """Strict metadata shared by sampled and locally invalid removal trials."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trial_key: str = Field(min_length=1)
    base_removals: tuple[_SampledNodeId, ...] = Field(max_length=MAX_CONTEXT_NODES)
    requested_removals: tuple[_SampledNodeId, ...] = Field(max_length=MAX_CONTEXT_NODES)
    effective_removals: tuple[_SampledNodeId, ...] = Field(max_length=MAX_CONTEXT_NODES)
    phase: SearchPhase

    @model_validator(mode="after")
    def validate_removals(self) -> _SampledRemovalTrial:
        if self.base_removals != tuple(sorted(set(self.base_removals))):
            raise ValueError("base_removals must contain sorted unique node IDs")
        if not self.requested_removals or len(set(self.requested_removals)) != len(
            self.requested_removals
        ):
            raise ValueError("requested_removals must contain nonempty unique node IDs")
        if self.effective_removals != tuple(sorted(set(self.effective_removals))):
            raise ValueError("effective_removals must contain sorted unique node IDs")
        if not set(self.base_removals) <= set(self.effective_removals):
            raise ValueError("effective_removals must include base removals")
        if self.trial_key != trial_key(self.phase, self.base_removals, self.requested_removals):
            raise ValueError("trial_key does not match the trial inputs")
        return self


class SampledCandidateTrial(_SampledRemovalTrial):
    """Removal metadata bound to a two-arm exploratory comparison."""

    comparison: SampledComparison

    @model_validator(mode="after")
    def validate_candidate(self) -> SampledCandidateTrial:
        requested = set(self.base_removals) | set(self.requested_removals)
        if not requested <= set(self.effective_removals):
            raise ValueError("effective_removals must include base and requested removals")
        return self

    @property
    def accepts_removal(self) -> bool:
        return self.comparison.action == "admit"

    @property
    def allows_search_continuation(self) -> bool:
        return self.comparison.action in {"admit", "retain"}


class SampledInvalidCandidateTrial(_SampledRemovalTrial):
    """A local dependency or reconstruction refusal, without sampling or a verdict."""

    classification: Literal["invalid"]

    @model_validator(mode="after")
    def validate_invalid(self) -> SampledInvalidCandidateTrial:
        if self.effective_removals != self.base_removals:
            raise ValueError("invalid trials cannot change effective removals")
        return self

    @property
    def accepts_removal(self) -> bool:
        return False

    @property
    def allows_search_continuation(self) -> bool:
        return True


type SampledTrial = SampledCandidateTrial | SampledInvalidCandidateTrial


def section_trial_key(
    field: TextSectionRemoval,
    phase: SearchPhase,
    base_section_ids: tuple[str, ...],
    requested_section_ids: tuple[str, ...],
) -> str:
    """Bind a finer trial to its frozen source field, policy and cumulative edit."""

    return content_digest(
        {
            "stage": "text-sections",
            "field": field.model_dump(mode="json"),
            "phase": phase,
            "base_section_ids": list(base_section_ids),
            "requested_section_ids": list(requested_section_ids),
        }
    )


class SampledSectionTrial(BaseModel):
    """One field's exploratory removal, or a pure reconstruction refusal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trial_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    field: TextSectionRemoval
    base_section_ids: tuple[str, ...] = Field(max_length=1024)
    requested_section_ids: tuple[str, ...] = Field(min_length=1, max_length=1024)
    mutation: TextMutation
    removed_utf8_bytes: int = Field(ge=1, le=1024 * 1024)
    phase: SearchPhase
    comparison: SampledComparison | None = None

    @model_validator(mode="after")
    def validate_trial(self) -> SampledSectionTrial:
        eligible = self.field.section_ids
        base, requested = set(self.base_section_ids), set(self.requested_section_ids)
        if (
            self.base_section_ids != tuple(unit for unit in eligible if unit in base)
            or len(requested) != len(self.requested_section_ids)
            or not requested <= set(eligible)
            or base & requested
        ):
            raise ValueError("finer trial units must be canonical eligible original units")
        expected = self.field.model_copy(
            update={"section_ids": tuple(unit for unit in eligible if unit in base | requested)}
        )
        if self.mutation != TextMutation(edits=(expected,)):
            raise ValueError("finer trial mutation differs from its field and removals")
        if self.trial_key != section_trial_key(
            self.field, self.phase, self.base_section_ids, self.requested_section_ids
        ):
            raise ValueError("finer trial key differs from its frozen inputs")
        return self

    @property
    def accepts_removal(self) -> bool:
        return self.comparison is not None and self.comparison.action == "admit"

    @property
    def allows_search_continuation(self) -> bool:
        return self.comparison is None or self.comparison.action in {"admit", "retain"}


def validate_section_search(
    evidence: CaseEvidence, plan: SampledReductionPlan
) -> tuple[TextSectionCatalog, ...]:
    """Re-derive every frozen field from source before calibration or collection."""

    config = plan.section_search
    if config is None:
        return ()
    return tuple(evidence.validate_text_sections(field) for field in config.fields)


def section_candidates(
    plan: SampledReductionPlan, removed_context_ids: tuple[str, ...]
) -> Generator[
    tuple[TextSectionRemoval, tuple[str, ...], tuple[str, ...], SearchPhase],
    SampledSectionTrial,
    None,
]:
    """Regenerate the frozen per-field order without accumulating across fields."""

    from ithrl.reduce.search import removal_candidates

    config = plan.section_search
    if config is None:
        return
    seen: dict[str, SampledSectionTrial] = {}
    for field in config.fields:
        if field.context_id in removed_context_ids:
            continue
        candidates = removal_candidates(field.section_ids)
        try:
            base, requested, phase = next(candidates)
            while True:
                ordered_base = tuple(unit for unit in field.section_ids if unit in base)
                key = section_trial_key(field, phase, ordered_base, requested)
                trial = seen.get(key)
                if trial is None:
                    trial = yield field, ordered_base, requested, phase
                    seen[key] = trial
                if not trial.allows_search_continuation:
                    return
                updated = frozenset((*base, *requested)) if trial.accepts_removal else base
                base, requested, phase = candidates.send(updated)
        except StopIteration:
            continue


def _sampled_screen_action(evidence: SamplingBlockEvidence) -> SampledScreenAction | None:
    if not evidence.eligible:
        return None
    if not evidence.complete:
        return "incomplete"
    original = evidence.original_counts
    candidate = evidence.candidate_counts
    # Exact ratios preserve the admission boundary without a floating-point tolerance.
    difference = Fraction(candidate.matches, candidate.valid) - Fraction(
        original.matches, original.valid
    )
    return "admit" if difference >= -Fraction(str(evidence.plan.preservation.margin)) else "retain"


class SampledReductionCheckpoint(BaseModel):
    """Sampled search evidence without a scheduler or a minimality claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    reduction: SampledReductionPlan
    original_identity: ObservationIdentity
    original_candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    best_candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    removed_context_ids: tuple[_SampledNodeId, ...] = Field(max_length=MAX_CONTEXT_NODES)
    trials: tuple[SampledTrial, ...] = Field(default=(), max_length=MAX_CANDIDATE_RUNS)
    active_block: SamplingBlockEvidence | None = None
    final_sweep_complete: bool = False
    final_sweep_trial_keys: tuple[str, ...] = Field(default=(), max_length=MAX_CANDIDATE_RUNS)
    expected_final_neighbors: tuple[
        Annotated[tuple[_SampledNodeId, ...], Field(max_length=MAX_CONTEXT_NODES)], ...
    ] = Field(default=(), max_length=MAX_CONTEXT_NODES)
    coarse_stopped: bool = False
    coarse_candidate_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    coarse_stop_reason: str | None = Field(default=None, max_length=128)
    fine_trials: tuple[SampledSectionTrial, ...] = Field(default=(), max_length=MAX_CANDIDATE_RUNS)
    selected_mutation: TextMutation = TextMutation()
    additional_removed_utf8_bytes: int = Field(default=0, ge=0, le=1024 * 1024)
    fine_complete: bool = False

    @model_validator(mode="after")
    def validate_checkpoint(self) -> SampledReductionCheckpoint:
        if self.removed_context_ids != tuple(sorted(set(self.removed_context_ids))):
            raise ValueError("removed_context_ids must contain sorted unique node IDs")
        keys = tuple(trial.trial_key for trial in self.trials)
        if len(set(keys)) != len(keys):
            raise ValueError("checkpoint trial keys must not contain duplicates")
        best = self.original_candidate_digest
        removed: tuple[str, ...] = ()
        for index, trial in enumerate(self.trials):
            if trial.base_removals != removed:
                raise ValueError("trial base removals must match the current best candidate")
            if not trial.allows_search_continuation and (
                index + 1 < len(self.trials) or self.active_block is not None
            ):
                raise ValueError("search cannot continue after incomplete or ineligible evidence")
            if isinstance(trial, SampledCandidateTrial) and trial.accepts_removal:
                best = trial.comparison.evidence.plan.candidate_digest
                removed = trial.effective_removals
        if self.removed_context_ids != removed:
            raise ValueError("checkpoint best candidate must match admitted comparisons")
        self._validate_sections(best)
        blocks = self.blocks
        if self.active_block is not None and self.active_block.complete:
            raise ValueError("active block must be partial")
        for block in blocks:
            plan = block.plan
            if plan.role != "search":
                raise ValueError("sampled checkpoints require search blocks")
            if (
                plan.procedure != self.reduction
                or plan.original_identity != self.original_identity
                or plan.original_candidate_digest != self.original_candidate_digest
            ):
                raise ValueError("checkpoint blocks must share the frozen reduction and original")
        _validate_unique_sampled_references(blocks)
        self._validate_final_sweep()
        return self

    def _validate_sections(self, coarse_digest: str) -> None:
        config = self.reduction.section_search
        if config is None:
            if self.best_candidate_digest != coarse_digest:
                raise ValueError("checkpoint best candidate must match admitted comparisons")
            if (
                self.coarse_stopped
                or self.coarse_candidate_digest is not None
                or self.coarse_stop_reason is not None
                or self.fine_trials
                or not self.selected_mutation.is_empty
                or self.additional_removed_utf8_bytes
                or self.fine_complete
                or self.best_candidate_digest != coarse_digest
            ):
                raise ValueError("coarse-only checkpoints cannot contain finer search state")
            return
        if self.coarse_stopped != (self.coarse_candidate_digest is not None):
            raise ValueError("a stopped coarse stage requires its immutable candidate identity")
        if self.coarse_stopped and self.coarse_candidate_digest != coarse_digest:
            raise ValueError("finer search must retain the frozen coarse candidate")
        if not self.coarse_stopped and (
            self.fine_trials
            or self.fine_complete
            or not self.selected_mutation.is_empty
            or self.coarse_stop_reason is not None
        ):
            raise ValueError("finer search requires a durably stopped coarse stage")
        if (
            sum(isinstance(t, SampledCandidateTrial) for t in self.trials)
            > config.max_coarse_comparisons
        ):
            raise ValueError("coarse stage exceeds its frozen comparison ceiling")
        if sum(t.comparison is not None for t in self.fine_trials) > config.max_fine_comparisons:
            raise ValueError("fine stage exceeds its frozen comparison ceiling")
        best, mutation, size = coarse_digest, TextMutation(), 0
        seen: set[str] = set()
        for index, trial in enumerate(self.fine_trials):
            if trial.trial_key in seen or trial.field not in config.fields:
                raise ValueError("finer trials require distinct frozen-field identities")
            seen.add(trial.trial_key)
            if not trial.allows_search_continuation and (
                index + 1 < len(self.fine_trials) or self.active_block is not None
            ):
                raise ValueError("finer search cannot follow incomplete or ineligible evidence")
            if (
                trial.accepts_removal
                and trial.removed_utf8_bytes > size
                and trial.comparison is not None
            ):
                best = trial.comparison.evidence.plan.candidate_digest
                mutation, size = trial.mutation, trial.removed_utf8_bytes
        if (
            self.best_candidate_digest,
            self.selected_mutation,
            self.additional_removed_utf8_bytes,
        ) != (best, mutation, size):
            raise ValueError(
                "selected finer candidate must maximize admitted bytes in stable order"
            )

    def _validate_final_sweep(self) -> None:
        """Check the supplied terminal witness; graph coverage belongs to the generator."""

        if not self.final_sweep_complete:
            if self.final_sweep_trial_keys or self.expected_final_neighbors:
                raise ValueError("incomplete final sweeps cannot carry a completion witness")
            return
        if (self.active_block is not None and not self.coarse_stopped) or any(
            not trial.allows_search_continuation for trial in self.trials
        ):
            raise ValueError("final sweep completion requires complete eligible search evidence")
        keys = self.final_sweep_trial_keys
        neighbors = self.expected_final_neighbors
        if len(set(keys)) != len(keys) or len(set(neighbors)) != len(neighbors):
            raise ValueError("final sweep witnesses must not contain duplicates")
        if any(not item or len(set(item)) != len(item) for item in neighbors):
            raise ValueError("final neighbors must contain nonempty unique node IDs")
        trials = {trial.trial_key: trial for trial in self.trials}
        if any(key not in trials for key in keys):
            raise ValueError("final sweep references must belong to checkpoint trials")
        selected = tuple(trials[key] for key in keys)
        if any(
            trial.phase != "final-sweep"
            or trial.base_removals != self.removed_context_ids
            or trial.accepts_removal
            or not trial.allows_search_continuation
            for trial in selected
        ):
            raise ValueError("final sweep requires complete eligible retained final neighbors")
        if {trial.requested_removals for trial in selected} != set(neighbors):
            raise ValueError("final sweep must cover exactly the declared final neighbors")

    @property
    def blocks(self) -> tuple[SamplingBlockEvidence, ...]:
        """All search blocks, including a partial dispatch block when present."""

        completed = tuple(
            trial.comparison.evidence
            for trial in self.trials
            if isinstance(trial, SampledCandidateTrial)
        )
        completed += tuple(
            trial.comparison.evidence for trial in self.fine_trials if trial.comparison is not None
        )
        return completed if self.active_block is None else (*completed, self.active_block)

    @property
    def best_candidate_identity(self) -> ObservationIdentity:
        """Execution identity of the most recently admitted candidate or original."""

        for trial in self.fine_trials:
            if (
                trial.accepts_removal
                and trial.mutation == self.selected_mutation
                and trial.comparison is not None
            ):
                return trial.comparison.evidence.plan.candidate_identity
        for trial in reversed(self.trials):
            if isinstance(trial, SampledCandidateTrial) and trial.accepts_removal:
                return trial.comparison.evidence.plan.candidate_identity
        return self.original_identity

    @property
    def search_complete(self) -> bool:
        """Whether both configured search domains were exhausted, without sampled minimality."""

        return self.final_sweep_complete and (
            self.reduction.section_search is None or self.fine_complete
        )

    @property
    def additional_reduction_result(
        self,
    ) -> Literal["not-assessed", "no-retained-fields", "below-threshold", "criterion-met"]:
        """Interpret the frozen byte criterion separately from preservation and debugging value."""

        config = self.reduction.section_search
        if config is None or not self.coarse_stopped:
            return "not-assessed"
        if not any(field.context_id not in self.removed_context_ids for field in config.fields):
            return "no-retained-fields"
        return (
            "criterion-met"
            if self.additional_removed_utf8_bytes >= config.minimum_removed_utf8_bytes
            else "below-threshold"
        )


def validate_section_checkpoint(
    evidence: CaseEvidence, checkpoint: SampledReductionCheckpoint, *, run_id: str
) -> None:
    """Pure source binding and exact finer-path replay for resume and offline inspection."""

    from ithrl.runner import Candidate

    catalogs = validate_section_search(evidence, checkpoint.reduction)
    config = checkpoint.reduction.section_search
    if config is None:
        return
    by_field = {(c.context_id, c.content_index): c for c in catalogs}
    removed = frozenset(checkpoint.removed_context_ids)
    if evidence.context.dependency_closure(removed) != removed:
        raise ResumeMismatchError("finer checkpoint coarse removals are not dependency closed")

    def candidate(mutation: TextMutation) -> Candidate:
        for edit in mutation.edits:
            if not isinstance(edit, TextSectionRemoval) or edit.context_id in removed:
                raise ResumeMismatchError("finer checkpoint edits a removed or unsupported field")
            evidence.validate_text_sections(edit)
        if evidence.has_request:
            evidence.materialize(removed, mutation=mutation)
        return Candidate(
            case_digest=evidence.digest,
            execution_plan_digest=checkpoint.original_identity.execution_plan_digest,
            source_artifact_digests=tuple(a.sha256 for a in evidence.source_artifacts),
            retained_context_ids=tuple(n.id for n in evidence.context.nodes if n.id not in removed),
            removed_context_ids=tuple(n.id for n in evidence.context.nodes if n.id in removed),
            materialization="exact-request" if evidence.has_request else "source-backed",
            mutation=mutation,
        )

    if (
        checkpoint.coarse_stopped
        and candidate(TextMutation()).digest != checkpoint.coarse_candidate_digest
    ):
        raise ResumeMismatchError("finer checkpoint coarse source identity does not match")
    path = section_candidates(checkpoint.reduction, checkpoint.removed_context_ids)
    next_candidate = next(path, None)
    for trial in checkpoint.fine_trials:
        if next_candidate != (
            trial.field,
            trial.base_section_ids,
            trial.requested_section_ids,
            trial.phase,
        ):
            raise ResumeMismatchError("finer trials do not follow the frozen traversal")
        catalog = by_field[(trial.field.context_id, trial.field.content_index)]
        edit = trial.mutation.edits[0]
        if not isinstance(edit, TextSectionRemoval):
            raise ResumeMismatchError("finer trial requires source-bound section removal")
        selected = set(edit.section_ids)
        size = sum(section.utf8_bytes for section in catalog.sections if section.id in selected)
        if size != trial.removed_utf8_bytes:
            raise ResumeMismatchError("finer trial byte count differs from immutable source")
        if trial.comparison is not None and (
            candidate(trial.mutation).digest != trial.comparison.evidence.plan.candidate_digest
            or trial.comparison.evidence.plan.candidate_identity != checkpoint.original_identity
        ):
            raise ResumeMismatchError(
                "finer trial candidate identity differs from immutable source"
            )
        try:
            next_candidate = path.send(trial) if trial.allows_search_continuation else None
        except StopIteration:
            next_candidate = None
    if checkpoint.fine_complete and any(
        not t.allows_search_continuation for t in checkpoint.fine_trials
    ):
        raise ResumeMismatchError("finer completion requires complete eligible comparisons")
    if checkpoint.fine_complete and next_candidate is not None:
        raise ResumeMismatchError("finer completion omits eligible generated candidates")
    if checkpoint.coarse_stopped and checkpoint.active_block is not None:
        if checkpoint.fine_complete or next_candidate is None:
            raise ResumeMismatchError("active finer block has no next generated candidate")
        field, base, requested, phase = next_candidate
        selected = {*base, *requested}
        edit = field.model_copy(
            update={"section_ids": tuple(unit for unit in field.section_ids if unit in selected)}
        )
        key = section_trial_key(field, phase, base, requested)
        seed = int(content_digest({"run_id": run_id, "trial_key": key})[:16], 16)
        plan = checkpoint.active_block.plan
        if (
            plan.candidate_digest != candidate(TextMutation(edits=(edit,))).digest
            or plan.candidate_identity != checkpoint.original_identity
            or plan.block_id != "search-" + key
            or plan.schedule_seed != seed
        ):
            raise ResumeMismatchError("active finer block differs from the next frozen candidate")
    if candidate(checkpoint.selected_mutation).digest != checkpoint.best_candidate_digest:
        raise ResumeMismatchError("selected finer candidate identity differs from immutable source")


def classify_confirmation(
    evidence: SamplingBlockEvidence, *, policy: SampledReductionPlan | None = None
) -> FixedSampleClassification | None:
    """Assess complete eligible preservation evidence under its confirmation policy."""

    if not evidence.eligible:
        return None
    if not evidence.complete:
        return FixedSampleClassification(FixedSampleOutcome.INCOMPLETE, None)
    policy = evidence.plan.preservation if policy is None else policy
    phase = policy.confirmation
    candidate, original = evidence.candidate_counts, evidence.original_counts
    return classify_fixed_sample(
        BinomialSample(candidate.matches, candidate.valid),
        BinomialSample(original.matches, original.valid),
        required_candidate=phase.candidate.required_valid,
        required_original=phase.original.required_valid,
        confidence=phase.one_sided_confidence,
        margin=policy.margin,
    )


class SampledConfirmation(BaseModel):
    """One fresh final comparison with the fixed-sample statistical outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence: SamplingBlockEvidence
    outcome: FixedSampleOutcome | None

    @classmethod
    def from_evidence(cls, evidence: SamplingBlockEvidence) -> SampledConfirmation:
        classification = classify_confirmation(evidence)
        return cls(evidence=evidence, outcome=classification.outcome if classification else None)

    @model_validator(mode="after")
    def validate_outcome(self) -> SampledConfirmation:
        if not isinstance(self.evidence.plan.procedure, SampledReductionPlan):
            raise ValueError("preservation confirmation requires a reduction procedure")
        if self.evidence.plan.role != "confirmation":
            raise ValueError("sampled confirmation requires confirmation evidence")
        classification = classify_confirmation(self.evidence)
        expected = classification.outcome if classification else None
        if self.outcome != expected:
            raise ValueError("confirmation outcome does not match complete eligible evidence")
        return self

    @property
    def operational_refusal(self) -> str | None:
        """Operational refusal remains separate from the four statistical outcomes."""

        return self.evidence.eligibility_refusal


def _validate_unique_sampled_references(blocks: tuple[SamplingBlockEvidence, ...]) -> None:
    for references, label in (
        (tuple(block.plan.block_id for block in blocks), "block"),
        (tuple(item for block in blocks for item in block.observation_ids), "observation"),
        (tuple(item for block in blocks for item in block.attempt_ids), "attempt"),
    ):
        if len(set(references)) != len(references):
            raise ValueError(f"sampled {label} references must not be reused")
