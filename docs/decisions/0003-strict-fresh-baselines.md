# ADR 0003: Retain strict fresh baselines for sampled reduction

Status: accepted, 2026-10-15.

## Context

Each exploratory candidate is compared with the immutable original context. Reusing an
original observation block across sibling candidates could reduce calls, but it correlates
screens and changes observation ownership, budget admission, expiry and resume contracts.
Current production blocks require block-local observations; copying a baseline into another
ordinary block would violate that contract.

The
[reduction-design studies](../research/reduction-design.md#baseline-sharing-failed-prefetch-and-useful-conditional-design)
tested alternatives. Eager prefetch wasted work after an accepted removal and increased cost for one
instruction position. Lazy sharing of a frozen two-sibling group saved about 6.61% charged calls per
confirmed useful result in a declared synthetic workload. A strict allocation with 40 original/80
candidate observations saved more but narrowly failed its mildly degraded usefulness gate. Neither
alternative is a native qualification result.

Freshness is material: an exclusive 240-slot synthetic lifetime made lazy sharing fail at three
of four positions, reducing aggregate useful success to 25%. Synthetic slots do not define a
native evidence lifetime. The completed native profiles use strict sampling; their
[measurements](../measurements.md) establish no practical sharing savings or finite shared-group
freshness admission.

## Decision

Use **fresh original and candidate observations for every exploratory comparison**, always
relative to the immutable original. Use a separate fresh original/candidate block for the
single durably selected final candidate. Calibration, search and confirmation observations
remain disjoint. Shared-baseline production support stays disabled.

Keep lazy groups of two as a conditional optimization. Add shared ownership or epoch machinery only
after the conditions below are met. The previous 20% fixed-budget threshold and
benefit-at-every-position rule had no workload-derived justification and are not future acceptance
requirements. A smaller saving can be worthwhile if native evidence supports it and the
implementation cost is justified. Fixed-six arithmetic or synthetic point savings alone are
insufficient.

## Conditions for reconsideration

A bounded follow-up must establish meaningful workload savings using total charged work per
unconditionally confirmed useful result, including unsuccessful/incomplete work, calibration,
replacements and fresh confirmation. Freeze workload weights, precision and usefulness/error
criteria before evaluation; report position-specific effects and expected versus maximal work.

Before any group dispatch, conservative call/time admission must fit one original arm, both
candidate arms, bounded replacements and protected confirmation capacity strictly within the
original evidence deadline. Select strict sampling before dispatch if that cannot be proved.
Native measurements must establish the applicable lifetime in seconds; a synthetic slot count
cannot supply it.

Only then consider a focused implementation for one frozen ordered two-member group. It must
give baseline observations one durable owner, bind both comparisons to it, charge each attempt
once, reconcile reservations/observations/checkpoints on resume, and refuse stale evidence.
Acceptance or any parent change ends reuse. No sibling may be appended after observing the
baseline, and unfavorable baselines may not be redrawn. Final confirmation never shares data.

Credential-free composed failure/recovery gates and separately approved exact-profile native
cost/usefulness validation must precede enabling the option. A default change additionally
needs representative evidence. This decision creates no provider-call or publication authority.

## Consequences

Strict sampling spends more original-context calls than the qualified synthetic lazy policy,
but preserves the tested evidence ownership and resume model. The current approximately 10k
profile measurements remain applicable. They do not cover larger contexts, shared-baseline savings
or separately timed construction and initialization.
