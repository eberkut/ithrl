# Measurements

Ordinary Codex and Copilot saved-session experiments removed **580** and **6,512 additional UTF-8
bytes** from retained prior-user text and passed fresh preservation confirmation. An earlier Codex
whole-record reduction lowered mean confirmation input tokens by **50.17%**. This page reports
these results, synthetic fuzzing campaigns and their costs, followed by reference measurements.

For execution instructions, see [native runs](native-runs.md). For statistical assumptions,
validity and call accounting, see [results](results.md). Results apply to the recorded workloads,
predicates and runner policies. General session success rates, robustness, causal explanations and
monetary savings remain unmeasured.

Harness versions identify the measured installations. Compatibility depends on detected operations,
capabilities and safety controls; there is no exact-release allowlist. Durations below are rounded
to the nearest second; KiB and MiB use binary units.

## Saved-session experiments

Saved-session experiments select decisions from existing coding sessions and use predicates chosen
before collection. Their raw session content, identifiers and routing remain private;
only aggregates and reviewed, sanitized interpretations are published.

### Ordinary-session paragraph removal

These final-response experiments tested finer removal after whole-record reduction on ordinary
saved sessions. The deterministic `paragraph-v1` policy removed sections from one eligible retained
prior-user text field while preserving the fixed triggering input. The usefulness criterion was at
least **512 additional UTF-8 bytes** removed relative to the frozen coarse candidate; that byte
criterion is separate from behavioral confirmation.

| Measurement | Codex | Copilot |
| --- | --- | --- |
| Measured native installation | CLI `0.167.0` | CLI `1.0.104`, native-prefix SDK reconstruction |
| Additional text removed from coarse candidate | 580 UTF-8 bytes; selected field 1,282 → 702 bytes | 6,512 UTF-8 bytes |
| Whole-record removal | 34 source context units | 70 of 181 source context records |
| Calibration observations | 3 | 3 |
| Exploratory observations | 4 | 52 across 8 coarse and 5 fine comparisons |
| Fresh original / selected matches | 11/11 / 11/11 | 20/20 / 20/20 |
| Candidate-minus-original lower bound | −19.7405 percentage points | −11.9158 percentage points |
| Confirmation rule | Newcombe method 10; margin 20 points; nominal 95% one-sided confidence | Same |
| Total observations | 29 | 95 |
| Actual / additional possible / pending calls | 37 / 0 / 0 | 95 / 0 / 0 |
| Known input + output tokens | 3,334,167 | 4,697,910 |
| Durable workflow elapsed time | 14 min 31 s | 1 h 34 min 19 s |

Both lower bounds exceed −20 percentage points. Confirmation observations were fresh, disjoint from
calibration and search, and compared the selected coarse-plus-fine result with the immutable full
original. The Codex result used a separately frozen reduced confirmation allocation, approved before
collection. No old samples were pooled. All recorded observations were valid; no possible or pending
calls remained in these completed runs. The bounded searches establish no sampled minimality,
necessity or global optimum.

Codex mean input tokens per confirmation observation fell from **126,911 to 104,911.91**, or
**17.334%**. Copilot fell from **59,092 to 30,313**, or **48.702%**. These are selected
**coarse-plus-fine versus full-original** comparisons, including all requests contributing to each
observation. The experiment did not measure a confirmation token difference between the coarse
candidate and the fine candidate; none of these token savings can be attributed to the paragraph
edit alone. Cached input is included in input totals; measured token use does not establish monetary
savings.

The Codex measurement qualified a private candidate wheel on its exact uncompacted workload and
runtime. Its private wire auditor checked all 37 external requests and continuations against the
source projection, selected edit, fixed trigger and settings, with explicitly recorded native
normalizations. That audit is evidence for the private experiment; it is not a claim that the
shipped runner exposes the same audit instrumentation. It does not prove complete historical
request equivalence, provider-internal interpretation, or compaction support on `0.167.0`. Opaque
reasoning remained opaque. Public compatibility and integration boundaries are described in
[native runs](native-runs.md#compatibility-and-measurements).

Both workflows verified unchanged source/workspace and cleanup, with zero executed actions or
tool-side network effects. Codex exercised active interruption/resume and completed-result resume;
the latter republished a summary snapshot without new observations or calls. A surrounding audit
wrapper initially rejected the new summary path; the retained result and sampling state were then
verified unchanged. This wrapper error remains part of the investigation history.

The [worked investigation](research/ordinary-session-investigation.md) explains the Codex debugging
question, retained evidence, interpretation and next action.

The integrated wheel and sdist-built wheel reproduced both workloads' original, coarse and selected
materializations without provider requests. Codex worker requests, rendered configuration and
safety facts also matched the qualified build; its reconstruction identifier changed deliberately.
The Copilot runner modules are unchanged from its qualified wheel. These comparisons verify the
integration boundary, not a new provider-backed experiment. The historical Copilot search used
partition-first fine traversal; current search tries all selected sections first. Its old result
requires the original build for inspection and cannot resume under the current traversal. The
integrated inspector accepts the final Codex result. Installed credential-free workflows cover the
current CLI, inspection and recovery contracts.

#### Investigation cost and unsuccessful work

The final successful run is part of a larger qualification investigation. The following ledger
keeps scoped subtotals separate from their components. Calls are inference requests; native
authentication and model-catalogue traffic are outside these inference counts. Monetary cost is
unknown throughout.

| Scope | Known calls | Additional possible calls | Known input + output tokens | Outcome or accounting limit |
| --- | ---: | --- | ---: | --- |
| Current Codex selection, including its final 37-call run | 75 | 1 | Incomplete aggregate | 76 conservatively charged; separately approved earlier investigations excluded |
| Earlier Codex mixed-field selection, pilot and stopped search combined | 103 | 12 | 4,984,018, incomplete | Selected mixed field was removed during coarse search; no fine search or confirmation |
| Earlier Codex literal-predicate workload | 7 | Unresolved outer-process envelope | 1,014,768 known | Seven nonmatches; calibration failed; observer/cleanup uncertainty remained |
| Copilot pilot and reconstruction controls | 7 | 0 | 423,523 | Three calibration and four control observations; no search or confirmation |
| Copilot completed reduction, separate from its pilot | 95 | 0 | 4,697,910 | Confirmed result reported above |

The current Codex selection's **75 known plus one possible** requests already include the completed
37-call run; adding that run again would double-count it. Its full token use and elapsed duration
have not been reconciled. Separate private diagnostic and aborted-run receipts remain retained;
the scoped 76-call charge is not a global total across every earlier approved investigation.
The earlier mixed-field selection's 103-known/12-possible subtotal likewise already includes its
pilot. Its search component lasted 18 min 25 s; complete pilot-plus-search elapsed time is unknown.

For the failed literal-predicate workload, the retained observer receipt permits an outer envelope
of up to **2,548 total requests including the seven observed requests**. This is an uncertainty
bound, not observed spend. The seven requests took 5 min 13 s of aggregate runner time; the
9 min 6 s receipt-publication interval is not an exact whole-workflow duration. Missing observation
or cleanup evidence cannot be treated as zero cost or a successful safety check. These historical
uncertainties prevent an exact all-investigation call, token or elapsed-time total.

The Copilot pilot lasted 5 min 8 s and the completed workflow 1 h 34 min 19 s; their separately
measured intervals total 1 h 39 min 27 s, excluding unmeasured preparation. They used **102 actual
requests** and **5,121,433 known tokens** combined. Pilot calibration was not reused for final
confirmation. Failed, stopped and diagnostic attempts never enter confirmation denominators, and
no expense or savings claim is inferred from these resource counts.

### Confirmed Codex reduction

A final-response case ran through `codex-app-server-inject-items-v1` on Codex CLI `0.166.0` and
Python `3.12.3`, retaining the captured model/provider routing. Before collection, the experiment
set a usefulness target: a nonempty confirmed reduction with at least 25% lower mean input tokens
per confirmation observation.

| Setting | Configuration |
| --- | --- |
| Calibration | 3 valid observations, at least 2 matches, at most 7 attempts |
| Exploratory comparison | Fresh original 20 valid / 30 attempts; candidate 20 valid / 30 attempts |
| Search limit | 2 comparisons |
| Fresh confirmation | Original 100 valid / 124 attempts; selected candidate 100 valid / 124 attempts |
| Preservation rule | 20-percentage-point margin; Newcombe method 10, nominal 95% one-sided confidence |
| Execution limits | 375 attempts; 6,000 physical requests; 16 requests per attempt; 120 seconds per attempt |
| Time policy | 16-hour wall limit; 15-second inter-attempt pacing; 24-hour evidence freshness |
| Sampling policy | Strict fresh original baselines; opt-in `safe-provider-overload` eligibility |

Calibration matched in two of three valid observations. Two exploratory comparisons selected
**180 of 218 context records**, removing 38. Fresh confirmation matched **69/100 original** and
**67/100 candidate** observations. The estimated candidate-minus-original difference was
**−2 percentage points**, with bounds of approximately **−12.71 to +8.78 points**. The lower bound
exceeds −20 points, confirming preservation within the declared tolerance against the fresh original
baseline.

Mean input tokens per valid confirmation observation fell from **65,043 to 32,409.66**, a **50.17%
decrease**. Each mean includes all physical requests contributing to an observation: 100 original
observations used 100 requests; 100 candidate observations used 108. Input usage was known for every
observation.

| Measurement | Observed value |
| --- | --- |
| Observations | 283 valid; zero invalid observations or runner errors |
| Provider calls | 293 actual and charged; zero additional possible or pending |
| Input tokens | 13,933,472, including 299,700 cached input tokens |
| Output tokens | 126,011 |
| Reported CLI wall time | 2 h 4 min 37 s, including pacing |
| Aggregate runner time | 51 min 23 s |
| Native output per observation | Maximum 465.3 KiB; median 461.2 KiB |
| Peak disposable state | 4.06 MiB across 104 entries |

The 6,000-call ceiling covers the attempt limits and native continuations. Whole-command startup
and exit were not separately timed. Cached input is included in the input total.

Confirmation followed durable candidate selection and used observations disjoint from calibration
and search. Counts, usage and the interval were checked against the saved native artifacts. Every
attempt verified unchanged source/workspace and cleanup without residual state; zero actions
executed or tool-side network effects occurred.

Search stopped at the two-comparison limit with coverage incomplete. Confirmation applies to the
frozen literal predicate and declared margin; minimality remains unestablished. The original's
69% match rate is the baseline for this experiment.

## Native context fuzzing

The qualified final-response profiles completed discovery, intervention simplification and one fresh
confirmation using `gpt-6.1-sol`:

| Harness | Measured installation | Controlled surface | Provider |
| --- | --- | --- | --- |
| Codex | CLI `0.164.0` | `codex-app-server-inject-items-v1` | `openai`, ChatGPT Responses endpoint |
| Copilot | CLI `1.0.104`; Python SDK `1.0.28` | `copilot-native-prefix-sdk-v1` | `github-copilot`, managed endpoint |

### Workload and controls

The editable surface is one eligible prior-user text field. An earlier message establishes a
response token, and the fixed triggering input asks for the latest active token. The stress append
contains three irrelevant notes and one instruction revising that token. The target checks for
the original token. This competing instruction makes the campaign's intended behavioral change
explicit. Public sources and the example configuration are under
[`examples/fuzz/`](https://github.com/eberkut/ithrl/tree/main/examples/fuzz).

Each profile passed a six-observation identical-text duplication control: four original and two
duplicated-input observations matched. Reconstruction checks verified the exact append, unchanged
remaining history, fixed triggering input and policy settings. Copilot resumed the native prefix
without an added user message; native input receipts matched that prefix in every campaign attempt.
Complete provider prompts may contain further harness context.

### Configuration and confirmed result

| Setting | Measured configuration |
| --- | --- |
| Mutation generation | Both campaigns: seed `20261027`, `append-stale-text` and stress text from `examples/fuzz/campaign.toml` |
| Calibration | 3 valid observations, at least 2 matches, at most 7 attempts |
| Exploratory comparison | Fresh original 8 valid / 18 attempts; mutant 8 valid / 18 attempts |
| Exploratory threshold | Original-minus-mutant match-rate decrease of at least 40 percentage points |
| Fresh confirmation | Original 20 valid / 36 attempts; selected mutant 20 valid / 36 attempts |
| Final inference | One selected finding; exact Clopper–Pearson union-bound method, `alpha=.05` |
| Required decrease | Strictly greater than 10 percentage points |
| Search limits | One generated candidate; at most 10 simplification trials |
| Provider-call ceiling | Codex: 350; Copilot: 339 |
| Time policy | 6-hour wall limit; 120-second attempts; 24-hour freshness |
| Inter-attempt pacing | Codex: 15 seconds; Copilot: zero |
| Eligibility | Opt-in `safe-provider-overload`; no overload replacement occurred |

The measured Codex profile declared a one-request allowance per attempt. The
[native-run guide](native-runs.md#run-the-synthetic-native-campaign) scales the total allowance for
the current default cap of 16; Copilot's cap remains one.

Each campaign completed one discovery and five simplification comparisons. Calibration matched in
all three observations. Simplification reduced the **1,676-byte initial append** (1,674 stress-text
bytes and two separating newlines) to **142 UTF-8 bytes**, a **91.5% decrease**. It changed only the
append; the original history remained intact.

Fresh confirmation produced **20/20 original matches and 0/20 mutant matches** for each harness.
The estimated original-minus-mutant difference was **100 percentage points**. The exact confirmation
method gave a one-sided lower bound of approximately **66.31 points**, exceeding the required 10
at `alpha=.05`. Both ended `confirmed-change`. All 40 fresh confirmation observations followed
durable selection in one block, with distinct attempts and no
repeated completed holdout. See [fuzz results](results.md#fuzz-results) for interpretation and
[the research method](research/context-fuzzing.md#confirmation-method-and-assumptions) for
assumptions.

### Calls, duration and resource use

| Workflow | Actual / additional possible / charged calls | Known input + output tokens | Whole-command time |
| --- | --- | --- | --- |
| Codex duplication control | 6 / 0 / 6 | 30,820 | 1 min 44 s |
| Codex fuzz campaign | 139 / 0 / 139 | 722,235 | 46 min 12 s |
| Copilot native-prefix duplication control | 6 / 0 / 6 | 18,325 | 1 min 8 s |
| Copilot native-prefix fuzz campaign | 139 / 0 / 139 | 431,763 | 27 min 1 s |

Each campaign recorded 139 valid observations, zero runner errors or invalid observations, and zero
unknown provider acceptance or token usage. No possible or pending calls remained. Codex used
15-second inter-attempt pacing; Copilot used none. Provider, native envelope and pacing differences
prevent a direct comparison of harness speed.

| Resource | Codex campaign | Copilot native-prefix campaign |
| --- | --- | --- |
| Aggregate runner time | 10 min 54 s | 26 min 2 s |
| Reported CLI wall time | 46 min 9 s | 26 min 54 s |
| Native output maximum / median | 12.6 / 11.2 KiB | 153.4 / 79.6 KiB |
| Peak disposable state | 3.64 MiB / 104 entries | 235.56 MiB / 405 entries |

Whole-command time includes process startup and exit. Every attempt verified unchanged
source/workspace and cleanup without residual state. No actions executed or tool-side network
effects occurred. The Copilot campaign needed no catalogue retries; provider-free fault injection
checked bounded catalogue recovery, terminal certificate failures and inference non-replay. See
[troubleshooting](troubleshooting.md#native-setup-and-preflight) for recovery limits.

Separate interrupted Codex work consumed 34 actual calls. Including that work and the control,
Codex qualification consumed **179 actual calls**. Copilot native-prefix preparation used six
calls for an earlier control and three before catalogue retrieval stopped a campaign. A TLS refusal
used zero model calls. Including the fresh control and completed campaign, native-prefix
qualification consumed **154 actual calls**. The fresh control's eight-call ceiling and campaign's
339-call ceiling kept those runs and the three interrupted calls within a 350-call allowance.
Failed observations contribute no behavioral losses or confirmation samples.

Copilot shell-action fuzzing remains unsupported.

## Reference measurements

### Earlier Copilot reductions

Two Copilot CLI `1.0.104` cases completed fresh 20-observation original and candidate arms. Both
used a 20-percentage-point preservation margin and nominal 95% one-sided Newcombe bounds. Their
retained summaries and state do not name the reconstruction method. These measurements therefore
provide no qualification evidence for the [native-prefix SDK](#native-context-fuzzing).

| Target | Context records | Original / candidate matches | Difference bounds (percentage points) | Actual calls | Recorded duration |
| --- | --- | --- | --- | --- | --- |
| Final response | 181 → 111 | 20/20 / 20/20 | −11.92 to +11.92 | 74 | 1 h 12 min 38 s |
| Denied shell request | 109 → 61 | 19/20 / 20/20 | −7.53 to +19.60 | 86 | 29 min |

Both candidates were confirmed preserving among valid observations. The final-response search
reached its eight-comparison cap with coverage incomplete.

All 40 confirmation observations in each case reported input usage. Mean input tokens per
observation fell from **59,092 to 31,991** for final response (45.86% lower), and from **49,541 to
27,235** for the shell request (45.04% lower). Each arm's median equaled its mean. No possible or
pending calls remained. Source/workspace checks passed. The shell workflow observed 141 requested
actions, denied all 141 and executed zero.

### Inconclusive and incomplete reductions

An earlier Codex case selected 189 of 218 context records. Fresh confirmation matched 19/20 original
and 11/20 candidate observations, with difference bounds of approximately −58.20 to −17.90
percentage points. Preservation remained inconclusive at the 20-point margin. Three runs on a
different saved-session case stopped on execution errors. Their costs remain part of the retained
evidence:

| Earlier Codex outcome | Actual / additional possible / charged calls | Reported CLI wall time |
| --- | --- | --- |
| Inconclusive confirmation | 91 / 269 / 360 | 45 min 31 s |
| Search interrupted by a runner error | 89 / 99 / 188 | 38 min 8 s |
| Transport stream lost during confirmation | 151 / 1 / 152 | 58 min 27 s |
| Local refusal after four completed requests | 85 / 0 / 85 | 29 min 16 s |

These four runs recorded **416 actual calls and 369 additional possible calls**, conservatively
charged as 785. The possible calls remain included in that charge. The local refusal followed four
completed HTTP 200 requests; its retained `provider-transport` error does not identify whether the
relay stopped or the request cap was exhausted. See
[execution diagnostics](troubleshooting.md#provider-errors-and-interruption) for current error
handling.

A separate Copilot reduction stopped after a structured quota refusal, with 74 actual calls and
no preservation verdict. All these failures contribute execution cost and no behavioral losses.
Protected-state and cleanup checks passed for the four Codex runs.

### Bounded fuzz campaigns

One final-response campaign per harness screened two mutations on saved-session cases. Each
collected 18 valid observations and used 18 actual provider calls, with zero runner errors or
executed actions. Both ended `no-candidate-found`: neither selected a mutation for fresh
confirmation.

### Earlier Copilot semantic-import profile

An earlier campaign used `copilot-semantic-session-import-v1`, which supplied a triggering message
and added a per-attempt `<current_datetime>` element during its transformation. It used the same
sampling settings, zero pacing and a 350-call ceiling. It also reduced the append to 142 bytes and
confirmed 20/20 original versus 0/20 mutant matches with the same exact bound. That result applies
to semantic import; the native-prefix campaign above qualified the current reconstruction.

The campaign used 139 actual calls, with token usage unavailable for all observations. Whole-command
time was 23 min 58 s; CLI wall time was 23 min 50 s and runner time 23 min 15 s.
Native output peaked at 22.9 KiB, with median 11.0 KiB. Peak disposable state was
235.70 MiB across 382 entries. Cleanup and protected-state checks passed; zero actions
executed. Including nine control calls, three of which failed, qualification consumed 148 actual
calls. Unknown usage remains explicit and cannot be interpreted as zero token use.

## Synthetic reduction reference

The planted reduction workload has four independent prior-user history groups followed by a fixed
trigger. One group contains an instruction; three contain irrelevant filler. The trigger requests
the earlier instruction with a fallback when absent. The target is exactly `ITHRL_M4_CONTEXT_OK`
for final response, or a native `bash` request for `pwd` with execution denied. Prompt content,
instruction position, targets and sampling settings were fixed before collection.

The larger cases used 10,000 text bytes per group, approximately 10k input tokens by a bytes/4
estimate that excludes harness context. Editable bytes count native history-message encodings,
excluding the fixed header, envelope, trigger and stored evidence. All three completed runs kept
the instruction-bearing group and removed the three irrelevant groups: **75% of editable bytes**.

Search used four fresh original and five candidate observations, with eight and nine attempt limits.
Ten comparisons completed each single-group sweep within a fourteen-comparison limit. Calibration
used 3 valid / 7 attempts with 2 matches required; fresh confirmation used 100 valid / 124 attempts
per arm, a 20-percentage-point margin and nominal 95% one-sided Newcombe bounds. All confirmation
arms matched 100/100, with candidate-minus-original bounds of approximately −2.63 to +2.63 points.

| Profile | Measured CLI | Editable bytes before → after | Actual / additional possible / charged calls | Whole-command time |
| --- | --- | --- | --- | --- |
| Codex final response | `0.162.1` | 40,376 → 10,094 | 293 / 0 / 293 | 1 h 41 min 26 s |
| Copilot final response | `1.0.95` | 40,620 → 10,155 | 293 / 0 / 293 | 37 min 17 s |
| Copilot denied `bash` request | `1.0.95` | 40,620 → 10,155 | 293 / 293 / 586 | 35 min 46 s |

All 293 observations per run were valid with zero runner errors. Cleanup and unchanged
source/workspace checks passed; zero actions executed. Shell accounting includes up to one
additional possible continuation per attempt. Copilot token usage was unavailable throughout.
Codex used 2,728,585 input tokens (including 1,462,528 cached) and 3,484 output tokens, with
24 min 49 s of runner time and 15-second pacing. Peak disposable state was 3.49 MiB/104 entries
for Codex, 196.67 MiB/345 for Copilot final response and 196.67 MiB/345 for shell.

This workload established instruction-retaining reduction and confirmation. Earlier failures and
interrupted work are recorded in
[the reduction research](research/reduction-design.md#failed-and-interrupted-native-predecessors).
100k qualification remains deferred expansion work.

## Statistical reference checks

Reduction confirmation uses the uncorrected independent-arm Wilson hybrid-score construction from
Newcombe method 10. At `one_sided_confidence = 0.95`, each bound uses the normal quantile at `.95`;
the corresponding central interval has nominal coefficient `.90`. Preservation uses the lower
bound for candidate-minus-original match probability.

Public
[reference vectors](https://github.com/eberkut/ithrl/blob/main/tests/fixtures/statistics_reference.json)
were generated independently using statsmodels `0.15.0`, `confint_proportions_2indep`,
`method="newcomb"`, `compare="diff"` and `alpha = 2 * (1 - confidence)`. They cover unequal samples
and boundary counts. From a development checkout, run:

```bash
uv run pytest -o addopts= tests/unit/test_statistics.py -q
```

The checks reproduce the stored vectors. The
[reduction study package](https://github.com/eberkut/ithrl/tree/main/research/reduction) replays
synthetic sharing, failure, freshness and layout-budget models. The
[fuzzing study](research/context-fuzzing.md) describes the exact confirmation method and its
synthetic error/usefulness evaluation. Numerical agreement alone cannot establish native usefulness.
