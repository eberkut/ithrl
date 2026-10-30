"""Structured command output and safe terminal display."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum, StrEnum
from functools import wraps
from typing import Annotated, Any, Literal, Never, override

import typer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console
from rich.text import Text
from typer._click import ClickException
from typer._click.core import Exit
from typer.core import TyperGroup

from ithrl.experiment import SamplingEligibilityPolicy
from ithrl.observations import Observation, ObservationOutcome


def last_runner_error(
    observations: Sequence[Observation],
) -> tuple[str | None, str | None]:
    """Return only fixed diagnostics from the latest runner-error observation."""
    errors = (
        item
        for item in observations
        if getattr(item, "outcome", None) is ObservationOutcome.RUNNER_ERROR
    )
    latest = max(errors, key=lambda item: item.recorded_at, default=None)
    if latest is None:
        return None, None
    return latest.error_category, latest.attempt_evidence.reason_code


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    UNSUPPORTED = 3
    INCONCLUSIVE = 4
    PROVIDER = 5
    BUDGET = 6
    CANCELLED = 130
    INTERNAL = 70


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


class _OutputEnvelope(BaseModel):
    """Strict envelope for the current CLI output format."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1


class CommandOutput(_OutputEnvelope):
    kind: Literal["deterministic"] = "deterministic"
    command: Literal["extract", "shrink"]
    status: Literal[
        "case-created",
        "ready",
        "calibrated",
        "reduced",
        "stopped",
        "interrupted",
        "confirmation-failed",
        "not-observed",
        "failed",
    ]
    case_path: str
    case_digest: str
    result_path: str | None = None
    original_context_count: int | None = None
    retained_context_count: int | None = None
    calibration_result: str | None = None
    search_result: str | None = None
    confirmation_result: str | None = None
    candidate_runs_used: int = 0
    max_candidate_runs: int | None = None
    observations_recorded: int = 0
    execution_plan_digest: str | None = None
    runner_type: str | None = None
    runner_version: str | None = None
    capture_type: str | None = None
    capture_completeness: str | None = None
    replay_state: str | None = None
    replay_environment: str | None = None
    harness: str | None = None
    harness_version: str | None = None
    session_id: str | None = None
    decision_id: str | None = None
    target_id: str | None = None
    target_type: str | None = None
    evidence_support: str | None = None
    experimental_support: str | None = None
    limitations: tuple[str, ...] = ()
    protected_context_ids: tuple[str, ...] = ()
    run_state_path: str | None = None
    network_calls: Literal[0] = 0
    provider_call_accounting: Literal["not-started"] = "not-started"
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0
    next_action: str | None = None
    next_command: str | None = None
    next_command_shell: Literal["posix", "powershell"] | None = None


class ErrorOutput(_OutputEnvelope):
    kind: Literal["deterministic"] = "deterministic"
    command: Literal["ithrl", "extract", "shrink", "fuzz"]
    status: Literal["error"] = "error"
    exit_code: int
    error_code: str
    message: str
    network_calls: Literal[0] = 0
    provider_call_accounting: Literal["not-started"] = "not-started"
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0


class SampledOutput(_OutputEnvelope):
    """Content-free result contract for a sampled shrink run."""

    kind: Literal["sampled"] = "sampled"
    command: Literal["shrink"] = "shrink"
    eligibility_policy: SamplingEligibilityPolicy = "independent-safe-preaccept-only"
    status: Literal[
        "ready",
        "measurable",
        "not-measurable",
        "inconclusive",
        "incomplete",
        "refused",
        "interrupted",
    ]
    harness_started: bool | None = None
    plan_digest: str = Field(min_length=1, max_length=128)
    capability_digest: str = Field(min_length=1, max_length=128)
    harness: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    harness_version: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._+-]+$")
    target: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    controlled_surface: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    decision_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requested_model: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:/+-]+$"
    )
    supported_context_operations: tuple[
        Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")], ...
    ] = ()
    protected_state_enforcement: Literal["not-applicable", "postcondition", "preventive"] = (
        "not-applicable"
    )
    disposable_state_limit_enforcement: Literal["not-applicable", "observed", "enforced"] = (
        "not-applicable"
    )
    process_containment: Literal["not-applicable", "process-group", "process-tree"] = (
        "not-applicable"
    )
    input_token_limit_enforcement: Literal["unavailable", "pre-dispatch"] = "unavailable"
    action_visibility: Literal["not-applicable", "visible", "unknown"] = "unknown"
    action_denial: Literal["not-applicable", "enforced", "unverified"] = "unverified"
    deviations: tuple[
        Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$")], ...
    ] = Field(default=(), max_length=16)
    safety_policy_digest: str = Field(min_length=1, max_length=128)
    safety_effects: tuple[
        Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")], ...
    ] = Field(default=(), max_length=16)
    provider: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    endpoint: str | None = Field(
        default=None,
        max_length=200,
        pattern=(
            r"^(?:https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?"
            r"(?:/[A-Za-z0-9._~/-]{0,160})?|copilot://managed)$"
        ),
    )
    max_attempts: int = Field(ge=0)
    required_valid: int | None = Field(default=None, ge=1)
    minimum_matches: int | None = Field(default=None, ge=1)
    max_provider_calls: int = Field(ge=0)
    max_provider_calls_per_attempt: int = Field(default=1, ge=0, le=16)
    max_runner_seconds: float = Field(ge=0)
    max_wall_time_seconds: int | None = Field(default=None, ge=1)
    inter_attempt_delay_seconds: int = Field(default=0, ge=0, le=86_400)
    max_tokens: int | None = Field(default=None, ge=0)
    attempt_timeout_seconds: int | None = Field(default=None, ge=1)
    max_native_output_bytes: int | None = Field(default=None, ge=1)
    max_disposable_state_bytes: int | None = Field(default=None, ge=0)
    max_disposable_state_entries: int | None = Field(default=None, ge=0)
    provider_call_accounting: Literal["not-started", "started-unknown", "accounted"]
    provider_calls: int | None = Field(default=None, ge=0)
    attempts_used: int | None = Field(default=None, ge=0)
    runner_seconds: float | None = Field(default=None, ge=0)
    wall_time_seconds: float | None = Field(default=None, ge=0)
    stop_reason: (
        Literal[
            "valid-sample-limit",
            "attempt-limit",
            "unsafe-retry",
            "safety-refusal",
            "user-interrupt",
            "wall-time-budget",
        ]
        | None
    ) = None
    matches: int | None = Field(default=None, ge=0)
    no_matches: int | None = Field(default=None, ge=0)
    invalid_observations: int | None = Field(default=None, ge=0)
    runner_errors: int | None = Field(default=None, ge=0)
    runner_error_category: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    runner_error_reason_code: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]{1,80}$"
    )
    actions_executed: int | None = Field(default=None, ge=0)
    result_path: str | None = Field(default=None, max_length=240)
    next_action: str | None = Field(default=None, max_length=240)
    next_command: str | None = Field(default=None, max_length=2048)
    next_command_shell: Literal["posix", "powershell"] | None = None

    @model_validator(mode="after")
    def validate_accounting(self) -> SampledOutput:
        _validate_provider_accounting(self.provider_call_accounting, self.provider_calls)
        if self.provider_call_accounting == "not-started" and self.attempts_used not in (None, 0):
            raise ValueError("an unstarted run cannot report completed work")
        if self.attempts_used is not None and self.attempts_used > self.max_attempts:
            raise ValueError("attempt count exceeds the preflight bound")
        if self.provider_calls is not None and self.provider_calls > self.max_provider_calls:
            raise ValueError("provider calls exceed the preflight bound")
        if self.status == "ready" and self.provider_call_accounting != "not-started":
            raise ValueError("a ready plan cannot have started execution")
        if self.required_valid is not None and self.required_valid > self.max_attempts:
            raise ValueError("required valid samples exceed the attempt bound")
        if (
            self.minimum_matches is not None
            and self.required_valid is not None
            and self.minimum_matches > self.required_valid
        ):
            raise ValueError("match threshold exceeds required valid samples")
        return self


class SampledErrorOutput(_OutputEnvelope):
    """Safe sampled-run refusal with fixed probe reasons, never native output."""

    kind: Literal["sampled"] = "sampled"
    command: Literal["shrink"] = "shrink"
    status: Literal["refused", "incomplete", "interrupted"]
    exit_code: int
    error_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    harness_started: bool | None = None
    provider_call_accounting: Literal["not-started", "started-unknown"] = "started-unknown"
    provider_calls: Literal[0] | None = None
    next_action: str | None = Field(default=None, max_length=240)
    probe_stage: str | None = Field(default=None, max_length=40, pattern=r"^[a-z-]+$")
    probe_exit_code: int | None = None
    probe_reason: str | None = Field(default=None, max_length=240)
    diagnostic_path: str | None = None
    minimum_provider_calls: int | None = Field(default=None, ge=0)
    minimum_wall_time_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_accounting(self) -> SampledErrorOutput:
        _validate_provider_accounting(self.provider_call_accounting, self.provider_calls)
        return self


def _validate_provider_accounting(
    accounting: Literal["not-started", "started-unknown", "accounted"], calls: int | None
) -> None:
    if accounting == "not-started" and calls != 0:
        raise ValueError("an unstarted run must report zero provider calls")
    if accounting == "started-unknown" and calls is not None:
        raise ValueError("unknown provider calls cannot have a count")
    if accounting == "accounted" and calls is None:
        raise ValueError("accounted provider calls require a count")


console = Console()
error_console = Console(stderr=True)

_SHRINK_HEADINGS = {
    "ready": "Execution plan is valid",
    "calibrated": "Original context tested",
    "reduced": "Reduced and confirmed",
    "stopped": "Search stopped before confirmation",
    "interrupted": "Search interrupted before confirmation",
    "confirmation-failed": "Reduced context did not pass confirmation",
    "not-observed": "Original context did not produce the target behavior",
    "failed": "Experiment failed before confirmation",
}
_CALIBRATION_LABELS = {
    "observed": "target observed",
    "not-observed": "target not observed",
    "failed": "failed",
    "not-run": "not run",
}
_SEARCH_LABELS = {
    "search-1-minimal": "1-minimal; all valid single removals tested",
    "search-incomplete": "incomplete",
    "not-run": "not run",
}
_CONFIRMATION_LABELS = {
    "confirmed-preserving": "target preserved",
    "failed": "failed",
    "not-run": "not run",
}
_LIMITATION_LABELS = {
    "experimental-support-unvalidated": "controlled experiment not validated",
    "opaque-context-frozen": "opaque context kept",
}


class JsonAwareGroup(TyperGroup):
    """Keep command-line parser errors inside the JSON output contract."""

    @override
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        if not _requests_json(arguments):
            return super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        try:
            result = super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except ClickException as error:
            command: Literal["ithrl", "extract", "shrink", "fuzz"] = "ithrl"
            if "extract" in arguments:
                command = "extract"
            elif "shrink" in arguments:
                command = "shrink"
            elif "fuzz" in arguments:
                command = "fuzz"
            report = ErrorOutput(
                command=command,
                exit_code=error.exit_code,
                error_code="invalid-command-line",
                message=str(error),
            )
            typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
            if standalone_mode:
                raise SystemExit(error.exit_code) from None
            raise Exit(error.exit_code) from None
        if standalone_mode and isinstance(result, int) and result != 0:
            raise SystemExit(result)
        return result


def _requests_json(arguments: Sequence[str]) -> bool:
    return any(
        argument == "--format=json"
        or (
            argument == "--format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
        )
        for index, argument in enumerate(arguments)
    )


def command_boundary(
    command: Literal["extract", "shrink"],
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Convert uncaught command failures to the public error format."""

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> None:
            output_format = kwargs.get("output_format", OutputFormat.HUMAN)
            if not isinstance(output_format, OutputFormat):
                output_format = OutputFormat.HUMAN
            try:
                function(*args, **kwargs)
            except typer.Exit:
                raise
            except typer.Abort:
                fail(
                    "Selection cancelled.",
                    ExitCode.CANCELLED,
                    output_format,
                    command,
                    "cancelled",
                )
            except OSError as error:
                fail(
                    f"filesystem operation failed: {error}",
                    ExitCode.INTERNAL,
                    output_format,
                    command,
                    "filesystem-error",
                )
            except Exception as error:
                fail(
                    f"unexpected internal error ({type(error).__name__})",
                    ExitCode.INTERNAL,
                    output_format,
                    command,
                    "internal-error",
                )

        return guarded

    return decorate


def render(report: CommandOutput, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return
    if report.command == "extract":
        _render_extract(report)
    else:
        _render_shrink(report)


def render_sampled(report: SampledOutput, output_format: OutputFormat) -> None:
    """Render a sampled result without implying unmeasured calls."""
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return

    print_plain(console, f"Sampled shrink: {report.status}")
    print_plain(console, f"  harness         {report.harness} {report.harness_version}")
    print_plain(console, f"  target          {report.target}")
    print_plain(console, f"  surface         {report.controlled_surface}")
    if report.decision_digest is not None:
        print_plain(console, f"  decision digest {report.decision_digest}")
    if report.requested_model is not None:
        print_plain(console, f"  requested model {report.requested_model}")
    if report.harness_started is not None:
        print_plain(console, f"  harness started {report.harness_started}")
    if report.supported_context_operations:
        print_plain(console, f"  context edits   {', '.join(report.supported_context_operations)}")
    print_plain(console, f"  containment     {report.process_containment}")
    print_plain(console, f"  protected state {report.protected_state_enforcement}")
    print_plain(console, f"  state bound     {report.disposable_state_limit_enforcement}")
    print_plain(
        console, f"  actions         {report.action_visibility}; denial {report.action_denial}"
    )
    print_plain(console, f"  plan digest     {report.plan_digest[:12]}")
    print_plain(console, f"  capability      {report.capability_digest[:12]}")
    print_plain(console, f"  safety policy   {report.safety_policy_digest[:12]}")
    if report.safety_effects:
        print_plain(console, f"  safety effects  {', '.join(report.safety_effects)}")
    for deviation in report.deviations:
        print_plain(console, f"  deviation       {deviation}", soft_wrap=True)
    if report.provider is not None:
        print_plain(console, f"  provider        {report.provider}")
    if report.endpoint is not None:
        print_plain(console, f"  endpoint        {report.endpoint}", soft_wrap=True)
    print_plain(console, f"  attempt bound   {report.max_attempts}")
    if report.required_valid is not None:
        print_plain(console, f"  valid samples   {report.required_valid}")
    if report.minimum_matches is not None:
        print_plain(console, f"  match threshold {report.minimum_matches}")
    print_plain(console, f"  call bound      {report.max_provider_calls}")
    print_plain(console, f"  calls / attempt {report.max_provider_calls_per_attempt}")
    print_plain(console, f"  runner bound    {report.max_runner_seconds:g}s")
    if report.max_wall_time_seconds is not None:
        print_plain(console, f"  command limit   {report.max_wall_time_seconds}s")
    print_plain(console, f"  attempt delay   {report.inter_attempt_delay_seconds}s")
    print_plain(
        console,
        f"  token bound     {report.max_tokens if report.max_tokens is not None else 'unknown'}",
    )
    print_plain(console, f"  token enforcement {report.input_token_limit_enforcement}")
    if report.attempt_timeout_seconds is not None:
        print_plain(console, f"  attempt timeout {report.attempt_timeout_seconds}s")
    if report.max_native_output_bytes is not None:
        print_plain(console, f"  output cap      {report.max_native_output_bytes} bytes")
    if report.max_disposable_state_bytes is not None:
        print_plain(console, f"  state cap       {report.max_disposable_state_bytes} bytes")
    if report.max_disposable_state_entries is not None:
        print_plain(console, f"  state entries   {report.max_disposable_state_entries}")
    if report.stop_reason is not None:
        print_plain(console, f"  stop reason     {report.stop_reason}")
    if report.matches is not None:
        print_plain(
            console, f"  samples         {report.matches} matches; {report.no_matches} no matches"
        )
    if report.invalid_observations is not None:
        print_plain(console, f"  invalid samples {report.invalid_observations}")
    if report.runner_errors is not None:
        print_plain(console, f"  runner errors   {report.runner_errors}")
    if report.runner_error_category is not None or report.runner_error_reason_code is not None:
        diagnostic = ": ".join(
            value
            for value in (report.runner_error_category, report.runner_error_reason_code)
            if value is not None
        )
        print_plain(console, f"  last runner error {diagnostic}")
    if report.actions_executed is not None:
        print_plain(console, f"  actions executed {report.actions_executed}")
    if report.attempts_used is not None:
        print_plain(console, f"  attempts used   {report.attempts_used}")
    if report.runner_seconds is not None:
        print_plain(console, f"  runner time     {report.runner_seconds:g}s")
    if report.wall_time_seconds is not None:
        print_plain(console, f"  command time    {report.wall_time_seconds:g}s")
    if report.provider_call_accounting == "not-started":
        print_plain(console, "  provider calls  0 (execution did not start)")
    elif report.provider_call_accounting == "accounted":
        print_plain(console, f"  provider calls  {report.provider_calls}")
    else:
        print_plain(console, "  provider calls  unknown (execution started)")
    if report.result_path is not None:
        print_plain(console, f"  result          {report.result_path}", soft_wrap=True)
    _render_next_step(report)


def render_sampled_error(report: SampledErrorOutput, output_format: OutputFormat) -> None:
    """Render safe refusal metadata and an explicit private diagnostic location."""
    if output_format is OutputFormat.JSON:
        exclude = {
            name
            for name in ("minimum_provider_calls", "minimum_wall_time_seconds")
            if getattr(report, name) is None
        }
        typer.echo(json.dumps(report.model_dump(mode="json", exclude=exclude), sort_keys=True))
        return
    message = {
        "refused": "Sampled run refused before calibration.",
        "incomplete": "Sampled run did not complete; provider-call accounting may be incomplete.",
        "interrupted": "Sampled run interrupted; provider-call accounting may be incomplete.",
    }[report.status]
    print_plain(error_console, f"{message} ({report.error_code})")
    if report.harness_started is not None:
        state = "started" if report.harness_started else "not started"
        print_plain(error_console, f"Local harness probe: {state}.")
    if report.provider_call_accounting == "not-started":
        print_plain(error_console, "Provider calls: 0 (model execution did not start).")
    else:
        print_plain(error_console, "Provider calls: unknown (execution may have started).")
    if report.next_action is not None:
        print_plain(error_console, report.next_action, soft_wrap=True)
    if report.minimum_provider_calls is not None:
        print_plain(
            error_console,
            f"Required minimum provider-call budget: {report.minimum_provider_calls}.",
        )
    if report.minimum_wall_time_seconds is not None:
        print_plain(
            error_console,
            f"Required minimum runner-time allowance: {report.minimum_wall_time_seconds} seconds; local overhead is additional.",
        )
    if report.probe_stage is not None:
        print_plain(error_console, f"Failing probe stage: {report.probe_stage}.")
    if report.probe_exit_code is not None:
        print_plain(error_console, f"Probe exit code: {report.probe_exit_code}.")
    if report.probe_reason is not None:
        print_plain(error_console, report.probe_reason)
    if report.diagnostic_path is not None:
        print_plain(
            error_console, f"Private probe diagnostic: {report.diagnostic_path}", soft_wrap=True
        )


def fail_sampled(
    *,
    error_code: str,
    code: ExitCode,
    output_format: OutputFormat,
    status: Literal["refused", "incomplete", "interrupted"],
    harness_started: bool | None = None,
    provider_call_accounting: Literal["not-started", "started-unknown"] = "started-unknown",
    probe_stage: str | None = None,
    probe_exit_code: int | None = None,
    probe_reason: str | None = None,
    diagnostic_path: str | None = None,
    minimum_provider_calls: int | None = None,
    minimum_wall_time_seconds: int | None = None,
) -> Never:
    """Emit a content-free sampled error and exit with its public code."""
    try:
        render_sampled_error(
            SampledErrorOutput(
                status=status,
                exit_code=code,
                error_code=error_code,
                harness_started=harness_started,
                provider_call_accounting=provider_call_accounting,
                provider_calls=0 if provider_call_accounting == "not-started" else None,
                next_action=_sampled_error_next_action(error_code),
                probe_stage=probe_stage,
                probe_exit_code=probe_exit_code,
                probe_reason=probe_reason,
                diagnostic_path=diagnostic_path,
                minimum_provider_calls=minimum_provider_calls,
                minimum_wall_time_seconds=minimum_wall_time_seconds,
            ),
            output_format,
        )
    except (OSError, KeyboardInterrupt):
        # A closed output stream must not reach the deterministic command boundary,
        # which would incorrectly assert that no provider calls were made.
        raise typer.Exit(code) from None
    raise typer.Exit(code)


def _sampled_error_next_action(error_code: str) -> str:
    """Give stable guidance without echoing private diagnostics."""

    if error_code == "sampled-reduction-plan-required":
        return "Supply --experiment with a complete sampled reduction TOML plan, then inspect --dry-run."
    if error_code in {"sampling-budget-required", "sampling-budget-too-small"}:
        return "Set --max-provider-calls and --max-wall-time to cover calibration, one complete comparison and fresh confirmation; allow time for local overhead."
    if error_code == "sampled-resume-mismatch":
        return "Resume the saved run with its original case, workspace, observation store and statistical plan; only administrative budgets may increase."
    if error_code == "sampling-freshness-too-small":
        return "Choose attempt deadlines and a declared freshness window that admit a complete comparison, including cancellation time, before expiry."
    if error_code == "confirmation-unattainable":
        return "Revise the confirmation sample plan: even all matching samples cannot establish preservation at the configured margin and confidence."

    if error_code in {"acknowledgement-required", "acknowledgement-declined"}:
        return "Inspect the dry-run plan, then add --acknowledge-model-execution to the selected execution mode when ready."
    if error_code in {"original-workspace-required", "original-workspace-unavailable"}:
        return "Supply --original-workspace with the existing original workspace directory."
    if error_code in {"sampling-options-required", "calibration-settings-required"}:
        return (
            "Supply --required-valid, --minimum-matches, and --max-attempts for native calibration."
        )
    if error_code == "runtime-unavailable":
        return "Make the selected harness executable and the required isolation runtime available, then rerun --dry-run."
    if error_code in {"codex-authentication-unavailable", "copilot-authentication-unavailable"}:
        return "Configure local harness authentication without adding credentials to the case, then rerun --dry-run."
    if error_code in {"codex-probe-failed", "copilot-probe-failed"}:
        return "Check the local harness version and isolation runtime, then rerun --dry-run."
    if error_code == "input-token-limit-unenforceable":
        return "This profile cannot enforce --max-input-tokens. Choose a profile that can, or explicitly omit that cap."
    if error_code in {"provider-call-limit-too-small", "provider-call-budget-too-small"}:
        return (
            "Use a call budget that covers the displayed attempt bound, or reduce --max-attempts."
        )
    if error_code in {"capability-profile-mismatch", "runner-evidence-mismatch"}:
        return "Use a harness profile compatible with this case and rerun --dry-run before calibration."
    if error_code in {
        "calibration-incomplete",
        "calibration-interrupted",
        "calibration-accounting-invalid",
    }:
        return "Inspect private calibration evidence and unresolved attempt state before any further provider execution."
    if error_code == "sampled-search-unavailable":
        return "Use --dry-run or --calibrate-only; sampled context search is not available yet."
    if error_code == "observation-store-unavailable":
        return "Supply --observation-store with a writable private location, then rerun --dry-run."
    return "Resolve the reported preflight refusal and rerun --dry-run before calibration."


def _render_extract(report: CommandOutput) -> None:
    print_plain(console, "Case created")
    print_plain(console, f"  path            {report.case_path}", soft_wrap=True)
    print_plain(console, f"  case digest     {report.case_digest[:12]}")
    _render_capture_details(report)
    _render_decision_details(report)
    print_plain(console, f"  context items   {report.original_context_count}")
    print_plain(console, "  No network requests or provider calls. No actions executed.")
    _render_next_step(report)


def _render_capture_details(report: CommandOutput) -> None:
    if report.harness is not None:
        version = f" {report.harness_version}" if report.harness_version is not None else ""
        print_plain(console, f"  harness         {report.harness}{version}")
    if report.session_id is not None:
        print_plain(console, f"  session         {report.session_id}", soft_wrap=True)
    if report.capture_type is not None:
        print_plain(console, f"  capture         {report.capture_type}")
    if report.capture_completeness is not None:
        print_plain(console, f"  completeness    {report.capture_completeness}")
    if report.replay_state is not None:
        print_plain(console, f"  replay state    {report.replay_state}")
    if report.replay_environment is not None:
        print_plain(console, f"  environment     {report.replay_environment}")


def _render_decision_details(report: CommandOutput) -> None:
    if report.decision_id is not None:
        print_plain(console, f"  decision        {report.decision_id}")
    if report.target_type is not None:
        target = report.target_type
        if report.target_id is not None:
            target = f"{report.target_id} ({target})"
        print_plain(console, f"  target          {target}")
    if report.evidence_support is not None:
        print_plain(console, f"  evidence        {report.evidence_support}")
    if report.experimental_support is not None:
        print_plain(console, f"  experiment      {report.experimental_support}")
    if report.limitations:
        limitations = ", ".join(
            _LIMITATION_LABELS.get(limitation, limitation) for limitation in report.limitations
        )
        print_plain(console, f"  limitations     {limitations}")
    if report.runner_type == "unavailable":
        print_plain(console, "  controlled run  unavailable")


def _render_shrink(report: CommandOutput) -> None:
    print_plain(console, _SHRINK_HEADINGS.get(report.status, report.status))
    print_plain(console, f"  case            {report.case_path}", soft_wrap=True)
    print_plain(console, f"  case digest     {report.case_digest[:12]}")
    _render_execution_details(report)
    _render_phase_results(report)
    print_plain(console, f"  candidates      {report.candidate_runs_used}")
    if report.max_candidate_runs is not None:
        print_plain(console, f"  candidate limit {report.max_candidate_runs}")
    print_plain(console, f"  observations    {report.observations_recorded}")
    print_plain(console, "  No network requests or provider calls. No actions executed.")
    if report.result_path is not None:
        print_plain(console, f"  result          {report.result_path}", soft_wrap=True)
    if report.run_state_path is not None:
        print_plain(console, f"  run state       {report.run_state_path}", soft_wrap=True)
    _render_next_step(report)


def _render_execution_details(report: CommandOutput) -> None:
    if report.runner_type is not None:
        print_plain(console, f"  runner          {report.runner_type} {report.runner_version}")
    if report.target_type is not None:
        print_plain(console, f"  target          {report.target_type}")
    if report.execution_plan_digest is not None:
        print_plain(console, f"  plan digest     {report.execution_plan_digest[:12]}")
    protected_count = len(report.protected_context_ids)
    protected_label = "context item" if protected_count == 1 else "context items"
    print_plain(console, f"  protected       {protected_count} {protected_label}")
    if report.original_context_count is not None:
        print_plain(
            console,
            f"  context items   {report.original_context_count} -> {report.retained_context_count}",
        )


def _render_phase_results(report: CommandOutput) -> None:
    phases = (
        ("original test", report.calibration_result, _CALIBRATION_LABELS),
        ("search", report.search_result, _SEARCH_LABELS),
        ("confirmation", report.confirmation_result, _CONFIRMATION_LABELS),
    )
    for name, result, labels in phases:
        if result is None:
            continue
        print_plain(
            console,
            f"  {name:<15} {labels.get(result, result)}",
        )


def _render_next_step(report: CommandOutput | SampledOutput) -> None:
    if report.next_action is not None or report.next_command is not None:
        console.print()
        print_plain(console, "Next step")
        if report.next_action is not None:
            print_plain(console, f"  {report.next_action}")
        if report.next_command is not None:
            print_plain(
                console,
                f"  Command ({report.next_command_shell}): {report.next_command}",
                soft_wrap=True,
            )


def shell_command(arguments: tuple[str, ...]) -> tuple[str, Literal["posix", "powershell"]]:
    if os.name == "nt":
        quoted = " ".join(f"'{argument.replace("'", "''")}'" for argument in arguments)
        return f"& {quoted}", "powershell"
    return shlex.join(arguments), "posix"


def fail(
    message: str,
    code: ExitCode,
    output_format: OutputFormat,
    command: Literal["extract", "shrink"],
    error_code: str,
) -> Never:
    if output_format is OutputFormat.JSON:
        report = ErrorOutput(
            command=command,
            exit_code=code,
            error_code=error_code,
            message=message,
        )
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    else:
        print_plain(error_console, message)
        print_plain(
            error_console,
            "No network requests or provider calls. No actions executed.",
        )
    raise typer.Exit(code)


def print_plain(target: Console, value: object, *, soft_wrap: bool = False) -> None:
    """Print data without Rich markup or terminal control characters."""

    safe = "".join(
        character if character.isprintable() else f"\\u{ord(character):04x}"
        for character in str(value)
    )
    target.print(Text(safe), soft_wrap=soft_wrap)
