"""Plan, execute and resume one frozen comparison of explicit source-bound contexts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import typer
from platformdirs import user_data_path

from ithrl.case.filesystem import read_safe_text, reject_symlink_components
from ithrl.cli.comparison_output import (
    ComparisonOutput,
    ContextReport,
    comparison_exit_code,
    render_comparison,
    result_report,
    safe_display_identifier,
)
from ithrl.cli.comparison_session import (
    ComparisonProfile,
    ComparisonSession,
    ComparisonSessionError,
    load_comparison_profile,
    load_comparison_session,
    save_comparison_profile,
    save_comparison_session,
    validate_session_state,
)
from ithrl.cli.output import OutputFormat, error_console, shell_command
from ithrl.comparison.contracts import (
    ComparisonConfiguration,
    ComparisonConfigurationError,
    ComparisonProgress,
    load_comparison_configuration,
)
from ithrl.execution import MAX_PROVIDER_CALLS_PER_ATTEMPT

if TYPE_CHECKING:
    from ithrl.case import CaseWorkspace
    from ithrl.comparison.engine import PreparedComparison
    from ithrl.comparison.results import ComparisonEvidence
    from ithrl.experiment import AnyExperimentDefinition
    from ithrl.observations import ObservationStore, SamplingBlockEvidence
    from ithrl.run_state import SampledRunState, SampledRunStateFile


class ComparisonInputRefusal(ValueError):
    """A fixed diagnostic for a comparison input or mode refusal."""


class ComparisonResultConflict(ComparisonSessionError):
    """An existing destination cannot represent the canonical comparison evidence."""

    def __init__(self, session: ComparisonSession) -> None:
        super().__init__("comparison result destination conflicts with its saved evidence")
        self.session = session


def prepare_comparison_profile(
    workspace: CaseWorkspace,
    configuration: ComparisonConfiguration,
    original_workspace: Path | None,
    *,
    detect: bool,
    experiment: AnyExperimentDefinition,
    max_provider_calls_per_attempt: int | None = None,
) -> tuple[ComparisonProfile, PreparedComparison]:
    from ithrl.cli.native import native_selection
    from ithrl.comparison.engine import ComparisonCapacityError, prepare_comparison
    from ithrl.execution import ResourceLimits
    from ithrl.experiment import (
        FakeRunnerConfig,
        NativeRunnerConfig,
        SampledCalibrationPhase,
        SampledExperimentDefinition,
        UnavailableRunnerConfig,
    )
    from ithrl.workflow import prepare_experiment

    evidence = workspace.load()
    persisted = experiment
    if not isinstance(
        persisted.runner, (FakeRunnerConfig, NativeRunnerConfig, UnavailableRunnerConfig)
    ):
        raise ComparisonInputRefusal("unsupported-comparison-runner")
    if configuration.codex_replay is not None and evidence.manifest.harness != "codex":
        raise ComparisonInputRefusal("codex-replay-requires-codex")
    # The generic execution identity still has a calibration field. Comparison collection
    # uses its separately frozen two-arm plan, not the legacy full-context collector.
    placeholder = SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1)
    if not isinstance(persisted.runner, UnavailableRunnerConfig):
        experiment = SampledExperimentDefinition(
            target=persisted.target,
            oracle=persisted.oracle,
            runner=persisted.runner,
            calibration=placeholder,
            grading=persisted.grading
            if isinstance(persisted, SampledExperimentDefinition)
            else None,
        )
    if isinstance(experiment, SampledExperimentDefinition) and isinstance(
        experiment.runner, FakeRunnerConfig
    ):
        if max_provider_calls_per_attempt not in (None, 0):
            raise ComparisonInputRefusal("provider-call-limit-native-only")
        prepared = prepare_experiment(
            evidence,
            experiment=experiment,
            require_observed_match=False,
            resource_limits=ResourceLimits(
                max_wall_time_seconds=configuration.limits.attempt_timeout_seconds
            ),
        )
    else:
        if not detect or original_workspace is None:
            raise ComparisonInputRefusal("detection-required")
        selection = native_selection(
            workspace,
            evidence,
            original_workspace=original_workspace,
            experiment=experiment,
            calibration=placeholder,
            max_provider_calls_per_attempt=max_provider_calls_per_attempt,
            attempt_timeout_seconds=configuration.limits.attempt_timeout_seconds,
            codex_replay=configuration.codex_replay,
        )
        prepared = prepare_experiment(
            evidence,
            experiment=selection.experiment,
            require_observed_match=False,
            registry=selection.registry,
            safety_policy=selection.safety_policy,
            resource_limits=selection.resource_limits,
        )
    try:
        admission = prepare_comparison(evidence, prepared, configuration)
    except ComparisonCapacityError:
        raise ComparisonInputRefusal("comparison-evidence-capacity") from None
    prepared, binding = admission.prepared, admission.binding
    if not isinstance(prepared.experiment, SampledExperimentDefinition):
        raise ComparisonInputRefusal("sampled-experiment-required")
    return ComparisonProfile(
        case_path=workspace.root.absolute(),
        original_workspace=original_workspace.absolute() if original_workspace else None,
        configuration=configuration,
        experiment=prepared.experiment,
        execution_plan=prepared.execution_plan,
        identity=prepared.identity,
        binding=binding,
    ), admission


def report_for_profile(profile: ComparisonProfile, *, harness_started: bool) -> ComparisonOutput:
    from ithrl.comparison.engine import (
        comparison_budget_bounds,
        comparison_budget_refusal,
        comparison_evidence_refusal,
    )
    from ithrl.execution.budget import sampling_attempt_cost

    config, plan = profile.configuration, profile.execution_plan
    harness = plan.capability.runner_type
    if harness not in {"fake", "codex", "copilot"}:
        raise ComparisonInputRefusal("unsupported-comparison-runner")
    cost = sampling_attempt_cost(plan.capability, plan.resource_limits)
    bounds = comparison_budget_bounds(
        config,
        cost,
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
    )
    refusal = comparison_evidence_refusal(profile.binding, plan.inspection_metadata)
    if refusal is None:
        refusal = comparison_budget_refusal(config, cost)
    return ComparisonOutput(
        status="refused" if refusal else "ready",
        error_code=refusal,
        objective=config.objective,
        effect_direction="candidate-minus-reference"
        if config.objective == "repair"
        else "reference-minus-candidate",
        case_path=str(profile.case_path),
        case_digest=plan.case_digest,
        configuration_digest=config.digest,
        comparison_digest=profile.binding.digest,
        harness=harness,
        model=safe_display_identifier(plan.model_requested),
        provider=safe_display_identifier(plan.capability.provider),
        harness_started=harness_started,
        capability_source="builtin"
        if plan.capability.runner_type == "fake"
        else "current-detection",
        execution_metadata=plan.inspection_metadata,
        reference_context=ContextReport.from_candidate(profile.binding.reference),
        candidate_context=ContextReport.from_candidate(profile.binding.candidate),
        limits=config.limits,
        calibration_plan=config.calibration,
        confirmation_plan=config.confirmation,
        planned_attempts=bounds.planned_attempts,
        planned_provider_calls=bounds.planned_provider_calls,
        planned_wall_time_seconds=bounds.planned_wall_time_seconds,
        confirmation_provider_calls=bounds.confirmation_provider_calls,
        maximum_provider_calls_per_attempt=plan.capability.provider_calls_per_attempt,
        minimum_effect=config.minimum_effect,
        alpha=config.confirmation.alpha,
    )


def _allowance(profile: ComparisonProfile, run_id: str):
    from ithrl.comparison.engine import comparison_budget_bounds
    from ithrl.execution.budget import sampling_attempt_cost
    from ithrl.observations import SamplingBudgetAllowance

    config, plan = profile.configuration, profile.execution_plan
    bounds = comparison_budget_bounds(
        config, sampling_attempt_cost(plan.capability, plan.resource_limits)
    )
    return SamplingBudgetAllowance(
        run_id=run_id,
        max_provider_calls=config.limits.max_provider_calls,
        max_wall_time_seconds=config.limits.max_wall_time_seconds,
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
        max_candidate_comparisons=1,
        confirmation_provider_calls=bounds.confirmation_provider_calls,
        confirmation_runner_seconds=bounds.confirmation_runner_seconds,
    )


def _snapshot(
    session: ComparisonSession, state_file: SampledRunStateFile, store: ObservationStore
) -> ComparisonEvidence:
    from ithrl.comparison.results import ComparisonEvidence

    now = datetime.now(UTC)
    snapshot = state_file.snapshot(store, now=now)
    validate_session_state(session, snapshot.state)
    return ComparisonEvidence(
        state=snapshot.state,
        blocks=snapshot.blocks,
        execution_metadata=session.profile.execution_plan.inspection_metadata,
        recorded_at=now,
    )


def _saved_result(
    session: ComparisonSession, state_file: SampledRunStateFile, store: ObservationStore
) -> ComparisonEvidence | None:
    state = state_file.load(store)
    validate_session_state(session, state)
    if state.comparison_progress is None or state.comparison_progress.terminal is None:
        return None
    if session.result_path.exists() or session.result_path.is_symlink():
        return _load_terminal_result(session, state, state_file.evidence(store))
    return _snapshot(session, state_file, store)


def _load_terminal_result(
    session: ComparisonSession,
    state: SampledRunState,
    blocks: tuple[SamplingBlockEvidence, ...],
) -> ComparisonEvidence:
    from ithrl.case import CaseWorkspace
    from ithrl.comparison.results import load_comparison_result

    evidence = CaseWorkspace(session.profile.case_path).load()
    try:
        result = load_comparison_result(session.result_path, case_evidence=evidence)
    except (OSError, ValueError):
        raise ComparisonResultConflict(session) from None
    if (
        result.state != state
        or result.blocks != blocks
        or result.execution_metadata != session.profile.execution_plan.inspection_metadata
    ):
        raise ComparisonResultConflict(session)
    return result


def execute_comparison(
    session: ComparisonSession | None,
    admission: PreparedComparison | None,
    *,
    profile: ComparisonProfile | None = None,
    store_path: Path | None = None,
    dry_run: bool = False,
    calibration_only: bool = False,
) -> ComparisonOutput:
    from ithrl.case import CaseWorkspace
    from ithrl.case.filesystem import prepare_private_directory
    from ithrl.cli.progress import progress_display, sampling_status
    from ithrl.comparison.engine import run_comparison
    from ithrl.comparison.results import write_comparison_result
    from ithrl.observations import ObservationStore
    from ithrl.run_state import SampledRunStateFile, SamplingBudgetApproval

    prepared = admission.prepared if admission is not None else None
    if session is None:
        if profile is None or prepared is None or store_path is None:
            raise ValueError("new comparison requires prepared inputs")
        store = ObservationStore(store_path)
        store.initialize()
        started_at = datetime.now(UTC)
        run_id = f"compare-{uuid4().hex}"
        state_file = SampledRunStateFile.create(
            case_root=profile.case_path,
            run_id=run_id,
            store=store,
            started_at=started_at,
            original_identity=profile.identity,
            original_candidate_digest=profile.binding.reference.digest,
            provider_calls_per_attempt=profile.execution_plan.capability.provider_calls_per_attempt,
            approval=SamplingBudgetApproval(
                approval_id=uuid4().hex,
                approved_at=started_at,
                allowance=_allowance(profile, run_id),
            ),
            comparison_progress=ComparisonProgress(binding=profile.binding),
        )
        session = ComparisonSession(
            run_id=run_id,
            run_directory=state_file.path.parent,
            observation_store=store_path,
            profile=profile,
            started_at=started_at,
        )
        with state_file.acquire_lock(store):
            save_comparison_session(session, state_file, store)
    else:
        store = ObservationStore(session.observation_store)
        state_file = SampledRunStateFile.open(session.run_directory)
    with state_file.acquire_lock(store):
        state = state_file.load(store)
        validate_session_state(session, state)
        if state.allowance != _allowance(session.profile, state.run_id):
            raise ComparisonSessionError("comparison limits differ from the saved session")
        result = _saved_result(session, state_file, store)
        if result is None and dry_run:
            result = _snapshot(session, state_file, store)
    interrupted = False
    execution_refused = False
    if result is None:
        if prepared is None or prepared.identity != session.profile.identity:
            raise ComparisonInputRefusal("execution-identity-mismatch")
        render_comparison(
            report_for_profile(session.profile, harness_started=False),
            OutputFormat.HUMAN,
            target=error_console,
        )
        with progress_display() as progress:
            try:
                result = asyncio.run(
                    run_comparison(
                        evidence=CaseWorkspace(session.profile.case_path).load(),
                        prepared=prepared,
                        configuration=session.profile.configuration,
                        state_file=state_file,
                        store=store,
                        admission=admission,
                        calibration_only=calibration_only,
                        on_progress=lambda block: progress(
                            sampling_status(
                                block,
                                phase="Confirmation"
                                if block.plan.role == "confirmation"
                                else "Calibration",
                                candidate_label="candidate",
                                baseline_label="reference",
                            )
                        ),
                    )
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                interrupted = True
                with state_file.acquire_lock(store):
                    result = _snapshot(session, state_file, store)
            except Exception:
                execution_refused = True
                with state_file.acquire_lock(store):
                    result = _snapshot(session, state_file, store)
    with state_file.acquire_lock(store):
        current_state = state_file.load(store)
        validate_session_state(session, current_state)
        if current_state != result.state:
            raise ComparisonSessionError("comparison changed before result publication")
        session_fields = {
            "case_path": str(session.profile.case_path),
            "run_state_path": str(session.run_directory),
            "harness": session.profile.execution_plan.capability.runner_type,
            "model": safe_display_identifier(session.profile.execution_plan.model_requested),
            "provider": safe_display_identifier(session.profile.execution_plan.capability.provider),
            "source_validation": "verified",
            "harness_started": prepared is not None
            and session.profile.execution_plan.capability.runner_type != "fake",
            "limits": session.profile.configuration.limits,
        }
        report = result_report(result).model_copy(update=session_fields)
        if dry_run:
            return report.model_copy(
                update={"next_action": "Saved evidence inspected without native execution."}
            )
        if (
            result.state.comparison_progress is not None
            and result.state.comparison_progress.terminal is not None
        ):
            destination = session.result_path
        else:
            destination = session.result_path.with_name(f"{session.run_id}-{uuid4().hex}.json")
        try:
            prepare_private_directory(destination.parent)
            if destination.exists() or destination.is_symlink():
                result = _load_terminal_result(session, current_state, result.blocks)
                report = result_report(result).model_copy(update=session_fields)
            else:
                write_comparison_result(destination, result)
        except (OSError, ValueError) as error:
            resume_args = (
                "ithrl",
                "compare",
                str(session.run_directory),
                "--resume",
                "--acknowledge-model-execution",
            )
            if calibration_only:
                resume_args += ("--calibration-only",)
            command, shell = shell_command(resume_args)
            conflict = isinstance(error, ComparisonResultConflict)
            if conflict:
                next_action = "Preserve the conflicting result file elsewhere, then resume the canonical run to publish its saved evidence."
            elif calibration_only:
                next_action = "Restore private output access, then resume with --calibration-only to publish the saved pilot evidence without starting confirmation."
            else:
                next_action = "Restore private output access, then resume the canonical run to publish its saved evidence without repeating completed observations."
            return report.model_copy(
                update={
                    "error_code": "result-destination-conflict"
                    if conflict
                    else "result-publication-failed",
                    "result_path": str(destination),
                    "next_action": next_action,
                    "next_command": command,
                    "next_command_shell": shell,
                }
            )
        report = report.model_copy(update={"result_path": str(destination)})
        if execution_refused:
            report = report.model_copy(update={"error_code": "comparison-execution-refused"})
        if report.stop_reason in {"wall-time-exhausted", "freshness-expired"}:
            return report.model_copy(
                update={
                    "next_action": "Inspect the saved evidence. The frozen time limit has expired; a new comparison requires a new run."
                }
            )
        if (
            interrupted
            or report.status == "calibrated"
            or (
                result.outcome.value == "incomplete"
                and result.state.comparison_progress is not None
                and result.state.comparison_progress.terminal is None
            )
        ):
            calibration_passed = (
                report.status == "calibrated" and not interrupted and not execution_refused
            )
            resume_args = (
                "ithrl",
                "compare",
                str(session.run_directory),
                "--resume",
                "--acknowledge-model-execution",
            )
            if calibration_only and not calibration_passed:
                resume_args += ("--calibration-only",)
            command, shell = shell_command(resume_args)
            report = report.model_copy(
                update={
                    "status": "interrupted" if interrupted else report.status,
                    "next_action": "Calibration passed; confirmation has not started. If confirmation is authorized, use the displayed command within the original allowance."
                    if calibration_passed
                    else "Inspect saved accounting and resume only the same frozen comparison. Unresolved dispatches are never replayed.",
                    "next_command": command,
                    "next_command_shell": shell,
                }
            )
        return report


def _handle(
    source: Path,
    config_path: Path | None,
    profile_path: Path | None,
    original_workspace: Path | None,
    observation_store: Path | None,
    experiment_path: Path | None,
    dry_run: bool,
    detect: bool,
    resume: bool,
    acknowledge: bool,
    max_provider_calls_per_attempt: int | None,
    calibration_only: bool = False,
) -> ComparisonOutput:
    from ithrl.case import CaseWorkspace
    from ithrl.cli.paths import observation_storage_paths
    from ithrl.cli.sampled import _observation_store_path
    from ithrl.comparison.engine import _require_comparison_numerics
    from ithrl.comparison.results import validate_comparison_source
    from ithrl.experiment import NativeRunnerConfig, UnavailableRunnerConfig, parse_experiment_toml
    from ithrl.observations import ObservationStore
    from ithrl.run_state import SampledRunStateFile

    _require_comparison_numerics()
    if detect and (dry_run or resume or calibration_only):
        raise ComparisonInputRefusal("conflicting-modes")
    session = load_comparison_session(source) if resume else None
    profile = None
    if session is not None:
        if any(
            path is not None
            for path in (
                config_path,
                profile_path,
                original_workspace,
                observation_store,
                experiment_path,
            )
        ):
            raise ComparisonInputRefusal("resume-input-conflict")
        profile = session.profile
        if max_provider_calls_per_attempt not in (
            None,
            profile.execution_plan.capability.provider_calls_per_attempt,
        ):
            raise ComparisonInputRefusal("profile-provider-call-limit-mismatch")
        configuration = profile.configuration
        workspace = CaseWorkspace(profile.case_path)
        evidence = workspace.load()
        if evidence.digest != profile.identity.case_digest:
            raise ComparisonInputRefusal("profile-input-mismatch")
        validate_comparison_source(
            profile.binding, profile.execution_plan.inspection_metadata, evidence
        )
        original_workspace, observation_store = (
            profile.original_workspace,
            session.observation_store,
        )
        safe_store_path = _observation_store_path(
            workspace,
            original_workspace or workspace.root,
            session.observation_store,
            require_original_workspace=False,
        )
        if safe_store_path != session.observation_store:
            raise ComparisonInputRefusal("profile-input-mismatch")
        store = ObservationStore(safe_store_path)
        state_file = SampledRunStateFile.open(session.run_directory)
        with state_file.acquire_lock(store):
            if calibration_only and state_file.load(store).selection is not None:
                raise ComparisonInputRefusal("calibration-after-selection-refused")
            completed = _saved_result(session, state_file, store)
        if completed is not None or dry_run:
            return execute_comparison(
                session, None, dry_run=dry_run, calibration_only=calibration_only
            )
    else:
        if config_path is None:
            raise ComparisonInputRefusal("comparison-configuration-required")
        configuration = load_comparison_configuration(config_path)
        workspace = CaseWorkspace(source.absolute())
        profile = (
            load_comparison_profile(profile_path)
            if profile_path is not None and not detect
            else None
        )
    evidence = workspace.load()
    if profile is not None:
        if experiment_path is not None:
            raise ComparisonInputRefusal("profile-experiment-conflict")
        persisted = profile.experiment
    elif experiment_path is not None:
        reject_symlink_components(experiment_path.absolute())
        persisted = parse_experiment_toml(
            read_safe_text(experiment_path, max_bytes=1024 * 1024, label="experiment")
        )
    else:
        persisted = parse_experiment_toml(evidence.default_experiment)
    native = isinstance(persisted.runner, (NativeRunnerConfig, UnavailableRunnerConfig))
    if native and original_workspace is None:
        raise ComparisonInputRefusal("original-workspace-required")
    store_path = _observation_store_path(
        workspace, original_workspace or workspace.root, observation_store
    )
    if profile_path is not None:
        profile_path = _observation_store_path(
            workspace, original_workspace or workspace.root, profile_path
        )
        if profile_path in observation_storage_paths(store_path):
            raise ComparisonInputRefusal("profile-store-overlap")
    if profile is not None:
        saved_limit = profile.execution_plan.capability.provider_calls_per_attempt
        if max_provider_calls_per_attempt not in (None, saved_limit):
            raise ComparisonInputRefusal("profile-provider-call-limit-mismatch")
        max_provider_calls_per_attempt = saved_limit
        if (
            profile.case_path != workspace.root.absolute()
            or profile.configuration != configuration
            or profile.execution_plan.case_digest != evidence.digest
            or profile.original_workspace
            != (original_workspace.absolute() if original_workspace else None)
        ):
            raise ComparisonInputRefusal("profile-input-mismatch")
        validate_comparison_source(
            profile.binding, profile.execution_plan.inspection_metadata, evidence
        )
    if native and profile is None and not detect:
        if not dry_run:
            raise ComparisonInputRefusal("detection-required")
        arguments = (
            "ithrl",
            "compare",
            str(workspace.root),
            "--config",
            str(config_path),
            "--detect-capabilities",
            "--original-workspace",
            str(original_workspace),
        )
        if experiment_path is not None:
            arguments += ("--experiment", str(experiment_path))
        if max_provider_calls_per_attempt is not None:
            arguments += ("--max-provider-calls-per-attempt", str(max_provider_calls_per_attempt))
        command, shell = shell_command(arguments)
        return ComparisonOutput(
            status="detection-required",
            case_path=str(workspace.root),
            case_digest=evidence.digest,
            configuration_digest=configuration.digest,
            objective=configuration.objective,
            limits=configuration.limits,
            next_action="Detect the exact structural profile before native execution. This dry-run starts no harness.",
            next_command=command,
            next_command_shell=shell,
        )
    if native and not (dry_run or detect) and not acknowledge:
        return ComparisonOutput(
            status="refused",
            error_code="model-execution-not-acknowledged",
            next_action="Inspect --dry-run, then pass --acknowledge-model-execution.",
        )
    admission = None
    if not native or detect or not dry_run:
        current, admission = prepare_comparison_profile(
            workspace,
            configuration,
            original_workspace,
            detect=native,
            experiment=persisted,
            max_provider_calls_per_attempt=max_provider_calls_per_attempt,
        )
        if profile is not None and current != profile:
            raise ComparisonInputRefusal("capability-profile-mismatch")
        profile = current
    if profile is None:
        raise ComparisonInputRefusal("comparison-profile-required")
    report = report_for_profile(profile, harness_started=native and (detect or not dry_run))
    if native and dry_run:
        report = report.model_copy(update={"capability_source": "saved-detection"})
    if detect:
        if profile_path is None:
            profile_path = (
                user_data_path("ithrl", ensure_exists=False)
                / "comparison-profiles"
                / f"{profile.digest}.json"
            )
        profile_path = _observation_store_path(
            workspace, original_workspace or workspace.root, profile_path
        )
        if profile_path in observation_storage_paths(store_path):
            raise ComparisonInputRefusal("profile-store-overlap")
        save_comparison_profile(profile_path, profile)
        return report.model_copy(
            update={
                "status": "refused" if report.status == "refused" else "detected",
                "profile_path": str(profile_path),
                "next_action": "Revise the refused limits or configuration and detect again with a new profile."
                if report.status == "refused"
                else "Inspect a harness-free --dry-run with this --profile before acknowledging execution.",
            }
        )
    if report.status == "refused":
        return report
    if dry_run:
        command_args = (
            "ithrl",
            "compare",
            str(workspace.root),
            "--config",
            str(config_path),
            "--observation-store",
            str(store_path),
        )
        if experiment_path is not None:
            command_args += ("--experiment", str(experiment_path))
        if profile_path is not None:
            command_args += ("--profile", str(profile_path))
        if original_workspace is not None:
            command_args += ("--original-workspace", str(original_workspace))
        if native:
            command_args += ("--acknowledge-model-execution",)
        if calibration_only:
            command_args += ("--calibration-only",)
        command, shell = shell_command(command_args)
        return report.model_copy(
            update={
                "profile_path": str(profile_path) if profile_path else None,
                "next_action": "Inspect both context selections, objective, samples and limits before execution.",
                "next_command": command,
                "next_command_shell": shell,
            }
        )
    return execute_comparison(
        session,
        admission,
        profile=profile,
        store_path=store_path,
        calibration_only=calibration_only,
    )


def compare(
    source: Annotated[
        Path,
        typer.Argument(metavar="CASE_OR_RUN", help="Case, or canonical saved run with --resume."),
    ],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help="Private TOML selecting both contexts, direction, samples and limits."
        ),
    ] = None,
    experiment: Annotated[
        Path | None,
        typer.Option(
            "--experiment",
            help="Existing experiment with the fixed target, oracle and optional grader.",
        ),
    ] = None,
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile", help="Private structural profile; output path with --detect-capabilities."
        ),
    ] = None,
    original_workspace: Annotated[
        Path | None,
        typer.Option(
            "--original-workspace", help="Protected original workspace for native execution."
        ),
    ] = None,
    max_provider_calls_per_attempt: Annotated[
        int | None,
        typer.Option(
            "--max-provider-calls-per-attempt",
            min=1,
            max=MAX_PROVIDER_CALLS_PER_ATTEMPT,
            help="Hard native request ceiling per attempt; resume preserves its saved value.",
        ),
    ] = None,
    observation_store: Annotated[
        Path | None,
        typer.Option("--observation-store", help="Private SQLite store outside protected inputs."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Inspect inputs and limits without starting a native harness or provider.",
        ),
    ] = False,
    detect_capabilities: Annotated[
        bool,
        typer.Option(
            "--detect-capabilities",
            "--detect",
            help="Explicit zero-provider structural detection and profile persistence.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue the same frozen comparison without redispatching completed work.",
        ),
    ] = False,
    calibration_only: Annotated[
        bool,
        typer.Option(
            "--calibration-only",
            help="Stop after successful calibration, before selection or confirmation; preserve the full frozen allowance.",
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
    """Calibrate and freshly confirm one directed comparison of two explicit contexts."""
    from ithrl.cli.native import NativeSelectionError

    try:
        report = _handle(
            source,
            config,
            profile,
            original_workspace,
            observation_store,
            experiment,
            dry_run,
            detect_capabilities,
            resume,
            acknowledge_model_execution,
            max_provider_calls_per_attempt,
            calibration_only,
        )
    except ImportError:
        report = ComparisonOutput(
            status="refused",
            error_code="comparison-extra-required",
            next_action="Install ithrl[compare] before running comparisons.",
        )
    except ComparisonInputRefusal as error:
        report = ComparisonOutput(
            status="refused",
            error_code=str(error),
            next_action="Check the indicated input and inspect --dry-run before execution.",
        )
    except ComparisonConfigurationError:
        report = ComparisonOutput(
            status="refused",
            error_code="invalid-comparison-configuration",
            next_action="Check context selectors, explicit sampling fields and private file permissions.",
        )
    except ComparisonResultConflict as error:
        session = error.session
        command, shell = shell_command(("ithrl", "compare", str(session.run_directory), "--resume"))
        report = ComparisonOutput(
            status="refused",
            error_code="result-destination-conflict",
            result_path=str(session.result_path),
            run_state_path=str(session.run_directory),
            next_action="Preserve the conflicting result file elsewhere, then resume the canonical run to publish its saved evidence.",
            next_command=command,
            next_command_shell=shell,
        )
    except ComparisonSessionError:
        report = ComparisonOutput(
            status="refused",
            error_code="invalid-comparison-session",
            next_action="Check the canonical private profile/run and observation store.",
        )
    except NativeSelectionError as error:
        report = ComparisonOutput(
            status="refused",
            error_code=error.code,
            harness_started=error.harness_started,
            probe_stage=error.probe_stage,
            probe_exit_code=error.probe_exit_code,
            probe_reason=error.probe_reason,
            diagnostic_path=str(error.diagnostic_path) if error.diagnostic_path else None,
            next_action="Resolve structural detection before execution.",
        )
    except (KeyboardInterrupt, typer.Abort):
        report = ComparisonOutput(
            status="interrupted",
            error_code="interrupted",
            harness_started=None,
            next_action="Inspect saved accounting before resuming the same run.",
        )
    except Exception:
        report = ComparisonOutput(
            status="refused",
            error_code="comparison-input-or-execution-refused",
            harness_started=None,
            next_action="Inspect private inputs and saved evidence; no automatic retry was made.",
        )
    render_comparison(report, output_format)
    raise typer.Exit(comparison_exit_code(report))
