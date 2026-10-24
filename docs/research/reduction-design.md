# Sampled reduction design research

Studies completed on **2026-10-03–2026-10-15** led to exploratory candidate screening followed
by one independent fresh confirmation of a durably selected candidate. They also rejected eager
baseline prefetch, measured a limited synthetic benefit from lazy sharing, and retained strict
fresh baselines in production. This page preserves numerical results and failed alternatives.
[Results](../results.md) owns interpretation of current output; [measurements](../measurements.md)
owns the completed native workloads, settings and costs. [ADR 0003](../decisions/0003-strict-fresh-baselines.md)
records the baseline decision.

## Method and confidence convention

Confirmation uses the uncorrected independent-arm Wilson hybrid-score interval, **Newcombe
method 10**. Let arm 1 be the candidate and arm 2 the immutable original, with valid matches
`x_i`, valid counts `n_i > 0`, `q_i = x_i/n_i`, nominal one-sided confidence `C`,
`z = Phi^-1(C)` and `s = z*z`:

```text
center_i = (q_i + s/(2*n_i)) / (1 + s/n_i)
half_i = z * sqrt(q_i*(1-q_i)/n_i + s/(4*n_i*n_i)) / (1 + s/n_i)
l_i = center_i - half_i
u_i = center_i + half_i
d = q_1 - q_2
L = d - sqrt((q_1-l_1)^2 + (u_2-q_2)^2)
U = d + sqrt((u_1-q_1)^2 + (q_2-l_2)^2)
```

There is no continuity correction, pooling, pseudocount or Wald substitution. At `C=.95`,
both bounds use `Phi^-1(.95)`: the corresponding central interval has coefficient `.90`.
This is nominal approximate confidence, not an exact finite-sample guarantee. Preservation
uses the lower bound for `p(candidate)-p(original)` and the declared degradation margin.

The [SAS FREQ specification](https://support.sas.com/documentation/onlinedoc/stat/132/freq.pdf)
(printed page 2675) supplies the square-and-add formula; its [noninferiority documentation](https://support.sas.com/documentation/cdl/en/procstat/68142/PDF/default/procstat.pdf)
(printed page 182) supplies the central `1-2*alpha` convention. The independent comparator was
[statsmodels `0.15.0`](https://github.com/statsmodels/statsmodels/blob/v0.15.0/statsmodels/stats/proportion.py),
with explicit candidate-first `method="newcomb"`, `compare="diff"` and
`alpha=2*(1-C)`. Forty independently generated vectors remain in
[the public fixture](https://github.com/eberkut/ithrl/blob/main/tests/fixtures/statistics_reference.json).
Production agreement was within `1e-12`; full count-table maximum absolute difference was
`3.33e-16`, and 81 scaled cases differed by at most `2.22e-16`. Analytic endpoint identities
and arm-swap symmetry were also checked. Formula agreement alone does not establish error rates.

The [original Newcombe paper record](https://pubmed.ncbi.nlm.nih.gov/9595617/) identifies the
1999 erratum, *Statistics in Medicine* 18(10):1293,
DOI `10.1002/(SICI)1097-0258(19990530)18:10<1293::AID-SIM175>3.0.CO;2-7`.
The full publisher correction was checked from a supplied screenshot on 2026-10-01: **method
11** must use the first square-and-add expression because its second expression is not
equivalent. Method 10's two expressions are equivalent; paired-data definitions are unaffected.
The correction does not change the method implemented here. The screenshot is not a public
repository artifact; this records the historical verification, not a fresh publisher retrieval.

## Original joint gate and its failure

The planted benchmark has four dependency-valid editable groups: one behavioral instruction
and three irrelevant histories, at each of four instruction positions. Useful success requires
retaining the instruction, removing at least 50% of editable materialized bytes, completing the
final sweep and passing independent confirmation. Initial stationary independent match rates
were `.90` for original/retained context and `.10` after instruction removal. The gate required
at least 80% workflow success at every position, a `.20` degradation margin, `.95` one-sided
confirmation and fit within a 512-call comparison envelope. A two-hour command horizon was
another planning constraint. These were joint usefulness/cost targets, not a promise about
arbitrary ordinary-session behavior.

The initial fixed-confirmatory search study enumerated 198 allocation/confidence/margin settings.
The diagnostic rate grid included endpoints, near endpoints, margin boundaries, reflected
rates and unequal arms. At `.95` confidence, the engineering error ceiling was `.055`
(`alpha + min(.005,.10*alpha)`), including false preservation, boundary preservation and false
loss. Passing this finite grid never established uniform error control over unknown true rates.

| Equal confirmation samples per arm | Preservation power at `.90/.90` | Worst tested error event | Passed error grid |
| --- | ---: | ---: | --- |
| 20 | 63.09% | 7.004% | No |
| 40 | 88.83% | 5.869% | No |
| 50 | 93.66% | 5.981% | No |
| 80 | 99.22% | 5.758% | No |
| 100 | 99.82% | 5.280% | Yes |

Only four initial settings passed every declared stress point: original/candidate counts,
confidence, margin `(5,5,.99,.20)`, `(10,10,.95,.20)`, `(50,50,.95,.10)` and
`(50,50,.99,.10)`. With eligible 100/arm confirmation, the affordable margin-.20 search
settings achieved only 0.517–0.520% workflow success for 5/arm at `.99`, or 25.784–33.131%
for 10/arm at `.95`. Required one-call/two-call budgets were 262/524 and 322/644. Neither
met the 80% usefulness gate or the two-call 512 limit. These probabilities conditioned on
successful calibration and all-valid observations; calibration readiness was not yet modeled.

A frozen follow-up tested 72 statistical settings, 11 confirmation settings and 288 statistical
search/confirmation combinations. Farrington–Manning and Miettinen–Nurminen confirmation at
40/arm passed the `.055` finite grid, with `.90/.90` powers 86.69%/86.63% and worst tested
errors 5.366%/5.081%. Even the nine combinations meeting error, confirmation-power and at least
one call-profile screen achieved at best 16.67% minimum workflow success. Neither alternative
was added to production. This bounded failure does not prove all counts or methods infeasible.

The score-method comparison used independent constrained-likelihood maximization: 23,900 count
pairs agreed within `2.86e-13`, and six upstream R vectors agreed at their published tolerances.
An initial overstrict statistic-tolerance failure was retained; the corrected tolerance was
frozen before gate evaluation. The pinned statsmodels score path had a known nonzero-difference
MLE defect, so it was not used as the score oracle. See the [upstream correction](https://github.com/statsmodels/statsmodels/pull/10270)
and [pinned reference tests](https://github.com/statsmodels/statsmodels/blob/5cc7275739c4a03d023a1f7ac29e16274b61e961/statsmodels/stats/tests/test_proportion.py#L1514-L1567).

## Exploratory screening and a separate holdout

The adopted search screen admits a complete eligible removal when the candidate-minus-original
point estimate is at least `-margin`. It attaches no preservation confidence or minimality claim
to that action. One candidate is selected durably without confirmation feedback; fresh original
and candidate arms then test it. Search never resumes after selection, and failed confirmation
cannot trigger reselection or repeated testing until a pass. The [sample-splitting argument](https://academic.oup.com/biomet/article/110/3/597/6955603)
motivates independent selection/confirmation; it does not prove Newcombe coverage, provider
independence, historical causality or campaign-wide confidence.

In the original six-comparison conditional workload, a 4-original/5-candidate point-estimate
screen with fresh 100/100 confirmation achieved **86.32%** minimum success and fit 256/512
one-call/two-call budgets. Its position probabilities were `.8631725339`, `.9189502698`,
`.9189744495`, `.9266704484`. A 4/4 point screen achieved 60.68%; the `.80` Newcombe loss-veto
screen achieved 79.62% at 4/4 and 79.37% at 4/5. Those 79% outcomes failed the 80% gate.
Larger 8/8 and 12/12 point screens reached 82.27%/91.76% but required 596/692 two-call budgets.
Only the 4/5 point screen met the then-current conditional usefulness/cost gate for both profiles.

This favorable result was narrow: minimum success fell to 42.05% for `.90/.80` original/retained
rates and 22.63% for `.50/.50`. Worst cap exhaustion was 10.41%, and instruction removal 3.20%.
Requiring two `.90`-rate calibration matches would multiply the conditional minimum by `.81`,
giving 69.92%. With no confirmation replacement slack, even 1% independent invalidity gives
only `.99^200=13.40%` probability of collecting all confirmation observations. These findings
required an unconditional workflow model before integration.

## Budget and failure-tolerance studies

The next frozen programme evaluated 312 named plans, including calibration readiness, incomplete
arms, cap exhaustion, complete selection and independent confirmation. It relaxed the original
512-call/two-hour development constraints into explicit cost comparisons rather than concealing
the extra work. Confirmation at 100 and 200 per arm passed the 150-point diagnostic grid:

| Confirmation per arm | Maximum false preservation | Boundary preservation | False loss | Power `.90/.90` | Power `.50/.50` |
| --- | ---: | ---: | ---: | ---: | ---: |
| 100 | 5.0957% | 5.2799% | 4.7908% | 99.8168% | 88.5420% |
| 200 | 5.1263% | 5.3548% | 5.0219% | 99.9999% | 99.2908% |

| Calibration valid/minimum matches; search original/candidate; confirmation per arm | Maximum attempts / conservative two-call budget | Useful success `.90/.90`, failures 0% / 1% | `.90/.80`, 0% / 1% | `.50/.50`, 0% / 1% |
| --- | ---: | ---: | ---: | ---: |
| 3/2; 3/5; 100; no extras | 251 / 502 | 80.95% / 6.50% | 38.65% / 3.10% | 6.79% / 0.55% |
| 3/2; 40/40; 100; eligibility-limited extras | 717 / 1,434 | 96.85% / 84.62% | 52.79% / 46.12% | 38.26% / 33.43% |
| 10/2; 80/80; 200; eligibility-limited extras | 1,438 / 2,876 | 99.998% / 88.71% | 82.70% / 73.36% | 96.70% / 85.79% |

Each probability is the minimum unconditional useful success across positions. Failures here
are independently replaceable safe preaccept exclusions. The original 5% excluded-attempt
cutoff sharply reduced completion near its boundary without proving outcome independence.
At 5% failures, the best original-workload plan in that family achieved only 2.6774% success.

A 224-plan follow-up replaced that arbitrary fraction cutoff with fixed attempt ceilings sized
before outcomes. The displayed plans used a 10% planning failure rate and `.999` per-arm
completion target. The small plan (calibration 3/minimum 2, search 4/5, confirmation 100/arm)
used ceilings 7, 8/9 and 124/arm: 357 attempts/714 conservative two-call budget. Minimum success
was 83.8754% at 5% failures and 83.1356% at 10%. The large plan (10/minimum 2, 80/80,
200/arm) used 1,694 attempts/3,388 calls and achieved 99.9974%/98.8117% for `.90/.90`,
82.7006%/81.7200% for `.90/.80`, and 96.7022%/95.5556% for `.50/.50`. The old large-plan
5% cutoff control achieved only 0.02805% at 5% failures despite a 2,876-call budget.

Under this safe-preaccept model, failures have zero provider calls; conservative reservations
still cover every permitted attempt. The small/large model-specific provider-call upper bounds
were 514/2,740. They are distinct from the 714/3,388 admission budgets, actual calls and time.
The maximum complete-search false-confirmation probability in the frozen scenarios remained
2.208678%; it is not a uniform error bound. Known outcome-dependent exclusions remain a refusal.

## Baseline sharing: failed prefetch and useful conditional design

The first sharing study retained strict sampling. Its main cost stage used 200,000 repetitions
in each of 24 stationary cells (4.8 million simulated workflows). Eager sibling-cohort policies
prefetched candidate work that could become unnecessary after an accepted removal. Fixed-six
one-call budgets were small strict/cohort-2/cohort-4 **357/333/325**, and large
**1,694/1,394/1,294**. Only large/cohort-4 met the initial arbitrary 20% fixed-budget threshold.
Actual adaptive costs showed why that arithmetic was insufficient:

| Instruction position | Strict mean calls | Eager cohort-4 mean calls | Discarded-prefetch mean calls |
| --- | ---: | ---: | ---: |
| 0 | 1,369.9940 | 1,209.9984 | 0.0008 |
| 1 | 1,210.0012 | 1,209.9984 | 80.0060 |
| 2 | 1,209.9980 | 1,210.0008 | 80.0048 |
| 3 | 1,050.0012 | 1,210.0040 | 160.0048 |

At position 3, simultaneous cost bounds demonstrated at least **136.7484 additional expected
calls**, with comparable completed useful work. Adaptive worst-path allowances were larger
than fixed-six arithmetic: small strict/cohort-2/cohort-4 357/376/403 and large
1,694/1,894/2,194. Cost rejection stopped adoption evaluation: the full 8,632-tail risk family
was not run. The 13-scenario, 200-replicate pilot was descriptive, not risk qualification.

The follow-up prospectively removed the unjustified 20% and benefit-at-every-position rules.
Its objective was **expected total charged calls / unconditional probability of a confirmed
useful reduction**, using ratios of weighted expectations rather than averages of cell ratios.
The frozen mixture equally weighted positions and two byte shapes, `128/128/128/128` and
`128/128/256/512`; these weights describe a synthetic benchmark, not user traffic. Lazy sharing
froze sibling order and membership, sampled the original and first candidate, then sampled a
second sibling only after retention. Acceptance or parent change ended reuse. Confirmation
always remained fresh.

The 280-cell stationary finite-sum study produced:

| Configuration | Expected charged calls | Useful probability | Calls per useful result | Minimum equal mildly degraded success |
| --- | ---: | ---: | ---: | ---: |
| Large strict 80/80 | 1,209.997946 | 99.998767% | 1,210.012860 | 82.701140% |
| Large lazy group 2 | 1,129.996461 | 99.998767% | 1,130.010389 | 82.701140% |
| Large lazy group 4 | 1,129.996461 | 99.998767% | 1,130.010389 | 82.701140% |
| Strict 40 original / 80 candidate | 1,009.984239 | 99.993138% | 1,010.053544 | 79.650861% |

Group 2 saved **80.002471 calls per useful result (6.611704%)**. Group 4 added only about
`6.9e-9` calls of benefit, below numerical allowances. The simpler strict 40/80 allocation saved
16.525388% on the primary workload but missed the 80% mildly degraded position-0 floor by
0.349139 percentage points. That tradeoff was retained without rounding it into a pass.

A prospective operational study compared large strict and lazy group 2: seed `2026100707`,
20,000 replicas per cell, 11 scenarios, two shapes and four positions. Its first run stopped
at the 120-second resource ceiling with no completed report. Removing duplicate simulation of
the two byte shapes preserved every outcome stream and criterion; the successful run took
76.675 seconds internally. The 176 scored cells reused 1,760,000 policy workflows; 3,520,000
shape scores were not independent samples. Simultaneous coverage used `.01/10,000` tails,
exact-binomial endpoints and bounded-mean Hoeffding cost limits.

Healthy calls per useful result were 1,210.000 strict versus 1,130.001 lazy. Simultaneous
envelopes `[1,178.517669,1,242.340213]` and `[1,100.377130,1,160.426187]` resolved at least
18.091482 fewer calls. With 5% safe failures they were 1,273.706 versus 1,189.497. Mildly
degraded results were 1,420.764 versus 1,321.107 without failures and 1,499.915 versus
1,394.180 with 5%; minimum lazy usefulness lower bounds were 81.709704% and 81.324039%.

Expiry was a decisive limit. With an exclusive lifetime of 240 synthetic dispatch slots, all
20,000 lazy workflows at each of positions 0–2 expired without confirmation; only position 3
succeeded. Aggregate useful probability was 25%, giving **1,960 calls per useful result**
despite smaller raw consumption of 490 attempts; strict remained 1,210. A compatible seven-slot
pause retained benefit, while stale resume refused after 11 charged attempts without a
behavioral-loss classification. Synthetic slots do not establish freshness in native seconds.

For the declared independent constant-rate fresh-confirmation model, the maximum component
error plus allowance was `.053547757881` and factorized additional selected-result risk upper
bound `.001837546839`, below `.055`/`.005` gates. Realized intervals were weaker: 12/176
paired-publication upper bounds exceeded `.005`; all 64 realized conditional-error upper
bounds were unresolved, with 288 empty selected-null categories. Qualification used the
prospectively declared independent component factorization, not those unresolved intervals.
Drift within confirmation, unknown provider rates and uniform coverage remain unqualified.

The result selected lazy groups of two as a useful **synthetic** design. Production sharing
remained disabled pending native savings, finite freshness admission and composed ownership/
resume validation. It did not justify an epoch framework or a default change.

## Native-layout reconciliation and accepted envelope

Actual native message encodings changed the search path/budget calculation. The four-group
layout with the small plan and a ten-comparison cap achieved only 77.2418%, 77.2038% and
76.1337% minimum modeled usefulness at 0%, 5% and 10% independent safe preaccept failures.
The failure-free position-0 cap-exhaustion probability was 14.8483%. A prospectively frozen
comparison of caps 14 and 20 selected **14**, the smallest tested pass: 91.5047%, 91.4589%,
90.1717%. Twenty added about `.00013` percentage points at the worst position.

That selected configuration reserves `7 + 14*(8+9) + 2*124 = 493` attempts: at most 493
calls for final response or 986 for the two-call Copilot shell profile. The arithmetic protects
fresh confirmation and is an admission ceiling, not expected consumption. The actual three
successful native runs each used ten comparisons and 293 attempts. Their exact profiles,
75% editable-byte removal, 100/100 matches per confirmation arm, runtime and conservative
call charges are in [measurements](../measurements.md); those results are not a general native
success-rate estimate. Original/retained mild degradation and noisy workloads remain outside
the selected configuration's demonstrated useful envelope.

The accepted scope on 2026-10-15 was Codex final response, Copilot final response and Copilot
denied shell action at approximately 10k locally estimated input tokens. **100k qualification
was deferred** for quota/cost reasons. Larger source preparations and pure reconstruction did
not establish 100k execution, token savings, money savings or capacity. Complete-command and
aggregate runner clocks were accepted; separate candidate-construction/initialization spans
were unavailable. No native comparison established sharing savings or finite freshness, so
strict fresh baselines remained the production policy.

## Failed and interrupted native predecessors

The accepted runs did not erase unsuccessful predecessors. The retained historical aggregate
record includes these separately bounded experiments; it is not a random sample of campaigns.
Call columns distinguish known starts, additional possible calls and pending reservations.
Pending dispatches in interrupted runs were never permission to replay them.

| Profile / stop reason | Known / additional possible / pending calls | Conservative charge | Whole-command time |
| --- | --- | ---: | ---: |
| Codex initial interruption; search and confirmation incomplete | 56 / 0 / 0 | 56 | 287.19 s |
| Codex HTTP 503 during confirmation | 158 / 0 / 0 | 158 | 835.44 s |
| Copilot final response interrupted during confirmation | 150 / 0 / 1 | 151 | 1,422.58 s |
| Copilot shell interrupted during confirmation | 141 / 141 / 2 | 284 | 1,251.47 s |
| Codex `usageLimitExceeded`; search incomplete | 45 / 0 / 0 | 45 | 216.85 s |
| Codex `responseTooManyFailedAttempts`, HTTP 429; search incomplete | 9 / 0 / 0 | 9 | 53.42 s |
| Codex same structured error with HTTP 429; separate search | 21 / 0 / 0 | 21 | 131.15 s |
| Copilot shell optional-metadata rejection in calibration | 1 / 1 / 0 | 2 | 12.14 s |
| Codex later user interruption | 111 / 0 / 0 | 111 | 2,276.26 s |
| Codex paced structured overload; search incomplete | 23 / 0 / 0 | 23 | 514.71 s |

The HTTP 503 run completed its search, retained the instruction and removed 75% of editable
bytes, but confirmation stopped after 31 original and 33 candidate valid matches followed by
one error. It did not prove safe preacceptance or outcome-independent exclusion, so confirmation
was ineligible. The two interrupted Copilot searches likewise completed their sweeps and 75%
removal, but their holdouts stopped at 29/28 and 31/17 original/candidate matches. Neither
established preservation. All remain distinct from the later successful runs.

An earlier Copilot control also failed with empty output and authentication-error signals;
its possible call remained charged. After operator reauthentication, two separately requested
one-call smokes succeeded and remained separate from the controlled cohort. A simultaneous
control invocation was investigated from durable state and process evidence: there was no
unresolved reservation or Copilot observation, and the ownership/dispatch audit established
zero Copilot dispatch. Its generic failure message alone did not prove the cause. Subsequent
controls ran serially. A changed Codex installation was refused by pinned
preflight before a provider call; redetection and fresh controls preceded further execution.

The controls exposed parser defects relevant to validity: a completed tool-free response under
a shell target must be a valid nonmatch, and optional informational reasoning/opaque metadata
must not invalidate an otherwise proved completed outcome. Those bounded cases were corrected
and tested without loosening action/start/denial or completion proof. Earlier terminal failures
and charges were not rewritten. The final short-control cohort produced two full matches, two
retained matches and two removed nonmatches for each of the three profiles. No behavioral
conclusion was inferred from a protocol error.

The 429 errors did not distinguish allowance exhaustion from request-rate throttling and supplied
no reset time or retry delay. The usage-exhaustion error, generic HTTP 503 and later structured
overload are distinct categories; they do not justify one general retry rule. These outcomes
explain why successful-run cost is not expected campaign cost and repeated successful runs
cannot be selected to recover the original nominal error claim.

## Overload replacement: separate safety and inference limits

Pacing did not eliminate every external failure. On 2026-10-14 a 24-attempt, 15-second-paced
full-original diagnostic produced 24 matches with no errors, followed by a paced full run that
stopped after 23 charged calls with 12 matches, ten nonmatches and one structured
`serverOverloaded` error. No confirmation ran. The native error had `willRetry:false`, no
captured assistant/reasoning/tool output, verified cleanup and unchanged protected state.
Acceptance was unknown. It was retained as an error, not treated as a behavioral loss.

The prospectively frozen opt-in `safe-provider-overload` policy permits a new attempt only for
that structured no-output category with exact provenance, enforced action denial and verified
safety/cleanup. Generic 429/503, transport failure, timeout, cancellation, quota exhaustion and
malformed events keep their terminal handling. The failed observation and full conservative
charge remain; unknown acceptance remains unknown. A replacement has a new identity within
the existing arm ceiling and consumes existing call/time/freshness capacity. It never resolves
an unknown dispatch by replay or repeats completed confirmation. The default remains
`independent-safe-preaccept-only`.

Its estimand is `P(match | valid, frozen plan)`. With independent stationary joint draws of
validity and behavior and a prespecified validity rule, the first fixed number of valid marks
has that conditional distribution, including conditional arm completion within the fixed
ceiling. Within-draw independence of validity and behavior is not required for this conditional
estimand. Between-attempt dependence, drift or outcome-driven replacement invalidates the
argument. Preservation does not establish equal reliability or all-attempt match probability.
Accepted overloads were not modeled in the native-layout simulation, and the successful Codex
qualification used **no overload replacement**.

## Reproducibility and remaining limits

The public tree retains the 40 independent formula vectors, statistical unit/property tests,
sampling/budget/state tests, sanitized harness fixtures and native aggregate measurements.
Those verify current formula and implementation contracts; they do not rerun the original
workflow studies or expose private native evidence. The original temporary simulation drivers,
full numerical reports and some raw Step 1 artifacts are **not available in the public tree**.
Historical source/result hashes in development records cannot reconstruct absent programs.
The studies above are retained historical results with independently reviewed calculations,
not a claim of fresh reproducibility from the current checkout. Repeating a study requires a
new explicit driver, prospective programme and validation, rather than silently replacing its
historical evidence.

Exact finite binomial/path enumeration was used where affordable; Monte Carlo is identified
for the sharing operational studies. Independent formula vectors, numerical agreement,
workflow usefulness, operational validity and live safety answer different questions. For
Monte Carlo, prospective family sizes and simultaneous exact-binomial bounds controlled
simulation uncertainty; they did not make the behavioral interval exact. See [NIST's
exact-binomial bounds](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm).
The [NIST sample-size approximation](https://www.itl.nist.gov/div898/handbook/prc/section2/prc242.htm)
was planning context, not validation of the classifier or defaults.

Alternative-method research also did not establish a shortcut: [Shan, Lou and Wu (2023)](https://link.springer.com/article/10.1007/s44199-023-00054-8)
reported regime-dependent performance; the [Hawila–Berg exact-corrected proposal](https://arxiv.org/abs/2104.04660v3)
was challenged on monotonicity/coherence in a [2024 comment](https://arxiv.org/abs/2404.08352).
Changing the method would require demonstrated error, numerical and workload-cost benefit.
