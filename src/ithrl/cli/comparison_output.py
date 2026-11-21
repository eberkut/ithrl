"""Content-safe human and JSON reports for explicit-context comparisons."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from rich.console import Console

from ithrl.cli.output import ExitCode, OutputFormat, console, print_plain
from ithrl.comparison.contracts import (
    ComparisonCalibration,
    ComparisonConfirmation,
    ComparisonLimits,
)
from ithrl.execution import ExecutionPlanMetadata
from ithrl.mutation import TextAppend
from ithrl.observations import SamplingArmCounts
from ithrl.run_state import SampledRunUsage
from ithrl.runner import Candidate

if TYPE_CHECKING:
    from ithrl.comparison.results import ComparisonEvidence

type _ReportIdentifier = Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9._:/+-]+$")]
_IDENTIFIER = TypeAdapter(_ReportIdentifier)


class ContextReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_digest: str
    retained_context_count: int
    removed_context_ids: tuple[str, ...]
    mutation_digest: str
    text_operation: str | None = None
    text_context_id: str | None = None
    text_content_index: int | None = None
    appended_utf8_bytes: int | None = None
    removed_section_ids: tuple[str, ...] = ()

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> ContextReport:
        edit = next(iter(candidate.mutation.edits), None)
        return cls(
            candidate_digest=candidate.digest,
            retained_context_count=len(candidate.retained_context_ids),
            removed_context_ids=candidate.removed_context_ids,
            mutation_digest=candidate.mutation.digest,
            text_operation=edit.operation if edit is not None else None,
            text_context_id=edit.context_id if edit is not None else None,
            text_content_index=edit.content_index if edit is not None else None,
            appended_utf8_bytes=len(edit.text.encode()) if isinstance(edit, TextAppend) else None,
            removed_section_ids=edit.section_ids
            if edit is not None and not isinstance(edit, TextAppend)
            else (),
        )


class ComparisonOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    source_validation: Literal["not-requested", "verified"] = "not-requested"
    kind: Literal["comparison"] = "comparison"
    command: Literal["compare"] = "compare"
    status: Literal[
        "ready",
        "detection-required",
        "detected",
        "refused",
        "interrupted",
        "confirmed-improvement",
        "confirmed-degradation",
        "inconclusive",
        "calibration-failed",
        "calibrated",
        "incomplete",
    ]
    error_code: Annotated[str | None, Field(max_length=128, pattern=r"^[a-z][a-z0-9-]*$")] = None
    objective: Literal["repair", "degradation"] | None = None
    effect_direction: str | None = None
    case_path: str | None = None
    case_digest: str | None = None
    configuration_digest: str | None = None
    comparison_digest: str | None = None
    profile_path: str | None = None
    run_state_path: str | None = None
    result_path: str | None = None
    harness: Literal["fake", "codex", "copilot"] | None = None
    model: _ReportIdentifier | None = None
    provider: _ReportIdentifier | None = None
    harness_started: bool | None = False
    capability_source: Literal["builtin", "saved-detection", "current-detection", "unverified"] = (
        "unverified"
    )
    execution_metadata: ExecutionPlanMetadata | None = None
    reference_context: ContextReport | None = None
    candidate_context: ContextReport | None = None
    limits: ComparisonLimits | None = None
    calibration_plan: ComparisonCalibration | None = None
    confirmation_plan: ComparisonConfirmation | None = None
    planned_attempts: int | None = None
    planned_provider_calls: int | None = None
    planned_wall_time_seconds: int | None = None
    confirmation_provider_calls: int | None = None
    maximum_provider_calls_per_attempt: int | None = None
    minimum_effect: float | None = None
    alpha: float | None = None
    difference: float | None = None
    lower_bound: float | None = None
    calibration_reference: SamplingArmCounts | None = None
    calibration_candidate: SamplingArmCounts | None = None
    confirmation_reference: SamplingArmCounts | None = None
    confirmation_candidate: SamplingArmCounts | None = None
    execution_reliability: SamplingArmCounts | None = None
    usage: SampledRunUsage | None = None
    stop_reason: Annotated[str | None, Field(max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")] = None
    probe_stage: str | None = None
    probe_exit_code: int | None = None
    probe_reason: str | None = None
    diagnostic_path: str | None = None
    next_action: str | None = None
    next_command: str | None = None
    next_command_shell: Literal["posix", "powershell"] | None = None

    def public(self) -> dict[str, object]:
        result = self.model_dump(mode="json")
        if self.usage is not None:
            result["usage"] = {
                **self.usage.model_dump(mode="json"),
                "charged_provider_calls": self.usage.charged_provider_calls,
            }
        return result


def safe_display_identifier(value: str | None) -> str | None:
    """Omit identifiers that cannot be displayed safely without altering private evidence."""

    if value is None:
        return None
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError:
        return None


def _recorded_identity(
    result: ComparisonEvidence, field: Literal["model_requested", "provider"]
) -> str | None:
    values = {
        value
        for block in result.blocks
        for attempt in block.attempts
        if (value := getattr(attempt.observation.attempt_evidence, field)) is not None
    }
    if len(values) != 1:
        return None
    return safe_display_identifier(values.pop())


def result_report(result: ComparisonEvidence) -> ComparisonOutput:
    from ithrl.comparison.engine import calibration_passes

    binding = result.binding
    calibration, confirmation = result.calibration, result.confirmation
    progress = result.state.comparison_progress
    calibrated = (
        calibration is not None
        and calibration_passes(calibration) is True
        and result.state.selection is None
        and progress is not None
        and progress.terminal is None
    )
    difference, lower_bound = result._effect()
    return ComparisonOutput(
        status="calibrated" if calibrated else result.outcome.value,
        objective=binding.procedure.objective,
        effect_direction="candidate-minus-reference"
        if binding.procedure.objective == "repair"
        else "reference-minus-candidate",
        case_digest=result.original_identity.case_digest,
        comparison_digest=binding.digest,
        model=_recorded_identity(result, "model_requested"),
        provider=_recorded_identity(result, "provider"),
        execution_metadata=result.execution_metadata,
        reference_context=ContextReport.from_candidate(binding.reference),
        candidate_context=ContextReport.from_candidate(binding.candidate),
        calibration_plan=ComparisonCalibration(
            reference=binding.procedure.calibration.original,
            candidate=binding.procedure.calibration.candidate,
            positive_minimum_matches=binding.procedure.positive_minimum_matches,
            negative_maximum_matches=binding.procedure.negative_maximum_matches,
        ),
        confirmation_plan=ComparisonConfirmation(
            reference=binding.procedure.confirmation.original,
            candidate=binding.procedure.confirmation.candidate,
            alpha=binding.procedure.confirmation.alpha,
        ),
        minimum_effect=binding.procedure.minimum_effect,
        alpha=binding.procedure.confirmation.alpha,
        difference=difference,
        lower_bound=lower_bound,
        calibration_reference=calibration.original_counts if calibration is not None else None,
        calibration_candidate=calibration.candidate_counts if calibration is not None else None,
        confirmation_reference=confirmation.original_counts if confirmation is not None else None,
        confirmation_candidate=confirmation.candidate_counts if confirmation is not None else None,
        execution_reliability=SamplingArmCounts.from_observations(
            attempt.observation for block in result.blocks for attempt in block.attempts
        ),
        usage=result.usage,
        stop_reason=result.stop_reason,
        next_action="The frozen attempt schedule is exhausted. Report the incomplete evidence; a revised plan requires a new run."
        if result.stop_reason == "attempt-limit"
        else "Inspect the tested context change, counts and effect under the recorded execution profile.",
    )


def render_comparison(
    report: ComparisonOutput, output_format: OutputFormat, *, target: Console = console
) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.public(), sort_keys=True))
        return
    print_plain(target, f"Comparison: {report.status}")
    for label, value in (
        ("objective", report.objective),
        ("direction", report.effect_direction),
        ("harness", report.harness),
        ("model", report.model),
        ("provider", report.provider),
        ("attempt ceiling", report.planned_attempts),
        ("provider-call ceiling", report.planned_provider_calls),
        ("minimum effect", report.minimum_effect),
        ("alpha", report.alpha),
        ("difference", report.difference),
        ("lower bound", report.lower_bound),
        ("profile", report.profile_path),
        ("run", report.run_state_path),
        ("result", report.result_path),
        ("stop reason", report.stop_reason),
        ("error", report.error_code),
        ("probe stage", report.probe_stage),
        ("probe reason", report.probe_reason),
        ("private diagnostic", report.diagnostic_path),
    ):
        if value is not None:
            print_plain(target, f"  {label:<24} {value}", soft_wrap=True)
    if report.execution_metadata is not None:
        metadata = report.execution_metadata
        print_plain(target, f"  reconstruction           {metadata.reconstruction_method}")
        if metadata.reasoning_omitted:
            print_plain(target, "  historical reasoning     omitted from both inputs")
        print_plain(
            target, f"  declared policy          {metadata.declared_policy.model_dump_json()}"
        )
        print_plain(
            target, f"  attempt timeout          {metadata.resource_limits.max_wall_time_seconds}s"
        )
    if report.planned_wall_time_seconds is not None:
        print_plain(target, f"  planned wall time        {report.planned_wall_time_seconds}s")
    if report.limits is not None:
        print_plain(target, f"  wall-time ceiling        {report.limits.max_wall_time_seconds}s")
    for label, context in (
        ("reference", report.reference_context),
        ("candidate", report.candidate_context),
    ):
        if context is not None:
            print_plain(target, f"  {label} context: {context.candidate_digest}")
            print_plain(target, f"    retained units: {context.retained_context_count}")
            print_plain(
                target,
                f"    removed IDs: {json.dumps(context.removed_context_ids)}",
                soft_wrap=True,
            )
            if context.text_operation is not None:
                print_plain(
                    target,
                    f"    edit: {context.text_operation}; field {context.text_context_id}/{context.text_content_index}",
                )
                print_plain(target, f"    mutation digest: {context.mutation_digest}")
                if context.appended_utf8_bytes is not None:
                    print_plain(target, f"    appended bytes: {context.appended_utf8_bytes}")
                if context.removed_section_ids:
                    print_plain(
                        target,
                        f"    removed sections: {json.dumps(context.removed_section_ids)}",
                        soft_wrap=True,
                    )
    for label, plan in (
        ("calibration", report.calibration_plan),
        ("confirmation", report.confirmation_plan),
    ):
        if plan is not None:
            print_plain(
                target,
                f"  {label} samples: reference {plan.reference.required_valid} valid / {plan.reference.max_attempts} attempts; candidate {plan.candidate.required_valid} valid / {plan.candidate.max_attempts} attempts",
            )
    if report.calibration_plan is not None:
        positive = "candidate" if report.objective == "repair" else "reference"
        print_plain(
            target,
            f"  calibration gate: {positive} matches >= {report.calibration_plan.positive_minimum_matches}; other matches <= {report.calibration_plan.negative_maximum_matches}",
        )
    for label, counts in (
        ("reference calibration", report.calibration_reference),
        ("candidate calibration", report.calibration_candidate),
        ("reference confirmation", report.confirmation_reference),
        ("candidate confirmation", report.confirmation_candidate),
    ):
        if counts is not None:
            print_plain(
                target,
                f"  {label:<24} {counts.matches}/{counts.valid} matches; {counts.invalid_observations} invalid; {counts.runner_errors} errors",
            )
    if report.usage is not None:
        for name in (
            "actual_provider_calls",
            "possible_provider_calls",
            "pending_provider_calls",
            "charged_provider_calls",
            "known_tokens",
            "unknown_token_attempts",
            "wall_time_seconds",
        ):
            print_plain(target, f"  {name:<24} {getattr(report.usage, name)}")
    if report.next_action:
        print_plain(target, report.next_action)
    if report.next_command:
        print_plain(
            target, f"Command ({report.next_command_shell}): {report.next_command}", soft_wrap=True
        )


def comparison_exit_code(report: ComparisonOutput) -> ExitCode:
    if report.status == "interrupted":
        return ExitCode.CANCELLED
    if report.error_code is not None:
        return ExitCode.UNSUPPORTED if report.status == "refused" else ExitCode.PROVIDER
    if report.status in {
        "ready",
        "detected",
        "detection-required",
        "calibrated",
        "confirmed-improvement",
        "confirmed-degradation",
    }:
        return ExitCode.SUCCESS
    if report.status in {"inconclusive", "calibration-failed"}:
        return ExitCode.INCONCLUSIVE
    if report.stop_reason and any(
        word in report.stop_reason for word in ("budget", "limit", "exhausted")
    ):
        return ExitCode.BUDGET
    return ExitCode.UNSUPPORTED if report.status == "refused" else ExitCode.PROVIDER
