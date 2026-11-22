# Native reduction

Find a smaller context that preserves a selected behavior. Before starting:

- [Extract a decision](native-runs.md#select-a-saved-decision) and choose its behavior predicate.
- Check the [native prerequisites](native-runs.md#native-prerequisites).
- Choose a private directory outside the case and original workspace, with mode `0700`.

Replace `CASE` with the case directory, `ORIGINAL` with the original workspace and `PRIVATE` with
that private directory in the commands below. Preflight makes no provider calls; execution requires
explicit acknowledgement.

## 1. Inspect the local capabilities

For Codex, use the 16-request per-attempt cap below. For Copilot, replace it with
`--max-provider-calls-per-attempt 1`.

```bash
ithrl shrink CASE --original-workspace ORIGINAL \
  --required-valid 3 --minimum-matches 2 --max-attempts 7 \
  --attempt-timeout 120 --max-provider-calls-per-attempt 16 \
  --dry-run --save-preflight PRIVATE/preflight.json --format json
```

This starts bounded, network-denied local harness probes without a model turn. Inspect the returned
capabilities and safety controls. On success, `--save-preflight` creates a private receipt binding
the capability, case, target, oracle and resource limits. Use a new receipt path for each preflight.
The receipt contains private paths and provider configuration, without session text or
authentication. The complete-plan preflight checks runtime readiness again before execution.

## 2. Configure the experiment

Save the following as `PRIVATE/settings.toml`, with mode `0600`. Choose the sample counts and
acceptable degradation margin before collecting observations. This example uses the measured
Codex reduction's settings; adapt them to your decision and expected variability:

```toml
schema_version = 1
kind = "reduction-preparation"

[calibration]
required_valid = 3
minimum_matches = 2
max_attempts = 7

[reduction]
margin = 0.20
freshness_seconds = 86400
baseline_policy = "strict-fresh-original"
eligibility_policy = "safe-provider-overload"

[reduction.search.original]
required_valid = 20
max_attempts = 30

[reduction.search.candidate]
required_valid = 20
max_attempts = 30

[reduction.confirmation]
one_sided_confidence = 0.95

[reduction.confirmation.original]
required_valid = 100
max_attempts = 124

[reduction.confirmation.candidate]
required_valid = 100
max_attempts = 124
```

For Copilot, use `eligibility_policy = "independent-safe-preaccept-only"` and its own preflight
receipt. Do not add `[target]`, `[oracle]`, `[runner]` or source hashes to these settings.

This plan tests whether the selected candidate's match probability is no more than
**20 percentage points below the original's**, among valid observations. Confirmation uses Newcombe
method 10 with nominal 95% one-sided confidence. The
[measured saved-session Codex reduction](measurements.md#confirmed-codex-reduction) used these
counts, margin and a two-comparison search limit. It confirmed a reduction with 50.17% lower mean
input tokens per observation.

The [synthetic reference](measurements.md#synthetic-reduction-reference) used smaller search samples
on a planted instruction. See [sampled results](results.md#sampled-results) for statistical
assumptions and error handling.

### Optional section-removal settings

Coarse search removes whole records; fine search removes paragraphs within retained prior-user
text fields.

Section removal is supported for eligible prior-user text in Codex and Copilot final-response
reduction; shell targets and fuzzing do not accept it. See the
[qualification measurements](measurements.md#ordinary-session-paragraph-removal) for tested
workloads and limits.

List eligible paragraph-removal fields before preparation:

```bash
ithrl inspect sections CASE --format json
```

The report lists eligible fields, ordered section IDs and UTF-8 byte counts without text. Fields
exceeding policy limits are unavailable. Eligibility does not establish preservation or guarantee
that a field survives coarse search.

Choose the fields, removable sections, traversal order, additional-byte criterion and stage budgets
before collecting observations. Add these tables to `PRIVATE/settings.toml`, replacing the
placeholders with a field's `context_id`, `content_index` and ordered section IDs from the report:

```toml
[sections]
max_coarse_comparisons = 8
max_fine_comparisons = 8
minimum_removed_utf8_bytes = 64
require_criterion_for_confirmation = true
stop_when_no_retained_fields = true

[[sections.fields]]
context_id = "CONTEXT_ID"
content_index = 0
section_ids = ["SECTION_ID"]
```

Repeat the field table for each eligible field. `prepare` derives the source hashes and writes
`[reduction.section_search]` in the generated experiment. Omitted sections are protected during fine
search. Each candidate edits one field; accepted edits do not accumulate across fields. Search
first tries removing all declared sections in that field, then smaller groups if admission or
screening fails. Coarse search can still remove the entire field.

| Setting | Effect |
| --- | --- |
| `minimum_removed_utf8_bytes` | Minimum additional text removal relative to the coarse candidate; it measures neither preservation nor debugging value. |
| `require_criterion_for_confirmation = true` | Stop before final selection and confirmation if the byte criterion is unmet. The report records `section-criterion-not-met`; no confirmation attempts run. Default: `false`. |
| `stop_when_no_retained_fields = true` | Stop coarse search at a completed comparison boundary once all selected fields are gone, keeping the accepted removal. Requires the confirmation gate above. No fine search or confirmation follows. Default: `false`. |

Fields removed by coarse search are skipped, without substituting another field or source. If none
survives, the result records `no-retained-fields`; the early-stop rule also records that value as
`coarse_stop_reason`. An administrative budget stop can resume while frozen fine-search capacity
remains, but cannot bypass the confirmation gate.

`--max-candidates` caps comparisons across both stages. Each stage also has its frozen ceiling;
unused coarse capacity cannot enlarge fine search. Preflight reports both ceilings and caps its
attempt estimate at their sum. Every comparison and fresh confirmation uses the full immutable
original as its baseline. Field choices, stage ceilings, byte criterion, stop rules and sampling
plan cannot change on resume; changing them requires a new experiment.

For qualification, use the execution package's frozen byte criterion. The
[original workload proposal](research/reduction-design.md#text-section-workload-preparation) used
64 bytes for Codex and 256 for Copilot, separately from debugging value.

## 3. Generate and inspect the complete plan and budget

Generate the private experiment and its inspection command. The commands below use the Codex
16-request cap. For Copilot's one-request cap, use `--max-provider-calls 375` for whole-record
reduction or `--max-provider-calls 1215` for the section plan, and omit `--inter-attempt-delay 15`.

For whole-record reduction without `[sections]`, use the two-comparison budget:

```bash
ithrl prepare CASE --preflight PRIVATE/preflight.json \
  --settings PRIVATE/settings.toml --output PRIVATE/reduction.toml \
  --max-candidates 2 --max-provider-calls 6000 --max-wall-time 57600 \
  --inter-attempt-delay 15 --observation-store PRIVATE/observations.sqlite --format json
```

For the eight-coarse/eight-fine section settings above, allow all 16 comparisons:

```bash
ithrl prepare CASE --preflight PRIVATE/preflight.json \
  --settings PRIVATE/settings.toml --output PRIVATE/reduction.toml \
  --max-candidates 16 --max-provider-calls 19440 --max-wall-time 172800 \
  --inter-attempt-delay 15 --observation-store PRIVATE/observations.sqlite --format json
```

The section plan permits `7 + 16*(30+30) + 2*124 = 1215` attempts and 19,440 requests at the
16-request cap. Its 48-hour limit covers deadlines, cancellation and pacing. These are conservative
admission bounds, not expected usage. A two-comparison limit could end coarse search before fine
search starts. For a smaller plan, reduce stage ceilings and recalculate administrative budgets
together. Field survival and preservation remain untested until execution.

On success, `prepare` writes the private experiment and reports its budget and inspection command.
It preserves the target, oracle, grader and protected IDs. Preparation is offline: it starts no
harness, reads no authentication, opens no observation store and inspects no original workspace.
Existing output is never overwritten; changed settings require a new output path.

Settings define statistical policy; command arguments set administrative ceilings and pacing.
Per-attempt timeout, request and optional token bounds come from the receipt. Changed fixed native
limits require a new receipt.

Run the preparation report's `next_command`. It contains the chosen limits and `--dry-run`, without
execution acknowledgement. For the whole-record Codex example, it is equivalent to:

```bash
ithrl shrink CASE --experiment PRIVATE/reduction.toml \
  --original-workspace ORIGINAL --observation-store PRIVATE/observations.sqlite \
  --max-candidates 2 --max-provider-calls 6000 --max-wall-time 57600 \
  --max-provider-calls-per-attempt 16 \
  --attempt-timeout 120 --inter-attempt-delay 15 --dry-run --format json
```

This allows `7 + 2*(30+30) + 2*124 = 375` attempts, including 248 for fresh confirmation.
At the 16-request per-attempt cap, its maximum provider-call allowance is 6,000. The measured run
used 293 calls. The 16-hour wall limit admits conservative attempt deadlines, cancellation and
pacing; reported CLI wall time was 2 hours 4 minutes 37 seconds.

The measured run confirmed its candidate despite `search-incomplete`. To explore more than two
comparisons, increase `--max-candidates` and the call/time limits before execution. See
[sampled results](results.md#sampled-results) for search coverage versus confirmation.

A native turn can require several successful requests even with retries disabled. Codex accepts
`--max-provider-calls-per-attempt N` from 1 to 16 and defaults to 16. A lower cap can stop a turn
before completion. Recalculate the total-call allowance when changing it.

The Copilot budget substitutions above translate these plans' resource limits; the
[Copilot saved-session measurements](measurements.md#earlier-copilot-reductions) used separate
sampling configurations. The request gate stops shell execution and any continuation after denial.

See [native limits](native-runs.md#native-limits) for output, disposable-state and token bounds.

Review the complete-plan dry-run's provider, model, destination, safety policy and total limits.
It checks the current runtime and configuration against the recorded capability. Keep the
per-attempt cap unchanged between preflight and execution; increasing a total allowance on resume
does not change it.

Call/time minima conservatively cover maximum attempts, per-attempt caps, timeouts and pacing.
They predict neither actual cost nor subscription quota, and neither profile enforces a hard
monetary cap. Use [measurements](measurements.md) to estimate usage.

## 4. Run the experiment

Run the complete-plan preflight's `next_command` with the same installation and environment. It
includes `--acknowledge-model-execution` and makes real model requests for calibration, search and
fresh confirmation within the displayed limits. After execution, keep the reported result directory
and read `summary.json`. Use [sampled results](results.md#sampled-results) to distinguish search
coverage from confirmed preservation, and [results and resume](results.md) for recovery.

Progress shows valid samples and dispatched attempts for the original and candidate in each phase.
Terminals use a live display; redirected stderr receives progress lines. With `--format json`,
stdout remains one JSON report. Progress never prints captured responses or candidate text.

For calibration without a reduction plan, `--max-wall-time` and `--inter-attempt-delay` also apply.
Admission includes the remaining attempt deadlines and pauses. A `wall-time-budget` stop retains
partial counts and does not create a failed observation. A printed continuation command preserves
the experiment and pacing and reuses compatible saved calibration observations.
