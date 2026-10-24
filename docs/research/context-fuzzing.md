# Context fuzzing: a bounded single-finding design

The 2026-10-23 credential-free study selected a useful synthetic discovery, mutation
simplification and fresh-confirmation procedure for a planted target-rate decrease from `.90`
to `.10`. The minimum complete-workflow success rate was 90.25%, with a simultaneous 99% lower
bound of 88.86896%, exceeding the prospective 80% gate. These results justify the bounded design
and its implementation; they do not establish native fuzzing support or general session success.

Mutation contracts, deterministic generation/materialization/simplification, sampled comparison
services and budget admission were implemented at development checkpoint `ee9a32f`. Internal
durable campaign orchestration now adds frozen generation, final-selection persistence and exact
recovery through the shared observation store. The [architecture](../architecture.md#measurement-search-and-confirmation)
describes that contract. The public `ithrl fuzz` command and native mutation qualification remain
pending. The
[supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
owns public capability claims. The [decision record](../decisions/0004-single-finding-fuzz-confirmation.md)
records the selected architecture and alternatives.

## Question and scope

The debugging question is whether a bounded context intervention decreases the probability of
the case's existing mechanical binary target. The target stays fixed. Define
`D = p_original - p_mutant`, where each probability is conditional on valid execution, and test
`D > delta` for a frozen meaningful decrease `delta`. Two distinct nonmatching answers do not
constitute distinct findings under this target.

The first design allows at most one selected finding per campaign. Discovery and simplification
are exploratory: they may adapt to earlier exploratory observations, but their screening decisions
are never confirmation. Simplification removes parts of the added intervention, preserving the
immutable original as the control. An empty intervention is that original, not another candidate
to dispatch.

The two mutation questions are deliberately narrow:

- `duplicate-text` repeats eligible prior user text within its existing field. It tests sensitivity
  to repetition. The study treats it as a no-effect control; native semantic equivalence is an
  assumption to investigate, not an established property.
- `append-stale-text` appends caller-supplied synthetic stale or irrelevant paragraphs to an
  eligible prior user field. This is intentional stress. An induced effect can be expected and
  useful without establishing a model defect, causal explanation or security vulnerability.

Both use the same `append-text` representation. The intervention binds an original context-node
selector, content-block index, original-text digest and exact appended bytes. At most one original
text field is edited per candidate. The 64 KiB UTF-8 payload limit bounds state; the study does not
qualify usefulness or native execution at that size. Nonempty mutations cannot be combined with
context removal. Whitespace, Unicode and line endings remain exact, except an empty append
normalizes to the empty intervention.

General JSON Patch, role/tool-schema changes, new native records, arbitrary record reordering,
unknown/protected records, current decision-input mutation, original-context reduction, new
harnesses, parallel model calls, shared original observations, LLM judges, multiple findings and
sequential inference are outside this design.

## Editable seam and evidence identity

Pure reconstruction checks found a suitable Codex final-response seam: append only to an ordinary
`input_text` block of a copied prior `role=user`, `type=message` history item. Preserve message
identity, outer record order, other fields and the fixed triggering input. Instructions, tools,
model/provider routing, policy, assistant text, tool results and opaque/image blocks are excluded.
Copilot mutation reconstruction is deferred because its semantic session serialization needs
additional identity and ordering decisions.

The credential-free prototype checked no-op reconstruction, duplication and appended paragraphs
on sanitized fixture materialization. Non-text fields, trigger, source bytes and original objects
remained unchanged; rematerializing the original reproduced the original result. The fixture
evidence digest was
`3ef4dfd942afde6d62e8e15c204f2a0887e6a60f2adf53ef94d905a28ca6b087`.
This establishes a local overlay boundary, with no harness or provider execution.

The implemented materializers revalidate selectors, protected fields and original-value digests
on defensive copies. Pure candidate admission occurs before sampling allocation. Candidate identity
binds the complete intervention and the source, target, reconstruction, effective policy and
experiment; a generation seed/index or retained/removed partition alone cannot identify a mutation.
Native Codex and Copilot capability profiles remain removal-only. A fixture profile that exercises
the seam does not qualify a native execution profile.

## Confirmation method and assumptions

Use one-sided Clopper–Pearson bounds with `gamma = alpha/2`. For `x_o` original matches among
`n_o` valid observations and `x_m` mutant matches among `n_m` valid observations:

```text
L_o = 0                                      if x_o = 0
      BetaInv(gamma, x_o, n_o - x_o + 1)       otherwise

U_m = 1                                      if x_m = n_m
      BetaInv(1 - gamma, x_m + 1, n_m - x_m)   otherwise

lower_difference = L_o - U_m
confirm exactly when lower_difference > delta
```

`BetaInv(q, a, b)` denotes the beta-distribution quantile. The implementation uses
[`scipy.special.betaincinv(a, b, q)`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.special.betaincinv.html),
the inverse regularized incomplete beta function. The strict inequality matters: equality at the
effect boundary is inconclusive. Complete fixed sample targets are required; partial samples are
incomplete, and observations beyond those targets are rejected.

With independent stationary binomial observations in each arm, the probability that `L_o`
exceeds `p_original` is at most `gamma`, and the probability that `U_m` falls below `p_mutant`
is at most `gamma`. If neither failure occurs, `L_o - U_m <= D`. Therefore, under `D <= delta`,
a false declaration is contained in the union of those two failures and has probability at most
`alpha`. Discreteness makes the construction conservative. This is an analytic model-based
bound; a low simulated declaration rate is an implementation check, not its proof.

The argument applies conditional on selection using independent exploratory evidence. Exactly
one mutation must be durably frozen before any confirmation outcome is read, together with target,
payload, threshold, method, sample targets, eligibility and execution identity. Both final arms
must be fresh and disjoint from calibration, discovery and simplification. After final selection,
there is no further search, payload adjustment, candidate substitution, outcome-driven sample
extension or repeated confirmation until a pass. Recovery may finish the same frozen incomplete
block only after reconciling recorded observations; unresolved dispatches cannot be replayed.

This controls one selected comparison per campaign. It provides no guarantee over user-initiated
repeated campaigns or selectively reported favorable campaigns. Supporting up to `K` findings
would require a prospective allocation across all `K` comparisons, such as `alpha/(2K)` per arm
bound, plus new selection, capacity and persistence contracts.

The independent reference construction uses SciPy's
[`binomtest` exact interval](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html):
`alternative="greater"` with confidence `1-gamma` for the original lower bound and
`alternative="less"` for the mutant upper bound. A central interval at `1-alpha/2` is a different
tail allocation. The study verified 352 count vectors against that API, symmetry, analytic
endpoints and independent standard-library binomial-tail equations. The maximum API difference
was `1.5543122344752192e-15`. At `n=10`, the zero-match upper bound is `.3084971078187607`, and
the all-match lower bound is `.6915028921812392`.

Exact operating-characteristic enumeration covered original rates `{.10,.50,.80,.90,1.00}`,
thresholds `{0,.05,.10,.20}`, and feasible decreases `max(0, delta+offset)` for offsets
`{-.05,0,.05,.20,.80}`. Sample pairs were `(20,20)`, `(40,40)`, `(80,80)`, `(20,40)` and `(40,20)`.
Combinations with `p_mutant = p_original - decrease` outside `[0,1]` were excluded, as specified
in the frozen programme. All count pairs were enumerated. A further 1,001-point null-boundary grid
per sample-pair/threshold had maximum false-declaration probability `.0020214677846288323`, below
the analytic `.05` ceiling.
Neither finite grid establishes coverage outside the model assumptions.

SciPy was selected over a handwritten quantile solver. The study used Python 3.12.3, NumPy 2.5.4
and SciPy 1.18.1; its isolated numerical installation occupied approximately 183 MiB, including
bundled libraries. The implementation confines numerical imports to optional `ithrl[fuzz]` and
refuses a missing dependency before sampling preparation. Core extraction and reduction do not
require the numerical extra.

## Prospective workload, protocol and utility gate

The study froze its programme, code, seeds, repetition counts, sample/budget menu, inference rule
and acceptance criteria before evaluating workflow outcomes. It uses synthetic Bernoulli outcomes;
it does not invoke a native model or estimate native response rates.

The workload has four prior-user history groups and a fixed final decision input with a mechanical
answer target. A stress mutation appends four synthetic stale/irrelevant paragraphs of
256, 1,024, 512 and 2,048 UTF-8 bytes, plus delimiters. One planted essential paragraph changes
the valid-match probability from `.90` to `.10`; the others are irrelevant. The study varies all
four mutation placements and all four essential-paragraph positions. It separately tests all six
two-paragraph interactions at placement zero. Duplication and all-null controls have no planted
effect.

For adaptive workflows, success requires calibration, discovery, completed simplification, removal
of at least half the planted irrelevant mutation bytes, retention of every essential component,
and one complete eligible fresh confirmation. The byte criterion tests whether simplification
produces useful inspection work on this fixture. It is neither a provider-token metric nor a
general compression claim.

The prospective menu combines exploratory samples `{3,5,8}` per arm, final samples `{20,40,80}`
per arm and administrative call ceilings `{256,384,512}`: 27 designs. Each later arm allows
`A(n) = n + ceil(n/2) + 6` attempts. Calibration requires three valid observations and at least
two matches within seven attempts. A fresh original is collected for every comparison. Discovery
uses the first placement whose observed original-minus-mutant difference is at least `.40`.
Simplification removes added paragraphs in order, restarts after acceptance, caches identical
interventions and permits at most ten proposals. Empty interventions are refused without dispatch.

The utility gate requires at least 80% complete-workflow success in every single-component
placement/component cell at independent invalid rates `{0,.05,.10,.20}`. One-sided exact lower
bounds use tail error `.01/2376`, giving simultaneous 99% lower coverage across
`27 designs * 4 invalid rates * (16 single-component + 6 interaction cells) = 2,376` adaptive
cells. Interaction inclusion separately requires every tested interaction's simultaneous lower
bound to exceed 80%. This utility multiplicity allocation is distinct from the final finding's
`alpha=.05` allocation.

Each cell has 10,000 independent campaigns. Across the adaptive workflow and two baseline
workflows, the study contains 7,128 rows and 71,280,000 campaigns. Qualification seed is
`2026102301`; sensitivity seed is `2026102302`. The predetermined design-selection rule chooses
the passing design with lowest all-run mean cost, breaking ties by smaller allowance and then
smaller final sample count. Selection therefore accounts for the whole prospective menu rather
than selecting a favorable unadjusted success interval.

## Selected settings and measured synthetic cost

| Setting | Selected study design |
| --- | --- |
| Objective | Valid-only target-rate decrease strictly greater than `.10` |
| Final inference | One selected finding, `alpha=.05`, exact union-bound method |
| Calibration | 3 valid, at least 2 matches, at most 7 attempts |
| Exploratory comparison | Fresh original 8 valid / 18 attempts; mutant 8 valid / 18 attempts |
| Discovery | Four placements, first screen-passing candidate |
| Mutation simplification | At most ten ordered paragraph-removal proposals; identical trials cached |
| Fresh confirmation | Original 20 valid / 36 attempts; selected mutant 20 valid / 36 attempts |
| Administrative allowance | 384 modeled calls, one call per attempt; 72 protected for confirmation |

The minimum single-component useful-workflow success was 90.25%, with simultaneous lower bound
88.86896%. The minimum tested two-component interaction success was 90.37%, with lower bound
88.99607%. These exceed the frozen gate within the declared workload envelope.

| Independent invalid rate | Single-component success range | Equal-weight mean modeled calls | Largest cell p95 calls |
| --- | --- | ---: | ---: |
| 0% | 91.14–94.25% | 149.75 | 203 |
| 5% | 90.86–94.33% | 157.52 | 221 |
| 10% | 91.26–93.88% | 166.39 | 236 |
| 20% | 90.25–93.60% | 186.16 | 269 |

All-run cost includes calibration failures, rejected mutants, simplification, inconclusive or
incomplete work and independent invalid attempts. The maximum realized cost among selected
single-component cells was 302 calls. The equal-weight mean over their 64 location/component/
invalid-rate cells was 164.95 calls. This weighting describes the simulation grid, not a measured
distribution of user workloads. Per-cell p50, p95, maximum and terminal counts are retained with
the [reproducibility artifacts](https://github.com/eberkut/ithrl/blob/main/research/fuzzing/README.md).

Whole comparisons are admitted using maximum arm-attempt capacity while protecting the complete
final reserve. The model charges the required samples plus realized independent failures, capped
by each arm's attempt ceiling; both arms incur cost even if one is incomplete. It models one
provider call per attempt. Its sequential-arm IID draws have the same joint outcome law as
interleaving under those assumptions; they do not model native incomplete-block early termination,
latency, pacing or billing. No provider calls were actually made.

The implemented worst-case plan for four discovery and ten simplification comparisons is
`7 + 14*(18+18) + 36+36 = 583` attempts. The 384-call administrative ceiling can stop exploration
earlier through whole-block admission; 583 is neither a required budget nor a new product default.
A two-call execution profile doubles call bounds. Runner timeout, cancellation grace, pacing and
command wall time require separate admission. Finite token limits must refuse if no estimator can
enforce them. Actual, additional possible and pending calls, plus unknown usage, remain distinct.

## Alternatives and effect-size limits

The baseline workflows use the same calibration and exact final method. The sweep screens the
same four placements and confirms the first hit without simplification. Direct confirmation
uses one prespecified placement chosen uniformly, with no discovery or simplification.

| Workflow at selected counts/allowance | Worst confirmed rate | Worst simplified-and-confirmed rate | Equal-weight mean modeled calls |
| --- | ---: | ---: | ---: |
| Adaptive discovery and mutation simplification | 91.74% | 90.25% | 164.95 |
| Four-placement sweep, confirm first hit | 92.66% | 0% | 88.62 |
| One uniformly chosen placement, direct confirmation | 22.72% | 0% | 46.16 |

Adaptive simplification costs about 76.33 additional modeled calls over the unshrunk sweep and
produces the smaller witness. The sweep remains a lower-cost choice when a large mutation is
acceptable. Direct confirmation's coverage is inadequate for this discovery task. This comparison
does not establish that simplification saves calls.

Three- and five-sample screens failed the complete-workflow gate. Increasing only the final sample
count did not repair their exploratory selection/simplification errors. Some small-allowance,
large-holdout designs could not admit enough work without borrowing the reserve and correctly
failed. A 384-call design with eight screening samples and 40 final samples also passed, but its
207.06 mean calls exceeded the selected design. At 512 calls, eight screening samples and 80 final
samples passed at 291.25 mean calls. The predeclared cost rule selected the cheaper passing design.

The minimum effect of interest is `.10`, but usefulness is established for the much larger `.80`
planted decrease. Exact holdout power for `.90 -> .10` with 20 samples per arm is 98.4711%.
For `.90 -> .50`, the same holdout has 16.0458% power; 80 samples per arm give 93.1864%. Those
moderate-effect numbers concern confirmation alone. The `.40` exploratory screen would itself
need a prospective design study for smaller effects. No low-effect, arbitrary-session, native
success-rate or token-saving claim follows.

Nominal Newcombe preservation intervals remain appropriate to their existing declared purpose;
they were not adopted as a strict campaign error guarantee for fuzz findings. The
[independent-proportion interval documentation](https://www.statsmodels.org/dev/generated/statsmodels.stats.proportion.confint_proportions_2indep.html)
describes the separate interval family. Repeated holdouts, shared exploratory/final observations,
multiple findings without an allocation, and a custom beta-quantile solver were excluded to keep
selection, error control and numerical implementation inspectable.

## Validity and drift sensitivities

Execution reliability is a separate measurement. Errors never count as behavioral mismatches or
enter Bernoulli denominators. Preserve invalid/error counts per arm, eligibility, unknown provider
acceptance and conservative charges. Prespecified exclusions require verified outcome independence;
the existing opt-in safe-overload policy also requires structured overload evidence, no captured
output and verified safety/cleanup. It does not authorize blanket replacement of errors.

Under IID marked attempts with prespecified validity, obtaining a fixed number of valid samples
within an attempt ceiling leaves accepted outcomes binomial for the conditional target. Different
arm validity probabilities need not invalidate that explicitly conditional comparison. A sensitivity
with valid probabilities `.95` original / `.80` mutant, while holding conditional target rates at
`.90` / `.10`, achieved 91.98% complete useful workflows.

Outcome-dependent validity changes what those conditional probabilities mean. A second sensitivity
sets both unconditional match probabilities to `.50`. The original accepts matches with probability
1 and nonmatches with `.50`; the mutant reverses those acceptance probabilities. Both arms have
valid probability `.75`, yet their conditional match rates are `2/3` and `1/3`. The experiment
declared 281 changes in 10,000 campaigns: true conditional decreases despite equal unconditional
behavior. Equal observed failure rates therefore cannot establish comparable unconditional
behavior. There is no planted causative paragraph in this sensitivity, so geometry-based
simplification-success metrics are inapplicable. An unconditional claim needs a different justified
measurement design; outcome-driven filtering cannot be silently treated as benign missing data.

Across 22 all-null/duplication-control cells of 10,000 campaigns each, no final declaration occurred.
This does not prove zero error. A shuffled-arm common-drift sensitivity decreasing the common
response probability from `.90` to `.50` during confirmation produced 3 declarations in 10,000
campaigns. It deliberately violates stationarity, and the low observed count does not qualify
drift robustness. Interleaving and finite freshness mitigate temporal separation; they do not
demonstrate independence or stationarity.

Two sensitivity corrections were reviewed explicitly. Before sensitivity execution, filtering was
corrected to apply the mutant conditional rate `1/3` regardless of placement/components. Reporting
was then corrected to recognize those as real conditional effects and omit inapplicable paragraph
geometry metrics; the identical sensitivity seed was replayed. Original freezes and prior results
were retained. Qualification parameters and draws were unchanged, and twelve selected qualification
cells replayed identically after the corrections. Independent validation checked all 7,128 row
counts, terminal/cost invariants, replay equality and corrected sensitivity interpretation.

## Reproducibility and implementation evidence

The [study artifact index](https://github.com/eberkut/ithrl/blob/main/research/fuzzing/README.md)
identifies the sanitized programme, numerical method, study/validation code and aggregate evidence,
with reproduction instructions. Simulation and pure fixture reconstruction require no credentials,
native harness or provider. Regenerating study output and running current service tests answer
different questions: the study checks the prospective synthetic design; tests check the implemented
contracts and failure paths.

The corrected study freeze used these SHA-256 identities:

| Artifact | SHA-256 |
| --- | --- |
| `programme.json` | `37c0a9b43c39f0e8f5c5bed89ae1295cd66ab980d0fe972b645fb718783b20b8` |
| `method.py` | `fb6c5d1ada3d6a2f49dd18f2f25704970665ae73bf9f619e66531da893c00b6e` |
| `study.py` | `75e2afac5112e865fce37905b9d0bda8666f8fd69002c1b634e3499593d9d8f6` |

The original pre-correction programme and study identities were respectively
`ba830b8596048fd46292ca749c5f92ad6485d3302721a52597072c8c21687639` and
`8894418399aaeeaddcf61731f9e52d9d961bbcf06250704e66f7391b18e8a8bb`.
Any publication-only path or wording adjustments have their own identities in the artifact index;
these historical hashes identify the executed research freeze rather than claiming byte equality
with an edited public copy.

The [architecture map](../architecture.md#developer-map) locates the current implementation;
[statistical tests](https://github.com/eberkut/ithrl/blob/main/tests/unit/test_fuzz_statistics.py)
independently check tail equations, endpoints, unequal arms and strict boundaries. Composed
[sampling checks](https://github.com/eberkut/ithrl/blob/main/tests/integration/test_fuzz_sampling.py)
cover role separation, complete-block admission and failure handling.

The initial implementation checkpoint ([PR #3](https://github.com/eberkut/ithrl/pull/3)) was
validated with focused Python 3.12–3.14 suites, base/extra package installations and dependency
audits. Its full regression runs first
exposed stale fixtures; the corrected affected tests passed on each version, but those full runs
were not repeated locally as all-green runs. Hosted CI on PR #3 subsequently passed the full
Python 3.12–3.14 matrix and package checks. This implementation validation does not establish
native mutation qualification. Ongoing scope, acceptance and evidence belong in the
[fuzzing capability issue](https://github.com/eberkut/ithrl/issues/5).

## Native applicability

These synthetic results do not supply a native model effect, runtime estimate or universal preset.
Qualification must measure a useful complete workflow for a safe text-mutation profile with
faithful no-op/reconstruction controls, original calibration, protected-state checks and fresh
confirmation. Existing extraction/reduction support alone does not qualify insertion or replacement.
The [native qualification issue](https://github.com/eberkut/ithrl/issues/9) owns that gate's
scope, approvals and acceptance; the [practical-settings issue](https://github.com/eberkut/ithrl/issues/10)
owns the resulting supported envelope and limits.
