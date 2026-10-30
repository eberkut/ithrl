"""Content-safe human and JSON views of a bounded fuzz campaign."""

from __future__ import annotations

import json
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console

from ithrl.cli.output import ExitCode, OutputFormat, console, print_plain
from ithrl.experiment import SampledCalibrationPhase
from ithrl.fuzz.configuration import FuzzCommandLimits
from ithrl.fuzz.contracts import FuzzConfirmation, FuzzScreening
from ithrl.observations import Observation, ObservationOutcome
from ithrl.run_state import SampledRunUsage

type _Name = Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9._:/+-]+$")]


class FuzzCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dispatched: Annotated[int, Field(ge=0)] = 0
    matches: Annotated[int, Field(ge=0)] = 0
    no_matches: Annotated[int, Field(ge=0)] = 0
    invalid_observations: Annotated[int, Field(ge=0)] = 0
    runner_errors: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def consistent_counts(self) -> FuzzCounts:
        if (
            self.dispatched
            != self.matches + self.no_matches + self.invalid_observations + self.runner_errors
        ):
            raise ValueError("fuzz counts must include every dispatched outcome")
        return self

    @property
    def valid(self) -> int:
        return self.matches + self.no_matches

    def public(self) -> dict[str, int | float | None]:
        return {
            **self.model_dump(),
            "valid": self.valid,
            "match_rate": self.matches / self.valid if self.valid else None,
            "valid_rate": self.valid / self.dispatched if self.dispatched else None,
        }

    @classmethod
    def observations(cls, observations: tuple[Observation, ...]) -> FuzzCounts:
        return cls(
            dispatched=len(observations),
            matches=sum(o.outcome is ObservationOutcome.MATCH for o in observations),
            no_matches=sum(o.outcome is ObservationOutcome.NO_MATCH for o in observations),
            invalid_observations=sum(
                o.outcome is ObservationOutcome.INVALID_OBSERVATION for o in observations
            ),
            runner_errors=sum(o.outcome is ObservationOutcome.RUNNER_ERROR for o in observations),
        )


class FuzzOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    kind: Literal["fuzz"] = "fuzz"
    command: Literal["fuzz"] = "fuzz"
    status: Literal[
        "ready",
        "detection-required",
        "detected",
        "confirmed-change",
        "inconclusive",
        "incomplete",
        "no-candidate-found",
        "refused",
        "interrupted",
    ]
    error_code: Annotated[str | None, Field(max_length=128, pattern=r"^[a-z][a-z-]+$")] = None
    case_path: str | None = None
    case_digest: str | None = None
    run_state_path: str | None = None
    result_path: str | None = None
    profile_path: str | None = None
    configuration_digest: str | None = None
    plan_digest: str | None = None
    capability_digest: str | None = None
    capability_source: Literal["builtin", "saved-detection", "current-detection", "unverified"] = (
        "unverified"
    )
    harness_started: bool | None = False
    probe_stage: str | None = Field(default=None, max_length=40, pattern=r"^[a-z-]+$")
    probe_exit_code: int | None = None
    probe_reason: str | None = Field(default=None, max_length=240)
    diagnostic_path: str | None = None
    runner_error_category: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    runner_error_reason_code: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]{1,80}$"
    )
    harness: _Name | None = None
    harness_version: _Name | None = None
    runner_version: _Name | None = None
    model: _Name | None = None
    provider: _Name | None = None
    endpoint: Annotated[
        str | None,
        Field(
            max_length=256,
            pattern=r"^(?:https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?|copilot://managed)$",
        ),
    ] = None
    target: Literal["final-response", "shell-action"] | None = None
    controlled_surface: _Name | None = None
    protected_state_enforcement: str | None = None
    action_visibility: str | None = None
    action_denial: str | None = None
    tool_network: Literal["forbidden"] = "forbidden"
    supported_context_operations: tuple[str, ...] = ()
    mutators: tuple[Literal["duplicate-text", "append-stale-text"], ...] = ()
    mutation_surface: Literal["append-to-editable-text"] = "append-to-editable-text"
    editable_text_sites: int | None = None
    calibration_plan: SampledCalibrationPhase | None = None
    screening_plan: FuzzScreening | None = None
    confirmation_plan: FuzzConfirmation | None = None
    stress_text_bytes: int = 0
    selected_mutation_bytes: int | None = None
    selected_mutation_digest: str | None = None
    proposals_examined: int = 0
    trials_used: int = 0
    max_candidates: int | None = None
    max_simplification_trials: int | None = None
    limits: FuzzCommandLimits | None = None
    max_attempts: int | None = None
    planned_provider_calls: int | None = None
    max_provider_calls_per_attempt: int | None = Field(default=None, ge=0, le=16)
    confirmation_provider_calls: int | None = None
    minimum_wall_time_seconds: int | None = None
    max_native_output_bytes: int | None = None
    max_disposable_state_bytes: int | None = None
    max_disposable_state_entries: int | None = None
    freshness_seconds: int | None = None
    eligibility_policy: str | None = None
    estimand: Literal["valid-only"] = "valid-only"
    effect_direction: Literal["original-minus-mutant"] = "original-minus-mutant"
    minimum_decrease: float | None = None
    alpha: float | None = None
    confirmation_method: Literal["clopper-pearson-union-bound"] = "clopper-pearson-union-bound"
    confirmation_result: str = "not-run"
    difference: float | None = None
    lower_bound: float | None = None
    calibration: FuzzCounts = FuzzCounts()
    confirmation_original: FuzzCounts = FuzzCounts()
    confirmation_mutant: FuzzCounts = FuzzCounts()
    execution_reliability: FuzzCounts = FuzzCounts()
    usage: SampledRunUsage | None = None
    provider_call_accounting: Literal["not-started", "accounted", "started-unknown"] = "not-started"
    provider_calls: int | None = 0
    unknown_acceptance_attempts: int = 0
    actions_executed: int | None = 0
    stop_reason: str | None = None
    next_action: str | None = None
    next_command: str | None = None
    next_command_shell: Literal["posix", "powershell"] | None = None

    def public(self) -> dict[str, object]:
        result = self.model_dump(mode="json")
        for name in (
            "calibration",
            "confirmation_original",
            "confirmation_mutant",
            "execution_reliability",
        ):
            result[name] = getattr(self, name).public()
        if self.usage is not None:
            result["usage"]["charged_provider_calls"] = self.usage.charged_provider_calls
        return result


def render_fuzz(
    report: FuzzOutput, output_format: OutputFormat, *, target: Console = console
) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.public(), sort_keys=True))
        return
    print_plain(target, f"Fuzz: {report.status}")
    rows = (
        ("harness", report.harness),
        ("harness version", report.harness_version),
        ("runner version", report.runner_version),
        ("unknown acceptance", report.unknown_acceptance_attempts),
        ("model", report.model),
        ("provider", report.provider),
        ("destination", report.endpoint),
        ("target", report.target),
        ("capability", report.capability_source),
        ("surface", report.controlled_surface),
        ("protected state", report.protected_state_enforcement),
        ("action visibility", report.action_visibility),
        ("action denial", report.action_denial),
        ("tool network", report.tool_network),
        ("mutation surface", report.mutation_surface),
        ("editable text sites", report.editable_text_sites),
        ("mutators", ", ".join(report.mutators)),
        ("stress text", f"{report.stress_text_bytes} bytes"),
        ("trial limit", report.max_candidates),
        ("simplification limit", report.max_simplification_trials),
        ("attempt limit", report.max_attempts),
        ("provider calls per attempt", report.max_provider_calls_per_attempt),
        ("freshness", report.freshness_seconds),
        ("effect direction", report.effect_direction),
        ("minimum decrease", report.minimum_decrease),
        ("confirmation alpha", report.alpha),
        ("confirmation", report.confirmation_result),
        (
            "selected mutation",
            f"{report.selected_mutation_bytes} bytes"
            if report.selected_mutation_bytes is not None
            else None,
        ),
        ("difference", report.difference),
        ("lower bound", report.lower_bound),
        ("profile", report.profile_path),
        ("run state", report.run_state_path),
        ("result", report.result_path),
        ("stop reason", report.stop_reason),
        ("error", report.error_code),
        ("probe stage", report.probe_stage),
        ("probe exit code", report.probe_exit_code),
        ("probe reason", report.probe_reason),
        ("private diagnostic", report.diagnostic_path),
        (
            "last runner error",
            ": ".join(
                value
                for value in (report.runner_error_category, report.runner_error_reason_code)
                if value
            ),
        ),
    )
    for label, value in rows:
        if value is not None and value != "":
            print_plain(target, f"  {label:<22} {value}", soft_wrap=True)
    for label, plan in (
        ("calibration plan", report.calibration_plan),
        ("screening plan", report.screening_plan),
        ("confirmation plan", report.confirmation_plan),
    ):
        if plan is not None:
            print_plain(target, f"  {label:<22} {plan.model_dump_json()}")
    if report.limits is not None:
        for label, value in report.limits.model_dump().items():
            print_plain(target, f"  {label:<28} {value}")
    if report.usage is not None:
        for label in (
            "actual_provider_calls",
            "possible_provider_calls",
            "pending_provider_calls",
            "charged_provider_calls",
            "known_tokens",
            "unknown_token_attempts",
            "wall_time_seconds",
        ):
            print_plain(target, f"  {label:<28} {getattr(report.usage, label)}")
    for label, counts in (
        ("calibration", report.calibration),
        ("original holdout", report.confirmation_original),
        ("mutant holdout", report.confirmation_mutant),
    ):
        if counts.dispatched:
            rate = counts.matches / counts.valid if counts.valid else None
            print_plain(
                target,
                f"  {label:<22} {counts.matches}/{counts.valid} matches; rate {rate}; {counts.invalid_observations} invalid; {counts.runner_errors} errors",
            )
    reliability = report.execution_reliability
    if reliability.dispatched:
        print_plain(
            target,
            f"  execution reliability  {reliability.valid}/{reliability.dispatched} valid attempts",
        )
    print_plain(
        target,
        "  Behavior is estimated among valid observations; errors are excluded from behavioral rates.",
    )
    if report.next_action:
        print_plain(target, report.next_action)
    if report.next_command:
        print_plain(
            target, f"Command ({report.next_command_shell}): {report.next_command}", soft_wrap=True
        )


def fuzz_exit_code(report: FuzzOutput) -> ExitCode:
    if report.error_code == "result-publication-failed":
        return ExitCode.INTERNAL
    if report.runner_error_category == "provider-call-limit":
        return ExitCode.BUDGET
    if report.status in (
        "ready",
        "detected",
        "detection-required",
        "confirmed-change",
        "no-candidate-found",
    ):
        return ExitCode.SUCCESS
    if report.status == "inconclusive":
        return ExitCode.INCONCLUSIVE
    if report.status == "interrupted":
        return ExitCode.CANCELLED
    if report.stop_reason and any(
        token in report.stop_reason for token in ("budget", "limit", "exhausted")
    ):
        return ExitCode.BUDGET
    return ExitCode.UNSUPPORTED if report.status == "refused" else ExitCode.PROVIDER
