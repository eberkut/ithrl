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

The passive `reacquire` inspector reads one saved Codex or Copilot JSONL file directly. It needs no
case, completed decision, native harness, credentials or original workspace. The command is
implemented; ordinary-session and installed-artifact qualification is tracked in
[#65](https://github.com/eberkut/ithrl/issues/65). It does not establish active compaction support.

First list the recognized successful compactions, then choose an ID from that report:

```bash
ithrl inspect reacquire SESSION --format json
ithrl inspect reacquire SESSION --boundary BOUNDARY_ID --format json
```

The selected report compares the immediately adjacent source epochs. Both the native call and
its completion must lie within their epoch. Failed, incomplete, straddling and explicitly
truncated results cannot support a positive comparison. Failed or unresolved compaction events
limit the epochs; nested copied history is never counted as new tool execution.

For each exact recorded operation, the first eligible occurrence after compaction is paired with
the nearest eligible predecessor. Later occurrences of that operation are counted separately;
their results are not asserted equal. When the first occurrence has no exact predecessor, an
equal capture in the same native field can identify a different recorded operation. Its known
operation difference remains explicit. Copilot `content` and `detailedContent` are compared
separately. Codex arguments remain exact captured strings, including their native call kind and
namespace. Whitespace, timestamps and transport metadata are not removed for equality matching.

| Report evidence | What it establishes |
| --- | --- |
| `exact-recorded-operation` | Recorded tool identity, arguments and captured workspace identity match. Underlying resource identity remains unproven. |
| `different-recorded-operation` | A captured field matches under different recorded operations. |
| `equal` / `different` | Complete, nonempty captured fields compare exactly within the stated scope. |
| `unavailable` | The field is missing, empty or explicitly incomplete; no positive result comparison is supported. |
| `resource_state = "unknown"` | No historical resource snapshot was read or inferred. |

Source SHA-256, record digests and one-based line anchors locate the evidence. A boundary ID
includes its line and record digest; use it with the same immutable source. Default output excludes
paths, tool names, arguments and captured text. Generic Codex code/shell operations remain
unclassified; an identical command is not automatically a file read. Copilot's passive profile can
accept missing turn IDs when unique request/start/completion identities suffice; known turn
contradictions still refuse. This leaves experimental extraction's stricter turn validation
unchanged.

`--max-groups` caps reported boundaries and matching groups at 100 by default, up to 1,000. Coverage
records omitted boundaries or matching occurrences, ineligible operations, operations outside the
adjacent epochs and unknown records. Counts describe the bounded snapshot, not all historical
activity. Input limits are 64 MiB, 8 MiB per JSONL record, 250,000 records and 1,000,000 JSON items.
Sources exceeding a limit are refused rather than partially scanned.

For private investigation, add `--show-text --max-text-bytes 4096`. The total escaped detail
allowance is at most 65,536 bytes; truncation is explicit. Result sizes count canonical
ASCII-escaped JSON encoded as UTF-8, not native text length or model tokens. Captured completeness
does not prove that the tool returned an entire resource. Usage stays unknown and savings are not
estimated. Repetition establishes neither avoidable work nor an effect caused by compaction.
Normalized, heuristic, resume, fork, handoff and cross-harness comparisons are outside this
command's scope.

## Inspect retained context

Inspect a saved reduction alongside its original case:

```bash
ithrl inspect retained CASE --result RESULT
ithrl inspect retained CASE --result RESULT --format json
```

The inspector reads the case and saved deterministic or sampled result. It needs no installed
native harness, credentials, original workspace, live run directory or observation database.
It makes no provider calls, starts no harness probes and changes no input files.

The report lists retained units, their editability and dependencies, and the recorded removal
trials that concern them. Each trial identifies its search parent, requested and effective
removals, candidate, and outcome. Sampled comparisons retain the full immutable original as their
statistical baseline; the search parent is the context from which another removal was proposed.

| Evidence | Interpretation |
| --- | --- |
| Protected or frozen unit | The edit graph prevents its removal; this is a structural constraint. |
| Final-parent single-removal trial | Evidence about that complete removal from the final context, under the recorded procedure. |
| Joint or historical trial | An outcome for several removals or an earlier context; it cannot explain each retained item individually. |
| Invalid, incomplete, ineligible or untested work | No supported behavioral conclusion for that removal. |

Eligible retained prior-user fields also include `text_section_fields`. An `available-untested`
field has a source-bound paragraph catalog: its section IDs and ranges describe possible edits, not
recorded section-removal trials. `exploratory-tested` and `selected` distinguish fields with
recorded fine trials and the selected edit. Whole-record trial outcomes do not establish that an
individual section is needed. An `unavailable` field has no partial catalog; `section-policy-limit`
identifies a field outside the paragraph policy's limits, while `inspection-section-limit`
identifies the report's metadata allowance. See the
[section contract](architecture.md#source-bound-text-sections) for boundaries and limits. A
section-aware result also records fine trials and the selected section IDs. These exploratory
screens apply to complete tested edits; they do not establish necessity of individual retained
paragraphs. The final confirmation applies to the one selected candidate against the full original.

Deterministic reports include `failed_candidate_observation_count` for recorded candidate-run
failures. The checkpoint lacks their removal-set details, so failures cannot be attributed to
individual units or treated as behavioral losses. This count is unavailable for sampled results.

A sampled screen rejection is exploratory, not confirmed loss or necessity. Search coverage and
fresh confirmation are reported separately. The report includes the recorded target type,
confirmation counts, margin and interval when available. Missing fields are `null` in JSON and
shown as unavailable in human output. Current sampled snapshots do not contain their full execution
plan, so their reconstruction method and detailed runner policy remain unavailable.

Inspection validates saved identities and consistency against the supplied case; it does not recheck
observations in an external database or reproduce historical execution. Results concern the recorded
reconstructed context and runner policy. Context byte counts measure canonical JSON encoded as
UTF-8. `canonical_json_scope` identifies original source records before section edits; these counts
do not subtract text removals. In section catalogs, `original_utf8_bytes` and each section's
`utf8_bytes` count the original text only; `start` and `end` are half-open Unicode code-point
offsets. Neither byte measure establishes provider-token or monetary savings.

For private troubleshooting, explicitly include retained content with a total display allowance:

```bash
ithrl inspect retained CASE --result RESULT --show-text --max-text-bytes 4096
ithrl inspect retained CASE --result RESULT --show-text --text-node NODE_ID
```

This displays serialized request values or native source records. For a section-edited node it
displays only the selected field's exact retained text; `text_scope` and `text_content_index`
identify that narrower view. Other blocks in that node are not displayed. This output may contain
sensitive session content. Markup and terminal controls are escaped. The byte allowance applies
across all displayed content after escaping, with a maximum of 65,536 bytes; truncation is explicit.
Default output contains metadata only. Keep text-enabled output private.

Use `--text-node` with a retained unit ID from the report to inspect a later unit without spending
the allowance on earlier records. All unit metadata and trial evidence remain in the report;
only the selected unit's content is displayed.

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

Native reduction screens candidates against fresh samples of the original context, freezes one
selected candidate, then compares it with the immutable original using separate fresh observations.
A confirmed result supports preservation within the declared degradation margin among valid
observations. Errors are excluded from match-rate calculations; execution reliability is reported
separately.

The inference assumes independent valid observations and stationarity within each comparison.
Sampled confirmation does not establish global or confirmed minimality, causality or campaign-wide
confidence. `search-incomplete` records unfinished search coverage, for example at a comparison
limit. It can accompany `confirmed-preserving` when a safe search stop leaves a selected candidate
and enough budget for complete fresh confirmation. Report both outcomes.

A preservation verdict can also apply when the selected context is unchanged. Report actual
removals and usage to establish whether the run produced a useful reduction.

With `reduction.section_search`, results preserve the coarse candidate and stopping reason,
coarse and fine comparison counts, fine coverage, and the selected one-field mutation. The summary's
`additional_removed_utf8_bytes` counts only original editable text removed relative to the frozen
coarse candidate. It excludes whole-record deletions and native envelope changes. The independent
`additional_reduction_result` is `criterion-met`, `below-threshold`, `no-retained-fields`, or
`not-assessed`; none is a behavioral verdict. A confirmed candidate below the byte criterion still
fails that workload's additional-reduction gate. Debugging value requires its own interpretation.

Both search stages can be incomplete at final selection. `search-reduced` requires complete coarse
and fine coverage when fine search is configured; `coarse_final_sweep_complete` describes only the
whole-record sweep. The fresh holdout cannot reopen either stage or select a different edit.

The opt-in Codex `safe-provider-overload` policy permits a fresh attempt after a verified structured
overload with no captured output, unchanged protected state and verified cleanup. Failed
observations, unknown acceptance and their full conservative call charge remain recorded. Its
valid-only interpretation assumes independent, stationary attempts with prespecified validity.

## Fuzz results

Fuzzing explores text appends to editable context, selects at most one mutation and confirms a
**decrease** in the original target's match rate. `confirmed-change` requires separate fresh fixed
samples and an exact Clopper–Pearson union-bound lower bound above the declared decrease. The
threshold, alpha, original/mutant counts and rates appear in the summary. This estimates behavior
among valid observations under independent, stationary attempts; it does not establish causality,
global minimality or overall execution reliability.

| Outcome | Meaning |
| --- | --- |
| `confirmed-change` | Fresh confirmation establishes a decrease greater than the configured threshold. |
| `no-candidate-found` | The bounded exploration selected no mutation for confirmation. |
| `inconclusive` | Calibration did not establish a measurable target, or complete eligible confirmation did not establish the declared decrease. |
| `incomplete` | Required work or eligible evidence was unavailable. |

Use the stop reason to distinguish calibration failure from inconclusive confirmation.
Confirmation remains `not-run` when calibration stops the campaign. Errors remain separate from
valid match/nonmatch counts.

Private fuzz results contain `summary.json`, `fuzz-session.json` and
`sampling/sampling-state.json`; the observation database remains at its frozen external path.
The session and state retain exact mutation payloads and configuration. Normal human/JSON output
shows mutation type, byte count and digest without printing payload text. The fixed triggering
input covers what ithrl supplies to the harness; the harness may add context. Keep the entire
result and database private.

Resume with `ithrl fuzz RUN_OR_RESULT --resume`, adding `--acknowledge-model-execution` for a
native campaign. The session freezes configuration, source, profile, store and limits. Resume
continues only that campaign, rechecks native identity before execution and preserves actual,
additional possible, pending and conservatively charged calls. It never redispatches an unresolved
attempt, changes final selection or repeats completed confirmation. `--dry-run --resume` inspects
the saved state without starting a harness.

If result saving fails, `result-publication-failed` retains the known usage and canonical run
path and exits with code 70. Resolve the storage problem, then resume from `run_state_path` to
save a new receipt. Completed observations and confirmation are retained.

## Research basis

The [feasibility study](research/feasibility.md#research-basis) explains the original research
motivations and source papers. [Reduction design](research/reduction-design.md) and
[context fuzzing](research/context-fuzzing.md) describe the statistical methods, measured usefulness
and limits behind the result claims.

## Budgets and resume

The summary reports calibration, search, confirmation, eligibility, reliability and known usage.
Actual calls record known starts; additional possible calls retain usage that the runner cannot
exclude. Completed attempts are charged their known calls. Failed attempts with started provider
work retain the full per-attempt allowance, and pending dispatch reservations stay charged until
reconciled. Reopening a run reconciles saved observations before admitting another complete block;
it never repeats an unresolved dispatch.

One valid observation can include several physical requests. Compare input usage by totaling all
requests contributing to each observation and including every valid observation in each declared
arm. A complete arm mean requires known input usage for every observation; do not silently average
only the known subset. Cached input tokens are already included in total input tokens.

Copilot's request gate permits one inference request per attempt and blocks continuation after
denial. Token usage depends on the native evidence available for each attempt; unknown usage stays
explicit. Zero known tokens with nonzero unknown-token attempts does not mean zero token use.

`max_provider_calls_per_attempt` reports the administrative cap frozen in the profile and run
identity; see [plan limits](native-runs.md#3-generate-and-inspect-the-complete-plan-and-budget) for
configuration. Successful native continuations can exhaust it even when retries are disabled. A
`provider-call-limit` error ends the run with a budget outcome and retained usage; it supplies no
behavioral loss, automatic retry or continuation command. Inspect the evidence before choosing a
different cap for a new run.

When a search stops at a budget, follow its printed continuation command. Deterministic searches
can resume from a saved result or active run. Sampled resume preserves the experiment and completed
work. For sampled `shrink`, supported administrative limits may increase; fuzz resume keeps its
original limits. Neither repeats completed confirmation or automatically replays an unresolved
dispatch.

Starting sampled `shrink` again from the case without `--resume` creates another run with its own
budget. To continue existing work, use `--resume` with its saved run or result directory. Reusing
the observation database alone does not resume that run.

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
