"""Budgeted deterministic reduction and exact checkpoint continuation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ithrl.behavior import BehaviorTarget
from ithrl.case import CaseEvidence
from ithrl.context import ContextGraphError, JsonObject, content_digest
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.reduce.contracts import (
    ALGORITHM_DIGEST,
    MAX_CANDIDATE_RUNS,
    CandidateTrial,
    ReductionCheckpoint,
    ReductionError,
    ReductionResult,
    ResumeMismatchError,
    SearchPhase,
    trial_key,
    validate_checkpoint_identity,
)
from ithrl.reduce.search import run_search
from ithrl.runner import Runner


@dataclass(slots=True)
class _Budget:
    maximum: int | None
    used: int = 0

    def admit(self) -> bool:
        if self.maximum is not None and self.used >= self.maximum:
            return False
        self.used += 1
        return True


class DeterministicReducer:
    """Delete groups, then test each valid single-group removal."""

    def __init__(
        self,
        *,
        runner: Runner,
        target: BehaviorTarget,
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
        self._runner = runner
        self._target = target
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
        reduced_request = evidence.materialize(removed)
        retained = tuple(node.id for node in evidence.context.nodes if node.id not in removed)
        return ReductionResult(
            status="search-1-minimal" if final_sweep_complete else "search-incomplete",
            case_digest=evidence.digest,
            experiment_digest=self._identity.experiment_digest,
            original_request=evidence.request,
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

        request = evidence.materialize(effective)
        candidate_digest = content_digest(request)
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
            request=request,
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
        request: JsonObject,
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
                target=self._target,
                identity=self._identity,
                store=self._store,
                request=request,
                role=ObservationRole.SEARCH,
                arm=ObservationArm.CANDIDATE,
            )
        except RunnerExecutionError as error:
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
