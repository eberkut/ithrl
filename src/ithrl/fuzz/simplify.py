"""Simplify added paragraphs under an explicit changed-behavior decision."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

from ithrl.fuzz.contracts import MAX_FUZZ_CANDIDATES
from ithrl.mutation import TextAppend, TextMutation


class MutationDecision(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SimplificationResult:
    mutation: TextMutation = field(repr=False)
    complete: bool
    proposals: int
    evaluations: int


async def simplify_mutation(
    mutation: TextMutation,
    evaluate: Callable[[TextMutation], Awaitable[MutationDecision]],
    *,
    max_trials: int,
) -> SimplificationResult:
    """Revert added paragraphs, restart on acceptance, and never redraw an identical edit.

    Initial evaluation establishes the changed-behavior precondition, separately
    from the proposal cap. An owning evaluator can reuse the discovery decision.
    Callback errors propagate; invalid trials cannot establish a retained change.
    """

    steps = simplification_steps(mutation, max_trials=max_trials)
    proposed = next(steps)
    while True:
        decision = await evaluate(proposed)
        try:
            proposed = steps.send(decision)
        except StopIteration as finished:
            return finished.value


def simplification_steps(
    mutation: TextMutation, *, max_trials: int
) -> Generator[TextMutation, MutationDecision, SimplificationResult]:
    """Replay the bounded traversal using recorded decisions, without executing a runner."""

    if type(max_trials) is not int or not 1 <= max_trials <= MAX_FUZZ_CANDIDATES:
        raise ValueError("simplification requires a finite positive trial bound")
    if mutation.is_empty or (yield mutation) is not MutationDecision.CHANGED:
        raise ValueError("simplification requires an observed changed-behavior mutation")
    edit = mutation.edits[0]
    boundaries = sorted(
        {0, len(edit.text), *(m.start() for m in re.finditer(r"\r?\n\r?\n", edit.text))}
    )
    parts = [edit.text[a:b] for a, b in pairwise(boundaries)]
    cache = {mutation.digest: MutationDecision.CHANGED}
    selected = mutation
    cursor = proposals = evaluations = 0
    while cursor < len(parts) and proposals < max_trials:
        proposals += 1
        text = "".join(parts[:cursor] + parts[cursor + 1 :])
        proposed = TextMutation(
            edits=(
                TextAppend(
                    context_id=edit.context_id,
                    content_index=edit.content_index,
                    original_text_digest=edit.original_text_digest,
                    text=text,
                ),
            )
        )
        if proposed.is_empty:
            cursor += 1
            continue
        decision = cache.get(proposed.digest)
        if decision is None:
            decision = yield proposed
            evaluations += 1
            cache[proposed.digest] = decision
        if decision is MutationDecision.CHANGED:
            selected = proposed
            parts.pop(cursor)
            cursor = 0
        else:
            cursor += 1
    return SimplificationResult(selected, cursor == len(parts), proposals, evaluations)
