# Measurements

These measurements describe completed synthetic native reductions and a Codex context-fuzzing
campaign using `gpt-6.1-sol`. The reduction sections provide a reference for interpreting the
[example experiment](native-runs.md#2-configure-the-experiment). The
[native context-fuzzing section](#native-context-fuzzing) records the qualified append-text
workflow and its limits.

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

A synthetic Codex final-response case completed discovery, intervention simplification and one
fresh confirmation. The measured harness was Codex CLI `0.164.0`, using `gpt-6.1-sol` through the
`openai` provider's ChatGPT Responses endpoint. The editable surface was one eligible prior-user
text field; the selected decision's triggering input remained fixed. These version and provider
facts identify the measured configuration. Ongoing use still requires structural detection,
plan review and calibration as described in the [native-run guide](native-runs.md#context-fuzzing).

A prior user message establishes a response token, and the fixed triggering input asks for the
latest active token. The stress append contains four paragraphs: three irrelevant notes and one
instruction revising the active token. The target checks for the original token. This workload
deliberately introduces a competing instruction so the campaign can locate and simplify it.

### Controls and configuration

An identical-text duplication control produced six matching observations: four original and two
duplicated-input observations. Transcript checks verified the exact append and unchanged remaining
history, triggering input and thread settings. This control checks reconstruction on the selected
case; it does not establish that repetition preserves meaning for arbitrary inputs.

The main campaign used caller-supplied synthetic stale text, one generated candidate and at most
ten simplification trials. It completed one discovery comparison and five simplification
comparisons. Its sampling and resource limits were:

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
| Inter-attempt pacing | 15 seconds |
| Freshness | 24 hours |
| Eligibility | Opt-in `safe-provider-overload`; no overload replacement occurred |

These are the executed campaign's settings. The
[practical-settings issue](https://github.com/eberkut/ithrl/issues/10) owns evaluation of broader
workloads and suitable defaults.

### Confirmed result

Calibration matched the original target in all three observations. Simplification reduced the
appended intervention from **1,674 to 142 UTF-8 bytes**, a **91.5% decrease**. It changed only the
append; it did not reduce the original history.

Fresh confirmation produced **20/20 original matches and 0/20 mutant matches**. The estimated
original-minus-mutant difference was `1.0`; its exact one-sided lower bound was
`0.6631330580338293`, exceeding the required `.10` decrease at `alpha=.05`. The campaign ended
with `confirmed-change`.

All exploratory observations preceded durable selection, and all 40 confirmation observations
followed it in one confirmation block. The confirmation transcripts verified the exact selected
142-byte append, unchanged remaining history, triggering input and thread settings, and distinct
attempts. The selected payload was fixed before confirmation and no completed holdout was repeated.
See [fuzz results](results.md#fuzz-results) and the
[research method](research/context-fuzzing.md#confirmation-method-and-assumptions) for
interpretation and assumptions.

### Calls, duration and resource use

| Workflow | Actual / additional possible / charged calls | Known input + output tokens | Whole-command time |
| --- | --- | --- | --- |
| Duplication control | 6 / 0 / 6 | 30,820 | 104.15 seconds |
| Complete fuzz campaign | 139 / 0 / 139 | 722,235 | 2,772.42 seconds (46 minutes 12 seconds) |

The main campaign had 139 valid observations, zero runner errors, zero invalid observations and
zero observations with unknown provider acceptance. No calls remained pending, and no token usage
was unknown. Runner time totaled 654.263 seconds; CLI wall time was 2,768.789216 seconds. The
whole-command measurement includes process startup and exit; the command includes the configured
15-second inter-attempt pacing.

Native output was at most 12,921 bytes per observation, with median 11,517 bytes. Peak disposable
state was 3,817,339 bytes across 104 entries. All 139 attempts verified cleanup without residual
state and unchanged source/workspace. No returned actions executed and no tool-side network
effects occurred.

A separate interrupted campaign consumed 34 actual calls and remains incomplete. Including that
work and the six control calls, qualification consumed **179 actual calls**; the completed result
above uses only its own 139 observations. Interrupted work is not confirmation evidence.

This single synthetic case demonstrates the complete qualified workflow and its measured cost.
It does not estimate ordinary-session success rates, repeated-campaign costs or monetary cost.
Intervention-byte reduction does not establish provider-token savings, causal explanation or a
model defect. Copilot text mutation remains unsupported.
