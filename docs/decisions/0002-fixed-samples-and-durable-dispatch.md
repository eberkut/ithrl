# ADR 0002: Fixed samples and durable dispatch

Status: Accepted.

## Context

Model decisions are stochastic. Adaptive search can select a context that looks successful by
chance, and a changing baseline can hide degradation. Retrying an interrupted request can also
duplicate provider calls when acceptance is unknown. Statistical validity therefore depends on
the same durable attempt, eligibility and accounting boundaries as safe execution.

The deterministic example remains a separate execution mode. Its completed single-group sweep
can establish search 1-minimality. Native sampled search needs a useful exploratory phase and
independent final evidence without inheriting that stronger claim.

## Decision

### Freeze complete independent-arm blocks

Use fixed valid-observation targets and fixed maximum-attempt limits for each original/candidate
arm. Freeze the schedule, freshness, identities and eligibility policy before collection. Collect
serially in recorded seeded interleaving, taking a fresh immutable-original baseline for each
comparison. The implemented baseline policy is `strict-fresh-original`; sharing baseline samples
is deferred until its distinct statistical, ownership and native-use gates pass.

Interleaving limits exposure to time drift but does not create paired observations. The design
assumes independent valid observations and stationarity within each comparison. Neither search
nor confirmation accepts early by repeatedly inspecting a fixed-sample interval. Attempt
ceilings bound collection; incomplete or ineligible blocks do not receive a statistical verdict.

Calibration first establishes that the target is measurable on the configured full-context
surface. Exploratory reduction then uses the original/candidate point-rate difference to guide
search. That screen has no preservation-confidence or minimality claim. The shared collector
returns complete block evidence and accounting; the owning workflow decides how to screen,
select and classify it. Internal fuzz discovery and simplification use the same collection
boundary with a separate change objective and confirmation procedure.

### Confirm one selected candidate with untouched observations

Persist one final candidate selection before final confirmation. Collect fresh original and
selected-candidate arms, disjoint from calibration and all exploratory observations. Do not resume
search, reselect using holdout outcomes or repeat confirmation until a pass.

Reduction confirmation estimates candidate match probability minus immutable-original match
probability. It uses the uncorrected Newcombe method 10 independent-arm difference interval built
from Wilson limits. Preservation requires the eligible complete block's lower bound to be at
least the negative configured margin. Confidence is nominal and approximate, with the configured
value applying to each one-sided bound; it is not an exact finite-sample or campaign-wide
guarantee. [Measurements](../measurements.md#statistical-reference-checks) documents the numerical
reference checks.

Keep search coverage, final preservation and operational reliability separate. A safe search
capacity stop can leave search incomplete while still allowing the selected candidate's reserved
confirmation. Unsafe or ineligible progress cannot. Even a successful holdout does not establish
global minimality, indispensability of retained groups or causality.

### Keep errors outside behavioral denominators

A valid observation is a match or nonmatch. Errors and invalid observations remain recorded
outside Bernoulli denominators and consume their applicable resources. The estimand is
`P(match | valid observation, frozen execution plan)`, with dispatched, valid, invalid and
runner-error counts reported per arm.

The default `independent-safe-preaccept-only` policy permits replacement only when safe
preaccept failure and independence from the potential behavioral outcome are verified. A failure
label or a digest alone proves neither. Safety requires evidence of no provider acceptance,
forbidden effects, residual disposable state or limit breach. Outcome-dependent invalidity and
broken protocol/oracle assumptions refuse classification.

The explicit `safe-provider-overload` policy additionally permits a fresh scheduled attempt after
a validated structured overload with no captured assistant, reasoning or tool output, verified
cleanup, unchanged protected state and enforced action denial. Preserve the failed observation,
unknown acceptance and full conservative call charge. This policy's valid-only interpretation
assumes independent, stationary attempt draws with a prespecified validity rule; it does not
prove unconditional reliability or outcome-independent missingness. Replacements remain within
the frozen arm ceiling, freshness and resource allowance. Harness-internal retries stay disabled.

### Reserve before dispatch and reconcile without replay

Admit an entire block before starting it, retaining provider-call and runner-time capacity for
final confirmation. Account for maximum attempts, profile call bounds, timeouts, cancellation
grace and future pacing. Pacing is opt-in, defaults to zero and persists across resume; wait
before publishing the next dispatch reservation. A finite token allowance requires preventive
estimation/enforcement; reporting usage after execution cannot supply it.

Persist a dispatch reservation bound to the prepared attempt before execution. After an
observation is durably stored, reconcile the reservation against that exact observation. If
interruption occurs between these writes, resume resolves the recorded attempt and reconciles
it without dispatching again. An unresolved reservation blocks progress; archiving partial
evidence cannot authorize a replacement cohort. Completed confirmation is never repeated.

Preserve actual, additional possible and pending provider calls as distinct facts. Unknown
acceptance or usage remains explicit and conservatively charged. A denied shell action may
still leave a possible provider continuation; denying execution does not prove a single-call
attempt. Administrative allowance extensions retain accumulated consumption and frozen
experimental settings rather than creating fresh capacity by forgetting prior work.

## Consequences

The final claim rests on one selected artifact and fresh evidence rather than adaptive-search
luck. Complete-block admission makes cost and confirmation capacity reviewable. Durable
reservations prevent uncertain dispatch from becoming an automatic duplicate call, at the cost
of refusing progress when reconciliation cannot prove what happened.

Fixed samples may be expensive near the degradation margin or at lower target rates. Preflight
checks whether confirmation is attainable under the proposed plan; it does not silently widen
the margin, reduce confidence or turn an exploratory screen into confirmation. Measured
workload usefulness and practical cost are qualification evidence, separate from numerical
interval checks. [Results](../results.md#sampled-results) states the public interpretation;
[architecture](../architecture.md#measurement-search-and-confirmation) maps implementation owners.

## Alternatives

Repeated fixed-interval peeking would add optional-stopping error. Reusing calibration/search
observations for final selection would contaminate the holdout. Treating errors as nonmatches
would conflate reliability with behavior, while dropping failed calls from accounting would
understate resource use. Automatic replay after unknown acceptance would duplicate uncertain
work. None meets the frozen experimental contract.

Anytime-valid sequential methods, genuine paired randomness and baseline sharing require
separate validated contracts. They remain possible optimizations, not implicit properties of
the current independent-arm collector.
