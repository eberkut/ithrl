"""Deterministic chunk search and final single-removal sweep."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ithrl.case import CaseEvidence
from ithrl.reduce.contracts import CandidateTrial, ReductionCheckpoint, SearchPhase


class _Classifier(Protocol):
    async def __call__(
        self,
        *,
        evidence: CaseEvidence,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        phase: SearchPhase,
        prior_trials: deque[CandidateTrial],
        completed_trials: Sequence[CandidateTrial],
        publish: bool,
    ) -> CandidateTrial | None: ...


type _CheckpointPublisher = Callable[
    [Iterable[str], Iterable[CandidateTrial], bool], ReductionCheckpoint
]


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    removed: frozenset[str]
    trials: list[CandidateTrial]
    complete: bool


@dataclass(slots=True)
class _Search:
    evidence: CaseEvidence
    removable: list[str]
    classify: _Classifier
    prior_trials: deque[CandidateTrial]
    publish: bool
    publish_checkpoint: _CheckpointPublisher
    trials: list[CandidateTrial]

    async def run(self) -> _SearchOutcome:
        removed, chunk_complete = await self._run_chunks()
        if not chunk_complete:
            return _SearchOutcome(removed, self.trials, False)
        removed, final_complete = await self._run_final_sweep(removed)
        return _SearchOutcome(removed, self.trials, final_complete)

    async def _run_chunks(self) -> tuple[frozenset[str], bool]:
        removed: frozenset[str] = frozenset()
        granularity = min(2, len(self.removable))
        while self.removable and granularity:
            removed, reduced, complete = await self._try_chunks(removed, granularity)
            if not complete:
                return removed, False
            if reduced:
                granularity = min(max(granularity - 1, 2), len(self.removable))
                continue
            if granularity >= len(self.removable):
                break
            granularity = min(len(self.removable), granularity * 2)
        return removed, True

    async def _try_chunks(
        self, removed: frozenset[str], granularity: int
    ) -> tuple[frozenset[str], bool, bool]:
        for chunk in _partition(self.removable, granularity):
            trial = await self._run_trial(removed, chunk, "chunk")
            if trial is None:
                return removed, False, False
            if trial.classification == "preserves":
                removed = frozenset(trial.effective_removals)
                self._remove_effective(removed)
                return removed, True, True
        return removed, False, True

    async def _run_final_sweep(self, removed: frozenset[str]) -> tuple[frozenset[str], bool]:
        restart = True
        while restart:
            restart = False
            for node_id in tuple(self.removable):
                trial = await self._run_trial(removed, (node_id,), "final-sweep")
                if trial is None:
                    return removed, False
                if trial.classification == "preserves":
                    removed = frozenset(trial.effective_removals)
                    self._remove_effective(removed)
                    restart = True
                    break
        return removed, True

    async def _run_trial(
        self,
        removed: frozenset[str],
        requested: Sequence[str],
        phase: SearchPhase,
    ) -> CandidateTrial | None:
        trial = await self.classify(
            evidence=self.evidence,
            base_removals=removed,
            requested_removals=requested,
            phase=phase,
            prior_trials=self.prior_trials,
            completed_trials=self.trials,
            publish=self.publish,
        )
        if trial is None:
            return None
        self.trials.append(trial)
        if self.publish:
            checkpoint_removed = (
                trial.effective_removals if trial.classification == "preserves" else removed
            )
            self.publish_checkpoint(checkpoint_removed, self.trials, False)
        return trial

    def _remove_effective(self, removed: frozenset[str]) -> None:
        self.removable = [item for item in self.removable if item not in removed]


async def run_search(
    evidence: CaseEvidence,
    *,
    removable: list[str],
    classify: _Classifier,
    prior_trials: deque[CandidateTrial],
    publish: bool,
    publish_checkpoint: _CheckpointPublisher,
) -> _SearchOutcome:
    """Run chunk reduction followed by the single-removal proof sweep."""

    search = _Search(
        evidence=evidence,
        removable=removable,
        classify=classify,
        prior_trials=prior_trials,
        publish=publish,
        publish_checkpoint=publish_checkpoint,
        trials=[],
    )
    return await search.run()


def _partition(items: Sequence[str], count: int) -> Iterable[tuple[str, ...]]:
    if count < 1:
        raise ValueError("partition count must be at least one")
    size, remainder = divmod(len(items), count)
    start = 0
    for index in range(count):
        width = size + (1 if index < remainder else 0)
        if width:
            yield tuple(items[start : start + width])
        start += width
