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
