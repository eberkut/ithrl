"""Credential-free deterministic change evaluation against one immutable original."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ithrl.behavior import evaluate_target
from ithrl.case import CaseEvidence
from ithrl.fuzz.simplify import MutationDecision
from ithrl.measurement import normalize_run_result
from ithrl.mutation import MutationError, TextMutation
from ithrl.runner import Candidate, PreparedAttempt, prepare_candidate, validate_run_result

if TYPE_CHECKING:
    from ithrl.workflow import PreparedExperiment


@dataclass(slots=True)
class DeterministicMutationEvaluator:
    evidence: CaseEvidence = field(repr=False)
    prepared: PreparedExperiment = field(repr=False)
    attempts: int = field(default=0, init=False)
    _baseline: bool = field(default=False, init=False, repr=False)
    _cache: dict[str, MutationDecision] = field(default_factory=dict, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        capability = self.prepared.execution_plan.capability
        if (
            not capability.deterministic
            or capability.provider_transport != "forbidden"
            or capability.provider_calls_per_attempt != 0
            or capability.disposable_state_effect != "none"
            or self.prepared.safety_policy.provider_transport != "forbidden"
        ):
            raise ValueError("deterministic mutation evaluation requires a credential-free runner")

    async def _observe(self, candidate: Candidate, *, baseline: bool = False) -> bool:
        plan = self.prepared.execution_plan
        if (
            self.prepared.runner.fingerprint != plan.runner_fingerprint
            or self.prepared.runner.capability_profile != plan.capability
        ):
            raise ValueError("runner identity changed after experiment preparation")
        attempt = PreparedAttempt(
            evidence=self.evidence,
            candidate=candidate,
            target=self.prepared.experiment.target,
            execution_plan=self.prepared.execution_plan,
            safety_policy=self.prepared.safety_policy,
            resource_limits=self.prepared.resource_limits,
            phase="calibration" if baseline else "search",
            arm="baseline" if baseline else "candidate",
        )
        self.attempts += 1
        async with asyncio.timeout(attempt.resource_limits.max_wall_time_seconds):
            result = await self.prepared.runner.run(attempt)
        validate_run_result(attempt, result)
        return evaluate_target(self.prepared.experiment.target, normalize_run_result(result))

    async def evaluate(self, mutation: TextMutation) -> MutationDecision:
        async with self._lock:
            return await self._evaluate_owned(mutation)

    async def _evaluate_owned(self, mutation: TextMutation) -> MutationDecision:
        previous = self._cache.get(mutation.digest)
        if previous is not None:
            return previous
        try:
            candidate = prepare_candidate(
                self.evidence, self.prepared.execution_plan, mutation=mutation
            )
        except MutationError:
            self._cache[mutation.digest] = MutationDecision.INVALID
            return MutationDecision.INVALID
        if not self._baseline:
            original = prepare_candidate(self.evidence, self.prepared.execution_plan)
            if not await self._observe(original, baseline=True):
                raise ValueError("the unchanged original does not emit the declared target")
            self._baseline = True
        if mutation.is_empty:
            return MutationDecision.UNCHANGED
        target_observed = await self._observe(candidate)
        decision = MutationDecision.UNCHANGED if target_observed else MutationDecision.CHANGED
        self._cache[mutation.digest] = decision
        return decision
