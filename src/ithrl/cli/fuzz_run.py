"""Compose calibration, durable campaign execution and private evidence publication."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ithrl.calibration import CalibrationInFlightError, _read_inflight, run_sampled_calibration
from ithrl.case import CaseWorkspace
from ithrl.cli.fuzz_output import FuzzCounts, FuzzOutput, render_fuzz
from ithrl.cli.fuzz_session import FuzzProfile, FuzzSession, publish_fuzz_result, save_fuzz_session
from ithrl.cli.output import OutputFormat, error_console, last_runner_error, shell_command
from ithrl.cli.progress import calibration_status, progress_display, sampling_status
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.fuzz.campaign import _initialize_fuzz_campaign_owned, _run_fuzz_campaign_owned
from ithrl.fuzz.sampling import confirm_fuzz_evidence, fuzz_budget_bounds
from ithrl.observations import (
    ObservationArm,
    ObservationRole,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBudgetAllowance,
)
from ithrl.run_state import SampledRunState, SampledRunStateFile, SamplingBudgetApproval
from ithrl.runner import prepare_candidate
from ithrl.sampling import sampling_pacing_delay
from ithrl.workflow import PreparedExperiment


class _CalibrationBudgetStop(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _sampling_status(trial: int, block: SamplingBlockEvidence) -> str:
    phase = (
        "Confirmation"
        if block.plan.role == "confirmation"
        else f"Simplification trial {trial}"
        if block.plan.role == "simplification"
        else f"Exploratory trial {trial}"
    )
    return sampling_status(block, phase=phase, candidate_label="mutant")


def report_for_profile(profile: FuzzProfile, *, harness_started: bool) -> FuzzOutput:
    config, plan = profile.configuration, profile.execution_plan
    capability = plan.capability
    # The original is derived from the immutable case; this also checks saved
    # materialization against current evidence without a harness or runtime probe.
    evidence = CaseWorkspace(profile.case_path).load()
    campaign = config.bind(prepare_candidate(evidence, plan))
    if profile.identity.algorithm_digest != campaign.digest:
        raise ValueError("profile differs from its bound campaign policy")
    bounds = fuzz_budget_bounds(
        campaign,
        sampling_attempt_cost(capability, plan.resource_limits),
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
    )
    first_comparison = config.screening.original.max_attempts + config.screening.mutant.max_attempts
    required_attempts = (
        config.calibration.max_attempts + first_comparison + bounds.confirmation_attempts
    )
    cost = sampling_attempt_cost(capability, plan.resource_limits)
    required_calls = required_attempts * cost.provider_calls
    required_wall = required_attempts * (
        cost.runner_seconds + config.limits.inter_attempt_delay_seconds
    )
    refusal = (
        "provider-call-budget"
        if required_calls > config.limits.max_provider_calls
        else "wall-time-budget"
        if required_wall > config.limits.max_wall_time_seconds
        else None
    )
    return FuzzOutput(
        status="refused" if refusal else "ready",
        error_code=refusal,
        stop_reason=refusal,
        minimum_wall_time_seconds=required_wall,
        case_path=str(profile.case_path),
        case_digest=plan.case_digest,
        configuration_digest=config.digest,
        plan_digest=plan.digest,
        capability_digest=capability.digest,
        capability_source="builtin" if capability.runner_type == "fake" else "current-detection",
        harness_started=harness_started,
        harness=capability.runner_type,
        harness_version=capability.runtime_harness_version,
        runner_version=capability.runner_version,
        model=plan.model_requested,
        provider=capability.provider,
        endpoint=capability.endpoint,
        target=plan.target_type,
        controlled_surface=capability.reconstruction_method,
        protected_state_enforcement=capability.protected_state_enforcement,
        action_visibility=capability.action_visibility,
        action_denial=capability.action_denial,
        supported_context_operations=capability.supported_context_operations,
        editable_text_sites=len(evidence.editable_text()),
        calibration_plan=config.calibration,
        screening_plan=config.screening,
        confirmation_plan=config.confirmation,
        mutators=config.mutators,
        stress_text_bytes=len(config.stale_text.encode()),
        max_candidates=config.max_candidates,
        max_simplification_trials=config.max_simplification_trials,
        limits=config.limits,
        max_attempts=bounds.planned_attempts,
        planned_provider_calls=bounds.planned_provider_calls,
        max_provider_calls_per_attempt=capability.provider_calls_per_attempt,
        confirmation_provider_calls=bounds.confirmation_provider_calls,
        max_native_output_bytes=plan.resource_limits.max_native_output_bytes,
        max_disposable_state_bytes=plan.resource_limits.max_disposable_state_bytes,
        max_disposable_state_entries=plan.resource_limits.max_disposable_state_entries,
        freshness_seconds=config.freshness_seconds,
        eligibility_policy=config.eligibility_policy,
        minimum_decrease=config.minimum_decrease,
        alpha=config.confirmation.alpha,
    )


def _allowance(profile: FuzzProfile, run_id: str) -> SamplingBudgetAllowance:
    config, plan = profile.configuration, profile.execution_plan
    campaign = config.bind(prepare_candidate(CaseWorkspace(profile.case_path).load(), plan))
    bounds = fuzz_budget_bounds(
        campaign, sampling_attempt_cost(plan.capability, plan.resource_limits)
    )
    return SamplingBudgetAllowance(
        run_id=run_id,
        max_provider_calls=config.limits.max_provider_calls,
        max_wall_time_seconds=config.limits.max_wall_time_seconds,
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
        max_candidate_comparisons=bounds.exploratory_comparisons,
        confirmation_provider_calls=bounds.confirmation_provider_calls,
        confirmation_runner_seconds=bounds.confirmation_runner_seconds,
    )


def _calibrate(
    session: FuzzSession,
    prepared: PreparedExperiment,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    progress: Callable[[str], None],
) -> str | None:
    profile = session.profile
    config = profile.configuration
    evidence = CaseWorkspace(profile.case_path).load()
    original = prepare_candidate(evidence, prepared.execution_plan)

    def refresh() -> SampledRunState:
        observations = store.list_compatible(
            prepared.identity,
            candidate_digest=original.digest,
            role=ObservationRole.CALIBRATION,
            arm=ObservationArm.BASELINE,
        )
        state = state_file.record_calibration(store, tuple(o.observation_id for o in observations))
        valid = sum(o.valid for o in observations)
        progress(calibration_status(valid, config.calibration.required_valid, len(observations)))
        return state

    cost = sampling_attempt_cost(prepared.runner.capability_profile, prepared.resource_limits)
    search = config.screening.original.max_attempts + config.screening.mutant.max_attempts
    confirmation = (
        config.confirmation.original.max_attempts + config.confirmation.mutant.max_attempts
    )

    def admit() -> None:
        state = refresh()
        delay = sampling_pacing_delay(state, store, datetime.now(UTC))
        if delay:
            time.sleep(delay)
        usage = state_file.usage(store, now=datetime.now(UTC))
        remaining = config.calibration.max_attempts - len(state.calibration_observation_ids)
        if (
            usage.charged_provider_calls + (remaining + search + confirmation) * cost.provider_calls
            > state.allowance.max_provider_calls
        ):
            raise _CalibrationBudgetStop("provider-call-budget")
        if (
            usage.wall_time_seconds
            + (remaining + search + confirmation)
            * (cost.runner_seconds + state.allowance.inter_attempt_delay_seconds)
            > state.allowance.max_wall_time_seconds
        ):
            raise _CalibrationBudgetStop("wall-time-budget")

    refresh()
    try:
        result = run_sampled_calibration(
            evidence=evidence,
            prepared=prepared,
            store=store,
            before_attempt=admit,
            eligibility_policy=config.eligibility_policy,
        )
    except _CalibrationBudgetStop as stop:
        return stop.reason
    finally:
        refresh()
    if result.outcome == "measurable":
        return None
    if result.stop_reason == "valid-sample-limit":
        return f"calibration-{result.outcome}"
    return result.stop_reason


def _report(
    session: FuzzSession,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    *,
    stop_reason: str | None,
    dry_run: bool,
) -> FuzzOutput:
    snapshot = state_file.snapshot(store, now=datetime.now(UTC))
    state, blocks, usage = snapshot.state, snapshot.blocks, snapshot.usage
    profile, progress = session.profile, state.fuzz_progress
    if state.allowance != _allowance(profile, state.run_id):
        raise ValueError("fuzz budget differs from its frozen session")
    report = report_for_profile(
        profile, harness_started=not dry_run and profile.experiment.runner.type != "fake"
    )
    holdout = next((b for b in blocks if b.plan.role == "confirmation"), None)
    confirmation = confirm_fuzz_evidence(holdout) if holdout else None
    observations = (*snapshot.calibration, *(a.observation for b in blocks for a in b.attempts))
    runner_error_category, runner_error_reason_code = last_runner_error(observations)
    unknown = bool(
        usage.unknown_provider_attempts
        or usage.possible_provider_calls
        or usage.pending_provider_calls
    )
    # An interrupted calibration reservation lives in the shared calibration
    # collector, outside block dispatch. Preserve its conservative charge.
    pending_calibration = _read_inflight(store)
    owns_calibration = (
        pending_calibration is not None and pending_calibration.identity == profile.identity
    )
    if owns_calibration:
        unknown = True
        usage = usage.model_copy(
            update={
                "pending_provider_calls": usage.pending_provider_calls
                + state.provider_calls_per_attempt,
                "pending_runner_seconds": usage.pending_runner_seconds
                + profile.execution_plan.resource_limits.max_wall_time_seconds,
            }
        )
    terminal = progress.terminal if progress else None
    status = terminal.value if terminal else "incomplete"
    if dry_run:
        status = "ready"
    elif stop_reason == "user-interrupt" and terminal is None:
        status = "interrupted"
    elif (
        stop_reason in {"calibration-not-measurable", "calibration-inconclusive"}
        and terminal is None
    ):
        status = "inconclusive"
    selected = progress.selected_mutation if progress else None
    safe_resume = (
        terminal is None
        and state.pending is None
        and not owns_calibration
        and all(
            o.allows_fresh_attempt(profile.configuration.eligibility_policy)
            for o in snapshot.calibration
        )
        and usage.wall_time_seconds < state.allowance.max_wall_time_seconds
        and stop_reason
        not in {
            "attempt-limit",
            "unsafe-retry",
            "safety-refusal",
            "valid-sample-limit",
            "calibration-not-measurable",
            "calibration-inconclusive",
        }
    )
    command, shell = (
        shell_command(
            (
                "ithrl",
                "fuzz",
                str(session.run_directory),
                "--resume",
                "--acknowledge-model-execution",
            )
        )
        if safe_resume
        else (None, None)
    )
    return FuzzOutput.model_validate(
        {
            **report.model_dump(),
            "status": status,
            "capability_source": "saved-detection"
            if dry_run and profile.experiment.runner.type != "fake"
            else report.capability_source,
            "run_state_path": str(session.run_directory),
            "usage": usage,
            "calibration": FuzzCounts.observations(snapshot.calibration),
            "confirmation_original": FuzzCounts.observations(
                tuple(a.observation for a in holdout.attempts if a.observation.arm == "baseline")
            )
            if holdout
            else FuzzCounts(),
            "confirmation_mutant": FuzzCounts.observations(
                tuple(a.observation for a in holdout.attempts if a.observation.arm == "candidate")
            )
            if holdout
            else FuzzCounts(),
            "execution_reliability": FuzzCounts.observations(observations),
            "runner_error_category": runner_error_category,
            "runner_error_reason_code": runner_error_reason_code,
            "provider_call_accounting": "started-unknown"
            if unknown
            else "accounted"
            if observations
            else "not-started",
            "provider_calls": None if unknown else usage.actual_provider_calls,
            "unknown_acceptance_attempts": sum(
                o.attempt_evidence.acceptance_unknown for o in observations
            )
            + int(state.pending is not None)
            + int(owns_calibration),
            "actions_executed": None
            if state.pending is not None
            or owns_calibration
            or any(o.attempt_evidence.accounting is None for o in observations)
            else sum(
                o.attempt_evidence.accounting.actions_executed
                for o in observations
                if o.attempt_evidence.accounting is not None
            ),
            "confirmation_result": confirmation.outcome.value if confirmation else "not-run",
            "difference": confirmation.difference if confirmation else None,
            "lower_bound": confirmation.lower_bound if confirmation else None,
            "selected_mutation_bytes": sum(len(e.text.encode()) for e in selected.edits)
            if selected
            else None,
            "selected_mutation_digest": selected.digest if selected else None,
            "proposals_examined": progress.proposals_examined if progress else 0,
            "trials_used": len(progress.trials) if progress else 0,
            "stop_reason": stop_reason
            or (progress.collection_refusal or progress.stop_reason if progress else None),
            "next_action": "The per-attempt provider call budget was exhausted. Inspect the saved evidence; a different limit requires a new run."
            if runner_error_category == "provider-call-limit"
            else "Resume the same frozen campaign within its remaining limits."
            if safe_resume
            else "Inspect the saved evidence. Final selection and completed confirmation are never repeated.",
            "next_command": None if runner_error_category == "provider-call-limit" else command,
            "next_command_shell": None if runner_error_category == "provider-call-limit" else shell,
        }
    )


def execute_fuzz(
    session: FuzzSession | None,
    prepared: PreparedExperiment | None,
    *,
    profile: FuzzProfile | None = None,
    store_path: Path | None = None,
    started_at: datetime | None = None,
    dry_run: bool = False,
) -> FuzzOutput:
    if session is None:
        if profile is None or prepared is None or store_path is None or started_at is None:
            raise ValueError("new campaign requires complete prepared inputs")
        store = ObservationStore(store_path)
        store.initialize()
        run_id = f"fuzz-{uuid4().hex}"
        state_file = SampledRunStateFile.create(
            case_root=profile.case_path,
            run_id=run_id,
            store=store,
            started_at=started_at,
            original_identity=profile.identity,
            original_candidate_digest=prepare_candidate(
                CaseWorkspace(profile.case_path).load(), profile.execution_plan
            ).digest,
            provider_calls_per_attempt=profile.execution_plan.capability.provider_calls_per_attempt,
            approval=SamplingBudgetApproval(
                approval_id=uuid4().hex,
                approved_at=started_at,
                allowance=_allowance(profile, run_id),
            ),
        )
        session = FuzzSession(
            run_id=run_id,
            run_directory=state_file.path.parent,
            observation_store=store_path,
            profile=profile,
            started_at=started_at,
        )
        with state_file.acquire_lock(store):
            save_fuzz_session(session, state_file, store)
    else:
        store = ObservationStore(session.observation_store)
        state_file = SampledRunStateFile.open(session.run_directory)
    with state_file.acquire_lock(store):
        state = state_file.load(store)
        if state.allowance != _allowance(session.profile, state.run_id):
            raise ValueError("fuzz budget differs from its frozen session")
        if dry_run:
            return _report(session, state_file, store, stop_reason=None, dry_run=True)
        if prepared is None or prepared.identity != session.profile.identity:
            raise ValueError("execution must match the frozen campaign identity")
        render_fuzz(
            report_for_profile(session.profile, harness_started=False),
            OutputFormat.HUMAN,
            target=error_console,
        )
        stop = None
        with progress_display() as progress:
            try:
                if state.fuzz_progress is None:
                    stop = _calibrate(session, prepared, state_file, store, progress)
                    if stop is None:
                        evidence = CaseWorkspace(session.profile.case_path).load()
                        _initialize_fuzz_campaign_owned(
                            evidence=evidence,
                            prepared=prepared,
                            campaign=session.profile.configuration.bind(
                                prepare_candidate(evidence, prepared.execution_plan)
                            ),
                            stale_text=session.profile.configuration.stale_text,
                            state_file=state_file,
                            store=store,
                        )
                if stop is None:
                    result = asyncio.run(
                        _run_fuzz_campaign_owned(
                            evidence=CaseWorkspace(session.profile.case_path).load(),
                            prepared=prepared,
                            state_file=state_file,
                            store=store,
                            on_progress=lambda trial, block: progress(
                                _sampling_status(trial, block)
                            ),
                        )
                    )
                    stop = result.stop_reason
            except (KeyboardInterrupt, asyncio.CancelledError):
                stop = "user-interrupt"
            except CalibrationInFlightError:
                stop = "unresolved-calibration-dispatch"
            except Exception:
                stop = "execution-refused"
        report = _report(session, state_file, store, stop_reason=stop, dry_run=False)
        try:
            result_path = publish_fuzz_result(session, state_file, store, report)
        except Exception:
            # Sampling evidence is already durable. Retain its accounting and run
            # identity even when the separate immutable receipt cannot be saved.
            command, shell = report.next_command, report.next_command_shell
            if report.status in {"confirmed-change", "inconclusive", "no-candidate-found"}:
                command, shell = shell_command(
                    (
                        "ithrl",
                        "fuzz",
                        str(session.run_directory),
                        "--resume",
                        "--acknowledge-model-execution",
                    )
                )
            return report.model_copy(
                update={
                    "status": "incomplete",
                    "error_code": "result-publication-failed",
                    "next_action": "Result receipt could not be saved. Inspect the canonical run before resuming; completed observations remain recorded.",
                    "next_command": command,
                    "next_command_shell": shell,
                }
            )
        return report.model_copy(update={"result_path": str(result_path)})
