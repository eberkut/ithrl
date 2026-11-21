"""Private comparison evidence with offline, source-bound interpretation."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.case import CaseEvidence
from ithrl.case.filesystem import (
    atomic_write,
    lstat_regular_file,
    prepare_private_directory,
    read_safe_text,
    reject_symlink_components,
)
from ithrl.comparison.contracts import (
    MAX_COMPARISON_RESULT_BYTES,
    BoundComparison,
    ComparisonOutcome,
)
from ithrl.context import content_digest
from ithrl.execution import ExecutionPlanMetadata
from ithrl.mutation import EditableText, TextAppend
from ithrl.observations import ObservationIdentity, SamplingBlockEvidence
from ithrl.results.validation import loads_json
from ithrl.run_state import SampledRunState, SampledRunStateFile, SampledRunUsage
from ithrl.runner import Candidate

_MAX_BYTES = MAX_COMPARISON_RESULT_BYTES


class ComparisonEvidence(BaseModel):
    """Frozen progress and its resolved observations; summaries are always recomputed.

    Recorded paths identify the original run but grant no execution or continuation authority.
    The snapshot can be interpreted without a runner, source workspace or observation database.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    kind: Literal["comparison-evidence"] = "comparison-evidence"
    state: SampledRunState = Field(repr=False)
    blocks: tuple[SamplingBlockEvidence, ...] = Field(repr=False)
    execution_metadata: ExecutionPlanMetadata
    recorded_at: datetime
    refusal: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")] = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("comparison evidence time requires a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_evidence(self) -> ComparisonEvidence:
        from ithrl.comparison.engine import validate_comparison_progress

        if self.execution_metadata.digest != self.original_identity.execution_plan_digest:
            raise ValueError("comparison metadata differs from its execution identity")
        if self.recorded_at < self.state.started_at:
            raise ValueError("comparison evidence precedes its run")
        progress = self.state.comparison_progress
        if (
            progress is not None
            and progress.stopped_at is not None
            and (progress.stopped_at > self.recorded_at)
        ):
            raise ValueError("comparison stop follows its evidence snapshot")
        if (
            progress is not None
            and progress.terminal is not None
            and self.refusal not in {None, progress.stop_reason}
        ):
            raise ValueError("comparison refusal differs from its terminal progress")
        if tuple(progress.plan for progress in self.state.blocks) != tuple(
            block.plan for block in self.blocks
        ):
            raise ValueError("comparison state and observation blocks disagree")
        for progress, block in zip(self.state.blocks, self.blocks, strict=True):
            if len(progress.observations) != len(block.attempts):
                raise ValueError("comparison references omit recorded attempts")
            for reference, attempt in zip(progress.observations, block.attempts, strict=True):
                observation = attempt.observation
                policy = observation.attempt_evidence.effective_policy_digest
                if policy is not None and policy != self.execution_metadata.safety_policy_digest:
                    raise ValueError("comparison observation effective policy differs")
                if (
                    reference.schedule_position != attempt.schedule_position
                    or reference.observation_id != observation.observation_id
                    or reference.attempt_id != observation.attempt_evidence.attempt_id
                    or reference.attempt_digest != observation.attempt_evidence.attempt_digest
                    or reference.independence_evidence_digest
                    != attempt.independence_evidence_digest
                    or observation.recorded_at > self.recorded_at
                ):
                    raise ValueError("comparison observation differs from its persisted reference")
        validate_comparison_progress(
            self.state, (), self.blocks, require_current_implementation=False
        )
        return self

    @property
    def run_id(self) -> str:
        return self.state.run_id

    @property
    def original_identity(self) -> ObservationIdentity:
        return self.state.original_identity

    @property
    def binding(self) -> BoundComparison:
        progress = self.state.comparison_progress
        if progress is None:
            raise ValueError("comparison evidence has no frozen pair")
        return progress.binding

    @property
    def calibration(self) -> SamplingBlockEvidence | None:
        return next((block for block in self.blocks if block.plan.role == "calibration"), None)

    @property
    def confirmation(self) -> SamplingBlockEvidence | None:
        return next((block for block in self.blocks if block.plan.role == "confirmation"), None)

    @property
    def selected_at(self) -> datetime | None:
        progress = self.state.comparison_progress
        return None if progress is None else progress.selected_at

    @property
    def outcome(self) -> ComparisonOutcome:
        progress = self.state.comparison_progress
        return (
            ComparisonOutcome.INCOMPLETE
            if progress is None or progress.terminal is None
            else progress.terminal
        )

    @property
    def difference(self) -> float | None:
        return self._effect()[0]

    @property
    def lower_bound(self) -> float | None:
        return self._effect()[1]

    def _effect(self) -> tuple[float | None, float | None]:
        from ithrl.comparison.engine import confirm_comparison

        block = self.confirmation
        if block is None or self.outcome in {
            ComparisonOutcome.INCOMPLETE,
            ComparisonOutcome.CALIBRATION_FAILED,
        }:
            return None, None
        _, difference, bound = confirm_comparison(block)
        return difference, bound

    @property
    def usage(self) -> SampledRunUsage:
        return SampledRunStateFile._usage(self.state, (), self.blocks, now=self.recorded_at)

    @property
    def stop_reason(self) -> str | None:
        progress = self.state.comparison_progress
        return self.refusal or (None if progress is None else progress.stop_reason)

    def validate_for_case(self, evidence: CaseEvidence) -> None:
        """Re-derive source partitions and edits without reconstructing a private runner plan."""

        validate_comparison_source(self.binding, self.execution_metadata, evidence)


def validate_comparison_source(
    binding: BoundComparison, execution_metadata: ExecutionPlanMetadata, evidence: CaseEvidence
) -> None:
    """Validate the two saved contexts against source evidence without loading a runner."""

    from ithrl.adapters.registry import decision_input_node_id
    from ithrl.extraction import validate_extracted_case

    if binding.reference.case_digest != evidence.digest:
        raise ValueError("comparison belongs to different case evidence")
    session = validate_extracted_case(evidence)
    trigger = None
    if session is not None and evidence.decision is not None:
        trigger = decision_input_node_id(
            session,
            evidence.decision.selector,
            evidence.context,
            records=session.records,
        )
    sites: dict[tuple[str, int], EditableText] | None = None
    for selection, candidate in (
        (binding.procedure.reference, binding.reference),
        (binding.procedure.candidate, binding.candidate),
    ):
        removed = evidence.context.dependency_closure(selection.removed_context_ids)
        if trigger is not None and trigger in removed:
            raise ValueError("comparison removes its fixed decision input")
        expected = Candidate(
            case_digest=evidence.digest,
            execution_plan_digest=execution_metadata.digest,
            source_artifact_digests=tuple(source.sha256 for source in evidence.source_artifacts),
            retained_context_ids=tuple(n.id for n in evidence.context.nodes if n.id not in removed),
            removed_context_ids=tuple(n.id for n in evidence.context.nodes if n.id in removed),
            materialization="exact-request" if evidence.has_request else "source-backed",
            mutation=selection.mutation,
        )
        if (
            candidate != expected
            or candidate.materialization != execution_metadata.candidate_materialization
        ):
            raise ValueError("comparison context differs from its immutable source")
        if evidence.has_request:
            evidence._protect_section_fixed_input(removed)
            evidence.materialize(removed, mutation=selection.mutation)
        else:
            for edit in selection.mutation.edits:
                if isinstance(edit, TextAppend):
                    if sites is None:
                        sites = {
                            (site.context_id, site.content_index): site
                            for site in evidence.editable_text()
                        }
                    site = sites.get((edit.context_id, edit.content_index))
                    if (
                        site is None
                        or content_digest(site.original_text) != edit.original_text_digest
                    ):
                        raise ValueError("comparison append differs from its source field")
                else:
                    evidence.validate_text_sections(edit)


def write_comparison_result(path: Path, result: ComparisonEvidence) -> Path:
    """Publish one private immutable snapshot; never replace an existing destination."""

    path = path.absolute()
    validated = ComparisonEvidence.model_validate_json(result.model_dump_json())
    content = validated.model_dump_json(indent=2) + "\n"
    if len(content.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("comparison result exceeds its size limit")
    prepare_private_directory(path.parent)
    atomic_write(path, content)
    return path


def load_comparison_result(
    path: Path, case_evidence: CaseEvidence | None = None
) -> ComparisonEvidence:
    """Validate saved evidence offline without probing native state or providers."""

    try:
        path = path.absolute()
        reject_symlink_components(path)
        metadata = lstat_regular_file(path, label="comparison result")
        if metadata.st_nlink != 1 or (
            os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("comparison result must be private and single-link")
        raw = read_safe_text(path, max_bytes=_MAX_BYTES, label="comparison result")
        loads_json(raw, label="comparison result")
        result = ComparisonEvidence.model_validate_json(raw)
        if case_evidence is not None:
            result.validate_for_case(case_evidence)
        return result
    except (OSError, ValueError):
        raise ValueError("cannot load private comparison evidence") from None
