"""Shared candidate order and final single-removal sweep."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator, Iterable, Sequence
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
        candidates = removal_candidates(self.removable)
        removed: frozenset[str] = frozenset()
        try:
            base, requested, phase = next(candidates)
            while True:
                trial = await self._run_trial(base, requested, phase)
                if trial is None:
                    return _SearchOutcome(removed, self.trials, False)
                removed = frozenset(trial.effective_removals) if trial.accepts_removal else base
                base, requested, phase = candidates.send(removed)
        except StopIteration:
            return _SearchOutcome(removed, self.trials, True)

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


def removal_candidates(
    removable: Sequence[str],
) -> Generator[tuple[frozenset[str], tuple[str, ...], SearchPhase], frozenset[str], None]:
    """Pure candidate order, shared by execution and offline resume verification.

    The caller sends the next accepted cumulative removal set after every trial.
    Refused or non-admitted trials send their unchanged base.
    """

    remaining = list(removable)
    removed: frozenset[str] = frozenset()
    granularity = min(2, len(remaining))
    while remaining and granularity:
        reduced = False
        for chunk in _partition(remaining, granularity):
            updated = yield removed, chunk, "chunk"
            if updated != removed:
                removed = updated
                remaining = [item for item in remaining if item not in removed]
                reduced = True
                break
        if reduced:
            granularity = min(max(granularity - 1, 2), len(remaining))
        elif granularity >= len(remaining):
            break
        else:
            granularity = min(len(remaining), granularity * 2)
    restart = True
    while restart:
        restart = False
        for item in tuple(remaining):
            updated = yield removed, (item,), "final-sweep"
            if updated != removed:
                removed = updated
                remaining = [item for item in remaining if item not in removed]
                restart = True
                break
