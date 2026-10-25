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

## Prepare native reduction

You need:

- A supported installed harness and its existing local authentication.
- Linux with Bubblewrap available and usable by the current user.
- An extracted case and the original workspace to protect.
- A private directory, mode `0700`, outside the case and original workspace for the observation store.

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
and makes no provider call. Inspect the returned capabilities, safety controls and `capability_digest`.

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

This plan permits a **20-percentage-point decrease** in match probability among valid observations,
with nominal 95% one-sided Newcombe method 10 confirmation. Choose the margin deliberately and
budget the complete plan. These settings must be supplied explicitly. See [interpreting sampled
results](results.md#sampled-results) for assumptions and error handling.

### 3. Inspect the complete plan and budget

```bash
ithrl shrink CASE --experiment PRIVATE_EXPERIMENT \
  --original-workspace ORIGINAL --observation-store PRIVATE_STORE \
  --max-candidates 14 --max-provider-calls 493 --max-wall-time 86400 \
  --attempt-timeout 120 --inter-attempt-delay 15 --dry-run --format json
```

For Copilot, omit `--inter-attempt-delay 15`; a shell-action run needs `--max-provider-calls 986`.
The plan protects up to 248 confirmation attempts within 493 total attempt slots. Copilot shell
allows for two provider calls per attempt because denial can trigger a continuation. These profiles
cannot enforce a hard input-token limit and refuse a plan requiring one.

Check the provider, model, destination, safety policy and total limits. Subscription quota is not
predictable from provider-call limits.

### 4. Run the experiment

Use the complete-plan preflight's `next_command`, which includes `--acknowledge-model-execution`.
This makes real model requests. Calibration, search and fresh confirmation run within the displayed
limits. Keep the printed result directory and inspect `summary.json`; see [results and
resume](results.md) for its interpretation and continuation rules.


## Context fuzzing

`ithrl fuzz` explores bounded text appends and confirms at most one decrease in the selected
behavior's match rate. Install the optional numerical dependency with `uv tool install 'ithrl[fuzz]'`
(or `uv sync --locked --all-groups --all-extras` in a checkout). Native text mutation is awaiting
qualification; current native profiles refuse execution of this operation.

To exercise the complete workflow locally, extract the [bundled example](https://github.com/eberkut/ithrl/blob/main/README.md#try-it)
and save the following as `ithrl-demo/fuzz.toml`, with mode `0600`:

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

These small fixed counts demonstrate the fake runner; they are not native sampling defaults.
The alternative mutator, `duplicate-text`, appends a copy of an existing editable text item.
Both mutators preserve protected records and keep payloads bounded. `stale_text` is private input
and is never printed in normal output.

Inspect without execution, then run with a dedicated store:

```bash
ithrl fuzz ithrl-demo/case --config ithrl-demo/fuzz.toml \
  --observation-store ithrl-demo/fuzz-observations.sqlite --dry-run
ithrl fuzz ithrl-demo/case --config ithrl-demo/fuzz.toml \
  --observation-store ithrl-demo/fuzz-observations.sqlite
```

The fake runner makes zero provider calls and executes no actions. Fixed original/mutant samples
exercise the shared collector and fresh confirmation; [fuzz results](results.md#fuzz-results)
explains the saved evidence and resume behavior. Use `--format json` for one JSON report on stdout;
execution progress and the inspected plan go to stderr.

For a future qualified native profile, detection is a separate explicit operation:

```bash
ithrl fuzz CASE --config PRIVATE_TOML --original-workspace ORIGINAL \
  --detect --profile PRIVATE_PROFILE
ithrl fuzz CASE --config PRIVATE_TOML --original-workspace ORIGINAL \
  --profile PRIVATE_PROFILE --dry-run
```

Fuzz `--dry-run` starts no harness or provider. Without a saved native profile it reports
`detection-required`; it does not guess capabilities, destination or call bounds. `--detect`
performs credential-free structural discovery and saves private planning metadata. Inspect the
model, provider, destination, mutation scope, fixed samples, action denial and resource limits.
Execution additionally requires `--acknowledge-model-execution`, rechecks the current exact
profile and refuses unsupported text mutation before any provider attempt.

## Compatibility and measurements

Extraction validates record structure and recognized relationships. Codex rollouts are recognized
by their initial `session_meta` record; they have no record-schema version field. Copilot's current
adapter reads event schema `1`. Both adapters preserve unknown records without interpreting them.

Native runners check the installed harness's required operations, permissions, isolation and action
denial locally. Support is determined by these checks and calibration. Harness release numbers are
recorded for reproducibility; there is no exact-release allowlist. After a harness or configuration
change, rerun preflight and use its capability digest for a new experiment. A changed digest prevents
reuse of an old pinned plan or incompatible saved run.

The [measurements](measurements.md) record the benchmark construction, completed reductions,
tested harness versions, confirmation results and costs. Other releases go through the same local
compatibility checks. The example's 24-hour wall-time limit is an administrative ceiling;
use the measured times to understand the completed workload's duration.
