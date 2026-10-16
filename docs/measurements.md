# Measurements

These measurements describe three completed synthetic native reductions using `gpt-6.1-sol`.
They provide a reference for interpreting the [example experiment](native-runs.md#2-configure-the-experiment)
and its costs.

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

Public [reference vectors](https://github.com/eberkut/ithrl/blob/main/tests/fixtures/statistics_reference.json)
were generated independently using statsmodels 0.15.0's `confint_proportions_2indep` with
`method="newcomb"`, `compare="diff"` and `alpha = 2 * (1 - confidence)`. The fixture records source
URLs, source hashes and arm ordering. They cover unequal samples and boundary counts. From a
development checkout, run the credential-free checks with:

```bash
uv run pytest -o addopts= tests/unit/test_statistics.py -q
```

Those checks reproduce the comparison with the stored vectors. Historical temporary simulation
artifacts are not reproduced by that command. Native observations and raw receipts remain private;
the public aggregate table supplies the workload and cost context without session content.
