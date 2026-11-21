# Compare two contexts

`ithrl compare` tests one declared difference between two contexts derived from the same immutable
case. Use it to ask whether restoring context improves a fixed behavior, or whether an intervention
reduces that behavior. Install `ithrl[compare]` for statistical inference.

Both contexts keep the same triggering input, target, grader, model, reconstruction, policy and
attempt limits. A second historical capture can suggest an intervention; it cannot serve as the
other execution source. The [worked investigations](research/comparative-debugging.md) record
qualified profiles, costs and interpretation.

## Try without a provider

Create the bundled deterministic case:

```bash
mkdir -m 700 ithrl-compare-demo
ithrl extract --example --output ithrl-compare-demo/case
```

The fake runner matches only when earlier user inputs `A` and `C` remain; restoring either alone
leaves the target unmatched. Their source-bound IDs are `ctx-4c4e9663bfd5daae` and
`ctx-8a6964db1606f4e7`.
Save this configuration as `ithrl-compare-demo/comparison.toml` with mode `0600`:

```toml
schema_version = 1
kind = "comparison"
objective = "repair"
minimum_effect = 0.1
seed = 1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400

[reference]
removed_context_ids = ["ctx-4c4e9663bfd5daae", "ctx-8a6964db1606f4e7"]

[candidate]
removed_context_ids = []

[calibration]
positive_minimum_matches = 2
negative_maximum_matches = 0

[calibration.reference]
required_valid = 2
max_attempts = 2

[calibration.candidate]
required_valid = 2
max_attempts = 2

[confirmation]
alpha = 0.05

[confirmation.reference]
required_valid = 20
max_attempts = 20

[confirmation.candidate]
required_valid = 20
max_attempts = 20

[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
```

Inspect the pair and resource limits, then collect observations:

```bash
ithrl compare ithrl-compare-demo/case --config ithrl-compare-demo/comparison.toml \
  --observation-store ithrl-compare-demo/observations.sqlite --dry-run
ithrl compare ithrl-compare-demo/case --config ithrl-compare-demo/comparison.toml \
  --observation-store ithrl-compare-demo/observations.sqlite
```

This run finishes with `confirmed-improvement`: restoring `A` and `C` raises the match rate from
zero to one, with a directional lower bound greater than `0.1`. It collects four calibration and
40 fresh confirmation observations, makes zero provider requests and executes no returned action.
These sample counts demonstrate a large deterministic effect; they are not a general sample-size
recommendation.

Both contexts may be reduced. To try this, also remove the
irrelevant `B` input (`ctx-c30510481bc8c960`) from both selections and start a new run.
Use IDs from the selected case for real investigations, rather than these example IDs.

## Specify the investigation

Choose the target and intervention before collecting observations. `--experiment FILE` accepts an
existing experiment containing the fixed target, oracle and optional cooperative grader. A saved
profile reuses its frozen experiment and cannot be combined with `--experiment`. Otherwise,
omitting `--experiment` uses the case default.
For a native case, the predicate must keep the captured target category (`final-response` or
`shell-action`), but need not match the historical response. It defines the prospective behavior
tested in both arms; the captured decision remains unchanged and supplies no sample credit.
The comparison configuration sets sample counts, direction, freshness and resource limits;
the experiment retains the target and grader.

Each selection lists `removed_context_ids`, with dependency closure applied, and an optional
source-bound `mutation`. An empty selection means the full original. Selections use the same
[text edit contracts](architecture.md#source-bound-text-sections) as reduction and fuzzing;
unsupported edits and removal of the triggering input are refused before sampling. A selection
may restore a bundle of records removed by an earlier reduction.

### Select records and text

Use the `id` values in the case's private `context.jsonl` edit graph for whole-record removals.
The graph records dependencies and protected units; do not use native session IDs as selectors.
For a previous reduction, `retained-context.jsonl` identifies its surviving units, and
`ithrl inspect retained CASE --result RESULT --format json` relates the retained partition to the
original graph. Copy the chosen removals into `[reference]` and `[candidate]`; restoring a record
means omitting its ID from that selection's removals. Keep the source case unchanged.

List eligible text fields and their source-bound selectors without a model call:

```bash
ithrl inspect sections CASE --format json
```

For paragraph removal, copy `context_id`, `content_index`, `original_text_digest` and
`policy_digest` from one `fields` entry, then choose its `sections[].id` values in source order.
Replace the uppercase placeholders below with those values:

```toml
[[candidate.mutation.edits]]
operation = "remove-text-sections"
context_id = "CONTEXT_ID"
content_index = 0
original_text_digest = "ORIGINAL_TEXT_DIGEST"
policy_digest = "POLICY_DIGEST"
section_ids = ["SECTION_ID"]
```

To append literal text instead, use this edit shape with an eligible prior-user field's selectors:

```toml
[[candidate.mutation.edits]]
operation = "append-text"
context_id = "CONTEXT_ID"
content_index = 0
original_text_digest = "ORIGINAL_TEXT_DIGEST"
text = "Additional context."
```

Use the field's reported `content_index`, which need not be zero. Each context permits at most one
text-field edit. Appends require an otherwise unchanged context; section removals may accompany
record removals when the edited field survives. Native admission checks support for the selected
target and edit before sampling.

### Choose direction and samples

Set `objective` according to the question:

| Objective | Positive calibration arm | Directional effect |
| --- | --- | --- |
| `repair` | Candidate | `p(candidate match) − p(reference match)` |
| `degradation` | Reference | `p(reference match) − p(candidate match)` |

The positive arm must reach `positive_minimum_matches`; the other arm must have no more than
`negative_maximum_matches`. Both require their complete eligible calibration samples. A failed
gate ends with `calibration-failed`, without confirmation. Calibration checks the declared contrast;
its observations receive no credit toward confirmation.

After calibration, the command durably selects the pair and collects one fresh, randomized
confirmation block. A confirmed effect requires its one-sided lower bound to exceed `minimum_effect`
at the declared `alpha`. See [comparison results](results.md#inspect-a-context-comparison) for
outcomes, statistical assumptions and accounting.

## Prepare native execution

Use the [native prerequisites](native-runs.md#native-prerequisites). Execution uses the captured
model and provider unless you select the
[optional Codex profile](#choose-a-prospective-codex-profile). Keep the observation store and
profile outside the case and original workspace. The native runner preserves source evidence,
protects the original workspace and denies execution of returned actions. The
[supported-inputs table](https://github.com/eberkut/ithrl#supported-inputs) owns public capability
claims; structural detection alone does not establish experimental support.

Before detection, replace the demonstration's synthetic selectors and `[limits]` settings. Choose
an `attempt_timeout_seconds` suitable for the native continuation, a nonzero `max_provider_calls`
and a `max_wall_time_seconds` that can admit both calibration and confirmation. Each arm's
`max_attempts` includes any permitted replacement attempts. The dry-run report calculates the
complete request and wall-time ceilings from these counts and the detected per-attempt cap,
cancellation grace and pacing; do not size a full comparison from calibration alone.

In the commands below, `CASE` is the case directory, `ORIGINAL` the original workspace, `STORE`
the private observation database and `PROFILE` the saved profile file. Omit
`--experiment experiment.toml` to use the case's default experiment.

1. Detect and save a profile, with zero provider requests:

    ```bash
    ithrl compare CASE --config comparison.toml --experiment experiment.toml \
      --original-workspace ORIGINAL --detect-capabilities --profile PROFILE
    ```

2. Inspect the plan without starting a harness:

    ```bash
    ithrl compare CASE --config comparison.toml --profile PROFILE \
      --original-workspace ORIGINAL --observation-store STORE --dry-run
    ```

    Check both contexts, objective, provider, model, action denial and request/time limits. If the
    report refuses the budget or freshness window, revise the configuration and repeat detection
    with a new profile before execution. Configuration changes invalidate the saved profile.

3. Execute the reviewed plan within your authorization:

    ```bash
    ithrl compare CASE --config comparison.toml --profile PROFILE \
      --original-workspace ORIGINAL --observation-store STORE --acknowledge-model-execution
    ```

    Execution rechecks the saved profile and makes provider requests within its frozen limits.

`--max-provider-calls-per-attempt` can set a lower native request cap during detection. It becomes
part of the saved execution identity. The command admits whole blocks and reserves confirmation
capacity. Its displayed ceilings are conservative bounds, not token or monetary budgets.
The four arms may schedule at most 1,000 attempts in total. Large context bindings can require a
smaller plan to fit the private result snapshot; preparation reports `comparison-evidence-capacity`
before collecting observations.

### Choose a prospective Codex profile

To test the contexts under a different Codex model or provider, add this table before detection.
Replace `MODEL` with a model available through the selected provider's local authentication:

```toml
[codex_replay]
provider = "openai"
model = "MODEL"
reasoning = "omit"
```

Both arms use the declared profile. Reconstruction omits historical reasoning records, including
summaries, from both inputs and reports `reasoning_omitted`; the source evidence and fixed trigger
remain unchanged. The comparison tests that profile, without reproducing historical reasoning
state. Changing the profile requires a new configuration, detection and fresh observations.

### Stop after calibration

Add `--calibration-only` to a new run or an unselected resume to stop before selection and
confirmation:

```bash
ithrl compare CASE --config comparison.toml --profile PROFILE \
  --original-workspace ORIGINAL --observation-store STORE \
  --acknowledge-model-execution --calibration-only
```

The configuration must still reserve the complete comparison budget. With three required valid
observations and three maximum attempts per calibration arm, the pilot permits six attempts;
provider requests are bounded by the profile's per-attempt cap. A one-request cap permits six
requests. Successful calibration reports `calibrated` and saves an inspectable nonterminal snapshot.
It does not establish a confirmed effect. Repeating `--resume --calibration-only` reuses the
completed pilot without dispatching more observations.

Review calibration before continuing. If confirmation is authorized, resume without
`--calibration-only` to collect fresh observations within the original allowance and deadline.
Calibration observations supply no confirmation credit, and calibration-only mode is refused
after confirmation has been selected.

## Results and resume

Normal output contains identities, edit metadata, counts, bounds and accounting, without source
text or model responses. `--format json` writes one JSON report to stdout; plan and progress go to
stderr. Save the reported `result_path` and `run_state_path`.

Use [comparison inspection](results.md#inspect-a-context-comparison) to read a private snapshot,
interpret its outcome or validate its source selections without a native runtime.

Resume only the canonical run directory:

```bash
ithrl compare RUN_DIRECTORY --resume --acknowledge-model-execution
```

Resume preserves the pair, target, policy and allowance. It never repeats completed confirmation
or redispatches an unresolved attempt. Inspecting an already completed run requires no execution
acknowledgment or native detection. Resume requires the implementation that created the run;
use offline inspection for results from a different implementation. An expired freshness window or
wall-time allowance ends the comparison; inspect its saved evidence before preparing a new run.

If publication fails, follow the reported recovery action. Restore write access for an output
failure, or preserve a conflicting destination file elsewhere. Then resume to publish the saved
terminal evidence without collecting again; ithrl never overwrites the conflicting file.
