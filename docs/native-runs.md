# Native runs

<!-- Preserve incoming links to procedures now owned by the linked task guides. -->
<!-- rumdl-disable MD033 -->
<span id="prepare-native-reduction"></span>
<span id="1-inspect-the-local-capabilities"></span>
<span id="2-configure-the-experiment"></span>
<span id="optional-section-removal-settings"></span>
<span id="3-generate-and-inspect-the-complete-plan-and-budget"></span>
<span id="4-run-the-experiment"></span>
<span id="context-fuzzing"></span>
<span id="run-the-synthetic-native-campaign"></span>
<!-- rumdl-enable MD033 -->

[Select a saved decision](#select-a-saved-decision), then follow the task guide:

- [Native reduction](reduction.md): prepare, inspect and execute a bounded context-removal
    experiment.
- [Context fuzzing](fuzzing.md): try the local example or test text appends through a native
    harness.
- [Context comparison](comparison.md): test two selected contexts for a repair or degradation
    effect.

These workflows share the [prerequisites](#native-prerequisites) and
[compatibility checks](#compatibility-and-measurements). For output interpretation and recovery,
see [results and privacy](results.md).

## Select a saved decision

Run `ithrl extract` in a terminal to browse supported session stores and choose a completed
decision. You can also pass a saved session file:

```bash
ithrl extract path/to/session.jsonl --output .ithrl/case
```

For non-interactive use, pass the IDs reported by `extract`. Replace `HARNESS` with `codex` or
`copilot` and use the IDs from that adapter:

```bash
ithrl extract \
  --harness HARNESS \
  --session SESSION_ID \
  --decision DECISION_ID \
  --target TARGET_ID \
  --output .ithrl/case \
  --format json
```

When a selection is missing, non-interactive output lists available IDs without session content or
source paths. Successful extraction creates a case containing exact session bytes, decision metadata
and context dependencies. Keep it private; see [data and privacy](results.md#data-and-privacy).

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

By default, the model and provider come from captured evidence and supported local harness
configuration. A comparison can
[select execution settings](comparison.md#choose-execution-settings). Returned actions are observed
and denied. Tool-side network access, hosted tools and MCP are disabled for the current runner
profiles.

### Native limits

| Limit | Codex | Copilot |
| --- | --- | --- |
| Physical inference requests per attempt | 1–16; default 16 | 1 |
| Inter-attempt delay | Defaults to zero; measured reduction/fuzz examples use 15 seconds | Defaults to zero; measured reduction/fuzz examples use zero |
| Native output per attempt | 8 MiB | 8 MiB |
| Runner disposable state per attempt | 64 MiB; 10,000 entries | 512 MiB; 10,000 entries |
| Token usage | Reported for completed observations in the measured profiles | Available when native events report it; unknown usage remains explicit |
| Hard input-token cap | Unavailable | Unavailable |

### Codex tool-output limits

For Codex history containing tool outputs, set `tool_output_token_limit` before any TOML tables
in `$CODEX_HOME/config.toml`, or `~/.codex/config.toml` when `CODEX_HOME` is unset. For example:

```toml
tool_output_token_limit = 65536
```

Set a positive integer, at most `67108864`, large enough for every retained text output. ithrl
compares it with the output's serialized ASCII JSON byte size as a conservative truncation check;
the complete worker request must also fit its 64 MiB limit. A missing or inadequate setting is
refused before dispatch because the native default may truncate history. Changing it requires a
new experiment. This setting does not cap input tokens or cost. Image/audio tool outputs are
unsupported.

## Compatibility and measurements

Extraction validates record structure and recognized relationships. Codex rollouts are recognized
by their initial `session_meta` record; they have no record-schema version field. Copilot's current
adapter reads event schema `1`. Both adapters preserve unknown records without interpreting them.

Check [supported inputs](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs) for
qualified workflows. Local operation, permission, isolation and action-denial checks plus
calibration determine whether a run can proceed; passing them does not extend public support.
Versions are recorded, not allowlisted. After changing the harness or its configuration, repeat the
workflow's capability checks and create a new receipt or profile. Changed identities invalidate
pinned plans and incompatible saved runs.

The [measurements](measurements.md), [bundle comparisons](research/comparative-debugging.md) and
[tool output comparisons](research/tool-output-interventions.md) record tested profiles, outcomes
and costs. Tool output text qualification covers final-response targets; denied-shell bundle
comparisons retain their separate scope. Use these results to estimate usage for the recorded
workloads; request and wall-time limits are administrative ceilings.

### Codex reconstruction

Codex `compacted` rollout records are preserved as evidence. The Codex runner refuses decisions
whose source prefix includes them, even if a candidate removes the record: removing a compaction
marker cannot reconstruct the history it replaced. The ordinary paragraph-removal measurement used
an uncompacted source.

For a tool-output trigger, Codex injects the captured output with its original call identity and
continues without duplicating it as new input. Preflight identifies this reconstruction as
`codex-app-server-inject-checkpoint-v1`.

Codex verifies the returned thread policy and identity before inserting history or starting a
turn. Startup warnings are retained in private native output. A policy mismatch or a configuration
warning after verification stops the attempt.

The Codex final-response profile appends a fixed developer instruction declaring a context-only
continuation: tools are unavailable, the response must use the restored context, and missing
evidence must be stated. Captured developer instructions remain its prefix. The default
reconstruction preserves captured history and the decision input. When the selected model or
provider differs from the capture, reconstruction omits historical reasoning records, including
summaries, from both inputs. The profile records this policy and
reports `reasoning_omitted`; source evidence and the fixed trigger remain unchanged. Interpret the
comparison under that resolved history policy. The runner fingerprint binds the continuation
instruction and resolved settings.

### Copilot reconstruction

Copilot restores the selected session prefix through its installed CLI and SDK. Eligible prior-user
appends and paragraph removals require final-response targets. Reconstruction also implements
whole-record removal and comparison's
[source-bound tool output text spans](architecture.md#source-bound-tool-output-text). The
shell-action profile keeps the native `bash` request visible while denying execution. Compatibility
checks apply to the CLI, SDK and effective policy together; a working SDK import alone does not
establish a usable native profile.
