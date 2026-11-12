# Results and privacy

Start with `summary.json` in the printed result directory for the outcome, evidence identifiers,
counts and next action.

## Saved files

| File | Contents |
| --- | --- |
| `summary.json` | Outcome, phase results, context counts, usage and continuation |
| `retained-context.jsonl` | Retained source context records, in order; sampled section edits are recorded separately in `result.json` |
| `request.json` | Original or reduced request when exact-request materialization is available |
| `checkpoint.json` | Deterministic search checkpoint |
| `result.json` | Sampled candidate selection, search checkpoint and confirmation evidence |
| `sampling/sampling-state.json` | Sampled run state and observation references |

Other files record the experiment, execution plan and provenance. Native reconstruction may have
no `request.json`; use the retained-context records and recorded reconstruction method. Leave
internal JSON and SQLite state unchanged so validation and resume can verify it.

## Inspect recorded compaction

`reacquire` compares recorded operations and captured results around compaction in one saved Codex
or Copilot JSONL file. No case, completed decision, harness, credentials or original workspace is
needed. See the [worked investigation](research/passive-compaction-investigation.md) for qualified
profiles and ordinary examples.

First list the recognized successful compactions, then choose an ID from that report:

```bash
ithrl inspect reacquire SESSION --format json
ithrl inspect reacquire SESSION --boundary BOUNDARY_ID --format json
```

### Read the comparison

The report compares intervals immediately before and after compaction. A call and its completion
must share an interval; failed or unresolved compactions limit those intervals. Copied history
is not new execution. Failed, incomplete, straddling or explicitly truncated captures cannot support
positive comparisons. Codex exec headers supply missing exit status: nonzero exits fail and
unrecognized headers remain unresolved.

The first eligible post-compaction occurrence of an exact recorded operation is paired with its
nearest eligible predecessor. Later occurrences are counted without asserting equal results.
Without an exact predecessor, an equal capture in the same native field can identify a *different*
recorded operation; the difference stays explicit.

Equality uses the exact captured values: Copilot `content` and `detailedContent` are compared
separately; Codex preserves native call kind, namespace and argument strings. Matching does not
strip whitespace, timestamps or transport metadata.

| Report evidence | What it establishes |
| --- | --- |
| `exact-recorded-operation` | Recorded tool identity, arguments and captured workspace identity match. Underlying resource identity remains unproven. |
| `different-recorded-operation` | A captured field matches under different recorded operations. |
| `equal` / `different` | Complete, nonempty captured fields compare exactly within the stated scope. |
| `unavailable` | The field is missing, empty or explicitly incomplete; no positive result comparison is supported. |
| `resource_state = "unknown"` | No historical resource snapshot was read or inferred. |

SHA-256, record digests and one-based lines locate evidence. Boundary IDs include line and record
digest; reuse them only with unchanged sources. Generic Codex code/shell operations stay
unclassified: identical commands do not establish a file read. Copilot accepts missing turn IDs
when unique request/start/completion identities suffice, but rejects contradictions. Experimental
extraction keeps stricter turn validation.

### Limits and private detail

Default output excludes paths, tool names, arguments and captured text. To include bounded private
detail, add `--show-text --max-text-bytes 4096`. The total escaped display allowance cannot exceed
65,536 bytes; truncation is explicit.

`--max-groups` defaults to 100, up to 1,000 boundaries or matching groups. Coverage counts omitted
boundaries/matches, ineligible or out-of-interval operations and unknown records in the bounded
snapshot. Input limits are 64 MiB, 8 MiB per record, 250,000 records and 1,000,000 JSON items;
exceeding any limit refuses the source instead of partially scanning it.

Sizes measure canonical UTF-8 JSON used for digests, not native text lengths or model tokens.
Complete captures need not contain entire resources. Usage stays unknown; savings, avoidable work
and compaction's causal effects remain unestablished. Active reconstruction and normalized,
heuristic, resume, fork, handoff or cross-harness comparisons are unsupported.

## Inspect retained context

Inspect a saved reduction alongside its original case:

```bash
ithrl inspect retained CASE --result RESULT
ithrl inspect retained CASE --result RESULT --format json
```

Inspection reads the case and saved deterministic or sampled result without changing either. It
needs no harness, credentials, original workspace, live run directory or observation database, and
makes no provider calls or probes.

### Interpret retained units and trials

The report lists units, editability, dependencies and removal trials. Trials identify requested
and effective removals, candidate, outcome and search parent—the context from which removal was
proposed. Sampled comparisons use the full immutable original as their statistical baseline.

| Evidence | Interpretation |
| --- | --- |
| Protected or frozen unit | The edit graph prevents its removal; this is a structural constraint. |
| Final-parent single-removal trial | Evidence about that complete removal from the final context, under the recorded procedure. |
| Joint or historical trial | An outcome for several removals or an earlier context; it cannot explain each retained item individually. |
| Invalid, incomplete, ineligible or untested work | No supported behavioral conclusion for that removal. |

Eligible retained prior-user fields include `text_section_fields`:

- `available-untested`: a source-bound paragraph catalog describes possible edits, without recorded
  section-removal trials.
- `exploratory-tested` or `selected`: fine trials were recorded, or that field's edit was selected.
- `unavailable`: no partial catalog is supplied. `section-policy-limit` means the field exceeds the
  paragraph policy; `inspection-section-limit` means it exceeds the report's metadata allowance.

See the [section contract](architecture.md#source-bound-text-sections) for boundaries and limits.
Section-aware results record fine trials and selected section IDs. Whole-record outcomes and fine
screens do not establish the necessity of individual paragraphs. Fresh confirmation applies only
to the selected candidate against the full original.

Deterministic reports include `failed_candidate_observation_count` for recorded candidate-run
failures. The checkpoint lacks their removal-set details, so failures cannot be attributed to
individual units or treated as behavioral losses. This count is unavailable for sampled results.

A rejected screen is not confirmed loss or necessity. Coverage and fresh confirmation are separate.
Target type, confirmation counts, margin and interval appear when saved; missing fields are `null`
in JSON and unavailable in human output. Sampled snapshots omit the full execution plan, leaving
detailed runner policy and reconstruction method unavailable.

Inspection checks saved identities against the case. It does not recheck an external observation
database or reproduce historical execution. Byte counts have two scopes:

- Context counts measure canonical UTF-8 JSON. `canonical_json_scope` identifies original source
  records before section edits; those counts do not subtract text removals.
- `original_utf8_bytes` and section `utf8_bytes` measure original text. `start` and `end` are
  half-open Unicode code-point offsets.

Neither measure establishes provider-token or monetary savings.

### Display private content

Explicitly request retained content with a total display allowance:

```bash
ithrl inspect retained CASE --result RESULT --show-text --max-text-bytes 4096
ithrl inspect retained CASE --result RESULT --show-text --text-node NODE_ID
```

Keep text-enabled output private. It displays serialized request values or native source records.
For a section-edited node, only the selected field's exact retained text appears; `text_scope` and
`text_content_index` identify that scope. Other blocks are omitted. Markup and terminal controls
are escaped; the total allowance applies after escaping, cannot exceed 65,536 bytes and reports
truncation explicitly.

Use `--text-node` with a retained unit ID to spend the allowance only on that unit. Metadata and
trial evidence for all units remain in the report. Default output contains metadata only.

## Deterministic results

The bundled example uses the deterministic local runner. A `search-1-minimal` result means every
valid single-group removal from the final context was tested and none preserved the behavior
under the recorded runner, settings and budget. It does not establish the smallest possible
context, a unique solution or a causal explanation of the decision.

Useful options, using the private example directory created in the
[quick start](https://github.com/eberkut/ithrl/blob/main/README.md#try-it):

```bash
# Validate the plan without running it.
ithrl shrink ithrl-demo/case --dry-run

# Test only the original context.
ithrl shrink ithrl-demo/case --observation-store ithrl-demo/observations.sqlite --calibrate-only

# Return one JSON object on stdout; progress goes to stderr.
ithrl shrink ithrl-demo/case --observation-store ithrl-demo/observations.sqlite --format json
```

## Sampled results

Native reduction screens candidates against fresh original samples, freezes one candidate, then
compares it against the immutable original with separate fresh observations. A
`confirmed-preserving` result supports preservation within the declared margin among valid
observations. Errors are excluded from match rates; execution reliability is reported separately.

Inference assumes independent valid observations and stationarity within comparisons. Confirmation
establishes neither minimality, causality nor campaign-wide confidence. `search-incomplete` records
unfinished coverage, such as a comparison limit; it can accompany `confirmed-preserving` if a safe
stop leaves a candidate and enough budget for complete fresh confirmation. Report both outcomes.

A preservation verdict can also apply when the selected context is unchanged. Report actual
removals and usage to establish whether the run produced a useful reduction.

Section-search results retain the coarse candidate/stop reason, coarse/fine comparison counts,
fine coverage and selected one-field mutation. `additional_removed_utf8_bytes` counts editable text
removed from the frozen coarse candidate, excluding whole-record deletions and envelope changes.
`additional_reduction_result` is `criterion-met`, `below-threshold`, `no-retained-fields` or
`not-assessed`, independently of the behavioral verdict. Confirmation below the byte criterion
still fails the workload's additional-reduction gate. Debugging value needs separate interpretation.

Both search stages can be incomplete at final selection. `search-reduced` requires complete coarse
and fine coverage when fine search is configured; `coarse_final_sweep_complete` describes only the
whole-record sweep. The fresh holdout cannot reopen either stage or select a different edit.

The opt-in Codex `safe-provider-overload` policy permits a fresh attempt after a verified structured
overload with no captured output, unchanged protected state and verified cleanup. Failed
observations, unknown acceptance and their full conservative call charge remain recorded. Its
valid-only interpretation assumes independent, stationary attempts with prespecified validity.

## Fuzz results

Fuzzing selects at most one text append and confirms a **decrease** in the original target's match
rate. `confirmed-change` requires separate fresh fixed samples and an exact Clopper–Pearson
union-bound lower bound above the declared decrease. The summary reports threshold, alpha and
original/mutant counts/rates. Inference concerns valid observations under independent, stationary
attempts, not causality, global minimality or overall execution reliability.

| Outcome | Meaning |
| --- | --- |
| `confirmed-change` | Fresh confirmation establishes a decrease greater than the configured threshold. |
| `no-candidate-found` | The bounded exploration selected no mutation for confirmation. |
| `inconclusive` | Calibration did not establish a measurable target, or complete eligible confirmation did not establish the declared decrease. |
| `incomplete` | Required work or eligible evidence was unavailable. |

Use the stop reason to distinguish calibration failure from inconclusive confirmation.
Confirmation remains `not-run` when calibration stops the campaign. Errors remain separate from
valid match/nonmatch counts.

Private fuzz results contain `summary.json`, `fuzz-session.json` and `sampling/sampling-state.json`,
including exact mutation payloads and configuration. The database stays at its frozen external
path. Normal output shows mutation type, byte count and digest without payload text. Fixed input
means what ithrl supplies; the harness may add context. Keep results and the database private.

Use `ithrl fuzz RUN_OR_RESULT --resume`, adding `--acknowledge-model-execution` for native runs.
Resume keeps configuration, source, profile, store, limits and all call accounting, and rechecks
native identity. It never redispatches unresolved attempts, changes final selection or repeats
completed confirmation. `--dry-run --resume` inspects state without starting a harness.

If result saving fails, `result-publication-failed` retains the known usage and canonical run
path and exits with code 70. Resolve the storage problem, then resume from `run_state_path` to
save a new receipt. Completed observations and confirmation are retained.

## Research basis

The [feasibility study](research/feasibility.md#research-basis) explains the original research
motivations and source papers. [Reduction design](research/reduction-design.md) and
[context fuzzing](research/context-fuzzing.md) describe the statistical methods, measured usefulness
and limits behind the result claims.

## Budgets and resume

The summary reports calibration, search, confirmation, eligibility, reliability and usage. Actual
calls are known starts; additional possible calls are usage the runner cannot exclude. Completed
attempts are charged known calls; failures with started provider work retain the full per-attempt
allowance. Pending reservations stay charged until reconciliation. Resume reconciles observations
before admitting another block and never repeats unresolved dispatches.

One valid observation can include several physical requests. Total all contributing requests and
include every valid observation when comparing arm means. If any observation has unknown input
usage, a complete arm mean is unavailable; averaging only known observations changes the sample.
Cached input tokens are already included in the total.

Copilot permits one inference request per attempt and blocks continuation after denial. Available
native evidence determines token reporting. Zero known tokens alongside unknown-token attempts
does not mean zero token use.

`max_provider_calls_per_attempt` reports the administrative cap frozen in the profile and run
identity; see [plan limits](reduction.md#3-generate-and-inspect-the-complete-plan-and-budget) for
configuration. Successful native continuations can exhaust it even when retries are disabled. A
`provider-call-limit` error ends the run with a budget outcome and retained usage; it supplies no
behavioral loss, automatic retry or continuation command. Inspect the evidence before choosing a
different cap for a new run.

After a budget stop, use the printed continuation command with the saved result or active run.
Sampled resume preserves the experiment and completed work. Supported administrative limits may
increase for `shrink`; fuzz keeps its original limits. Neither repeats completed confirmation or
unresolved dispatches.

For sampled `shrink`, `--resume` requires the saved run or result directory. Starting from the case
without it creates a new run and budget; reusing the observation database alone does not resume.

`--inter-attempt-delay SECONDS` defaults to zero, persists across sampled resume and consumes the
overall wall-time allowance. A run also stops when its evidence becomes stale or its safety
requirements cannot be verified. Retain the failed evidence; a fresh invocation is a separate run.

## Data and privacy

Cases can contain prompts, source code, command output, paths and credentials. Keep cases, traces,
observation stores and native output private. Never commit them or attach them to a public issue
without reviewing and sanitizing their contents.

By default, observations are stored in the platform's standard user-data directory. Use
`--observation-store PATH` for a dedicated SQLite store inside a private directory. On POSIX
systems, ithrl refuses case files and directories with unexpected permissions.

Source evidence is snapshotted before native execution. The original session and workspace remain
protected; disposable candidate state is separate. Returned actions are observations and never
execute under the current profiles.

Content digests detect inconsistent changes. Another process running as the same user can still
replace local files; ithrl assumes that has not happened. On POSIX, it checks ownership and write
permissions for source and case paths. A shared sticky parent is allowed only above a private
user-owned directory. On platforms without ownership checks, custom paths must stay inside the
active user profile.
