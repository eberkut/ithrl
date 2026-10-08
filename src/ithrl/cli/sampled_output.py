"""Content-free presentation of strict sampled search and fresh confirmation."""

from __future__ import annotations

import json
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.cli.output import (
    ExitCode,
    OutputFormat,
    _validate_provider_accounting,
    console,
    print_plain,
)
from ithrl.observations import SamplingArmCounts

type ReductionStatus = Literal[
    "ready",
    "running",
    "confirmed",
    "confirmation-failed",
    "inconclusive",
    "incomplete",
    "refused",
    "interrupted",
]
type ConfirmationResult = Literal[
    "not-run", "confirmed-preserving", "loses", "inconclusive", "incomplete", "refused"
]
type Assumption = Literal[
    "independent-valid-observations",
    "fixed-complete-samples",
    "fresh-original-baselines",
    "fresh-independent-confirmation",
    "stationary-within-comparison",
    "valid-only-does-not-estimate-execution-reliability",
    "no-campaign-wide-error-guarantee",
]

_ASSUMPTIONS: tuple[Assumption, ...] = (
    "independent-valid-observations",
    "fixed-complete-samples",
    "fresh-original-baselines",
    "fresh-independent-confirmation",
    "stationary-within-comparison",
    "valid-only-does-not-estimate-execution-reliability",
    "no-campaign-wide-error-guarantee",
)


class SampledReductionOutput(BaseModel):
    """Safe public summary; observations, context IDs and native content stay private.

    Execution metadata uses the sampled preflight constraints. Reduction status and
    stop reasons have their own contract, separate from calibration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["sampled"] = "sampled"
    command: Literal["shrink"] = "shrink"
    status: ReductionStatus
    phase: Literal["calibration", "search", "confirmation", "finished"]
    harness_started: bool | None = None
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    harness: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    harness_version: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._+-]+$")
    target: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_model: str | None = Field(
        default=None, max_length=256, pattern=r"^[A-Za-z0-9._:/+-]+$"
    )
    action_visibility: Literal["not-applicable", "visible", "unknown"] = "unknown"
    action_denial: Literal["not-applicable", "enforced", "unverified"] = "unverified"
    safety_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    max_provider_calls: int = Field(ge=0)
    max_runner_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_wall_time_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_candidate_comparisons: int = Field(ge=0)
    candidate_capacity: int = Field(ge=0)
    minimum_provider_calls: int = Field(ge=0)
    minimum_wall_time_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=0)
    attempt_timeout_seconds: int | None = Field(default=None, ge=1)
    max_native_output_bytes: int | None = Field(default=None, ge=1)
    max_disposable_state_bytes: int | None = Field(default=None, ge=0)
    max_disposable_state_entries: int | None = Field(default=None, ge=0)
    search_original_required_valid: int = Field(ge=1)
    search_original_max_attempts: int = Field(ge=1)
    search_candidate_required_valid: int = Field(ge=1)
    search_candidate_max_attempts: int = Field(ge=1)
    confirmation_original_required_valid: int = Field(ge=1)
    confirmation_original_max_attempts: int = Field(ge=1)
    confirmation_candidate_required_valid: int = Field(ge=1)
    confirmation_candidate_max_attempts: int = Field(ge=1)
    confirmation_provider_calls: int = Field(ge=0)
    confirmation_runner_seconds: float = Field(ge=0, allow_inf_nan=False)
    confirmation_capacity: Literal["protected", "in-use", "consumed", "unavailable"]
    freshness_seconds: int | None = Field(default=None, ge=1)
    baseline_policy: Literal["strict-fresh-original"] = "strict-fresh-original"
    estimand: Literal["valid-only"] = "valid-only"
    confirmation_method: Literal["newcombe-method-10"] = "newcombe-method-10"
    margin: float = Field(ge=0, lt=1, allow_inf_nan=False)
    one_sided_confidence: float = Field(gt=0.5, lt=1, allow_inf_nan=False)
    assumptions: tuple[Assumption, ...] = _ASSUMPTIONS
    calibration_result: Literal[
        "not-run", "measurable", "not-measurable", "inconclusive", "incomplete", "refused"
    ] = "not-run"
    search_result: Literal["not-run", "search-reduced", "search-incomplete"] = "not-run"
    confirmation_result: ConfirmationResult = "not-run"
    confirmation_original: SamplingArmCounts | None = None
    confirmation_candidate: SamplingArmCounts | None = None
    confirmation_difference: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    confirmation_lower_bound: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    confirmation_upper_bound: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    evidence_eligibility: Literal["not-assessed", "eligible", "refused"] = "not-assessed"
    reliability_status: Literal["not-started", "complete", "incomplete", "unknown"] = "not-started"
    original_context_count: int = Field(ge=0)
    retained_context_count: int = Field(ge=0)
    removed_context_count: int = Field(ge=0)
    candidate_comparisons_used: int = Field(default=0, ge=0)
    provider_call_accounting: Literal["not-started", "started-unknown", "accounted"]
    provider_calls: int | None = Field(default=None, ge=0)
    actual_provider_calls: int = Field(default=0, ge=0)
    possible_provider_calls: int = Field(default=0, ge=0)
    pending_provider_calls: int = Field(default=0, ge=0)
    charged_provider_calls: int = Field(default=0, ge=0)
    unknown_provider_attempts: int = Field(default=0, ge=0)
    attempts_used: int = Field(default=0, ge=0)
    runner_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unknown_runner_attempts: int = Field(default=0, ge=0)
    pending_runner_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    wall_time_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    known_tokens: int = Field(default=0, ge=0)
    unknown_token_attempts: int = Field(default=0, ge=0)
    matches: int = Field(default=0, ge=0)
    no_matches: int = Field(default=0, ge=0)
    invalid_observations: int = Field(default=0, ge=0)
    runner_errors: int = Field(default=0, ge=0)
    actions_executed: Literal[0] = 0
    stop_reason: Annotated[str | None, Field(max_length=80, pattern=r"^[a-z][a-z0-9-]*$")] = None
    result_path: str | None = Field(default=None, max_length=240)
    run_state_path: str | None = Field(default=None, max_length=240)
    next_action: str | None = Field(default=None, max_length=240)
    next_command: str | None = Field(default=None, max_length=2048)
    next_command_shell: Literal["posix", "powershell"] | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> SampledReductionOutput:
        _validate_provider_accounting(self.provider_call_accounting, self.provider_calls)
        if self.attempts_used > self.max_attempts:
            raise ValueError("attempt count exceeds the run bound")
        for required, maximum in (
            (self.search_original_required_valid, self.search_original_max_attempts),
            (self.search_candidate_required_valid, self.search_candidate_max_attempts),
            (self.confirmation_original_required_valid, self.confirmation_original_max_attempts),
            (self.confirmation_candidate_required_valid, self.confirmation_candidate_max_attempts),
        ):
            if required > maximum:
                raise ValueError("fixed valid target exceeds its attempt bound")
        if self.retained_context_count + self.removed_context_count != self.original_context_count:
            raise ValueError("context counts must partition the original context")
        if self.charged_provider_calls != (
            self.actual_provider_calls + self.possible_provider_calls + self.pending_provider_calls
        ):
            raise ValueError("charged calls must include known, possible and pending calls")
        if self.charged_provider_calls > self.max_provider_calls:
            raise ValueError("charged provider calls exceed the run bound")
        if self.provider_call_accounting == "accounted" and (
            self.provider_calls != self.actual_provider_calls
            or self.possible_provider_calls
            or self.pending_provider_calls
            or self.unknown_provider_attempts
        ):
            raise ValueError("complete accounting cannot contain unresolved provider usage")
        if self.provider_call_accounting == "not-started" and (
            self.charged_provider_calls or self.attempts_used
        ):
            raise ValueError("an unstarted run cannot charge provider calls")
        if self.status == "ready" and self.provider_call_accounting != "not-started":
            raise ValueError("ready output cannot report started execution")
        if self.confirmation_provider_calls > self.max_provider_calls:
            raise ValueError("confirmation call budget exceeds the run bound")
        if self.confirmation_runner_seconds > self.max_wall_time_seconds:
            raise ValueError("confirmation time budget exceeds the run bound")
        if self.status == "confirmed" and (
            self.confirmation_result != "confirmed-preserving"
            or self.evidence_eligibility != "eligible"
        ):
            raise ValueError("confirmation success requires eligible fresh confirmation")
        if self.status == "confirmation-failed" and self.confirmation_result != "loses":
            raise ValueError("a behavioral loss requires a losing confirmation")
        if (self.confirmation_original is None) != (self.confirmation_candidate is None):
            raise ValueError("fresh confirmation counts require both arms")
        interval = (self.confirmation_lower_bound, self.confirmation_upper_bound)
        if (interval[0] is None) != (interval[1] is None):
            raise ValueError("fresh confirmation bounds must be supplied together")
        if interval[0] is not None and interval[1] is not None:
            if self.confirmation_difference is None or interval[0] > interval[1]:
                raise ValueError("fresh confirmation requires an ordered difference interval")
            if self.confirmation_original is None or self.confirmation_candidate is None:
                raise ValueError("fresh confirmation interval requires actual arm counts")
            if (
                self.confirmation_original.valid != self.confirmation_original_required_valid
                or self.confirmation_candidate.valid != self.confirmation_candidate_required_valid
                or self.evidence_eligibility != "eligible"
            ):
                raise ValueError("fresh confirmation interval requires complete eligible samples")
        if (self.next_command is None) != (self.next_command_shell is None):
            raise ValueError("continuation command and shell must be supplied together")
        for value in (self.result_path, self.run_state_path, self.next_action, self.next_command):
            if value is not None and not value.isprintable():
                raise ValueError("public paths and continuation must be printable")
        return self


def sampled_reduction_exit_code(report: SampledReductionOutput) -> ExitCode:
    """Map operational and statistical outcomes without treating errors as losses."""
    if report.status in {"ready", "confirmed"}:
        return ExitCode.SUCCESS
    if report.status == "interrupted":
        return ExitCode.CANCELLED
    if report.status == "refused":
        return ExitCode.UNSUPPORTED
    if report.stop_reason in {
        "candidate-limit",
        "trial-limit",
        "provider-call-budget",
        "wall-time-budget",
        "wall-time-exhausted",
        "freshness-budget",
        "attempt-limit",
    }:
        return ExitCode.BUDGET
    return ExitCode.INCONCLUSIVE


def render_sampled_reduction(
    report: SampledReductionOutput, output_format: OutputFormat, *, verbose: bool = False
) -> None:
    """Emit one JSON object or the same safe summary as plain human-readable text."""
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return
    print_plain(console, f"Sampled shrink: {report.status}")
    calls = report.provider_calls if report.provider_calls is not None else "unknown"
    core: tuple[tuple[str, object], ...] = (
        ("phase", report.phase),
        ("calibration", report.calibration_result),
        ("search", report.search_result),
        ("confirmation", report.confirmation_result),
        (
            "context",
            f"{report.retained_context_count} kept; {report.removed_context_count} removed; {report.original_context_count} original",
        ),
        (
            "provider / model",
            f"{report.provider or 'unknown'} / {report.requested_model or 'unknown'}",
        ),
        ("destination", report.endpoint),
        (
            "action policy",
            f"visibility {report.action_visibility}; denial {report.action_denial}; {report.actions_executed} executed",
        ),
        ("safety", ", ".join(report.safety_effects)),
        (
            "provider calls",
            f"{calls} actual; {report.charged_provider_calls} charged / {report.max_provider_calls} maximum",
        ),
        (
            "attempts",
            f"{report.attempts_used} used / {report.max_attempts} maximum; {report.attempt_timeout_seconds or 'unknown'}s per attempt",
        ),
        (
            "execution outcomes",
            f"{report.matches} matches; {report.no_matches} no matches; {report.invalid_observations} invalid; {report.runner_errors} runner errors",
        ),
        (
            "tokens",
            f"{report.known_tokens} known; {report.unknown_token_attempts} attempts with unknown usage",
        ),
        (
            "native output / disposable state limits",
            f"{report.max_native_output_bytes if report.max_native_output_bytes is not None else 'unknown'} bytes / "
            f"{report.max_disposable_state_bytes if report.max_disposable_state_bytes is not None else 'unknown'} bytes; "
            f"{report.max_disposable_state_entries if report.max_disposable_state_entries is not None else 'unknown'} entries",
        ),
        (
            "unknown call accounting",
            f"{report.unknown_provider_attempts} attempts; {report.possible_provider_calls} possible additional calls; {report.pending_provider_calls} pending",
        ),
        (
            "confirmation budget",
            f"{report.confirmation_provider_calls} calls; {report.confirmation_runner_seconds:g}s; {report.confirmation_capacity}",
        ),
        (
            "elapsed / wall limit",
            f"{report.wall_time_seconds:g}s / {report.max_wall_time_seconds:g}s",
        ),
        (
            "candidate capacity",
            f"{report.candidate_capacity}; {report.candidate_comparisons_used} comparisons used",
        ),
        (
            "minimum budget",
            f"{report.minimum_provider_calls} calls; {report.minimum_wall_time_seconds:g}s",
        ),
        (
            "inference",
            f"{report.estimand}; margin {report.margin:g}; nominal one-sided confidence {report.one_sided_confidence:g}",
        ),
        ("baseline policy", report.baseline_policy),
        (
            "eligibility / reliability",
            f"{report.evidence_eligibility} / {report.reliability_status}",
        ),
    )
    for label, value in core:
        print_plain(
            console, f"  {label}: {value if value is not None else 'unknown'}", soft_wrap=True
        )
    for label, counts in (
        ("fresh confirmation original", report.confirmation_original),
        ("fresh confirmation candidate", report.confirmation_candidate),
    ):
        if counts is not None:
            print_plain(
                console,
                f"  {label}: {counts.valid} valid ({counts.matches} matches, {counts.no_matches} no matches); "
                f"{counts.invalid_observations} invalid; {counts.runner_errors} runner errors",
                soft_wrap=True,
            )
    if report.confirmation_difference is not None:
        print_plain(
            console,
            f"  fresh confirmation difference (candidate - original): {report.confirmation_difference:g}",
        )
    if report.confirmation_lower_bound is not None:
        print_plain(
            console,
            f"  fresh confirmation interval: [{report.confirmation_lower_bound:g}, {report.confirmation_upper_bound:g}]",
        )
    print_plain(
        console,
        "  Valid-only preservation excludes errors from behavioral counts; execution reliability is reported separately.",
        soft_wrap=True,
    )
    print_plain(
        console,
        "  Confidence assumes independent valid observations and stationarity within comparisons; no campaign-wide error guarantee.",
        soft_wrap=True,
    )
    if verbose:
        values: tuple[tuple[str, object], ...] = (
            ("phase", report.phase),
            ("harness", f"{report.harness} {report.harness_version}"),
            ("target", report.target),
            ("requested model", report.requested_model),
            ("provider", report.provider),
            ("endpoint", report.endpoint),
            ("harness started", report.harness_started),
            ("plan digest", report.plan_digest),
            ("capability", report.capability_digest),
            ("safety policy", report.safety_policy_digest),
            ("action visibility", report.action_visibility),
            ("action denial", report.action_denial),
            ("safety effects", ", ".join(report.safety_effects)),
            ("calibration", report.calibration_result),
            ("search", report.search_result),
            ("confirmation", report.confirmation_result),
            ("eligibility", report.evidence_eligibility),
            ("reliability", report.reliability_status),
            ("baseline policy", report.baseline_policy),
            ("estimand", report.estimand),
            ("confirmation method", report.confirmation_method),
            ("margin", report.margin),
            ("nominal one-sided confidence", report.one_sided_confidence),
            ("original context", report.original_context_count),
            ("kept context", report.retained_context_count),
            ("removed context", report.removed_context_count),
            ("comparisons used", report.candidate_comparisons_used),
            ("comparison limit", report.max_candidate_comparisons),
            ("attempt limit", report.max_attempts),
            ("maximum provider calls", report.max_provider_calls),
            ("maximum runner seconds", report.max_runner_seconds),
            ("maximum wall seconds", report.max_wall_time_seconds),
            ("freshness seconds", report.freshness_seconds),
            ("maximum tokens", report.max_tokens),
            ("attempt timeout seconds", report.attempt_timeout_seconds),
            ("maximum output bytes", report.max_native_output_bytes),
            ("maximum state bytes", report.max_disposable_state_bytes),
            ("maximum state entries", report.max_disposable_state_entries),
            ("search original valid target", report.search_original_required_valid),
            ("search original attempt limit", report.search_original_max_attempts),
            ("search candidate valid target", report.search_candidate_required_valid),
            ("search candidate attempt limit", report.search_candidate_max_attempts),
            ("confirmation original valid target", report.confirmation_original_required_valid),
            ("confirmation original attempt limit", report.confirmation_original_max_attempts),
            ("confirmation candidate valid target", report.confirmation_candidate_required_valid),
            ("confirmation candidate attempt limit", report.confirmation_candidate_max_attempts),
            ("confirmation call budget", report.confirmation_provider_calls),
            ("confirmation time budget", report.confirmation_runner_seconds),
            ("confirmation capacity", report.confirmation_capacity),
            ("provider accounting", report.provider_call_accounting),
            (
                "actual provider calls",
                report.provider_calls if report.provider_calls is not None else "unknown",
            ),
            ("known call lower bound", report.actual_provider_calls),
            ("possible additional calls", report.possible_provider_calls),
            ("pending calls", report.pending_provider_calls),
            ("charged calls", report.charged_provider_calls),
            ("unknown provider attempts", report.unknown_provider_attempts),
            ("attempts used", report.attempts_used),
            ("known runner seconds", report.runner_seconds),
            ("unknown runner attempts", report.unknown_runner_attempts),
            ("pending runner seconds", report.pending_runner_seconds),
            ("wall seconds", report.wall_time_seconds),
            ("known tokens", report.known_tokens),
            ("unknown token attempts", report.unknown_token_attempts),
            ("matches", report.matches),
            ("no matches", report.no_matches),
            ("invalid observations", report.invalid_observations),
            ("runner errors", report.runner_errors),
            ("actions executed", report.actions_executed),
            ("stop reason", report.stop_reason),
        )
        for label, value in values:
            print_plain(
                console, f"  {label}: {value if value is not None else 'unknown'}", soft_wrap=True
            )
        for assumption in report.assumptions:
            print_plain(console, f"  assumption: {assumption}", soft_wrap=True)
    for label, value in (
        ("stop reason", report.stop_reason),
        ("private result", report.result_path),
        ("private run state", report.run_state_path),
    ):
        if value is not None:
            print_plain(console, f"  {label}: {value}", soft_wrap=True)
    if report.next_action is not None:
        print_plain(console, f"Next action: {report.next_action}", soft_wrap=True)
    if report.next_command is not None:
        print_plain(
            console, f"Command ({report.next_command_shell}): {report.next_command}", soft_wrap=True
        )
