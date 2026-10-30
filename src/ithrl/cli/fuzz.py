"""Inspectable, explicitly acknowledged CLI composition of bounded fuzz campaigns."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import typer
from platformdirs import user_data_path

from ithrl.case import CaseWorkspace
from ithrl.cli.fuzz_output import FuzzOutput, fuzz_exit_code, render_fuzz
from ithrl.cli.fuzz_run import execute_fuzz, report_for_profile
from ithrl.cli.fuzz_session import (
    FuzzProfile,
    FuzzSessionError,
    load_fuzz_profile,
    load_fuzz_session,
    save_fuzz_profile,
)
from ithrl.cli.native import NativeSelectionError, native_selection
from ithrl.cli.output import OutputFormat, shell_command
from ithrl.cli.sampled import _observation_store_path
from ithrl.execution import ResourceLimits
from ithrl.experiment import (
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledExperimentDefinition,
    UnavailableRunnerConfig,
    parse_experiment_toml,
)
from ithrl.fuzz.configuration import (
    FuzzConfiguration,
    FuzzConfigurationError,
    load_fuzz_configuration,
)
from ithrl.fuzz.sampling import prepare_fuzz_sampling
from ithrl.fuzz.statistics import require_fuzz_numerics
from ithrl.runner import prepare_candidate
from ithrl.workflow import PreparedExperiment, prepare_experiment


class FuzzInputRefusal(ValueError):
    """A fixed content-safe reason before any attempt can start."""


def prepare_fuzz_profile(
    workspace: CaseWorkspace,
    config: FuzzConfiguration,
    original_workspace: Path | None,
    *,
    detect: bool,
) -> tuple[FuzzProfile, PreparedExperiment]:
    evidence = workspace.load()
    persisted = parse_experiment_toml(evidence.default_experiment)
    if not isinstance(
        persisted.runner, (FakeRunnerConfig, NativeRunnerConfig, UnavailableRunnerConfig)
    ):
        raise ValueError("unsupported fuzz runner")
    experiment = (
        None
        if isinstance(persisted.runner, UnavailableRunnerConfig)
        else SampledExperimentDefinition(
            schema_version=1,
            target=persisted.target,
            oracle=persisted.oracle,
            runner=persisted.runner,
            calibration=config.calibration,
        )
    )
    if experiment is not None and isinstance(experiment.runner, FakeRunnerConfig):
        prepared = prepare_experiment(
            evidence,
            experiment=experiment,
            resource_limits=ResourceLimits(
                max_wall_time_seconds=config.limits.attempt_timeout_seconds
            ),
        )
    else:
        if not detect or original_workspace is None:
            raise ValueError("native detection requires the original workspace")
        selection = native_selection(
            workspace,
            evidence,
            original_workspace=original_workspace,
            experiment=experiment,
            calibration=config.calibration,
            attempt_timeout_seconds=config.limits.attempt_timeout_seconds,
        )
        prepared = prepare_experiment(
            evidence,
            registry=selection.registry,
            safety_policy=selection.safety_policy,
            resource_limits=selection.resource_limits,
            experiment=selection.experiment,
        )
    campaign = config.bind(prepare_candidate(evidence, prepared.execution_plan))
    prepared = prepare_fuzz_sampling(evidence, prepared, campaign)
    profile = FuzzProfile(
        case_path=workspace.root.absolute(),
        original_workspace=original_workspace.absolute() if original_workspace else None,
        configuration=config,
        experiment=prepared.experiment,
        execution_plan=prepared.execution_plan,
        identity=prepared.identity,
    )
    return profile, prepared


class _Next(TypedDict):
    next_command: str
    next_command_shell: Literal["posix", "powershell"]


def _next(arguments: tuple[str, ...]) -> _Next:
    command, shell = shell_command(arguments)
    return {"next_command": command, "next_command_shell": shell}


def _require_admission(profile: FuzzProfile) -> None:
    if "append-text" not in profile.execution_plan.capability.supported_context_operations:
        raise FuzzInputRefusal("unsupported-context-operation")


def _handle(
    source: Path,
    config_path: Path | None,
    profile_path: Path | None,
    original_workspace: Path | None,
    observation_store: Path | None,
    dry_run: bool,
    detect: bool,
    resume: bool,
    acknowledge: bool,
) -> FuzzOutput:
    if detect and (dry_run or resume):
        raise FuzzInputRefusal("conflicting-modes")
    require_fuzz_numerics()
    session = load_fuzz_session(source) if resume else None
    if session is not None:
        if any(
            p is not None
            for p in (config_path, profile_path, original_workspace, observation_store)
        ):
            raise FuzzInputRefusal("resume-input-conflict")
        profile = session.profile
        config = profile.configuration
        workspace = CaseWorkspace(profile.case_path)
        original_workspace = profile.original_workspace
        observation_store = session.observation_store
    else:
        if config_path is None:
            raise FuzzInputRefusal("fuzz-configuration-required")
        config = load_fuzz_configuration(config_path)
        workspace = CaseWorkspace(source.absolute())
        profile = load_fuzz_profile(profile_path) if profile_path and not detect else None
    evidence = workspace.load()
    persisted = parse_experiment_toml(evidence.default_experiment)
    native = isinstance(persisted.runner, (NativeRunnerConfig, UnavailableRunnerConfig))
    if native and original_workspace is None:
        raise FuzzInputRefusal("original-workspace-required")
    # Pure path validation precedes both consent and detection. It creates no storage.
    store_path = _observation_store_path(
        workspace, original_workspace or workspace.root, observation_store
    )
    if profile is not None and (
        profile.case_path != workspace.root.absolute()
        or profile.configuration != config
        or profile.original_workspace
        != (original_workspace.absolute() if original_workspace else None)
        or profile.execution_plan.case_digest != evidence.digest
    ):
        raise FuzzInputRefusal("profile-input-mismatch")
    if native and profile is None and not detect:
        if not dry_run:
            raise FuzzInputRefusal("detection-required")
        next_step = _next(
            (
                "ithrl",
                "fuzz",
                str(workspace.root),
                "--config",
                str(config_path),
                "--detect",
                "--original-workspace",
                str(original_workspace),
            )
        )
        return FuzzOutput(
            status="detection-required",
            case_path=str(workspace.root),
            case_digest=evidence.digest,
            configuration_digest=config.digest,
            harness=(
                persisted.runner.harness
                if isinstance(persisted.runner, UnavailableRunnerConfig)
                else persisted.runner.type
            ),
            model=evidence.manifest.model_requested,
            target=persisted.target.type,
            calibration_plan=config.calibration,
            screening_plan=config.screening,
            confirmation_plan=config.confirmation,
            editable_text_sites=len(evidence.editable_text()),
            mutators=config.mutators,
            stress_text_bytes=len(config.stale_text.encode()),
            limits=config.limits,
            max_candidates=config.max_candidates,
            max_simplification_trials=config.max_simplification_trials,
            minimum_decrease=config.minimum_decrease,
            alpha=config.confirmation.alpha,
            freshness_seconds=config.freshness_seconds,
            eligibility_policy=config.eligibility_policy,
            next_action="Detect an exact structural profile; this dry-run starts no harness.",
            next_command=next_step["next_command"],
            next_command_shell=next_step["next_command_shell"],
        )
    if native and not (dry_run or detect) and not acknowledge:
        return FuzzOutput(
            status="refused",
            error_code="model-execution-not-acknowledged",
            next_action="Inspect --dry-run, then pass --acknowledge-model-execution.",
        )
    prepared = None
    if not native or detect or not dry_run:
        current, prepared = prepare_fuzz_profile(
            workspace, config, original_workspace, detect=native
        )
        if profile is not None and current != profile:
            raise FuzzInputRefusal("capability-profile-mismatch")
        profile = current
    if profile is None:
        raise ValueError("a verified structural profile is required")
    report = report_for_profile(profile, harness_started=native and (detect or not dry_run))
    if native and dry_run:
        report = report.model_copy(update={"capability_source": "saved-detection"})
    if detect:
        if profile_path is not None:
            profile_path = _observation_store_path(
                workspace, original_workspace or workspace.root, profile_path
            )
            if profile_path == store_path:
                raise FuzzInputRefusal("profile-store-overlap")
        if profile_path is None:
            profile_path = (
                user_data_path("ithrl", ensure_exists=False)
                / "fuzz-profiles"
                / f"{profile.digest}.json"
            )
        save_fuzz_profile(profile_path, profile)
        return report.model_copy(
            update={
                "status": "detected",
                "profile_path": str(profile_path),
                "next_action": "Inspect a harness-free dry-run using --profile before acknowledging execution.",
            }
        )
    _require_admission(profile)
    if report.status == "refused":
        return report
    if dry_run:
        if session is not None:
            return execute_fuzz(session, None, dry_run=True)
        arguments = (
            "ithrl",
            "fuzz",
            str(workspace.root),
            "--config",
            str(config_path),
            "--observation-store",
            str(store_path),
        )
        if native:
            arguments += (
                "--profile",
                str(profile_path),
                "--original-workspace",
                str(original_workspace),
                "--acknowledge-model-execution",
            )
        return report.model_copy(
            update={
                **_next(arguments),
                "profile_path": str(profile_path) if profile_path else None,
                "next_action": "Inspect the fixed sampling, mutation surface and limits before execution.",
            }
        )
    if prepared is None:
        raise ValueError("current execution preparation is unavailable")
    return execute_fuzz(
        session, prepared, profile=profile, store_path=store_path, started_at=datetime.now(UTC)
    )


def fuzz(
    source: Annotated[
        Path, typer.Argument(metavar="CASE_OR_RUN", help="Case, or saved run/result with --resume.")
    ],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help="Private TOML with explicit mutation, sampling and resource limits."
        ),
    ] = None,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Private structural profile; output path with --detect."),
    ] = None,
    original_workspace: Annotated[
        Path | None,
        typer.Option(
            "--original-workspace", help="Protected original workspace for a native case."
        ),
    ] = None,
    observation_store: Annotated[
        Path | None,
        typer.Option("--observation-store", help="Private SQLite store outside protected state."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Inspect saved inputs and limits without starting a harness or provider.",
        ),
    ] = False,
    detect: Annotated[
        bool,
        typer.Option(
            "--detect",
            help="Explicit credential-free structural harness detection; save a profile.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue exactly the saved campaign, without changing its inputs or budget.",
        ),
    ] = False,
    acknowledge_model_execution: Annotated[
        bool,
        typer.Option(
            "--acknowledge-model-execution",
            help="Approve the inspected native execution within its frozen limits.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.HUMAN,
) -> None:
    """Find and independently confirm a bounded decrease in the selected target rate."""
    try:
        report = _handle(
            source,
            config,
            profile,
            original_workspace,
            observation_store,
            dry_run,
            detect,
            resume,
            acknowledge_model_execution,
        )
    except ImportError:
        report = FuzzOutput(
            status="refused",
            error_code="fuzz-extra-required",
            next_action="Install the optional ithrl[fuzz] dependencies before running fuzz.",
        )
    except FuzzInputRefusal as error:
        report = FuzzOutput(
            status="refused",
            error_code=str(error),
            next_action="Check the indicated input or inspect --dry-run. No attempt was dispatched.",
        )
    except FuzzConfigurationError:
        report = FuzzOutput(
            status="refused",
            error_code="invalid-fuzz-configuration",
            next_action="Check TOML fields, limits and private file permissions (0600 on POSIX).",
        )
    except FuzzSessionError:
        report = FuzzOutput(
            status="refused",
            error_code="invalid-saved-fuzz-metadata",
            next_action="Check the private profile or canonical run and its observation store.",
        )
    except NativeSelectionError as error:
        report = FuzzOutput(
            status="refused",
            error_code=error.code,
            harness_started=error.harness_started,
            probe_stage=error.probe_stage,
            probe_exit_code=error.probe_exit_code,
            probe_reason=error.probe_reason,
            diagnostic_path=str(error.diagnostic_path) if error.diagnostic_path else None,
            next_action="Resolve the structural detection refusal before execution.",
        )
    except (KeyboardInterrupt, typer.Abort):
        report = FuzzOutput(
            status="interrupted",
            error_code="interrupted",
            harness_started=None,
            provider_call_accounting="started-unknown",
            provider_calls=None,
            actions_executed=None,
            next_action="Inspect saved run evidence before continuing.",
        )
    except Exception:
        # Never echo private payloads, native diagnostics or exception reprs. An early
        # unknown failure cannot truthfully assert that no external work happened.
        report = FuzzOutput(
            status="refused",
            error_code="fuzz-input-or-execution-refused",
            harness_started=None,
            provider_call_accounting="started-unknown",
            provider_calls=None,
            actions_executed=None,
            next_action="Check private configuration, profile and run evidence. No automatic retry was made.",
        )
    render_fuzz(report, output_format)
    raise typer.Exit(fuzz_exit_code(report))
