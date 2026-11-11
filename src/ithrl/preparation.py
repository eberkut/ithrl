"""Prepare native reduction configurations from recorded facts without native work."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence, CaseFormatError
from ithrl.context import content_digest
from ithrl.execution import ResolvedExecutionPlan, resolve_execution_plan
from ithrl.experiment import (
    AnyExperimentDefinition,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SamplingEligibilityPolicy,
    TextSectionSearchPlan,
    parse_experiment_toml,
)
from ithrl.extraction import validate_experiment_target, validate_extracted_case
from ithrl.reduce.contracts import validate_section_search
from ithrl.text_sections import MAX_TEXT_SECTIONS, TextSectionRemoval
from ithrl.workflow import OraclePrerequisiteError, validate_oracle_prerequisites

type _Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PreparationError(ValueError):
    """A content-safe refusal to prepare a configuration from recorded facts."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value


class NativePreparationReceipt(_Contract):
    """Private, unauthenticated evidence from one successful explicit native preflight."""

    schema_version: Literal[1]
    kind: Literal["native-preparation"]
    source_plan: ResolvedExecutionPlan = Field(repr=False)
    source_plan_digest: _Digest
    oracle_digest: _Digest
    original_workspace: Annotated[str, Field(min_length=1, max_length=4096, repr=False)]

    @model_validator(mode="after")
    def validate_recorded_facts(self) -> NativePreparationReceipt:
        if self.source_plan.digest != self.source_plan_digest:
            raise ValueError("receipt plan identity is inconsistent")
        if self.source_plan.capability.runner_type not in ("codex", "copilot"):
            raise ValueError("receipt requires a native runner")
        workspace = Path(self.original_workspace)
        if (
            not workspace.is_absolute()
            or ".." in workspace.parts
            or "\x00" in self.original_workspace
        ):
            raise ValueError("receipt workspace must be absolute without parent traversal or NUL")
        return self

    @classmethod
    def create(
        cls,
        plan: ResolvedExecutionPlan,
        experiment: AnyExperimentDefinition,
        original_workspace: Path,
    ) -> NativePreparationReceipt:
        """Record an already successful preflight; do not inspect its workspace."""

        if plan.experiment_digest != experiment.digest:
            raise PreparationError("preflight-experiment-mismatch")
        return cls(
            schema_version=1,
            kind="native-preparation",
            source_plan=plan,
            source_plan_digest=plan.digest,
            oracle_digest=content_digest(experiment.oracle.model_dump(mode="json")),
            original_workspace=str(original_workspace),
        )

    @classmethod
    def from_json(cls, source: str) -> NativePreparationReceipt:
        """Reject malformed receipts without echoing private input or validation values."""

        try:
            return cls.model_validate_json(source)
        except ValidationError:
            raise PreparationError("invalid-preflight-receipt") from None


class SectionSelector(_Contract):
    """An ordered source selection whose hashes are derived from the case."""

    context_id: Annotated[
        str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    ]
    content_index: Annotated[int, Field(ge=0)]
    section_ids: Annotated[
        tuple[Annotated[str, Field(pattern=r"^section-[0-9a-f]{64}$")], ...],
        Field(min_length=1, max_length=MAX_TEXT_SECTIONS),
    ]


class SectionPreparationSettings(_Contract):
    """Prospective section choices, validated by the existing bound search contract."""

    fields: Annotated[tuple[SectionSelector, ...], Field(min_length=1, max_length=256)]
    max_coarse_comparisons: int
    max_fine_comparisons: int
    minimum_removed_utf8_bytes: int
    require_criterion_for_confirmation: bool
    stop_when_no_retained_fields: bool

    def bind(self, evidence: CaseEvidence) -> TextSectionSearchPlan:
        catalogs = {
            (catalog.context_id, catalog.content_index): catalog
            for catalog in evidence.text_sections()
        }
        fields: list[TextSectionRemoval] = []
        for selector in self.fields:
            catalog = catalogs.get((selector.context_id, selector.content_index))
            if catalog is None:
                raise PreparationError("section-selection-mismatch")
            fields.append(catalog.removal(selector.section_ids))
        return TextSectionSearchPlan(
            fields=tuple(fields),
            max_coarse_comparisons=self.max_coarse_comparisons,
            max_fine_comparisons=self.max_fine_comparisons,
            minimum_removed_utf8_bytes=self.minimum_removed_utf8_bytes,
            require_criterion_for_confirmation=self.require_criterion_for_confirmation,
            stop_when_no_retained_fields=self.stop_when_no_retained_fields,
        )


class _ReductionSettings(SampledReductionPlan):
    """Reuse statistical validation while refusing manually bound section descriptors."""

    eligibility_policy: SamplingEligibilityPolicy
    section_search: None = None


class ReductionPreparationSettings(_Contract):
    """Input-only statistical settings; output remains the canonical experiment format."""

    schema_version: Literal[1]
    kind: Literal["reduction-preparation"]
    calibration: SampledCalibrationPhase
    reduction: _ReductionSettings
    sections: SectionPreparationSettings | None = None

    @classmethod
    def from_toml(cls, source: str) -> ReductionPreparationSettings:
        """Parse explicit settings without target, oracle, runner or caller-supplied hashes."""

        try:
            raw = tomllib.loads(source)
            # TOML arrays become immutable tuples at the same contract boundary as experiments.
            sections = raw.get("sections")
            if isinstance(sections, dict) and isinstance(sections.get("fields"), list):
                sections["fields"] = tuple(
                    {**field, "section_ids": tuple(field["section_ids"])}
                    if isinstance(field, dict) and isinstance(field.get("section_ids"), list)
                    else field
                    for field in sections["fields"]
                )
            return cls.model_validate(raw)
        except (tomllib.TOMLDecodeError, ValidationError, TypeError, ValueError):
            raise PreparationError("invalid-preparation-settings") from None


def prepare_reduction(
    evidence: CaseEvidence,
    receipt: NativePreparationReceipt,
    settings: ReductionPreparationSettings,
) -> SampledExperimentDefinition:
    """Bind explicit choices to immutable source and recorded execution facts, offline."""

    try:
        return _prepare_reduction(evidence, receipt, settings)
    except PreparationError:
        raise
    except (ValueError, OraclePrerequisiteError):
        raise PreparationError("preparation-contract-mismatch") from None


def _prepare_reduction(
    evidence: CaseEvidence,
    receipt: NativePreparationReceipt,
    settings: ReductionPreparationSettings,
) -> SampledExperimentDefinition:
    validate_extracted_case(evidence)
    default = parse_experiment_toml(evidence.default_experiment)
    source = receipt.source_plan
    if content_digest(default.oracle.model_dump(mode="json")) != receipt.oracle_digest:
        raise PreparationError("preflight-oracle-mismatch")
    reduction = SampledReductionPlan.model_validate(settings.reduction.model_dump())
    if settings.sections is not None:
        reduction = reduction.model_copy(
            update={"section_search": settings.sections.bind(evidence)}
        )
    runner_type = source.capability.runner_type
    if runner_type not in ("codex", "copilot"):
        raise PreparationError("invalid-preflight-receipt")
    experiment = SampledExperimentDefinition(
        target=default.target,
        oracle=default.oracle,
        runner=NativeRunnerConfig(
            type=runner_type, required_capability_digest=source.capability.digest
        ),
        calibration=settings.calibration,
        reduction=reduction,
    )
    if evidence.decision is not None:
        try:
            validate_experiment_target(evidence.decision, experiment.target)
        except CaseFormatError:
            raise PreparationError("preflight-target-mismatch") from None
    validate_oracle_prerequisites(evidence, experiment)
    if reduction.section_search is not None:
        if (
            not isinstance(experiment.target, FinalResponseTarget)
            or "remove-text-sections" not in source.capability.supported_context_operations
        ):
            raise PreparationError("unsupported-section-search")
        validate_section_search(evidence, reduction)
    report = resolve_execution_plan(
        source.capability,
        experiment,
        evidence=evidence,
        runner_fingerprint=source.runner_fingerprint,
        safety_policy=source.safety_policy,
        resource_limits=source.resource_limits,
    )
    if not report.ready or report.plan is None:
        raise PreparationError("preparation-contract-mismatch")
    # The complete reduction intentionally differs from the source calibration experiment.
    if source.model_dump(exclude={"experiment_digest"}) != report.plan.model_dump(
        exclude={"experiment_digest"}
    ):
        raise PreparationError("preflight-evidence-mismatch")
    return experiment
