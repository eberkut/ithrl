"""Dependency-aware deterministic and sampled reduction with exact continuation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ithrl.behavior import BehaviorTarget
from ithrl.case import CaseEvidence
from ithrl.context import ContextGraphError, content_digest
from ithrl.execution import ResolvedExecutionPlan, ResourceLimits, SafetyPolicy
from ithrl.experiment import SampledExperimentDefinition
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    sampling_schedule,
)
from ithrl.reduce.contracts import (
    ALGORITHM_DIGEST,
    MAX_CANDIDATE_RUNS,
    CandidateTrial,
    ReductionCheckpoint,
    ReductionError,
    ReductionResult,
    ResumeMismatchError,
    SampledCandidateTrial,
    SampledComparison,
    SampledInvalidCandidateTrial,
    SampledReductionCheckpoint,
    SampledTrial,
    SearchPhase,
    trial_key,
    validate_checkpoint_identity,
)
from ithrl.reduce.search import run_search
from ithrl.runner import Candidate, PreparedAttempt, Runner, prepare_candidate

if TYPE_CHECKING:
    from ithrl.run_state import SampledRunState, SampledRunStateFile, SampledRunUsage
    from ithrl.workflow import PreparedExperiment


@dataclass(slots=True)
class _Budget:
    maximum: int | None
    used: int = 0

    def admit(self) -> bool:
        if self.maximum is not None and self.used >= self.maximum:
            return False
        self.used += 1
        return True

    def release(self) -> None:
        """Return capacity when pre-existing evidence prevents an attempt."""

        if self.used == 0:
            raise RuntimeError("cannot release unused candidate capacity")
        self.used -= 1


class DeterministicReducer:
    """Delete groups, then test each valid single-group removal."""

    def __init__(
        self,
        *,
        runner: Runner,
        target: BehaviorTarget,
        execution_plan: ResolvedExecutionPlan,
        safety_policy: SafetyPolicy,
        resource_limits: ResourceLimits,
        identity: ObservationIdentity,
        observation_store: ObservationStore,
        max_candidate_runs: int | None = None,
        resume: ReductionCheckpoint | None = None,
        on_checkpoint: Callable[[ReductionCheckpoint], None] | None = None,
    ) -> None:
        if max_candidate_runs is not None and not 1 <= max_candidate_runs <= MAX_CANDIDATE_RUNS:
            raise ValueError(f"max_candidate_runs must be between 1 and {MAX_CANDIDATE_RUNS}")
        if identity.algorithm_digest != ALGORITHM_DIGEST:
            raise ValueError("observation identity uses another reduction algorithm")
        expected_identity = (
            execution_plan.case_digest,
            execution_plan.experiment_digest,
            execution_plan.runner_fingerprint,
            execution_plan.digest,
            execution_plan.target_digest,
        )
        actual_identity = (
            identity.case_digest,
            identity.experiment_digest,
            identity.runner_fingerprint,
            identity.execution_plan_digest,
            identity.target_digest,
        )
        if actual_identity != expected_identity:
            raise ValueError("observation identity does not match the execution plan")
        self._runner = runner
        self._target = target
        self._execution_plan = execution_plan
        self._safety_policy = safety_policy
        self._resource_limits = resource_limits
        self._identity = identity
        self._store = observation_store
        self._budget = _Budget(maximum=max_candidate_runs)
        self._resume = resume
        self._failed_candidate_observation_ids = list(
            resume.failed_candidate_observation_ids if resume is not None else ()
        )
        self._on_checkpoint = on_checkpoint
        self._last_checkpoint = resume
        self._resume_position_verified = resume is None
        self._resume_validated = resume is None

    @property
    def checkpoint(self) -> ReductionCheckpoint:
        """Return the latest continuation state."""

        if self._last_checkpoint is None:
            return self._make_checkpoint((), (), False)
        return self._last_checkpoint

    async def reduce(self, evidence: CaseEvidence) -> ReductionResult:
        """Run or continue the deterministic search."""

        await self.validate_resume(evidence)
        removed, trials, final_sweep_complete = await self._run_search(evidence, publish=True)

        checkpoint = self._publish_checkpoint(removed, trials, final_sweep_complete)
        reduced_request = evidence.materialize(removed) if evidence.has_request else None
        retained = tuple(node.id for node in evidence.context.nodes if node.id not in removed)
        return ReductionResult(
            status="search-1-minimal" if final_sweep_complete else "search-incomplete",
            case_digest=evidence.digest,
            experiment_digest=self._identity.experiment_digest,
            original_request=evidence.request if evidence.has_request else None,
            reduced_request=reduced_request,
            removed_context_ids=tuple(sorted(removed)),
            retained_context_ids=retained,
            trials=tuple(trials),
            candidate_runs_used=self._budget.used,
            final_sweep_complete=final_sweep_complete,
            checkpoint=checkpoint,
        )

    async def validate_resume(self, evidence: CaseEvidence) -> None:
        """Validate all saved search evidence without executing the runner."""

        if self._resume_validated:
            return
        self._validate_resume(evidence)
        if self._resume is None:
            self._resume_validated = True
            return

        requested_limit = self._budget.maximum
        self._budget.maximum = self._resume.candidate_runs_used
        try:
            _, _, validation_complete = await self._run_search(evidence, publish=False)
            if self._resume.final_sweep_complete and not validation_complete:
                raise ResumeMismatchError(
                    "checkpoint claims a complete final sweep without all required trials"
                )
        finally:
            self._budget.maximum = requested_limit
            self._budget.used = self._resume.candidate_runs_used
            self._last_checkpoint = self._resume
            self._resume_position_verified = False
        self._resume_validated = True

    async def _run_search(
        self, evidence: CaseEvidence, *, publish: bool
    ) -> tuple[frozenset[str], list[CandidateTrial], bool]:
        prior_trials = self._prior_trials()
        outcome = await run_search(
            evidence,
            removable=[
                node.id for node in evidence.context.nodes if node.mutable and not node.protected
            ],
            classify=self._classify,
            prior_trials=prior_trials,
            publish=publish,
            publish_checkpoint=self._publish_checkpoint,
        )

        if prior_trials:
            raise ResumeMismatchError(
                "checkpoint contains trials outside the deterministic search path"
            )
        self._verify_resume_position(outcome.removed)
        return outcome.removed, outcome.trials, outcome.complete

    async def _classify(
        self,
        *,
        evidence: CaseEvidence,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        phase: SearchPhase,
        prior_trials: deque[CandidateTrial],
        completed_trials: Sequence[CandidateTrial],
        publish: bool,
    ) -> CandidateTrial | None:
        self._verify_resume_position(base_removals, pending_trials=prior_trials)
        key = trial_key(phase, base_removals, tuple(requested_removals))

        try:
            effective = evidence.context.dependency_closure((*base_removals, *requested_removals))
        except ContextGraphError as error:
            invalid = CandidateTrial(
                trial_key=key,
                candidate_digest=None,
                base_removals=tuple(sorted(base_removals)),
                requested_removals=tuple(requested_removals),
                effective_removals=tuple(sorted(base_removals)),
                target_observed=None,
                classification="invalid",
                phase=phase,
                runner_executed=False,
            )
            prior = _take_prior_trial(prior_trials, key)
            if prior is not None:
                if prior.model_copy(update={"reused": False}) != invalid:
                    raise ResumeMismatchError(
                        "checkpoint invalid-trial evidence does not match the context graph"
                    ) from error
                return prior.model_copy(update={"reused": True})
            return invalid

        candidate = prepare_candidate(evidence, self._execution_plan, effective)
        candidate_digest = candidate.digest
        prior = _take_prior_trial(prior_trials, key)
        if prior is not None:
            self._validate_prior_trial(
                prior,
                phase=phase,
                base_removals=base_removals,
                requested_removals=requested_removals,
                effective_removals=effective,
                candidate_digest=candidate_digest,
            )
            return prior.model_copy(update={"reused": True})
        if not self._budget.admit():
            return None
        return await self._observe_candidate(
            evidence=evidence,
            candidate=candidate,
            candidate_digest=candidate_digest,
            key=key,
            phase=phase,
            base_removals=base_removals,
            requested_removals=requested_removals,
            effective_removals=effective,
            completed_trials=completed_trials,
            publish=publish,
        )

    async def _observe_candidate(
        self,
        *,
        evidence: CaseEvidence,
        candidate: Candidate,
        candidate_digest: str,
        key: str,
        phase: SearchPhase,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        effective_removals: frozenset[str],
        completed_trials: Sequence[CandidateTrial],
        publish: bool,
    ) -> CandidateTrial:
        try:
            observation = await observe_once(
                runner=self._runner,
                identity=self._identity,
                store=self._store,
                attempt=PreparedAttempt(
                    evidence=evidence,
                    candidate=candidate,
                    target=self._target,
                    execution_plan=self._execution_plan,
                    safety_policy=self._safety_policy,
                    resource_limits=self._resource_limits,
                    phase="search",
                    arm="candidate",
                ),
            )
        except RunnerExecutionError as error:
            if not error.attempt_started:
                self._budget.release()
                raise
            if error.observation is None:
                raise
            self._failed_candidate_observation_ids.append(error.observation.observation_id)
            if publish:
                self._publish_checkpoint(base_removals, completed_trials, False)
            raise
        target_observed = observation.target_observed
        if target_observed is None:
            raise ReductionError("search observation has no structural verdict")
        return CandidateTrial(
            trial_key=key,
            candidate_digest=candidate_digest,
            base_removals=tuple(sorted(base_removals)),
            requested_removals=tuple(requested_removals),
            effective_removals=tuple(sorted(effective_removals)),
            target_observed=target_observed,
            classification="preserves" if target_observed else "loses",
            phase=phase,
            runner_executed=True,
            observation_id=observation.observation_id,
            observation_attempt=observation.attempt,
        )

    def _validate_prior_trial(
        self,
        prior: CandidateTrial,
        *,
        phase: SearchPhase,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        effective_removals: frozenset[str],
        candidate_digest: str,
    ) -> None:
        expected = (
            phase,
            tuple(sorted(base_removals)),
            tuple(requested_removals),
            tuple(sorted(effective_removals)),
            candidate_digest,
        )
        actual = (
            prior.phase,
            prior.base_removals,
            prior.requested_removals,
            prior.effective_removals,
            prior.candidate_digest,
        )
        if actual != expected:
            raise ResumeMismatchError(
                "checkpoint trial does not match deterministic candidate construction"
            )
        observations = self._store.list_compatible(
            self._identity,
            candidate_digest=candidate_digest,
            role=ObservationRole.SEARCH,
            arm=ObservationArm.CANDIDATE,
        )
        matching = [
            item
            for item in observations
            if item.observation_id == prior.observation_id
            and item.attempt == prior.observation_attempt
        ]
        if len(matching) != 1 or matching[0].target_observed != prior.target_observed:
            raise ResumeMismatchError(
                "checkpoint trial is not backed by its append-only observation"
            )

    def _verify_resume_position(
        self,
        removed: Iterable[str],
        *,
        pending_trials: deque[CandidateTrial] | None = None,
    ) -> None:
        if self._resume_position_verified or self._resume is None:
            return
        if pending_trials:
            return
        if tuple(sorted(removed)) != self._resume.removed_context_ids:
            raise ResumeMismatchError(
                "checkpoint best candidate does not match its completed trial path"
            )
        self._resume_position_verified = True

    def _validate_resume(self, evidence: CaseEvidence) -> None:
        if self._resume is None:
            return
        validate_checkpoint_identity(
            self._resume,
            case_digest=evidence.digest,
            identity=self._identity,
        )
        observations = {
            observation.observation_id: observation
            for observation in self._store.list_for_experiment(self._identity.experiment_digest)
        }
        for observation_id in self._resume.failed_candidate_observation_ids:
            observation = observations.get(observation_id)
            if (
                observation is None
                or observation.identity != self._identity
                or observation.role is not ObservationRole.SEARCH
                or observation.arm is not ObservationArm.CANDIDATE
                or observation.outcome is not ObservationOutcome.RUNNER_ERROR
            ):
                raise ResumeMismatchError(
                    f"failed candidate observation {observation_id!r} is unavailable or incompatible"
                )

    def _prior_trials(self) -> deque[CandidateTrial]:
        if self._resume is None:
            return deque()
        self._budget.used = self._resume.candidate_runs_used
        return deque(self._resume.trials)

    def _make_checkpoint(
        self,
        removed: Iterable[str],
        trials: Iterable[CandidateTrial],
        final_sweep_complete: bool,
    ) -> ReductionCheckpoint:
        return ReductionCheckpoint(
            case_digest=self._identity.case_digest,
            experiment_digest=self._identity.experiment_digest,
            runner_fingerprint=self._identity.runner_fingerprint,
            execution_plan_digest=self._identity.execution_plan_digest,
            target_digest=self._identity.target_digest,
            removed_context_ids=tuple(sorted(removed)),
            trials=tuple(trials),
            failed_candidate_observation_ids=tuple(self._failed_candidate_observation_ids),
            candidate_runs_used=self._budget.used,
            final_sweep_complete=final_sweep_complete,
        )

    def _publish_checkpoint(
        self,
        removed: Iterable[str],
        trials: Iterable[CandidateTrial],
        final_sweep_complete: bool,
    ) -> ReductionCheckpoint:
        checkpoint = self._make_checkpoint(removed, trials, final_sweep_complete)
        self._last_checkpoint = checkpoint
        if self._on_checkpoint is not None:
            self._on_checkpoint(checkpoint)
        return checkpoint


def _take_prior_trial(
    prior_trials: deque[CandidateTrial], expected_key: str
) -> CandidateTrial | None:
    if not prior_trials:
        return None
    prior = prior_trials[0]
    if prior.trial_key != expected_key:
        raise ResumeMismatchError("checkpoint trials do not follow the deterministic search order")
    return prior_trials.popleft()


@dataclass(frozen=True, slots=True)
class SampledSearchResult:
    """Exploratory search progress; fresh statistical confirmation is a separate phase."""

    checkpoint: SampledReductionCheckpoint
    usage: SampledRunUsage
    stop_reason: str | None

    @property
    def status(self) -> Literal["search-reduced", "search-incomplete"]:
        return "search-reduced" if self.checkpoint.final_sweep_complete else "search-incomplete"


class SampledReducer:
    """Apply completed exploratory screens through the existing candidate generator."""

    def __init__(
        self,
        *,
        prepared: PreparedExperiment,
        state_file: SampledRunStateFile,
        observation_store: ObservationStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        verify_independence: Callable[[Observation], str | None] | None = None,
    ) -> None:
        experiment = prepared.experiment
        if not isinstance(experiment, SampledExperimentDefinition) or experiment.reduction is None:
            raise ValueError("sampled reduction requires a frozen reduction plan")
        if prepared.runner.capability_profile.deterministic:
            raise ValueError("sampled reduction requires a nondeterministic runner")
        self._prepared = prepared
        self._experiment = experiment
        self._reduction = experiment.reduction
        self._state_file = state_file
        self._store = observation_store
        self._clock = clock
        self._verify_independence = verify_independence
        self._state: SampledRunState | None = None
        self._resume: SampledReductionCheckpoint | None = None
        self._unclassified: SamplingBlockEvidence | None = None
        self._active: SamplingBlockEvidence | None = None
        self._stop_reason: str | None = None

    async def validate_resume(self, evidence: CaseEvidence) -> None:
        """Validate authoritative membership and generator order without runner execution."""

        with self._state_file.acquire_lock(self._store):
            await self._load_and_validate(evidence)

    async def reduce(self, evidence: CaseEvidence) -> SampledSearchResult:
        """Continue the same sampled run, preserving both evidence and confirmation capacity."""

        with self._state_file.acquire_lock(self._store):
            await self._load_and_validate(evidence)
            self._archive_classified_block(self._loaded_state, self._resume)
            self._stop_reason = None
            self._active = None
            outcome = await self._run_search(evidence, publish=True)
            checkpoint = self._publish_checkpoint(
                outcome.removed, outcome.trials, outcome.complete, evidence=evidence
            )
            return SampledSearchResult(
                checkpoint,
                self._state_file.usage(self._store, now=self._clock()),
                self._stop_reason,
            )

    async def _load_and_validate(self, evidence: CaseEvidence) -> None:
        state = self._state_file.load(self._store)
        prepared = self._prepared
        original = prepare_candidate(evidence, prepared.execution_plan)
        if (
            state.original_identity != prepared.identity
            or prepared.identity.case_digest != evidence.digest
            or state.original_candidate_digest != original.digest
            or prepared.execution_plan.capability != prepared.runner.capability_profile
            or prepared.identity.runner_fingerprint != prepared.runner.fingerprint
            or prepared.identity.execution_plan_digest != prepared.execution_plan.digest
            or prepared.identity.experiment_digest != prepared.experiment.digest
            or prepared.identity.target_digest != prepared.experiment.target.digest
        ):
            raise ResumeMismatchError("sampled run does not match the prepared original")
        self._validate_calibration(state)
        blocks = self._state_file.evidence(self._store)
        if any(block.plan.role != "search" for block in blocks):
            raise ResumeMismatchError("search cannot continue after confirmation starts")
        checkpoint = state.checkpoint
        published = (
            ()
            if checkpoint is None
            else tuple(
                trial.comparison.evidence
                for trial in checkpoint.trials
                if isinstance(trial, SampledCandidateTrial)
            )
        )
        if blocks[: len(published)] != published or len(blocks) > len(published) + 1:
            raise ResumeMismatchError("checkpoint omits or reorders authoritative search blocks")
        if checkpoint is not None and checkpoint.reduction != self._reduction:
            raise ResumeMismatchError("checkpoint uses another frozen reduction plan")
        self._state = state
        self._resume = checkpoint
        self._active = None
        self._unclassified = blocks[-1] if len(blocks) > len(published) else None
        if self._unclassified is not None and state.blocks[-1].archived:
            raise ResumeMismatchError("unclassified search blocks cannot be archived or redrawn")
        outcome = await self._run_search(evidence, publish=False)
        if checkpoint is not None:
            if checkpoint.final_sweep_complete and self._unclassified is not None:
                raise ResumeMismatchError("completed search contains an unclassified block")
            expected = self._make_checkpoint(
                outcome.removed, outcome.trials, outcome.complete, evidence=evidence
            )
            if (
                checkpoint.removed_context_ids != expected.removed_context_ids
                or checkpoint.best_candidate_digest != expected.best_candidate_digest
                or (
                    checkpoint.final_sweep_complete
                    and (
                        not outcome.complete
                        or checkpoint.final_sweep_trial_keys != expected.final_sweep_trial_keys
                        or checkpoint.expected_final_neighbors != expected.expected_final_neighbors
                    )
                )
            ):
                raise ResumeMismatchError(
                    "checkpoint selection or final sweep differs from the generator"
                )

    def _validate_calibration(self, state: SampledRunState) -> None:
        phase = self._experiment.calibration
        observations = self._store.get_many(state.calibration_observation_ids)
        compatible = self._store.list_compatible(
            state.original_identity,
            candidate_digest=state.original_candidate_digest,
            role=ObservationRole.CALIBRATION,
            arm=ObservationArm.BASELINE,
        )
        if observations != compatible:
            raise ReductionError("calibration references omit authoritative observations")
        if (
            not observations
            or len(observations) > phase.max_attempts
            or sum(item.valid for item in observations) != phase.required_valid
            or sum(item.outcome is ObservationOutcome.MATCH for item in observations)
            < phase.minimum_matches
            or any(not item.attempt_evidence.automatic_retry_safe for item in observations)
        ):
            raise ReductionError("sampled search requires successful safe full-context calibration")
        valid = 0
        for index, item in enumerate(observations, start=1):
            if item.attempt != index or valid >= phase.required_valid:
                raise ReductionError(
                    "calibration references do not form the frozen complete sequence"
                )
            valid += item.valid

    async def _run_search(self, evidence: CaseEvidence, *, publish: bool):
        prior = deque(self._resume.trials if self._resume is not None else ())
        outcome = await run_search(
            evidence,
            removable=[
                node.id for node in evidence.context.nodes if node.mutable and not node.protected
            ],
            classify=self._classify,
            prior_trials=prior,
            publish=publish,
            publish_checkpoint=lambda removed, trials, complete: self._publish_checkpoint(
                removed, trials, complete, evidence=evidence
            ),
        )
        if prior:
            raise ResumeMismatchError("sampled trials do not follow the existing candidate order")
        return outcome

    async def _classify(
        self,
        *,
        evidence: CaseEvidence,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        phase: SearchPhase,
        prior_trials: deque[SampledTrial],
        completed_trials: Sequence[SampledTrial],
        publish: bool,
    ) -> SampledTrial | None:
        key = trial_key(phase, base_removals, tuple(requested_removals))
        prior = prior_trials.popleft() if prior_trials else None
        if prior is not None and prior.trial_key != key:
            raise ResumeMismatchError("sampled trials do not follow the existing candidate order")
        base = tuple(sorted(base_removals))
        requested = tuple(requested_removals)
        if prior is None and publish and len(completed_trials) >= MAX_CANDIDATE_RUNS:
            self._stop_reason = "trial-limit"
            return None
        if prior is None and publish:
            usage = self._state_file.usage(self._store, now=self._clock())
            if usage.wall_time_seconds > self._loaded_state.allowance.max_wall_time_seconds:
                self._stop_reason = "wall-time-exhausted"
                return None
        try:
            effective = evidence.context.dependency_closure((*base_removals, *requested))
        except ContextGraphError:
            invalid = SampledInvalidCandidateTrial(
                trial_key=key,
                base_removals=base,
                requested_removals=requested,
                effective_removals=base,
                phase=phase,
                classification="invalid",
            )
            if prior is not None:
                if prior != invalid:
                    raise ResumeMismatchError(
                        "invalid trial does not match the context graph"
                    ) from None
                return prior
            if not publish:
                if self._unclassified is not None:
                    raise ResumeMismatchError(
                        "sampling block belongs to an invalid graph candidate"
                    ) from None
                return None
            return invalid
        candidate = prepare_candidate(evidence, self._prepared.execution_plan, effective)
        if prior is not None:
            if (
                not isinstance(prior, SampledCandidateTrial)
                or prior.base_removals != base
                or prior.requested_removals != requested
                or prior.effective_removals != tuple(sorted(effective))
                or prior.phase != phase
                or prior.comparison.evidence.plan.candidate_digest != candidate.digest
            ):
                raise ResumeMismatchError(
                    "sampled trial differs from dependency-valid construction"
                )
            self._validate_plan(prior.comparison.evidence.plan, key, candidate.digest)
            return prior
        plan = (
            self._unclassified.plan
            if self._unclassified is not None
            else self._plan(key, candidate.digest)
        )
        self._validate_plan(plan, key, candidate.digest)
        if not publish:
            return None
        # State imports reduction contracts through the public package. Defer the
        # collector import until composition to keep that dependency acyclic.
        from ithrl.sampling import _collect_sampling_block_owned

        result = await _collect_sampling_block_owned(
            evidence=evidence,
            original=self._prepared,
            candidate=self._prepared,
            removed_context_ids=tuple(sorted(effective)),
            plan=plan,
            state_file=self._state_file,
            store=self._store,
            clock=self._clock,
            verify_independence=self._verify_independence,
        )
        self._stop_reason = result.stop_reason
        self._unclassified = None
        if not result.complete and (not result.evidence.complete or result.evidence.eligible):
            state = self._state_file.load(self._store)
            if (
                state.blocks
                and state.blocks[-1].plan.digest == plan.digest
                and not result.evidence.complete
            ):
                self._active = result.evidence
            return None
        return SampledCandidateTrial(
            trial_key=key,
            base_removals=base,
            requested_removals=requested,
            effective_removals=tuple(sorted(effective)),
            phase=phase,
            comparison=SampledComparison.from_evidence(result.evidence),
        )

    def _plan(self, key: str, candidate_digest: str) -> SamplingBlockPlan:
        state = self._loaded_state
        seed = int(content_digest({"run_id": state.run_id, "trial_key": key})[:16], 16)
        search = self._reduction.search
        return SamplingBlockPlan(
            block_id=f"search-{key}",
            role="search",
            reduction=self._reduction,
            original_identity=state.original_identity,
            candidate_identity=self._prepared.identity,
            original_candidate_digest=state.original_candidate_digest,
            candidate_digest=candidate_digest,
            frozen_at=self._clock(),
            schedule_seed=seed,
            schedule=sampling_schedule(
                search.original.max_attempts, search.candidate.max_attempts, seed
            ),
            provider_calls_per_attempt=state.provider_calls_per_attempt,
        )

    def _validate_plan(self, plan: SamplingBlockPlan, key: str, candidate_digest: str) -> None:
        state = self._loaded_state
        seed = int(content_digest({"run_id": state.run_id, "trial_key": key})[:16], 16)
        if (
            plan.block_id != f"search-{key}"
            or plan.role != "search"
            or plan.reduction != self._reduction
            or plan.original_identity != state.original_identity
            or plan.candidate_identity != self._prepared.identity
            or plan.original_candidate_digest != state.original_candidate_digest
            or plan.candidate_digest != candidate_digest
            or plan.schedule_seed != seed
            or plan.provider_calls_per_attempt != state.provider_calls_per_attempt
        ):
            raise ResumeMismatchError(
                "sampling block does not bind the generated trial and immutable original"
            )

    def _make_checkpoint(
        self,
        removed: Iterable[str],
        trials: Iterable[SampledTrial],
        final_sweep_complete: bool,
        *,
        evidence: CaseEvidence,
    ) -> SampledReductionCheckpoint:
        state = self._loaded_state
        removed_ids = tuple(sorted(removed))
        trials = tuple(trials)
        final_trials = (
            tuple(
                trial.trial_key
                for trial in trials
                if trial.phase == "final-sweep" and trial.base_removals == removed_ids
            )
            if final_sweep_complete
            else ()
        )
        neighbors = (
            tuple(
                (node.id,)
                for node in evidence.context.nodes
                if node.mutable and not node.protected and node.id not in removed_ids
            )
            if final_sweep_complete
            else ()
        )
        return SampledReductionCheckpoint(
            reduction=self._reduction,
            original_identity=state.original_identity,
            original_candidate_digest=state.original_candidate_digest,
            best_candidate_digest=prepare_candidate(
                evidence, self._prepared.execution_plan, removed_ids
            ).digest,
            removed_context_ids=removed_ids,
            trials=trials,
            active_block=self._active,
            final_sweep_complete=final_sweep_complete,
            final_sweep_trial_keys=final_trials,
            expected_final_neighbors=neighbors,
        )

    def _publish_checkpoint(
        self,
        removed: Iterable[str],
        trials: Iterable[SampledTrial],
        final_sweep_complete: bool,
        *,
        evidence: CaseEvidence,
    ) -> SampledReductionCheckpoint:
        checkpoint = self._make_checkpoint(removed, trials, final_sweep_complete, evidence=evidence)
        state = self._state_file.load(self._store)
        if state.pending is None:
            self._state_file.save_checkpoint(self._store, checkpoint)
            self._archive_classified_block(state, checkpoint)
        return checkpoint

    def _archive_classified_block(
        self, state: SampledRunState, checkpoint: SampledReductionCheckpoint | None
    ) -> None:
        """Recover a saved classification whose separate archival was interrupted."""

        if (
            checkpoint is not None
            and state.pending is None
            and state.blocks
            and not state.blocks[-1].archived
            and any(
                isinstance(trial, SampledCandidateTrial)
                and trial.comparison.evidence.plan.digest == state.blocks[-1].plan.digest
                and trial.allows_search_continuation
                for trial in checkpoint.trials
            )
        ):
            self._state = self._state_file.archive_block(self._store)

    @property
    def _loaded_state(self) -> SampledRunState:
        if self._state is None:
            raise RuntimeError("sampled reducer has not loaded owned state")
        return self._state
