# Compare two contexts

`ithrl compare` tests whether a declared context change improves or degrades a fixed behavior.
Both contexts come from one immutable case. Install `ithrl[compare]` for statistical inference.

Both contexts keep the same triggering input, target, grader, model, reconstruction, policy and
attempt limits. A second historical capture can suggest an intervention, but both arms execute from
the selected case. See [bundle comparisons](research/comparative-debugging.md) and
[tool output investigations](research/tool-output-interventions.md) for qualified profiles and
costs.

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

Inspect the pair and resource limits:

```bash
ithrl compare ithrl-compare-demo/case --config ithrl-compare-demo/comparison.toml \
  --observation-store ithrl-compare-demo/observations.sqlite --dry-run
```

The report describes both contexts and the complete calibration and confirmation budget without
collecting observations. Then run with the same configuration and store:

```bash
ithrl compare ithrl-compare-demo/case --config ithrl-compare-demo/comparison.toml \
  --observation-store ithrl-compare-demo/observations.sqlite
```

Expect `confirmed-improvement`: restoring `A` and `C` raises the match rate from
zero to one, with a directional lower bound greater than `0.1`. It collects four calibration and
40 fresh confirmation observations, makes zero provider requests and executes no returned action.
These sample counts demonstrate a large deterministic effect; they are not a general sample-size
recommendation.

Both contexts may be reduced. To try this, remove the irrelevant `B` input
(`ctx-c30510481bc8c960`) from both selections and start a new run. For your own investigation, use
IDs from its case.

## Specify the investigation

Choose the target and intervention before collecting observations. Use `--experiment FILE` to
supply the target, oracle and optional cooperative grader, or omit it to use the case default.
A saved profile contains the frozen experiment and cannot be combined with `--experiment`.
For a native case, the predicate must keep the captured target category (`final-response` or
`shell-action`), but need not match the historical response. It defines the prospective behavior
tested in both arms; the captured decision remains unchanged and supplies no sample credit.
The comparison configuration sets sample counts, direction, freshness and resource limits.

Each selection lists `removed_context_ids` and an optional source-bound `mutation`. Removing a
record also removes records that depend on it. An empty selection means the full original.
Prior-user edits use the same
[text contracts](architecture.md#source-bound-text-sections) as reduction and fuzzing; comparison
also implements the [tool output text span contract](architecture.md#source-bound-tool-output-text).
Unsupported edits and removal of the triggering input are refused before sampling.

### Use a different predicate

Copy the case's default experiment before changing the predicate. In the command below, `CASE` is
the extracted case and `PRIVATE` is an existing directory with mode `0700`, outside the case and
original workspace:

```bash
install -m 600 CASE/experiments/default.toml PRIVATE/experiment.toml
```

For a literal final-response check without task grading, replace the copy's entire `[target]`
table with:

```toml
[target]
type = "final-response"
text_contains = "EXPECTED_PHRASE"
```

Replace `EXPECTED_PHRASE` with the nonempty, case-sensitive text that defines the behavior you want
to measure. Use only one of `text_equals`, `text_contains` or `text_contains_any`; the last accepts
an array of literal alternatives. For shell targets, keep the copied tool identity and action count
and use either `command_equals` or `command_contains`. Keep the target category and all other
tables unchanged, including oracle protections, runner settings and any sampling or grading
configuration. A task grader requires its unconstrained final-response target.

Pass the copy with `--experiment PRIVATE/experiment.toml` during
[capability detection](#detect-inspect-and-execute). Once the profile is saved, omit `--experiment`:
the profile contains the frozen experiment. Keep the source case unchanged; a different predicate
requires a new profile and run.

### Select records and text

Use the `id` values in the case's private `context.jsonl` edit graph for whole-record removals.
It records dependencies and protected units; native session IDs are not context selectors.
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

### Select tool output text

Tool output text discovery and edit authoring are provider-free. Check
[supported inputs](https://github.com/eberkut/ithrl#supported-inputs) and the
[tool output investigations](research/tool-output-interventions.md) for qualified final-response
profiles.

List eligible prior tool output fields:

```bash
ithrl inspect tool CASE --format json
```

Select a reported `field_id`. Use the bounded
[tool output text display](results.md#inspect-tool-output) to examine it privately.

Ranges are nonempty and half-open, in original Unicode codepoints. For example, a string containing
`é`, one LF and `X` has three codepoints and four UTF-8 bytes. The range `[0, 1)` removes `é`;
`[1, 2)` removes the LF. An escaped display such as `é\nX` has different positions from the source.
Select text from the original field, not its escaped display or UTF-8 byte positions.

Save the exact substring in a private UTF-8 file without changing escapes, line endings or trailing
characters. The helper below locates a unique occurrence and reports original codepoint offsets.
Run it in the Python environment containing the same ithrl installation or build as your CLI.
Replace `CASE`, `FIELD_ID` and `SNIPPET` with the case path, discovered field ID and private snippet
file:

```bash
python - CASE FIELD_ID SNIPPET <<'PY'
import sys
from pathlib import Path
from ithrl.case import CaseWorkspace

evidence = CaseWorkspace.open(Path(sys.argv[1])).load()
_, site = evidence.tool_output_text_field(sys.argv[2])
snippet = Path(sys.argv[3]).read_bytes().decode("utf-8")
start = site.original_text.find(snippet)
if not snippet or start < 0 or site.original_text.find(snippet, start + 1) >= 0:
    raise SystemExit("The snippet must occur exactly once in the original field.")
print(f"START={start}\nEND={start + len(snippet)}")
PY
```

The helper prints only offsets. Reading bytes preserves line endings; an added newline or different
escaping will not match. For manual inspection, use `context_id` to find the node in
`context.jsonl`, then `source`, `selector.line` and `field_path` to locate its native snapshot,
JSONL line and JSON pointer.

Use the reported integer `START` and `END` values:

```bash
ithrl inspect tool CASE --field FIELD_ID --start START --end END --format json
```

Copy `mutation.edits[0]` from the report into the candidate configuration below. Replace every
uppercase placeholder and both offsets with the reported values, including the full field ID and
digests. The example offsets delete the field's first codepoint:

```toml
[reference]
removed_context_ids = []

[candidate]
removed_context_ids = []

[[candidate.mutation.edits]]
operation = "remove-tool-output-text-span"
context_id = "CONTEXT_ID"
field_id = "FIELD_ID"
original_text_digest = "ORIGINAL_TEXT_DIGEST"
start = 0
end = 1
removed_text_digest = "REMOVED_TEXT_DIGEST"
```

Only the candidate may contain a span deletion; keep the reference unmutated. Both contexts must
retain the same records after dependency closure, including the edited field and its dependencies.
The edit preserves the result envelope, triggering input and other fields. Removing the complete
field produces an empty string; native reconstruction may omit an empty content block. Common
record omissions condition the comparison on the retained context. Shrink and fuzz do not accept
this operation.

### Choose direction and samples

Set `objective` according to the question:

| Objective | Positive calibration arm | Directional effect |
| --- | --- | --- |
| `repair` | Candidate | `p(candidate match) − p(reference match)` |
| `degradation` | Reference | `p(reference match) − p(candidate match)` |

The positive arm must reach `positive_minimum_matches`; the other arm must have no more than
`negative_maximum_matches`. Both require complete eligible calibration samples. Failure ends the
run with `calibration-failed`; successful calibration permits fresh confirmation. Calibration
observations receive no confirmation credit.

To permit replacement attempts, set `eligibility_policy = "safe-provider-transport"` before any TOML
tables. Keep `required_valid` fixed and set a larger `max_attempts` for each arm before collection.
The policy permits a fresh scheduled attempt after a verified typed transport failure, preventive
timeout or structured overload with no behavioral output. It requires no requested actions, enforced
action denial, unchanged protected state and verified cleanup. Unknown provider acceptance and the
full conservative call charge remain recorded; errors never count as behavioral losses. Captured
behavioral output, unsafe attempts and unresolved dispatches remain ineligible. For the strict
`independent-safe-preaccept-only` and narrower `safe-provider-overload` policies, see
[sampling eligibility](results.md#sampled-results).

Replacement uses only frozen spare slots and resource allowance; it never extends a terminal plan.

After calibration, the command durably selects the pair and collects one fresh, randomized
confirmation block. A confirmed effect requires its one-sided lower bound to exceed `minimum_effect`
at the declared `alpha`. See [comparison results](results.md#inspect-a-context-comparison) for
outcomes, statistical assumptions and accounting.

## Prepare native execution

Check the [native prerequisites](native-runs.md#native-prerequisites) and
[supported inputs](https://github.com/eberkut/ithrl#supported-inputs). Keep the observation store
and profile outside the case and original workspace. Native execution protects the source case and
original workspace and denies returned actions. Structural detection alone does not establish
experimental support.

Before detection, replace the demonstration's synthetic selectors and `[limits]` settings. Choose
an `attempt_timeout_seconds` suitable for the native continuation, a nonzero `max_provider_calls`
and a `max_wall_time_seconds` that can admit both calibration and confirmation. Each arm's
`max_attempts` includes any permitted replacement attempts. The dry-run report calculates the
complete request and wall-time ceilings from these counts and the detected per-attempt cap,
cancellation grace and pacing; do not size a full comparison from calibration alone.

### Choose execution settings

Before detecting capabilities, place any execution overrides before the first TOML table in
`comparison.toml`. The same settings apply to both harnesses; replace the placeholders with
supported values and omit settings you do not need:

```toml
model = "MODEL_ID"
reasoning_effort = "EFFORT_LEVEL"
```

Omitted settings use the values saved in the experiment supplied with `--experiment`. Explicit
values that conflict with saved values are refused. Without a saved value, provider and model use
the captured values when available, and the runner selects reasoning effort.
Codex can select a locally configured provider with `provider = "PROVIDER_ID"`. Copilot uses
`github-copilot` and rejects other providers.

Both arms use the same resolved settings. Detection records the execution and history policy in the
profile; native execution verifies the requested settings before forwarding inference. Source
evidence remains unchanged. Inspect the resolved policy and report any history projection separately
from the context edit. Changing execution settings requires a new profile and fresh observations;
observations from different settings cannot be pooled.

### Detect, inspect and execute

In the commands below, `CASE` is the case directory, `ORIGINAL` the original workspace, `STORE`
the private observation database and `PROFILE` the saved profile file. Omit
`--experiment PRIVATE/experiment.toml` to use the case's default experiment; otherwise use the
[private copy](#use-a-different-predicate) prepared above.

1. Detect and save a profile, with zero provider requests:

    ```bash
    ithrl compare CASE --config comparison.toml --experiment PRIVATE/experiment.toml \
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
