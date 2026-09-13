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
from ithrl.cli.output import (
    CommandOutput,
    ExitCode,
    JsonAwareGroup,
    OutputFormat,
    command_boundary,
    error_console,
    fail,
    print_plain,
    render,
    shell_command,
)
from ithrl.cli.paths import private_data_root
from ithrl.cli.shrink import prepare_shrink, save_outcome, validate_phase_observations
from ithrl.experiment import ExperimentFormatError
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
) -> None:
    if resume and (calibrate_only or dry_run):
        fail(
            "--resume cannot be combined with --calibrate-only or --dry-run",
            ExitCode.USAGE,
            output_format,
            "shrink",
            "invalid-options",
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
        )
    except (
        CaseError,
        ExperimentFormatError,
        ResultSnapshotError,
        ResumeMismatchError,
        RunStateError,
    ) as error:
        fail(str(error), ExitCode.USAGE, output_format, "shrink", "invalid-input")
    except (OraclePrerequisiteError, PreflightRefusedError) as error:
        fail(str(error), ExitCode.UNSUPPORTED, output_format, "shrink", "unsupported")

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
            runner_type=prepared.execution_plan.runner_type,
            runner_version=prepared.execution_plan.runner_version,
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

    observations_recorded = sum(
        observation.identity == prepared.identity
        for observation in store.list_for_experiment(prepared.experiment.digest)
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
        observations_recorded=observations_recorded,
        run_state=run_state,
    )
    if phases.operational_error is not None:
        print_plain(error_console, phases.operational_error)
    render(outcome.report, output_format)
    if outcome.exit_code is not ExitCode.SUCCESS:
        raise typer.Exit(outcome.exit_code)


if __name__ == "__main__":
    app()
