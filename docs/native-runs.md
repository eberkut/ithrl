# Native runs

`extract` reads saved session evidence. `shrink` tests a selected behavior using a local harness
and its configured model provider. Extraction does not start a harness or make model requests.

## Select a saved decision

Run `ithrl extract` in a terminal to browse supported session stores and choose a completed
decision. You can also pass a saved session file:

```bash
ithrl extract path/to/session.jsonl --output .ithrl/case
```

For non-interactive use, pass the IDs reported by `extract`. This example selects a Codex session;
use the appropriate harness name and IDs for another supported adapter:

```bash
ithrl extract \
  --harness codex \
  --session SESSION_ID \
  --decision DECISION_ID \
  --target TARGET_ID \
  --output .ithrl/case \
  --format json
```

When a required selection is missing, the command lists available IDs. Selection and discovery
output exclude prompts, responses, commands, tool arguments and source paths. The created case
contains the exact session bytes, decision metadata and a dependency index of context records.
Keep it private; see [data and privacy](results.md#data-and-privacy).

By default, a final-response target matches the captured response exactly. For variable prose,
choose a literal behavior predicate before collecting observations. Use `--text-contains TEXT`
for one required substring, or repeat `--text-contains-any TEXT` for acceptable alternatives:

```bash
ithrl extract path/to/session.jsonl --decision DECISION_ID --target final-response \
  --text-contains-any 'expected phrase' --text-contains-any 'accepted alternative' \
  --output .ithrl/case
```

These comparisons are case-sensitive literal matches, not semantic judgments. For a shell target,
`--command-contains TEXT` retains the selected tool identity and action count while matching a
command substring. Choose one predicate option per extraction. The predicate must match the saved
decision, and becomes part of the experiment identity. A broad phrase can establish only the
behavior it actually checks; changing the predicate after seeing outcomes requires a new experiment.

## Prepare native reduction

You need:

- An installed Codex or Copilot CLI and its existing local authentication.
- Linux with Bubblewrap available and usable by the current user.
- An extracted case and the original workspace to protect.
- A private directory, mode `0700`, outside the case and original workspace for the observation
  store.

The model and provider come from captured evidence and supported local harness configuration.
There is no `--model` override. Returned actions are observed and denied; they never execute.
Tool-side network access, hosted tools and MCP are disabled for the current runner profiles.

### 1. Inspect the local capabilities

```bash
ithrl shrink CASE --original-workspace ORIGINAL \
  --required-valid 3 --minimum-matches 2 --max-attempts 7 \
  --attempt-timeout 120 --dry-run --format json
```

Native `--dry-run` can start bounded, network-denied local harness probes. It starts no model turn
and makes no provider call. Inspect the returned capabilities, safety controls and
`capability_digest`.

### 2. Configure the experiment

Create a private TOML file, mode `0600`. Copy `[target]` and `[oracle]` from
`CASE/experiments/default.toml`, preserving the behavior predicates and protected IDs. Add the
following configuration for Codex, replacing `CAPABILITY_DIGEST` with the value from preflight:

```toml
schema_version = 1
kind = "sampled"

[runner]
type = "codex"
required_capability_digest = "CAPABILITY_DIGEST"

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
required_valid = 4
max_attempts = 8

[reduction.search.candidate]
required_valid = 5
max_attempts = 9

[reduction.confirmation]
one_sided_confidence = 0.95

[reduction.confirmation.original]
required_valid = 100
max_attempts = 124

[reduction.confirmation.candidate]
required_valid = 100
max_attempts = 124
```

For Copilot, use `runner.type = "copilot"`, its target-specific capability digest and
`eligibility_policy = "independent-safe-preaccept-only"`. Keep the extracted target unchanged.
Leave the case's internal JSON and SQLite files unchanged.

This plan tests whether the selected candidate's match probability is no more than
**20 percentage points below the original's**, among valid observations. Confirmation uses Newcombe
method 10 with nominal 95% one-sided confidence. The counts and margin come from the
[measured synthetic workload](measurements.md); they are not general-purpose defaults. Choose the
margin deliberately and budget the complete plan. See [sampled results](results.md#sampled-results)
for assumptions and error handling.

### 3. Inspect the complete plan and budget

```bash
ithrl shrink CASE --experiment PRIVATE_EXPERIMENT \
  --original-workspace ORIGINAL --observation-store PRIVATE_STORE \
  --max-candidates 14 --max-provider-calls 7888 --max-wall-time 86400 \
  --max-provider-calls-per-attempt 16 \
  --attempt-timeout 120 --inter-attempt-delay 15 --dry-run --format json
```

The plan protects up to 248 confirmation attempts within 493 total attempt slots. Codex defaults
to a cap of 16 physical provider requests per attempt, giving a total allowance of 7,888.
`--max-provider-calls-per-attempt N` selects a cap from 1 to 16; recalculate the total-call budget
when changing it. A native turn can require several successful Responses requests even with
request and stream retries disabled. A lower cap can stop the turn before completion.

For either Copilot target, omit `--inter-attempt-delay 15`, set
`--max-provider-calls-per-attempt 1` and use `--max-provider-calls 493`. The request gate stops
shell execution and any continuation after denial. These profiles cannot enforce a hard
input-token limit and refuse a plan requiring one.

The per-attempt cap is part of capability and saved-run identity. Use the same cap throughout
preflight and execution; changing it requires a new plan. Increasing a sampled run's total
allowance on resume leaves that cap unchanged.

Check the provider, model, destination, safety policy and total limits. Subscription quota is not
predictable from provider-call limits.

### 4. Run the experiment

Use the complete-plan preflight's `next_command`, which includes `--acknowledge-model-execution`.
This makes real model requests. Calibration, search and fresh confirmation run within the displayed
limits. Keep the printed result directory and inspect `summary.json`; see [results and
resume](results.md) for its interpretation and continuation rules.

Progress shows valid samples and dispatched attempts for the original and candidate in each phase.
Terminals use a live display; redirected stderr receives progress lines. With `--format json`,
stdout remains one JSON report. Progress never prints captured responses or candidate text.

For calibration without a reduction plan, `--max-wall-time` and `--inter-attempt-delay` also apply.
Admission includes the remaining attempt deadlines and pauses. A `wall-time-budget` stop retains
partial counts and does not create a failed observation. A printed continuation command preserves
the experiment and pacing and reuses compatible saved calibration observations.

## Context fuzzing

`ithrl fuzz` explores bounded text appends and confirms at most one decrease in the selected
behavior's match rate. Fuzzing requires the optional `fuzz` extra (`ithrl[fuzz]`).
Codex supports appends to eligible prior
user input while preserving the selected decision's triggering input. A synthetic native campaign
completed qualification; its [settings, result and costs](measurements.md#native-context-fuzzing)
provide a measured example. Copilot text mutation remains unsupported.

To exercise the complete workflow locally, create a fresh example case:

```bash
mkdir -m 700 ithrl-fuzz-demo
ithrl extract --example --output ithrl-fuzz-demo/case
```

Save the following as `ithrl-fuzz-demo/fuzz.toml`, with mode `0600`:

```toml
seed = 1
mutators = ["append-stale-text"]
stale_text = "An irrelevant earlier instruction."
max_candidates = 4
max_simplification_trials = 4
minimum_decrease = 0.1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400

[calibration]
required_valid = 2
minimum_matches = 2
max_attempts = 2

[screening]
minimum_decrease = 0.4

[screening.original]
required_valid = 2
max_attempts = 2

[screening.mutant]
required_valid = 2
max_attempts = 2

[confirmation]
alpha = 0.05

[confirmation.original]
required_valid = 20
max_attempts = 20

[confirmation.mutant]
required_valid = 20
max_attempts = 20

[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
inter_attempt_delay_seconds = 0
```

These small fixed counts are for the deterministic fake-runner example. The alternative mutator,
`duplicate-text`, appends a copy of an existing editable text item.
Both mutators preserve protected records and keep payloads bounded. `stale_text` is private input
and is never printed in normal output.

Inspect without execution, then run with a dedicated store:

```bash
ithrl fuzz ithrl-fuzz-demo/case --config ithrl-fuzz-demo/fuzz.toml \
  --observation-store ithrl-fuzz-demo/fuzz-observations.sqlite --dry-run
ithrl fuzz ithrl-fuzz-demo/case --config ithrl-fuzz-demo/fuzz.toml \
  --observation-store ithrl-fuzz-demo/fuzz-observations.sqlite
```

The example finishes with `confirmed-change`, zero provider calls and zero executed actions. It uses
fixed original/mutant samples and fresh confirmation. See [fuzz results](results.md#fuzz-results)
for saved evidence and resume behavior. Use `--format json` for one JSON report on stdout; execution
progress and the inspected plan go to stderr.

Fuzz `--dry-run` starts no harness or provider. A native case without a saved capability profile
reports `detection-required`. To create the private structural profile for a native case, run:

```bash
ithrl fuzz CASE --config PRIVATE_CONFIG --detect --profile PRIVATE_PROFILE \
  --original-workspace ORIGINAL_WORKSPACE
```

For Codex, detection defaults to 16 requests per attempt; add
`--max-provider-calls-per-attempt N` to select a smaller cap. Size the total-call limit in
`PRIVATE_CONFIG` for calibration, complete exploratory blocks and protected fresh confirmation
at that cap. The saved profile retains it for execution and resume.

Detection runs bounded, network-denied local harness probes without a provider request. Inspect
the result, then use its profile in a harness-free plan review:

```bash
ithrl fuzz CASE --config PRIVATE_CONFIG --profile PRIVATE_PROFILE \
  --original-workspace ORIGINAL_WORKSPACE --observation-store PRIVATE_STORE --dry-run
```

The profile binds the case, configuration and original workspace. Freshly extracted Codex cases
can use detection directly without editing the case's experiment. Choose native sampling and
resource limits explicitly in `PRIVATE_CONFIG`; the fake-runner example above uses demonstration
settings. The [measured native campaign](measurements.md#native-context-fuzzing) records one
complete configuration without establishing general defaults. Review the provider, model,
destination, isolation, action-denial policy and complete budget before using the returned command
with `--acknowledge-model-execution`.
See the [CLI reference](cli.md#ithrl-fuzz) for options and
[troubleshooting](troubleshooting.md#fuzz-setup-and-resume) for refusals.

## Compatibility and measurements

Extraction validates record structure and recognized relationships. Codex rollouts are recognized
by their initial `session_meta` record; they have no record-schema version field. Copilot's current
adapter reads event schema `1`. Both adapters preserve unknown records without interpreting them.

Native runners check the installed harness's required operations, permissions, isolation and action
denial locally. These checks and calibration determine whether the configured run can proceed.
Public experimental support also requires a qualified live workflow for that target profile; see
the [supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs).
Harness release numbers are recorded for reproducibility; there is no exact-release allowlist.
After a harness or configuration change, rerun preflight and use its capability digest for a new
experiment. A changed digest prevents reuse of an old pinned plan or incompatible saved run.

The [measurements](measurements.md) record benchmark construction, completed reduction and fuzzing
workflows, tested harness versions, confirmation results and costs. Other releases go through the
same local compatibility checks. The example's 24-hour wall-time limit is an administrative ceiling;
use the measured times to understand the completed workload's duration.
