# 0004: Confirm one selected context mutation with fresh exact bounds

Status: accepted for the bounded context-fuzzing design, 2026-10-24.

## Context

Context fuzzing searches for an intervention that decreases the probability of an existing
mechanical binary target. Discovery and intervention simplification adapt to exploratory outcomes.
Reporting their best observed difference as a finding would couple selection to the evidence used
to justify it. The first implementation also needs a finite cost envelope, immutable original
control and a clear recovery boundary.

The [research record](../research/context-fuzzing.md) documents the prospective study, exact method,
assumptions, workload, costs and sensitivities. Its strong synthetic `.90 -> .10` workload passed
the declared usefulness gate. That evidence supports this bounded design, without qualifying a
native runner or general session success rate.

## Decision

Allow at most one final finding per campaign. Keep calibration, adaptive discovery, intervention
simplification and final confirmation observations separate. Compare every mutation with a fresh
immutable original. Simplify only the intervention, using bounded ordered paragraph deletion with
restart and exact-intervention deduplication.

Durably freeze one candidate, target, payload, meaningful decrease, sample targets, eligibility,
freshness and execution identity before confirmation. Use fresh fixed original/mutant samples
and one-sided Clopper–Pearson arm bounds, allocating `alpha/2` to each. Confirm only if the
original lower bound minus the mutant upper bound is strictly greater than the frozen decrease.
The union-bound argument controls a false declaration at `alpha` for the one independently
selected comparison under independent stationary valid observations. Use SciPy's maintained beta
inverse through optional `ithrl[fuzz]` rather than implementing a quantile solver.

The estimand is explicitly conditional on valid execution. Errors remain outside behavioral
denominators and are reported separately with reliability, unknown acceptance and conservative
resource charges. Preserve existing eligibility and safety rules. Equal failure rates do not
establish comparable unconditional behavior or stationarity.

Use the existing frozen-block collector, observation store, dispatch reconciliation and budget
admission. Admit exploratory comparisons only while protecting complete final-confirmation
capacity, including profile-specific call bounds and time. Campaign orchestration owns final
selection and exact recovery; the comparison service cannot claim that those workflow obligations
are already satisfied.

After final selection, do not resume search, change the payload, select another candidate, extend
samples based on outcomes or repeat completed confirmation. Recovery may only finish the same
frozen incomplete block with reconciled evidence. An inconclusive or failed final comparison
ends the campaign. An unavailable screen is incomplete evidence, not an unchanged-behavior result.

## Alternatives considered

| Alternative | Reason not selected for this scope |
| --- | --- |
| Confirm the best exploratory result using the same observations | Selection contaminates the final evidence; it does not provide the declared fresh-sample guarantee. |
| Repeated holdouts until one passes | Outcome-driven retries alter the error family and encourage favorable-result selection. |
| Multiple findings per campaign | Requires prospective error allocation, capacity, selection and persistence contracts beyond the first bounded path. |
| Reuse nominal Newcombe preservation intervals as strict fuzz error control | A separate purpose and nominal interval method do not establish this campaign's exact error bound. Existing preservation semantics remain unchanged. |
| Sequential inference or a custom exact solver | Adds method/state/numerical complexity without evidence that the first useful workload needs it. |
| Confirm an unshrunk four-placement sweep | Lower mean modeled cost, but supplies a larger intervention; retained as a valid option when simplification is unnecessary. |
| Directly confirm one prespecified placement | Cheapest comparison, but inadequate coverage for the declared discovery task. |

## Consequences

The selected procedure has an inspectable single-selection boundary and finite reserve. The
conservative exact construction was useful for the studied strong effect; smaller effects need a
separate power/usefulness study. Simplification buys a smaller witness at an additional sampling
cost. See the [research record](../research/context-fuzzing.md#alternatives-and-effect-size-limits)
for measured settings, costs and effect-size limits.

Outcome-dependent invalidity can produce different conditional target rates despite equal
unconditional rates, and drift violates the stationary binomial model. Report execution reliability
separately. Empirical false-declaration counts do not establish validity under these conditions.

The architecture avoids a second sampler or observation database. It does require durable campaign
ordering/selection and refusal on unresolved dispatches, incompatible identity or stale evidence.
Private payloads and native receipts stay outside public output. Project-owned schema tags remain
`1` during pre-1.0 evolution, with strict current-format validation and no migration layer.

Native mutation support requires a separate gate for the exact operation/profile, action denial,
immutable protected state, cleanup and complete measured cost. A pure reconstruction fixture or
successful simulated campaign does not satisfy that gate.
