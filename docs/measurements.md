# Measurements

Synthetic qualification uses `gpt-6.1-sol`; saved-session experiments retain the captured model and
provider configuration. The synthetic reduction sections provide a reference for interpreting the
[example experiment](native-runs.md#2-configure-the-experiment). The
[native context-fuzzing section](#native-context-fuzzing) records completed append-text workflows
and their measured policies.

[Saved-session experiments](#saved-session-experiments) report useful reductions, unsuccessful
searches and execution failures separately from synthetic qualification.

Positive results apply to the reconstruction, safety policy and budgets recorded for those runs.
They do not qualify a changed profile or guarantee that a selected administrative request cap
will complete another native turn.

## Workload

Each case contains four independent prior user-history groups followed by a fixed user trigger.
The first history group contains a planted instruction; the other three contain irrelevant
filler. The trigger asks for the earlier instruction and supplies a fallback when it is absent.
Prompt content, instruction position, targets and sampling settings were fixed before execution.

The final-response target is exactly `ITHRL_M4_CONTEXT_OK`. The shell-action target is one native
`bash` request for `pwd`, visible to ithrl with execution denied. Short controls use 512 text bytes
per history group and compare full, instruction-retained and instruction-removed context. Larger
cases use 10,000 text bytes per group, approximately 10k input tokens using a text-bytes/4 estimate.
That estimate excludes harness-added context and is separate from provider-reported usage.

Editable bytes count materialized native history-message encodings. They exclude the fixed
header, envelope, trigger and stored evidence. Each completed run retained the instruction-bearing
history and removed the three irrelevant groups. Native envelope/protected records remained.

## Sampling and confirmation

Calibration requires three valid observations and at least two matches, allowing seven attempts.
Search screens candidates using four fresh original observations and five candidate observations,
with eight and nine attempt limits respectively. Each completed run used ten comparisons and
finished the final single-group sweep. The experiment permits fourteen comparisons.

After durable selection, confirmation collects 100 fresh valid observations for each of the
immutable original and selected candidate, allowing 124 attempts per arm. Calibration, search
and confirmation observations are disjoint. The configured preservation margin is 20 percentage
points, using Newcombe method 10 with nominal 95% one-sided bounds.

Every completed profile produced 100/100 matching observations in each confirmation arm. The
estimated candidate-minus-original difference is zero; its bounds are approximately -2.63 and
+2.63 percentage points. Each profile was confirmed preserving under that configuration.
See [sampled results](results.md#sampled-results) for validity and independence assumptions.

## Completed-run costs

| Profile | Measured harness | Editable bytes before → after | Actual / additional possible / charged calls | Whole-command time |
| --- | --- | --- | --- | --- |
| Codex final response | `0.162.1` | 40,376 → 10,094 | 293 / 0 / 293 | 6,086.45 seconds |
| Copilot final response | `1.0.95` | 40,620 → 10,155 | 293 / 0 / 293 | 2,237.02 seconds |
| Copilot denied `bash` request | `1.0.95` | 40,620 → 10,155 | 293 / 293 / 586 | 2,145.67 seconds |

Each completed run removed **75% of editable history bytes**. All 293 observations per run
completed without runner errors or invalid observations. Cleanup and unchanged source/workspace
checks passed; zero returned actions executed. Shell accounting includes up to one additional
possible provider continuation per attempt, retained in the conservative charge. Copilot token
usage is unavailable for every observation.

Codex used 2,728,585 input tokens, including 1,462,528 cached input tokens, and 3,484 output tokens.
Its runner time totaled 1,488.739 seconds; the complete command included 15-second inter-attempt
pacing. No overload replacement was used in the successful run. Peak disposable state was
3,655,148 bytes/104 entries for Codex, 206,225,574 bytes/345 entries for Copilot final response,
and 206,218,948 bytes/345 entries for Copilot shell.

These totals cover the completed runs. Earlier failed and interrupted attempts also consumed
quota and remain separately recorded. A single completed workload per profile does not estimate
ordinary-session success rates or the expected cost across repeated campaigns. Larger contexts,
100k qualification and monetary cost remain unmeasured. Editable-byte removal does not establish
token savings.

## Statistical reference checks

The interval implementation uses the uncorrected independent-arm Wilson hybrid-score construction
from Newcombe method 10. `one_sided_confidence = 0.95` uses the normal quantile at 0.95 for each
bound; the corresponding central interval has nominal coefficient 0.90. The preservation test
uses the lower bound for candidate match probability minus original match probability.

Public
[reference vectors](https://github.com/eberkut/ithrl/blob/main/tests/fixtures/statistics_reference.json)
were generated independently using statsmodels 0.15.0's `confint_proportions_2indep` with
`method="newcomb"`, `compare="diff"` and `alpha = 2 * (1 - confidence)`. The fixture records source
URLs, source hashes and arm ordering. They cover unequal samples and boundary counts. From a
development checkout, run the credential-free checks with:

```bash
uv run pytest -o addopts= tests/unit/test_statistics.py -q
```

Those checks reproduce the comparison with the stored vectors. The
[reduction study package](https://github.com/eberkut/ithrl/tree/main/research/reduction) separately
replays synthetic sharing, failure, freshness and layout-budget models. The table above reports
the observed native workload, confirmation and cost aggregates.

## Native context fuzzing

Synthetic Codex and Copilot final-response cases completed discovery, intervention simplification
and one fresh confirmation each. Codex CLI `0.164.0` used the `openai` provider's ChatGPT Responses
endpoint; Copilot CLI `1.0.104` used the `github-copilot` managed endpoint. Both used `gpt-6.1-sol`.
The editable surface was one eligible prior-user text field; the selected decision's triggering
input remained fixed. Ongoing use requires structural detection, plan review and calibration as
described in the [native-run guide](native-runs.md#context-fuzzing).

The Copilot campaign used `copilot-semantic-session-import-v1`, which imported semantic history
and supplied a triggering message. It measures that reconstruction. Live qualification of the
native-prefix SDK runner remains separate.

A prior user message establishes a response token, and the fixed triggering input asks for the
latest active token. The stress append contains four paragraphs: three irrelevant notes and one
instruction revising the active token. The target checks for the original token. This workload
deliberately introduces a competing instruction so the campaign can locate and simplify it.

### Controls and configuration

Each harness passed an identical-text duplication control with six matching observations: four
original and two duplicated-input observations. Codex transcript checks verified the exact append,
unchanged remaining history, triggering input and thread settings. Copilot import receipts and
native output verified the exact imported append and fixed triggering stdin. The measured Copilot
import added a `<current_datetime>` element when transforming the native trigger on each attempt;
the changing time was the only observed variation in that transformed trigger. These checks cover
imported history and the supplied triggering input. Complete provider prompts may include further
harness context. Repetition's effect on other inputs remains unmeasured.

Both main campaigns used the same caller-supplied synthetic stress text, one generated candidate
and at most ten simplification trials. Each completed one discovery comparison and five
simplification comparisons. Their sampling and resource limits were:

| Setting | Measured configuration |
| --- | --- |
| Calibration | 3 valid observations, at least 2 matches, at most 7 attempts |
| Exploratory comparison | Fresh original 8 valid / 18 attempts; mutant 8 valid / 18 attempts |
| Exploratory threshold | Original-minus-mutant match-rate decrease at least `.40` |
| Fresh confirmation | Original 20 valid / 36 attempts; selected mutant 20 valid / 36 attempts |
| Final inference | One selected finding, `alpha=.05`, exact Clopper–Pearson union-bound method |
| Required decrease | Strictly greater than `.10` |
| Provider-call ceiling | 350 charged calls |
| Command wall-time ceiling | 6 hours |
| Per-attempt timeout | 120 seconds |
| Inter-attempt pacing | Codex: 15 seconds; Copilot: zero |
| Freshness | 24 hours |
| Eligibility | Opt-in `safe-provider-overload`; no overload replacement occurred |

The Copilot campaign used seed `20261027`. Public synthetic sources and the example campaign
configuration are available under
[`examples/fuzz/`](https://github.com/eberkut/ithrl/tree/main/examples/fuzz).
The published configuration uses a 15-second delay. The measured Copilot campaign used a separate
configuration with `limits.inter_attempt_delay_seconds = 0`.
The [native-run guide](native-runs.md#run-the-synthetic-native-campaign) owns reproduction and the
practical choice of limits and pacing.

### Confirmed result

For each harness, calibration matched the original target in all three observations. Simplification
reduced the **1,676-byte initial append** (1,674 bytes of stress text and two separating newlines)
to **142 UTF-8 bytes**, a **91.5% decrease**. It changed only the append; it did not reduce the
original history.

Fresh confirmation produced **20/20 original matches and 0/20 mutant matches**. The estimated
original-minus-mutant difference was `1.0`; its exact one-sided lower bound was
`0.6631330580338293`, exceeding the required `.10` decrease at `alpha=.05`. The campaign ended
with `confirmed-change` for both harnesses.

All exploratory observations preceded durable selection, and all 40 confirmation observations
followed it in one confirmation block. The confirmation transcripts verified the exact selected
142-byte append, unchanged remaining history and fixed triggering input, with distinct attempts.
That Copilot native context included the per-attempt time transformation described above. The
selected payload was fixed before confirmation and no completed holdout was repeated. See
[fuzz results](results.md#fuzz-results) and the
[research method](research/context-fuzzing.md#confirmation-method-and-assumptions) for
interpretation and assumptions.

### Calls, duration and resource use

| Workflow | Actual / additional possible / charged calls | Known input + output tokens | Whole-command time |
| --- | --- | --- | --- |
| Codex duplication control | 6 / 0 / 6 | 30,820 | 104.15 seconds |
| Codex complete fuzz campaign | 139 / 0 / 139 | 722,235 | 2,772.42 seconds (46 minutes 12 seconds) |
| Copilot duplication control | 6 / 0 / 6 | Unavailable | 114.98 seconds |
| Copilot complete fuzz campaign | 139 / 0 / 139 | Unavailable | 1,438.10 seconds (23 minutes 58 seconds) |

Each main campaign had 139 valid observations, zero runner errors, zero invalid observations and
zero observations with unknown provider acceptance. No calls remained pending. Codex reported all
token usage; Copilot reported zero known tokens and 139 unknown-token attempts. The Copilot total
therefore does not establish zero actual token use or monetary cost.

Codex runner time totaled 654.263 seconds; CLI wall time was 2,768.789216 seconds. Copilot runner
time totaled 1,395.322 seconds; CLI wall time was 1,429.50544 seconds. Whole-command measurements
include process startup and exit. Codex command time includes 15-second inter-attempt pacing;
Copilot used no pacing.

For Codex, native output was at most 12,921 bytes per observation, with median 11,517 bytes.
Peak disposable state was 3,817,339 bytes across 104 entries. Copilot native output was at most
23,400 bytes per observation, with median 11,304 bytes; peak disposable state was 247,150,276
bytes across 382 entries. Each campaign's 139 attempts verified cleanup without residual state and
unchanged source/workspace. Copilot import receipts matched independently reconstructed history
bytes, fixed triggering stdin and frozen policy settings in every attempt. The canonical sampled
state, result snapshot and summary agreed. No returned actions executed and no tool-side network
effects occurred.

Separate Codex interrupted work consumed 34 actual calls. Including that work and six control
calls, Codex qualification consumed **179 actual calls**. Copilot controls consumed nine calls,
including three failed observations outside the successful six-observation control; together with
the main campaign, they consumed **148 actual calls**. Main-campaign findings use only their own
139 observations; control and failed work are excluded from confirmation.

One synthetic case per harness demonstrates a completed workflow under its recorded profile. These
cases do not estimate ordinary-session success rates, repeated-campaign costs or monetary cost.
Intervention-byte reduction does not establish provider-token savings, causal explanation or a model
defect. Copilot shell-action fuzzing remains unsupported.

## Saved-session experiments

These experiments selected decisions from existing coding sessions, rather than planting a target
instruction. Final-response predicates used prespecified literal alternatives instead of requiring
the complete historical response verbatim. The shell predicate checked a native command request
while execution remained denied. Cases, session content, tool arguments, identifiers and native
diagnostics remain private; the aggregates below do not provide a public workload for replication.

### Confirmed Copilot reductions

Two Copilot CLI `1.0.104` cases completed separate fresh 20-observation original and candidate
arms. Both used a 20-percentage-point preservation margin and nominal 95% one-sided Newcombe
bounds for candidate-minus-original match probability.

The retained summaries and run state do not name the reconstruction method. These results
therefore do not qualify the native-prefix SDK runner.

| Target | Context records before → after | Fresh original / candidate matches | Difference bounds | Actual calls | Command time |
| --- | --- | --- | --- | --- | --- |
| Final response | 181 → 111 | 20/20 / 20/20 | −.11916 to +.11916 | 74 | 4,357.85 seconds |
| Denied shell request | 109 → 61 | 19/20 / 20/20 | −.07530 to +.19601 | 86 | 1,740.17 seconds |

Both selected candidates were confirmed preserving within the declared margin among valid
observations. The final-response search reached its eight-comparison cap; confirmation does not
establish search completion or minimality. Neither result establishes that all retained records
are necessary, or that the same margin and sample counts suit other decisions.

Provider-reported input tokens were available for all 40 confirmation observations in each case. For
the final response, mean input tokens per observation fell from 59,092 for the original to 31,991
for the candidate (45.86% lower). For the shell request, they fell from 49,541 to 27,235 (45.04%
lower). Each arm's median equaled its mean. These measured input-token changes do not establish
monetary savings or equivalent historical behavior. No possible or pending calls remained.
Source/workspace checks passed. The shell workflow observed 141 requested actions, denied all 141
and executed zero.

### Inconclusive and incomplete reductions

A Codex final-response case selected 189 of 218 context records. Fresh confirmation matched
19/20 original and 11/20 candidate observations, with candidate-minus-original bounds approximately
−.58197 to −.17902. This did not establish preservation at the declared `.20` margin. The run used
91 actual provider requests; the selected reduction must not be reported as confirmed.

Another Codex run stopped during confirmation after an incomplete provider transport stream,
with 151 actual requests and 152 conservatively charged calls. A subsequent run stopped when its
four-request per-attempt cap was exhausted, with 85 actual and charged calls, no possible or
pending calls, one runner error and 1,756.5 seconds elapsed. Source/workspace checks passed. The
cap was a local administrative choice; native turn continuations can require more requests even
when retries are disabled. These two incomplete runs consumed 236 actual calls plus one additional
possible call, conservatively charged as 237 calls, without a preservation verdict.

A separate Copilot run stopped after a structured quota refusal, with 74 actual calls and no
preservation verdict. These failures remain part of execution cost; a provider quota refusal does
not establish a compatibility failure.

### Bounded fuzz campaigns

One final-response campaign per harness screened two mutations on saved-session cases. Each
collected 18 valid observations and used 18 actual provider calls, with zero runner errors and zero
executed actions. Both ended `no-candidate-found`, without fresh confirmation. These negative
results describe the tested mutations and bounded exploration; they do not establish robustness
against other mutations or a general probability of finding a change.

This small selected set demonstrates useful Copilot reductions and bounded native fuzz execution.
It does not estimate ordinary-session success rates, expected costs across repeated campaigns or
confirmed Codex reduction usefulness. The [result guide](results.md) defines the claims and their
validity assumptions.
