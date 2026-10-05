"""Shared candidate order and final single-removal sweep."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ithrl.case import CaseEvidence
from ithrl.reduce.contracts import SearchPhase, trial_key


class _Trial(Protocol):
    @property
    def trial_key(self) -> str: ...

    @property
    def effective_removals(self) -> tuple[str, ...]: ...

    @property
    def accepts_removal(self) -> bool: ...

    @property
    def allows_search_continuation(self) -> bool: ...


class _Classifier[Trial: _Trial](Protocol):
    async def __call__(
        self,
        *,
        evidence: CaseEvidence,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        phase: SearchPhase,
        prior_trials: deque[Trial],
        completed_trials: Sequence[Trial],
        publish: bool,
    ) -> Trial | None: ...


type _CheckpointPublisher[Trial: _Trial] = Callable[[Iterable[str], Iterable[Trial], bool], object]


@dataclass(frozen=True, slots=True)
class _SearchOutcome[Trial: _Trial]:
    removed: frozenset[str]
    trials: list[Trial]
    complete: bool


@dataclass(slots=True)
class _Search[Trial: _Trial]:
    evidence: CaseEvidence
    removable: list[str]
    classify: _Classifier[Trial]
    prior_trials: deque[Trial]
    publish: bool
    publish_checkpoint: _CheckpointPublisher[Trial]
    trials: list[Trial]

    async def run(self) -> _SearchOutcome[Trial]:
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
            if trial.accepts_removal:
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
                if trial.accepts_removal:
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
    ) -> Trial | None:
        key = trial_key(phase, removed, tuple(requested))
        # Finer partitions can repeat an unchanged chunk. Keep its frozen decision
        # rather than duplicate the trial or redraw a favorable sampled block.
        previous = next((trial for trial in self.trials if trial.trial_key == key), None)
        if previous is not None:
            return previous if previous.allows_search_continuation else None
        replaying = bool(self.prior_trials)
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
        if self.publish and not replaying:
            checkpoint_removed = trial.effective_removals if trial.accepts_removal else removed
            self.publish_checkpoint(checkpoint_removed, self.trials, False)
        return trial if trial.allows_search_continuation else None

    def _remove_effective(self, removed: frozenset[str]) -> None:
        self.removable = [item for item in self.removable if item not in removed]


async def run_search[Trial: _Trial](
    evidence: CaseEvidence,
    *,
    removable: list[str],
    classify: _Classifier[Trial],
    prior_trials: deque[Trial],
    publish: bool,
    publish_checkpoint: _CheckpointPublisher[Trial],
) -> _SearchOutcome[Trial]:
    """Run the same chunk order and restarting final sweep for either decision policy."""

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
