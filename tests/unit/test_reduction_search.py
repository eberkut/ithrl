from __future__ import annotations

import pytest

from ithrl.reduce.search import removal_candidates


@pytest.mark.parametrize("start_with_all", [False, True])
def test_rejected_chunks_end_with_a_complete_single_removal_sweep(start_with_all: bool) -> None:
    candidates = removal_candidates(("A", "B", "C"), start_with_all=start_with_all)
    actual = []
    step = next(candidates)
    while True:
        removed, requested, phase = step
        assert removed == frozenset()
        actual.append((phase, requested))
        try:
            step = candidates.send(removed)
        except StopIteration:
            break
    initial = [("chunk", ("A", "B", "C"))] if start_with_all else []
    assert actual == [
        *initial,
        ("chunk", ("A", "B")),
        ("chunk", ("C",)),
        *(("chunk", (item,)) for item in "ABC"),
        *(("final-sweep", (item,)) for item in "ABC"),
    ]


@pytest.mark.parametrize("items", [(), ("A",), ("A", "B", "C")])
def test_accepted_chunks_finish_without_empty_or_repeated_removals(items: tuple[str, ...]) -> None:
    candidates = removal_candidates(items)
    removed: frozenset[str] = frozenset()
    step = next(candidates, None)
    while step is not None:
        base, requested, phase = step
        assert base == removed
        assert requested
        assert set(requested).isdisjoint(removed)
        assert phase == "chunk"
        removed |= frozenset(requested)
        try:
            step = candidates.send(removed)
        except StopIteration:
            break
    assert removed == frozenset(items)
