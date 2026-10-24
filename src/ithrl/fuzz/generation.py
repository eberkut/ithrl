"""Bounded seeded proposals over materializer-discovered original text."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import islice, product

from ithrl.context import content_digest
from ithrl.fuzz.contracts import FuzzCampaignConfig
from ithrl.mutation import EditableText, MutationError, TextAppend, TextMutation


@dataclass(frozen=True, slots=True)
class MutationBatch:
    mutations: tuple[TextMutation, ...] = field(repr=False)
    proposals_examined: int


def generate_mutations(
    sites: tuple[EditableText, ...], config: FuzzCampaignConfig, *, stale_text: str
) -> MutationBatch:
    """Generate at most the work cap; duplicates and empty proposals consume work too.

    The caller supplies literal synthetic stress content. This function reads no
    workspace, session or file and does not infer a stale instruction from secrets.
    """

    ordered = list(sites)
    random.Random(config.seed).shuffle(ordered)  # noqa: S311 - reproducible proposal order, no secrets
    mutations = []
    seen: set[str] = set()
    examined = 0
    for site, mutator in islice(product(ordered, config.mutators), config.max_candidates):
        examined += 1
        if site.context_id not in config.original.retained_context_ids:
            raise MutationError("mutation location does not belong to the original candidate")
        addition = site.original_text if mutator == "duplicate-text" else stale_text
        if not addition:
            continue
        try:
            edit = TextAppend(
                context_id=site.context_id,
                content_index=site.content_index,
                original_text_digest=content_digest(site.original_text),
                text="\n\n" + addition,
            )
        except ValueError:
            # A bounded source may still be too large to duplicate under the payload cap.
            continue
        mutation = TextMutation(edits=(edit,))
        if mutation.digest not in seen:
            seen.add(mutation.digest)
            mutations.append(mutation)
    return MutationBatch(tuple(mutations), examined)
