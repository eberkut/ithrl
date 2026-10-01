"""Public command-line interface."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from platformdirs import user_data_path

from ithrl.adapters import AdapterError, AdapterErrorCode
from ithrl.case import (
    CaseError,
    CaseEvidence,
    CaseExistsError,
    CaseFormatError,
    CaseWorkspace,
    UnsafeCasePathError,
)
from ithrl.cli.extract import ExtractionSelectionError, create_case_report
from ithrl.cli.native import NativeSelection, NativeSelectionError, native_selection
from ithrl.cli.output import (
    CommandOutput,
    ExitCode,
    JsonAwareGroup,
    OutputFormat,
    command_boundary,
    error_console,
    fail,
    fail_sampled,
    print_plain,
    render,
    shell_command,
)
from ithrl.cli.paths import private_data_root
from ithrl.cli.sampled import handle_sampled_shrink
from ithrl.cli.shrink import prepare_shrink, save_outcome, validate_phase_observations
from ithrl.execution.contracts import MAX_PROVIDER_CALLS_PER_ATTEMPT, MAX_WALL_TIME_SECONDS
from ithrl.experiment import (
    MAX_CALIBRATION_ATTEMPTS,
    ExperimentFormatError,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
)
from ithrl.observations import ObservationStore, ObservationStoreError
from ithrl.reduce import (
    MAX_CANDIDATE_RUNS,
    DeterministicReducer,
    ReductionCheckpoint,
    ResumeMismatchError,
)
from ithrl.results import ResultSnapshot, ResultSnapshotError
from ithrl.run_state import RunStateBusyError, RunStateError, RunStateFile
from ithrl.synthetic import (
    InferenceSelectionError,
    SyntheticTraceError,
)
from ithrl.workflow import (
    OraclePrerequisiteError,
    PreflightRefusedError,
    PreparedExperiment,
    run_phases,
)

app = typer.Typer(
    cls=JsonAwareGroup,
    add_completion=True,
    help="Test how agent context affects a model decision.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _version_option(value: bool) -> None:
    """Print the version before command validation."""

    if value:
        typer.echo(f"ithrl {distribution_version('ithrl')}")
        raise typer.Exit()


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_option,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Test how agent context affects a model decision."""


@dataclass(frozen=True, slots=True)
class _ObservationSession:
    store: ObservationStore
    path: Path
    continuation_path: Path | None


@app.command(
    help="Create a case from a saved agent session or supported synthetic trace.",
    epilog=(
        "Saved session: ithrl extract --harness codex --session SESSION --decision DECISION "
        "--target TARGET\nSynthetic trace: ithrl extract trace.json --inference inference-001"
    ),
)
@command_boundary("extract")
def extract(
    source: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            readable=True,
            metavar="SOURCE",
            help="Saved native session or supported synthetic trace. Omit to discover sessions.",
        ),
    ] = None,
    inference: Annotated[
        str | None,
        typer.Option("--inference", help="Synthetic trace inference ID."),
    ] = None,
    harness: Annotated[
        str | None,
        typer.Option("--harness", metavar="codex|copilot", help="Native session harness."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Native session ID."),
    ] = None,
    decision: Annotated[
        str | None,
        typer.Option("--decision", help="Completed decision ID."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Target ID or unique target type."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Path for the new case. Existing paths are not overwritten.",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.HUMAN,
) -> None:
    """Extract one decision without making a network request."""

    try:
        report = create_case_report(
            source=source,
            inference=inference,
            harness=harness,
            session_id=session,
            decision_id=decision,
            target=target,
            output=output,
            output_format=output_format,
        )
    except ExtractionSelectionError as error:
        fail(str(error), ExitCode.USAGE, output_format, "extract", error.code)
    except AdapterError as error:
        exit_code = (
            ExitCode.UNSUPPORTED
            if error.code
            in {
                AdapterErrorCode.NO_SESSIONS,
                AdapterErrorCode.UNSUPPORTED_FORMAT,
                AdapterErrorCode.UNSUPPORTED_VERSION,
            }
            else ExitCode.USAGE
        )
        fail(str(error), exit_code, output_format, "extract", error.code.value)
    except CaseExistsError as error:
        fail(str(error), ExitCode.USAGE, output_format, "extract", "case-exists")
    except UnsafeCasePathError as error:
        fail(str(error), ExitCode.USAGE, output_format, "extract", "unsafe-case-path")
    except CaseFormatError as error:
        fail(str(error), ExitCode.USAGE, output_format, "extract", "invalid-case")
    except (CaseError, SyntheticTraceError, InferenceSelectionError) as error:
        fail(
            str(error),
            ExitCode.USAGE,
            output_format,
            "extract",
            "invalid-extraction",
        )
    render(report, output_format)


@app.command(
    help="Find a smaller context that preserves the selected behavior.",
    epilog=("Example: ithrl shrink .ithrl/case\nResume:  ithrl shrink RESULT_DIRECTORY --resume"),
)
@command_boundary("shrink")
def shrink(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            readable=True,
            metavar="CASE_OR_RESULT",
            help="Case directory. With --resume, use a result or active run directory.",
        ),
    ],
    max_candidates: Annotated[
        int | None,
        typer.Option(
            "--max-candidates",
            min=1,
            max=MAX_CANDIDATE_RUNS,
            help=(
                "Maximum candidate tests across the active run and any resumed runs. "
                "Default: 100 for a new search; 100 or twice the saved count, whichever is "
                f"larger, capped at {MAX_CANDIDATE_RUNS:,}, when resuming."
            ),
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume a saved search."),
    ] = False,
    calibrate_only: Annotated[
        bool,
        typer.Option(
            "--calibrate-only",
            help="Test and save the original context without searching for a smaller one.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Check the case and execution plan without running the experiment.",
        ),
    ] = False,
    acknowledge_model_execution: Annotated[
        bool,
        typer.Option(
            "--acknowledge-model-execution",
            help="Allow the selected native runner to send case context to its provider.",
        ),
    ] = False,
    original_workspace: Annotated[
        Path | None,
        typer.Option(
            "--original-workspace",
            metavar="DIRECTORY",
            help="Original workspace to protect during native calibration.",
        ),
    ] = None,
    required_valid: Annotated[
        int | None,
        typer.Option(
            "--required-valid",
            min=1,
            max=MAX_CALIBRATION_ATTEMPTS,
            help="Number of valid baseline samples required for native calibration.",
        ),
    ] = None,
    minimum_matches: Annotated[
        int | None,
        typer.Option(
            "--minimum-matches",
            min=1,
            max=MAX_CALIBRATION_ATTEMPTS,
            help="Required matching baseline samples. Set with both other sample limits.",
        ),
    ] = None,
    max_attempts: Annotated[
        int | None,
        typer.Option(
            "--max-attempts",
            min=1,
            max=MAX_CALIBRATION_ATTEMPTS,
            help="Total calibration attempt limit, including operational failures.",
        ),
    ] = None,
    max_provider_calls: Annotated[
        int | None,
        typer.Option(
            "--max-provider-calls",
            min=1,
            max=MAX_CALIBRATION_ATTEMPTS * MAX_PROVIDER_CALLS_PER_ATTEMPT,
            help="Maximum total provider calls reserved for native calibration.",
        ),
    ] = None,
    attempt_timeout: Annotated[
        int | None,
        typer.Option(
            "--attempt-timeout",
            min=1,
            max=MAX_WALL_TIME_SECONDS,
            help="Per-attempt wall-time deadline in seconds. Native default: 300.",
        ),
    ] = None,
    max_input_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-input-tokens",
            min=1,
            help="Hard per-attempt input-token cap; refused unless the runner can enforce it.",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.HUMAN,
    observation_store: Annotated[
        Path | None,
        typer.Option(
            "--observation-store",
            help="SQLite observation store. Default: platform user data directory.",
        ),
    ] = None,
) -> None:
    """Test the original context, reduce it, confirm the result, and save the evidence."""

    active_state = RunStateFile.open(source)
    active_lock_held = resume and active_state.path.is_file()
    lock = active_state.acquire_lock() if active_lock_held else nullcontext()
    try:
        with lock:
            _shrink(
                source=source,
                max_candidates=max_candidates,
                resume=resume,
                calibrate_only=calibrate_only,
                dry_run=dry_run,
                output_format=output_format,
                observation_store=observation_store,
                active_lock_held=active_lock_held,
                acknowledge_model_execution=acknowledge_model_execution,
                original_workspace=original_workspace,
                required_valid=required_valid,
                minimum_matches=minimum_matches,
                max_attempts=max_attempts,
                max_provider_calls=max_provider_calls,
                attempt_timeout=attempt_timeout,
                max_input_tokens=max_input_tokens,
            )
    except RunStateBusyError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "run-in-use")
    except RunStateError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "invalid-input")


def _shrink(
    *,
    source: Path,
    max_candidates: int | None,
    resume: bool,
    calibrate_only: bool,
    dry_run: bool,
    output_format: OutputFormat,
    observation_store: Path | None,
    active_lock_held: bool,
    acknowledge_model_execution: bool = False,
    original_workspace: Path | None = None,
    required_valid: int | None = None,
    minimum_matches: int | None = None,
    max_attempts: int | None = None,
    max_provider_calls: int | None = None,
    attempt_timeout: int | None = None,
    max_input_tokens: int | None = None,
) -> None:
    if resume and (calibrate_only or dry_run):
        fail(
            "--resume cannot be combined with --calibrate-only or --dry-run",
            ExitCode.USAGE,
            output_format,
            "shrink",
            "invalid-options",
        )
    native_attempted = False
    calibration: SampledCalibrationPhase | None = None
    counts = (required_valid, minimum_matches, max_attempts)
    if any(count is not None for count in counts):
        if required_valid is None or minimum_matches is None or max_attempts is None:
            fail_sampled(
                error_code="calibration-settings-required",
                code=ExitCode.USAGE,
                output_format=output_format,
                status="refused",
                harness_started=False,
                provider_call_accounting="not-started",
            )
        try:
            calibration = SampledCalibrationPhase(
                required_valid=required_valid,
                minimum_matches=minimum_matches,
                max_attempts=max_attempts,
            )
        except ValueError:
            fail_sampled(
                error_code="calibration-settings-invalid",
                code=ExitCode.USAGE,
                output_format=output_format,
                status="refused",
                harness_started=False,
                provider_call_accounting="not-started",
            )

    def select_native(workspace: CaseWorkspace, evidence: CaseEvidence) -> NativeSelection:
        nonlocal native_attempted
        native_attempted = True
        if resume or max_candidates is not None or not (dry_run or calibrate_only):
            raise NativeSelectionError(
                "sampled-search-unavailable", "native execution currently supports calibration only"
            )
        if dry_run and calibrate_only:
            raise NativeSelectionError("invalid-options", "choose dry-run or calibration-only")
        return native_selection(
            workspace,
            evidence,
            original_workspace=original_workspace,
            calibration=calibration,
            max_provider_calls=max_provider_calls,
            attempt_timeout_seconds=attempt_timeout,
            max_input_tokens=max_input_tokens,
        )

    try:
        (
            workspace,
            evidence,
            prepared,
            previous,
            checkpoint,
            run_state,
            candidate_limit,
        ) = prepare_shrink(
            source=source,
            resume=resume,
            max_candidates=max_candidates,
            native_selection_factory=select_native,
        )
    except NativeSelectionError as error:
        fail_sampled(
            error_code=error.code,
            code=ExitCode.UNSUPPORTED,
            output_format=output_format,
            status="refused",
            harness_started=error.harness_started,
            provider_call_accounting="not-started",
            probe_stage=error.probe_stage,
            probe_exit_code=error.probe_exit_code,
            probe_reason=error.probe_reason,
            diagnostic_path=str(error.diagnostic_path)
            if error.diagnostic_path is not None
            else None,
        )
    except (
        CaseError,
        ExperimentFormatError,
        ResultSnapshotError,
        ResumeMismatchError,
        RunStateError,
    ) as error:
        if native_attempted:
            fail_sampled(
                error_code="invalid-sampled-input",
                code=ExitCode.USAGE,
                output_format=output_format,
                status="refused",
                harness_started=True,
                provider_call_accounting="not-started",
            )
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "invalid-input")
    except (OraclePrerequisiteError, PreflightRefusedError) as error:
        if native_attempted:
            issue_code = (
                error.report.issues[0].code
                if isinstance(error, PreflightRefusedError) and error.report.issues
                else "oracle-prerequisite"
            )
            fail_sampled(
                error_code=issue_code,
                code=ExitCode.UNSUPPORTED,
                output_format=output_format,
                status="refused",
                harness_started=True,
                provider_call_accounting="not-started",
            )
        fail(str(error), ExitCode.UNSUPPORTED, output_format, "shrink", "unsupported")
    except KeyboardInterrupt:
        if native_attempted:
            fail_sampled(
                error_code="native-selection-interrupted",
                code=ExitCode.CANCELLED,
                output_format=output_format,
                status="interrupted",
                harness_started=None,
                provider_call_accounting="not-started",
            )
        raise
    except Exception:
        if native_attempted:
            fail_sampled(
                error_code="native-selection-internal",
                code=ExitCode.INTERNAL,
                output_format=output_format,
                status="refused",
                harness_started=None,
                provider_call_accounting="not-started",
            )
        raise

    if isinstance(prepared.experiment, SampledExperimentDefinition):
        if max_candidates is not None:
            fail_sampled(
                error_code="sampled-search-unavailable",
                code=ExitCode.USAGE,
                output_format=output_format,
                status="refused",
                harness_started=True,
                provider_call_accounting="not-started",
            )
        handle_sampled_shrink(
            workspace=workspace,
            evidence=evidence,
            prepared=prepared,
            dry_run=dry_run,
            calibrate_only=calibrate_only,
            acknowledge_model_execution=acknowledge_model_execution,
            original_workspace=original_workspace,
            observation_store=observation_store,
            output_format=output_format,
        )
        return

    if (
        acknowledge_model_execution
        or original_workspace is not None
        or calibration is not None
        or max_provider_calls is not None
        or attempt_timeout is not None
        or max_input_tokens is not None
    ):
        fail(
            "native calibration options require a native case",
            ExitCode.USAGE,
            output_format,
            "shrink",
            "invalid-options",
        )

    if dry_run:
        _render_dry_run(
            workspace=workspace,
            evidence=evidence,
            prepared=prepared,
            candidate_limit=candidate_limit,
            observation_store=observation_store,
            output_format=output_format,
        )
        return

    try:
        observations = _open_observation_session(
            requested_path=observation_store,
            run_state=run_state,
            previous=previous,
            prepared=prepared,
            output_format=output_format,
        )
    except CaseError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "unsafe-data-path")

    initial_checkpoint = checkpoint or ReductionCheckpoint(
        case_digest=prepared.identity.case_digest,
        experiment_digest=prepared.identity.experiment_digest,
        runner_fingerprint=prepared.identity.runner_fingerprint,
        execution_plan_digest=prepared.identity.execution_plan_digest,
        target_digest=prepared.identity.target_digest,
        removed_context_ids=(),
        trials=(),
        candidate_runs_used=0,
    )

    def persist_checkpoint(saved: ReductionCheckpoint) -> None:
        if run_state is None:
            raise RuntimeError("run state is unavailable")
        run_state.save_checkpoint(saved)
        if saved.final_sweep_complete:
            print_plain(
                error_console,
                f"Search complete: {saved.candidate_runs_used} candidates tested",
            )
        elif saved.candidate_runs_used == 1 or saved.candidate_runs_used % 10 == 0:
            print_plain(
                error_console,
                f"Search progress: {saved.candidate_runs_used} candidates tested",
            )

    reducer = DeterministicReducer(
        runner=prepared.runner,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        identity=prepared.identity,
        observation_store=observations.store,
        max_candidate_runs=candidate_limit,
        resume=checkpoint,
        on_checkpoint=persist_checkpoint,
    )
    try:
        asyncio.run(reducer.validate_resume(evidence))
    except ResumeMismatchError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "resume-mismatch")
    except ObservationStoreError as error:
        fail(
            str(error),
            ExitCode.INTERNAL,
            output_format,
            "shrink",
            "observation-store-error",
        )

    if run_state is None:
        run_state = RunStateFile.create(
            case_root=workspace.root,
            run_id=f"run-{uuid4().hex}",
            observation_store=observations.path,
            checkpoint=initial_checkpoint,
        )
    run_lock = nullcontext() if active_lock_held else run_state.acquire_lock()
    with run_lock:
        _finish_shrink(
            workspace=workspace,
            evidence=evidence,
            prepared=prepared,
            previous=previous,
            reducer=reducer,
            run_state=run_state,
            store=observations.store,
            calibrate_only=calibrate_only,
            candidate_limit=candidate_limit,
            continuation_store_path=observations.continuation_path,
            output_format=output_format,
        )


def _render_dry_run(
    *,
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    candidate_limit: int,
    observation_store: Path | None,
    output_format: OutputFormat,
) -> None:
    next_arguments = [
        "ithrl",
        "shrink",
        str(workspace.root.absolute()),
        "--max-candidates",
        str(candidate_limit),
    ]
    if observation_store is not None:
        next_arguments.extend(("--observation-store", str(observation_store.absolute())))
    next_command, next_command_shell = shell_command(tuple(next_arguments))
    render(
        CommandOutput(
            command="shrink",
            status="ready",
            case_path=str(workspace.root.absolute()),
            case_digest=evidence.digest,
            original_context_count=len(evidence.context.nodes),
            retained_context_count=len(evidence.context.nodes),
            max_candidate_runs=candidate_limit,
            execution_plan_digest=prepared.execution_plan.digest,
            runner_type=prepared.execution_plan.capability.runner_type,
            runner_version=prepared.execution_plan.capability.runner_version,
            target_type=prepared.execution_plan.target_type,
            protected_context_ids=prepared.experiment.oracle.protected_context_ids,
            next_action="Run the experiment when ready.",
            next_command=next_command,
            next_command_shell=next_command_shell,
        ),
        output_format,
    )


def _open_observation_session(
    *,
    requested_path: Path | None,
    run_state: RunStateFile | None,
    previous: ResultSnapshot | None,
    prepared: PreparedExperiment,
    output_format: OutputFormat,
) -> _ObservationSession:
    default_path = (user_data_path("ithrl", ensure_exists=False) / "observations.sqlite").absolute()
    requested_path = requested_path.absolute() if requested_path is not None else None
    if run_state is not None:
        path = Path(run_state.load().observation_store)
        if requested_path is not None and requested_path != path:
            fail(
                "--observation-store does not match the store saved by the active run",
                ExitCode.USAGE,
                output_format,
                "shrink",
                "resume-mismatch",
            )
    elif requested_path is not None:
        path = requested_path
    else:
        path = private_data_root() / "observations.sqlite"

    store = ObservationStore(path)
    try:
        store.initialize()
        if previous is not None:
            validate_phase_observations(previous, prepared, store)
    except ResumeMismatchError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "resume-mismatch")
    except ObservationStoreError as error:
        fail(
            str(error),
            ExitCode.INTERNAL,
            output_format,
            "shrink",
            "observation-store-error",
        )
    return _ObservationSession(
        store=store,
        path=path,
        continuation_path=None if path == default_path else path,
    )


def _finish_shrink(
    *,
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    previous: ResultSnapshot | None,
    reducer: DeterministicReducer,
    run_state: RunStateFile,
    store: ObservationStore,
    calibrate_only: bool,
    candidate_limit: int,
    continuation_store_path: Path | None,
    output_format: OutputFormat,
) -> None:
    print_plain(error_console, f"Run state: {run_state.path.parent}", soft_wrap=True)
    try:
        phases = run_phases(
            evidence=evidence,
            prepared=prepared,
            store=store,
            reducer=reducer,
            calibrate_only=calibrate_only,
            progress=lambda message: print_plain(error_console, message),
        )
    except ResumeMismatchError as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "resume-mismatch")
    except ObservationStoreError as error:
        fail(
            str(error),
            ExitCode.INTERNAL,
            output_format,
            "shrink",
            "observation-store-error",
        )

    recorded_observations = tuple(
        observation
        for observation in store.list_for_experiment(prepared.experiment.digest)
        if observation.identity == prepared.identity
    )
    outcome = save_outcome(
        workspace=workspace,
        evidence=evidence,
        prepared=prepared,
        reducer=reducer,
        previous=previous,
        phases=phases,
        calibrate_only=calibrate_only,
        max_candidates=candidate_limit,
        observation_store_path=continuation_store_path,
        observations=recorded_observations,
        run_state=run_state,
    )
    if phases.operational_error is not None:
        print_plain(error_console, phases.operational_error)
    render(outcome.report, output_format)
    if outcome.exit_code is not ExitCode.SUCCESS:
        raise typer.Exit(outcome.exit_code)


if __name__ == "__main__":
    app()
