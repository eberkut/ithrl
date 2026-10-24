"""Private campaign ordering and exact interventions, separate from observation facts."""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import content_digest
from ithrl.fuzz.contracts import MAX_FUZZ_CANDIDATES, FuzzCampaignConfig, FuzzOutcome
from ithrl.fuzz.simplify import MutationDecision, simplification_steps
from ithrl.mutation import MAX_MUTATION_TEXT_BYTES, TextMutation
from ithrl.observations import SamplingBlockPlan


@lru_cache(maxsize=1)
def campaign_implementation_digest() -> str:
    """Bind recovery to the actual generator, traversal and orchestration implementation."""

    return content_digest(
        {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("generation.py", "simplify.py", "progress.py", "campaign.py")
        }
    )


class _Progress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class FuzzTrial(_Progress):
    role: Literal["discovery", "simplification"]
    mutation: TextMutation = Field(repr=False)
    # A pure, candidate-local admission refusal consumes work but no sampling block.
    plan: SamplingBlockPlan | None


class FuzzCampaignProgress(_Progress):
    schema_version: Literal[1] = 1
    kind: Literal["fuzz-campaign"] = "fuzz-campaign"
    config: FuzzCampaignConfig = Field(repr=False)
    implementation_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    generation_input_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    stale_text: Annotated[str, Field(max_length=MAX_MUTATION_TEXT_BYTES, repr=False)]
    generated: Annotated[
        tuple[TextMutation, ...], Field(max_length=MAX_FUZZ_CANDIDATES, repr=False)
    ]
    proposals_examined: Annotated[int, Field(ge=0, le=MAX_FUZZ_CANDIDATES)]
    trials: Annotated[tuple[FuzzTrial, ...], Field(max_length=2 * MAX_FUZZ_CANDIDATES)] = ()
    selected_mutation: TextMutation | None = Field(default=None, repr=False)
    terminal: FuzzOutcome | None = None
    stop_reason: Literal["no-candidate", "screen-incomplete", "confirmation-finished"] | None = None
    collection_refusal: Annotated[str | None, Field(max_length=128, pattern=r"^[a-z][a-z-]+$")] = (
        None
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> FuzzCampaignProgress:
        if not len(self.generated) <= self.proposals_examined <= self.config.max_candidates:
            raise ValueError("generated work exceeds the frozen proposal cap")
        digests = {mutation.digest for mutation in self.generated}
        if len(digests) != len(self.generated) or any(m.is_empty for m in self.generated):
            raise ValueError("generated mutations must be nonempty and deduplicated")
        if len(self.trials) > len(self.generated) + self.config.max_simplification_trials:
            raise ValueError("campaign trials exceed frozen work limits")
        if len({t.mutation.digest for t in self.trials}) != len(self.trials):
            raise ValueError("an identical intervention cannot be sampled twice")
        if (self.terminal is None) != (self.stop_reason is None):
            raise ValueError("a terminal campaign requires its stop reason")
        return self

    @property
    def selection_digest(self) -> str:
        return content_digest(
            self.model_dump(mode="json", exclude={"terminal", "stop_reason", "collection_refusal"})
        )


def campaign_steps(
    progress: FuzzCampaignProgress,
) -> Generator[
    tuple[Literal["discovery", "simplification"], TextMutation],
    MutationDecision,
    TextMutation | None,
]:
    """Deterministic discovery/simplification replay; cached answers consume no new work.

    The frozen generated sequence records the generator's position, including consumed
    duplicate/empty proposals. Replaying traversal only reads recorded decisions; it never
    redraws a completed trial or changes the finite proposal and simplification caps.
    """

    cache: dict[str, MutationDecision] = {}
    for mutation in progress.generated:
        decision = yield "discovery", mutation
        cache[mutation.digest] = decision
        if decision is MutationDecision.CHANGED:
            break
    else:
        return None

    steps = simplification_steps(mutation, max_trials=progress.config.max_simplification_trials)
    proposed = next(steps)
    while True:
        decision = cache.get(proposed.digest)
        if decision is None:
            decision = yield "simplification", proposed
            cache[proposed.digest] = decision
        try:
            proposed = steps.send(decision)
        except StopIteration as finished:
            return finished.value.mutation
