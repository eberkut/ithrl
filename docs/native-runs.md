# Native runs

`extract` reads saved session evidence. `shrink` and `fuzz` test a selected behavior using a local
harness and its configured model provider. Extraction does not start a harness or make model
requests.

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
- A private directory, mode `0700`, outside the case and original workspace for receipts,
  configuration and the observation store.

The model and provider come from captured evidence and supported local harness configuration.
There is no `--model` override. Returned actions are observed and denied; they never execute.
Tool-side network access, hosted tools and MCP are disabled for the current runner profiles.

For Codex history containing tool outputs, set an explicit `tool_output_token_limit` in the root
of the configuration selected by `--codex-config`, before any TOML tables. For example:

```toml
tool_output_token_limit = 65536
```

Choose a value large enough for every retained text output. Admission conservatively bounds each
output by its serialized ASCII JSON size and refuses a missing or inadequate limit before dispatch;
the native model's default may truncate injected history. Values must be positive integers no larger
than the worker-request byte limit (64 MiB). The rendered setting is bound into the configuration
and capability identities, so changing it requires a new experiment. This is a history-truncation
setting, not an input-token or cost cap. Image/audio tool outputs are unsupported.

### 1. Inspect the local capabilities

```bash
ithrl shrink CASE --original-workspace ORIGINAL \
  --required-valid 3 --minimum-matches 2 --max-attempts 7 \
  --attempt-timeout 120 --max-provider-calls-per-attempt 16 \
  --dry-run --save-preflight PRIVATE/preflight.json --format json
```

Native `--dry-run` can start bounded, network-denied local harness probes. It starts no model turn
and makes no provider call. Inspect the returned capabilities and safety controls. For Copilot,
use `--max-provider-calls-per-attempt 1`.

`--save-preflight` writes a private receipt after successful preflight. It requires a new native
dry-run and an absent output path outside the case and original workspace. The receipt records
capability, case, target, oracle and resource-limit identities without copying session text or
authentication. Keep it private: it includes local paths and provider configuration metadata.
It is recorded evidence, not authenticated provenance or proof that the runtime is still ready.

### 2. Configure the experiment

Save explicit statistical choices as `PRIVATE/settings.toml`, mode `0600`. This example uses the
counts from the measured Codex reduction:

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

Choose the predicate and an acceptable degradation margin before collecting observations. This
measured configuration provides a bounded starting point; its usefulness on other decisions depends
on their behavior, variability and removable context. The earlier
[synthetic reduction reference](measurements.md#synthetic-reduction-reference) used smaller search
samples on a planted instruction. See [sampled results](results.md#sampled-results) for assumptions
and error handling.

#### Optional section-removal settings

Finer final-response reduction has provider-free implementation tests and
[ordinary-session qualification measurements](measurements.md#ordinary-session-paragraph-removal)
for both harnesses. The README's supported-inputs table remains the public capability claim. Shell
targets and fuzzing do not accept section removal.

Before preparation, list source-bound catalogs without a saved reduction result:

```bash
ithrl inspect sections CASE --format json
```

The report contains eligible field IDs, ordered section IDs, half-open Unicode codepoint offsets
and UTF-8 byte counts, without text. Fields outside the section policy limits are reported as
unavailable. These catalogs describe structural edit choices; they do not show that a field will
survive coarse search or preserve behavior.

Choose the eligible fields, removable sections, traversal order, additional-byte criterion and
stage budgets before collecting new observations. Add these tables to `PRIVATE/settings.toml`
before running `prepare`, using field IDs and an ordered subset of section IDs from the catalog:

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

Repeat the field table for another eligible field. `prepare` derives and validates the source
hashes and writes the existing `[reduction.section_search]` contract into the generated experiment.
Omitted sections are protected during fine search. Each fine candidate edits one field; accepted
edits do not accumulate across fields.
The first fine candidate removes all declared eligible sections in that field. If it cannot be
admitted or fails screening, search tries smaller groups. This does not remove protected sections
or change whole-record search order.
This traversal has a new algorithm identity. Earlier partition-first experimental results require
their original build for inspection and cannot resume under the current implementation.
Fields deleted by coarse search are skipped. If none survives, the report records
`no-retained-fields`; it does not substitute another source or field. The
[original workload proposal](research/reduction-design.md#text-section-workload-preparation) used a
64-byte Codex threshold and a 256-byte Copilot threshold, assessed separately from debugging value.
Use the exact frozen criterion from the approved execution package for a qualification run.

Set `require_criterion_for_confirmation = true` to stop before final selection or confirmation
unless the selected fine candidate meets the frozen additional-byte criterion. The report records
`section-criterion-not-met` when no field survives or removal falls below the minimum; no
confirmation attempts are dispatched. The setting defaults to `false`, which allows confirmation
independently of the byte criterion. It is part of the frozen experiment and cannot change on
resume. Meeting the byte criterion alone establishes neither behavioral preservation nor
debugging value.
An administrative budget stop remains resumable while fine search has frozen comparison capacity;
the gate still prevents selection or confirmation before the criterion is met.

Set `stop_when_no_retained_fields = true` to stop coarse search when accepted removals leave none
of the selected fields available. This requires `require_criterion_for_confirmation = true`.
The decision is made at a completed comparison boundary, before reserving another sampling block;
the accepted removal is kept. When this rule ends coarse search, the report records
`coarse_stop_reason = "no-retained-fields"`.
No fine search or confirmation follows. Both the early-stop setting and its default `false` are
part of the frozen experiment; enabling it does not protect a field from coarse removal.

`--max-candidates` remains an administrative cap over both stages. Each stage also obeys its own
frozen comparison ceiling; unused coarse capacity cannot enlarge fine search. Preflight reports both
ceilings and caps its attempt estimate at their sum. Every comparison and final confirmation still
uses the full immutable original as the baseline. A budget increase cannot change the frozen field
selection, stage ceilings, criterion or sampling plan. Such a change requires a new experiment.

### 3. Generate and inspect the complete plan and budget

After completing the settings, generate the private experiment and its inspection command. Choose
one of these Codex preparation commands, according to whether the settings include `[sections]`.

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

The section plan permits `7 + 16*(30+30) + 2*124 = 1215` attempts and 19,440 provider requests at
the recorded 16-request cap. The 48-hour wall limit covers attempt deadlines, cancellation and
pacing. These are conservative admission bounds, not expected usage. The administrative candidate
limit covers both stages; a limit of two could be exhausted by coarse search before fine search.
For a smaller section plan, reduce the stage ceilings and recalculate the administrative budgets
together. Field survival and preservation still require observation.

`prepare` preserves the extracted target, oracle and protected IDs, binds the recorded capability
digest, and reuses the normal preflight budget calculations. It starts no harness, reads no
authentication and makes no provider calls. It neither opens the observation store nor inspects
the original workspace. Output is private, bounded and never overwrites an existing file. Later
settings edits require preparation to a new output path; they do not update an existing experiment.

The settings contain statistical policy; command arguments carry administrative ceilings and
pacing. Per-attempt timeout, request and optional token bounds come from the receipt. If fixed
native resource limits have changed, export a new receipt. Preparation does not choose sample
counts, a margin or a usefulness criterion from the available budget.

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

The two-comparison limit bounds exploratory cost. Search may finish with `search-incomplete` and
still confirm the selected candidate, as the measured run did. For a wider search, increase
`--max-candidates` before execution and raise the call and wall-time limits to cover the additional
complete comparisons. See [sampled results](results.md#sampled-results) for the distinction between
search coverage and confirmation.

A native turn can require several successful requests even with retries disabled. Codex accepts
`--max-provider-calls-per-attempt N` from 1 to 16 and defaults to 16. A lower cap can stop a turn
before completion. Recalculate the total-call allowance when changing it.

With a Copilot runner, the whole-record example allows at most 375 calls; the section example
allows at most 1,215. Set `--max-provider-calls-per-attempt 1` during native preflight, use the
corresponding total-call budget during preparation and omit the 15-second delay.
This translates the plans' resource limits; the
[Copilot saved-session measurements](measurements.md#earlier-copilot-reductions) used separate
sampling configurations. The request gate stops shell execution and any continuation after denial.

The current profiles have these operating limits:

| Limit | Codex | Copilot |
| --- | --- | --- |
| Physical inference requests per attempt | 1–16; default 16 | 1 |
| Inter-attempt delay | Defaults to zero; measured examples use 15 seconds | Defaults to zero; measured examples use zero |
| Native output per attempt | 8 MiB | 8 MiB |
| Disposable files per attempt | 64 MiB; 10,000 entries | 256 MiB; 10,000 entries |
| Token usage | Reported for completed observations in the measured profiles | Available when native events report it; unknown usage remains explicit |
| Hard input-token cap | Unavailable | Unavailable |

Both harnesses use the same frozen sampling and independent-confirmation contracts. The model and
provider are selected from captured evidence and supported local configuration. Token telemetry
depends on the native profile: the current Copilot SDK campaign reported usage, while older
semantic-import campaigns did not. See [measurements](measurements.md#native-context-fuzzing).

The dry-run's minimum call/time figures are conservative admission requirements based on maximum
attempts, the per-attempt request cap, timeouts and pacing. Use measured latency and usage to
estimate actual cost. Neither profile enforces a hard monetary cap.

The per-attempt cap is part of capability and saved-run identity. Use the same cap throughout
preflight and execution; changing it requires a new plan. Increasing a sampled run's total
allowance on resume leaves that cap unchanged.

This fresh native preflight checks the current runtime and configuration against the generated
capability binding. Preparation alone cannot establish reconstruction compatibility or execution
readiness. Check the provider, model, destination, safety policy and total limits. Subscription
quota is not predictable from provider-call limits.

### 4. Run the experiment

Use the complete-plan preflight's `next_command` through the same installation and environment
used for preflight. It includes `--acknowledge-model-execution`.
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
behavior's match rate. Fuzzing requires the optional `fuzz` extra (`ithrl[fuzz]`). Codex and Copilot
support appends to eligible prior user input while preserving the selected decision's triggering
input. The [measurements](measurements.md#native-context-fuzzing) record the completed synthetic
campaigns and their limits. Copilot restores the native session prefix through the installed CLI and
SDK. Copilot shell-action profiles support context removal only.

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

### Run the synthetic native campaign

Use a source checkout with the locked development environment, including the `fuzz` extra; see
[contributor setup](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md#development-setup).
You also need Linux, usable Bubblewrap, an installed native harness and its local authentication.
The public inputs below contain only the synthetic response-token exercise. Choose one harness
when extracting; the remaining detection and execution steps apply to either harness. A new native
run makes real provider requests and can produce different observations.

| Harness | Public source | Decision | Target |
| --- | --- | --- | --- |
| Codex | `examples/fuzz/codex.jsonl` | `decision-f5eaba307afd19db` | `final-response` |
| Copilot | `examples/fuzz/copilot.jsonl` | `decision-cf9cce503bc9fc40` | `final-response` |

From the checkout root, create private state outside the workspace and copy the public
configuration:

```bash
FUZZ_RUN=$(mktemp -d /tmp/ithrl-fuzz.XXXXXXXX)
mkdir -m 700 "$FUZZ_RUN/original"
install -m 600 examples/fuzz/campaign.toml "$FUZZ_RUN/campaign.toml"
```

The configuration uses the measured statistical settings: 3 calibration samples, 8 samples per
exploratory arm, 20 fresh samples per confirmation arm, a 40-percentage-point screening decrease
and a final required decrease greater than 10 percentage points at `alpha=.05`. It permits one
candidate and ten simplification trials, with a 350-call ceiling, six-hour wall-time ceiling,
120-second attempt timeout and 24-hour freshness. It uses the opt-in `safe-provider-overload`
eligibility policy. These settings measured a large planted effect; 20 confirmation samples have
weak power for moderate effects. See the
[effect-size limits](research/context-fuzzing.md#alternatives-and-effect-size-limits) before
adapting the experiment to another workload.

For Codex's default per-attempt cap of 16, set `max_provider_calls = 5600` in the private copy
before detection. This reserves 1,152 calls for confirmation and 576 for one exploratory block;
the supplied 350-call limit cannot admit both. The adjustment retains comparable budget headroom
under the conservative attempt bound. It does not predict actual call use or guarantee completion.

To select a different Codex cap, pass `--max-provider-calls-per-attempt N` during detection and
recalculate the total-call allowance before reviewing the plan. The saved profile and campaign
retain that cap; changing it requires a new profile and run.

Choose pacing in the private copy before detection. Keep `inter_attempt_delay_seconds = 15` for
Codex, matching its completed campaign. For Copilot, set `max_provider_calls = 339` and
`inter_attempt_delay_seconds = 0`, matching its measured native-prefix campaign. That campaign, its
control and interrupted-run calls shared a combined 350-call qualification allowance. Copilot's
per-attempt inference cap is one. Pacing is administrative, consumes wall time and does not change
the statistical sample targets. These settings apply to this synthetic exercise; provider throttling
and ordinary-session costs remain workload-dependent. The call ceiling can stop exploration early
while preserving the 72-attempt fresh-confirmation reserve. The six-hour ceiling is an
administrative limit; [measurements](measurements.md#calls-duration-and-resource-use) record the
completed durations.

Extract one case. For Codex:

```bash
uv run ithrl extract examples/fuzz/codex.jsonl --harness codex \
  --decision decision-f5eaba307afd19db --target final-response \
  --output "$FUZZ_RUN/case" --format json
```

For Copilot:

```bash
uv run ithrl extract examples/fuzz/copilot.jsonl --harness copilot \
  --decision decision-cf9cce503bc9fc40 --target final-response \
  --output "$FUZZ_RUN/case" --format json
```

The case retains captured routing and model evidence; there is no model override.

1. Detect structural capabilities without provider requests:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --detect --profile "$FUZZ_RUN/profile.json" \
      --original-workspace "$FUZZ_RUN/original" --format json
    ```

    Detection uses bounded, network-denied local harness probes. Inspect the detected append-text
    support, reconstruction deviations and effective safety policy.

2. Review the complete plan without starting a harness:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --profile "$FUZZ_RUN/profile.json" --original-workspace "$FUZZ_RUN/original" \
      --observation-store "$FUZZ_RUN/observations.sqlite" --dry-run --format json
    ```

    Check the provider, model, destination, isolation, action-denial policy and complete budget.
    The profile binds the case, configuration and original workspace. Changed inputs need a new
    profile. The native profile cannot enforce a hard input-token cap.

3. Authorize execution of the reviewed plan:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --profile "$FUZZ_RUN/profile.json" --original-workspace "$FUZZ_RUN/original" \
      --observation-store "$FUZZ_RUN/observations.sqlite" \
      --acknowledge-model-execution --format json
    ```

The same procedure accepts an eligible final-response case extracted from your own saved session.
Pass that session's original workspace instead of the empty example workspace, and keep its case and
configuration private. Fuzz `--dry-run` starts no harness or provider; a native case without a
profile reports `detection-required`. Resume from the printed run/result path as described in
[fuzz results](results.md#fuzz-results). See the [CLI reference](cli.md#ithrl-fuzz) for options and
[troubleshooting](troubleshooting.md#fuzz-setup-and-resume) for refusals.

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

Native runners check the installed harness's required operations, permissions, isolation and action
denial locally. These checks and calibration determine whether the configured run can proceed.
Public experimental support also requires a qualified live workflow for that target profile; see
the [supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs).
Harness release numbers are recorded for reproducibility; there is no exact-release allowlist.
After a harness or configuration change, export a new preflight receipt and prepare a new
experiment. A changed digest prevents reuse of an old pinned plan or incompatible saved run.

The [measurements](measurements.md) record benchmark construction, completed reduction and fuzzing
workflows, tested harness versions, confirmation results and costs. Other releases go through the
same local compatibility checks. Wall-time and call limits are administrative ceilings; use the
measurements to estimate the duration and usage of the recorded workloads.
