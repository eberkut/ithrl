# Native runs

[Select a saved decision](#select-a-saved-decision), then follow the task guide:

- [Native reduction](reduction.md): prepare, inspect and execute a bounded context-removal
    experiment.
- [Context fuzzing](fuzzing.md): try the local example or test text appends through a native
    harness.

Both native workflows use the shared [prerequisites](#native-prerequisites) and
[compatibility checks](#compatibility-and-measurements). For output interpretation and recovery,
see [results and privacy](results.md).

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

Missing selections produce a list of available IDs without prompts, responses, commands, tool
arguments or source paths. The case contains exact session bytes, decision metadata and context
dependencies. Keep it private; see [data and privacy](results.md#data-and-privacy).

By default, a final-response target matches the captured response exactly. For variable prose,
choose a literal behavior predicate before collecting observations. Use `--text-contains TEXT`
for one required substring, or repeat `--text-contains-any TEXT` for acceptable alternatives:

```bash
ithrl extract path/to/session.jsonl --decision DECISION_ID --target final-response \
  --text-contains-any 'expected phrase' --text-contains-any 'accepted alternative' \
  --output .ithrl/case
```

Matches are case-sensitive and literal. For a shell target, `--command-contains TEXT` matches a
command substring while retaining the selected tool identity and action count. Choose one
predicate option per extraction; it must match the saved decision and becomes part of the
experiment identity. A broad phrase supports only the behavior it checks. Changing the predicate
after seeing outcomes requires a new experiment.

## Native prerequisites

You need:

- An installed Codex or Copilot CLI and its existing local authentication.
- Linux with Bubblewrap available and usable by the current user.
- An extracted case and the original workspace to protect.
- A private directory, mode `0700`, outside the case and original workspace for receipts,
  configuration and the observation store.

The model and provider come from captured evidence and supported local harness configuration.
There is no `--model` override. Returned actions are observed and denied; they never execute.
Tool-side network access, hosted tools and MCP are disabled for the current runner profiles.

For Codex history containing tool outputs, set `tool_output_token_limit` before any TOML tables
in `$CODEX_HOME/config.toml`, or `~/.codex/config.toml` when `CODEX_HOME` is unset. For example:

```toml
tool_output_token_limit = 65536
```

Set a positive integer large enough for every retained text output, up to the 64 MiB worker-request
limit. Admission uses serialized ASCII JSON size as a conservative bound and refuses missing or
inadequate settings before dispatch; the native default may truncate history. Changing the setting
changes configuration and capability identities and requires a new experiment. It limits history
truncation, not input tokens or cost. Image/audio tool outputs are unsupported.

## Prepare native reduction

Follow the [native reduction guide](reduction.md).

### 1. Inspect the local capabilities

[Inspect capabilities and export a receipt](reduction.md#1-inspect-the-local-capabilities).

### 2. Configure the experiment

[Choose the statistical settings](reduction.md#2-configure-the-experiment).

#### Optional section-removal settings

[Configure paragraph removal](reduction.md#optional-section-removal-settings).

### 3. Generate and inspect the complete plan and budget

[Generate and review the plan](reduction.md#3-generate-and-inspect-the-complete-plan-and-budget).

### 4. Run the experiment

[Authorize the reviewed experiment](reduction.md#4-run-the-experiment).

## Context fuzzing

Follow the [context-fuzzing guide](fuzzing.md), starting with its provider-free local example.

### Run the synthetic native campaign

[Run the bounded synthetic campaign](fuzzing.md#run-the-synthetic-native-campaign).

## Compatibility and measurements

Extraction validates record structure and recognized relationships. Codex rollouts are recognized
by their initial `session_meta` record; they have no record-schema version field. Copilot's current
adapter reads event schema `1`. Both adapters preserve unknown records without interpreting them.

Codex `compacted` rollout records are preserved as evidence. The Codex runner refuses decisions
whose source prefix includes those records, even if a candidate removes the record. Removing a
compaction marker does not reconstruct the history it replaced. The ordinary paragraph-removal
measurement used an uncompacted source and does not establish compaction support.

For a tool-output trigger, Codex injects the captured output with its original call identity and
continues without duplicating it as new input. Current preflight reports
`codex-app-server-inject-checkpoint-v1`; older saved reconstruction profiles require a new
experiment. The private qualification auditor is not part of the shipped runner.

Local operation, permission, isolation and action-denial checks plus calibration determine whether
a run can proceed. Public support also requires a qualified live workflow for that profile; see
[supported inputs](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs). Versions
are recorded, not allowlisted. After a harness or configuration change, export a new receipt and
prepare a new experiment; a changed digest blocks old pinned plans and incompatible saved runs.

The [measurements](measurements.md) record benchmark construction, completed reduction and fuzzing
workflows, tested harness versions, confirmation results and costs. Other releases go through the
same local compatibility checks. Wall-time and call limits are administrative ceilings; use the
measurements to estimate the duration and usage of the recorded workloads.
