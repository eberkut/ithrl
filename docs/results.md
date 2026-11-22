# Results and privacy

For reduction and fuzzing, start with `summary.json` in the printed result directory for the
outcome, evidence identifiers, counts and next action. For `compare`, use the reported
`result_path` and [comparison inspection](#inspect-a-context-comparison).

## Saved files

Reduction results contain the files below; fuzzing and comparison use the workflow-specific files
described in their sections.

| File | Contents |
| --- | --- |
| `summary.json` | Outcome, phase results, context counts, usage and continuation |
| `retained-context.jsonl` | Retained source context records, in order; sampled section edits are recorded separately in `result.json` |
| `request.json` | Original or reduced request when exact-request materialization is available |
| `checkpoint.json` | Deterministic search checkpoint |
| `result.json` | Sampled candidate selection, search checkpoint and confirmation evidence |
| `cli-session.json` | Frozen sampled session inputs, private paths and identity-bound original-plan metadata |
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

## Inspect tool output

List source-bound native tool output text fields without a harness, credentials or model call:

```bash
ithrl inspect tool CASE --format json
```

Codex fields are paired `function_call_output` or `custom_tool_call_output` records: either a
string `payload.output`, or each `text` string in an output array containing only `input_text`
blocks. Copilot fields are string `data.result.content` values in prior `tool.execution_complete`
events. Copilot `detailedContent`, structured values and mixed Codex output arrays are excluded.

Each field reports its identity, source node and native field path, original-text digest,
codepoint count and UTF-8 byte size. Only prior eligible fields are included; the current decision
input remains fixed. Fields beyond the 1 MiB policy appear in `unavailable_fields`.
`metadata_limit` bounds the combined field listings at 4,096 entries; `omitted_field_count`
reports additional entries. A partial listing does not mean the omitted fields are ineligible.

To inspect one field privately, replace `FIELD_ID` with a reported identity:

```bash
ithrl inspect tool CASE --field FIELD_ID --show-text --max-text-bytes 4096
```

Display is escaped and bounded in UTF-8 bytes. `text_truncated` reports incomplete display;
displayed escape characters are not source offsets. Select ranges in the original Unicode string
using the [source-location procedure](comparison.md#select-tool-output-text).
Supplying `--start START --end END` creates a source-bound deletion
descriptor; with `--show-text`, the displayed field is the retained text after that deletion.
See [comparison configuration](comparison.md#select-tool-output-text) for its use and supported
profiles. Discovery alone establishes neither native delivery nor a behavioral effect.

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
proposed. Sampled reduction trials use the full immutable original as their statistical baseline.

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
in JSON and unavailable in human output.

Saved results bind the following plan metadata to `execution_plan_digest`.
`plan_metadata_scope = "immutable-original"` identifies the original plan; candidate-arm plans
can differ. Sampled snapshots retain this metadata without the full private execution plan.

| Field | Interpretation |
| --- | --- |
| `execution_plan_digest` | Identity committing to the full private plan and its disclosed metadata. |
| `capture_type` | Evidence classification used by the plan. |
| `candidate_materialization` | How the runner constructs a candidate. |
| `reconstruction_method` | The capability profile's reconstruction method. |
| `safety_policy_digest` | Identity of the declared safety policy, including its private provider destination. |
| `declared_policy` | Provider-transport permission, forbidden tool network access, denied action execution and unchanged-source/workspace declarations. Private provider destinations are excluded. |
| `resource_limits` | Per-attempt limits bound by the plan. |

These fields describe the declared plan; they do not prove historical enforcement.
Within `recorded_policy_evidence`, `observations_with_digest` and `observations_without_digest`
count embedded observations with and without an `effective_policy_digest`.
`referenced_observations_unavailable` counts references without embedded observations. Sampled
coarse, fine, active-block and confirmation observations contribute embedded evidence. Calibration
retains only observation references; deterministic snapshots likewise retain references rather
than embedded attempt evidence. Inspection does not retrieve those observations.

`effective_policy_digests` contains at most 16 sorted, unique recorded digests across both arms;
`omitted_digest_count` counts additional unique values. A digest identifies recorded attempt
evidence without revealing the policy's content. Live validation requires each completed attempt's
digest to match its own plan's safety policy. A mismatch is a safety error and may remain in failed
attempt evidence. Inspection reports saved digests without independently verifying enforcement.

Loading a saved sampled result requires its identity-bound execution metadata. Incompatible
snapshots are refused; missing metadata is not reconstructed from current settings. See the
[format policy](architecture.md#developer-map) before regenerating incompatible artifacts.

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

The opt-in `safe-provider-overload` policy permits a fresh attempt after a verified structured
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

## Inspect a context comparison

Read a saved comparison without a harness, profile, original workspace or observation store:

```bash
ithrl inspect comparison RESULT --format json
ithrl inspect comparison RESULT --case CASE --format json
```

Both forms validate the snapshot's identities, phase chronology and observation references. Adding
`--case` re-derives its source partitions and text edits, with `source_validation` set to
`verified`; otherwise that field is `not-requested`. The snapshot and case remain private. Default
output contains edit metadata and accounting without session text or model responses.

For a tool output text edit, `candidate_context.tool_output_text_edit` preserves its source-bound
selector and range. With `--case`, `candidate_context.tool_output_text_field` also reports original,
removed and retained codepoint and UTF-8 byte counts. To display the retained field privately:

```bash
ithrl inspect comparison RESULT --case CASE --show-text --max-text-bytes 4096
```

This preview reconstructs the retained string from the verified source and edit; it is not a capture
of native delivered input. It uses the same escaped byte allowance as tool output text inspection.
Common record omissions may remove duplicate carriers from both arms. The native envelope and
surviving carriers still condition the result; deleting one occurrence does not establish absence of
that information.

The report includes both context digests, removed IDs, text-edit metadata, sampling counts and
execution declarations. Recorded requested model and provider appear when their known values agree;
absent, conflicting or unsafe identifiers are omitted. Neither source validation nor the recorded
identities verify the current native environment. `harness_started` describes the current
invocation; offline inspection and completed-run resume do not start a harness.

The reference and candidate are two explicit contexts from one immutable source, rather than two
independent historical captures. For repair, the effect is candidate minus reference match rate;
for degradation, reference minus candidate. `confirmed-improvement` and `confirmed-degradation`
mean the fresh directional lower bound exceeds the declared minimum effect. `inconclusive` means
complete confirmation did not meet that criterion. `calibration-failed` ends before confirmation;
`incomplete` means required eligible evidence was not obtained.

Calibration observations are reported separately and receive no confirmation credit. Operational
errors are not behavioral nonmatches. Counts and usage include unsuccessful work; actual, possible
and pending provider calls remain distinct. Inference assumes independent, stationary valid outcomes
under the prespecified eligibility policy. The result concerns the complete intervention, without
establishing component necessity, minimality, an interaction or a unique historical cause.
See the [comparison guide](comparison.md) for configuration, limits and continuation.

With `safe-provider-transport`, eligible output-free transport failures, preventive timeouts and
structured overloads may consume frozen spare attempts while completed samples remain intact.
Replacement requires no requested actions, unchanged protected state, enforced action denial and
verified cleanup. Unknown acceptance retains the full conservative call charge. The match rate
estimates `P(match | valid)`; it does not include operational failures as nonmatches. Read invalid
counts and remaining spare capacity alongside the valid-sample target. Strict and overload-only
policies retain their own rules.

Safety and cleanup checks do not prove that failures are independent of behavior. A timeout can
select on response latency, which may relate to the answer. The conditional match rate therefore
does not establish the unconditional match probability or execution reliability.

## Budgets and resume

Reports separate phase outcomes, eligibility, reliability and usage. Actual
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
increase for `shrink`; fuzzing and comparison keep their original limits. No workflow repeats
completed confirmation or unresolved dispatches.

For sampled `shrink`, `--resume` requires the saved run or result directory. Starting from the case
without it creates a new run and budget; reusing the observation database alone does not resume.

For sampled `shrink`, `--inter-attempt-delay SECONDS` defaults to zero, persists across resume and
consumes the overall wall-time allowance. Fuzzing and comparison set this delay in their
configuration. A run also stops when its evidence becomes stale or its safety
requirements cannot be verified. Retain the failed evidence; a fresh invocation is a separate run.

## Estimate token costs

`ithrl cost` applies explicit reference rates to recorded token counts or a hypothetical scenario.
It uses the bundled `genai-prices` catalog offline. It does not fetch prices, run a harness or
change execution limits. Estimates are token-price equivalents, not invoices or subscription
charges.

### Price recorded observations

Create a private `pricing.json` with mode `0600`. Choose exact public provider/model identifiers
and a reference time. Set `recorded_source` to the provider, returned model and endpoint in the
private attempt evidence; this explicitly maps a deployment or alias to the chosen rates.

Find provider and model `id` values in the
[GenAI Prices provider catalog](https://github.com/pydantic/genai-prices/tree/main/prices/providers).
Each model's `price_variants[].when` entries define supported context keys and values. Ithrl uses
its installed catalog version, which can differ from the online catalog.

```json
{
  "provider": "openai",
  "model": "gpt-6.1-sol",
  "as_of": "2026-11-19T00:00:00Z",
  "context": {"service_tier": "default"},
  "recorded_source": {
    "provider": "RECORDED_PROVIDER",
    "model": "RECORDED_MODEL",
    "endpoint": "RECORDED_ENDPOINT"
  }
}
```

Use the case and experiment digests from the investigation's saved result. The database is an
explicit input; the command never follows a path saved inside a result.
The database must be private and use rollback-journal mode; WAL databases are unsupported.

```bash
ithrl cost OBSERVATIONS.sqlite --case-digest CASE_DIGEST \
  --experiment-digest EXPERIMENT_DIGEST --pricing pricing.json --output costs.json
ithrl inspect costs costs.json --format json
```

The report covers recorded observations for that case and experiment, including failed attempts.
It counts a repeated physical attempt once and refuses conflicting copies. This scope can include
several runs that used the same case and configuration. `provider_calls_possible_recorded`
preserves possible-call counts present in the evidence, including cap-based counts. Pending
reservations and further cap-dependent exposure remain unknown; the subtotal is not a complete
investigation cost or a monetary execution limit.

Private deployment identifiers and recorded service tiers appear only as digests in the report. When
a recorded tier conflicts with the selected reference tier, the attempt stays unpriced with
`service-tier-unmatched`. A missing tier remains an explicit reference-price assumption. Known and
possible calls overlap rather than add. Started-call counters with unknown acceptance are lower
bounds, even when the recorded counter is zero. Missing usage stays unpriced; verified zero-provider
work has a zero subtotal. Multi-request attempts remain unpriced because their aggregate counts do
not identify each request's model and context threshold.

### Read coverage and rates

`subtotal_usd` sums priced components. `null` means no amount could be established. Recorded reports
list `unpriced_reasons`; hypothetical reports put reasons in `scenario.components[].reason`.
A priced output or cached-input component does not imply complete input coverage.
The JSON view includes at most 20 observation details and counts omissions. The saved report
retains every included observation, up to the selected row limit.
Human output also groups unpriced token components by dimension and reason across the full report.

Diagnostics identify the failing input: `cost-pricing-invalid` for the pricing specification,
`cost-usage-invalid` for hypothetical counts, `cost-source-invalid` for recorded evidence and
`cost-output-invalid` for the destination. `cost-input-mode` identifies missing or mixed mode
arguments. Messages give recovery actions without echoing private paths or input values.

#### Check token counts

Input totals include cached reads and writes; output totals include reasoning tokens. Subsets are
subtracted rather than charged twice. Missing cache counts or write durations can leave input
components unpriced. Contradictory counts refuse estimation. Context thresholds apply to the full
prompt and may change output rates too.
An absent cache-write rate does not establish zero writes; supply an explicit zero count when
writes are known not to apply. Supplying both five-minute and one-hour counts declares an exhaustive
breakdown; their sum must equal any supplied write total. A partial breakdown does not identify
the remainder's retention, so missing duration counts are not inferred. OpenAI catalog write
rates require retention evidence and stay unpriced with `cache-retention-unknown` when it is
unavailable; explicit custom rates record the operator's pricing assumption.

#### Check pricing assumptions

Saved reports bind the reference time and context, package and catalog identity, effective rates,
counts and amounts. Offline inspection validates the digest and arithmetic without the source
database or a current price lookup. The reference time selects pricing; it need not be the provider
request date. Unknown provider/model identifiers, unmatched context
variants and dates outside the catalog's modeled constraints refuse instead of selecting a
fallback. The catalog is reference data, not a complete historical price archive.
When context is omitted and the catalog defines an applicable `default` service tier, the report
records that tier and checks it against recorded evidence. Custom rates keep the supplied context.

Managed Copilot catalog rates are labeled `published-reference-rate`; plan allowances and billing
remain outside the estimate. Other catalog models use `api-list-price-equivalent`. Only token
charges are included; separate tool, storage and other units are outside this report.

### Use custom rates

For custom prices, add `custom_rates` to the specification. Supported keys are `input_mtok`,
`output_mtok`, `cache_read_mtok`, `cache_write_mtok`, `cache_write_5m_mtok` and
`cache_write_1h_mtok`. Quote rates as decimal strings to preserve precision; JSON numbers are
refused. Values are USD per million tokens, such as `"2.50"`, or a cliff schedule:
`{"base":"2","tiers":[{"start":200000,"price":"4"}]}`. Above a threshold, its rate applies to
all tokens. Custom-rate selections carry their own digest and the `custom-rate-equivalent` label.
An absent rate stays unknown; an explicit zero rate is valid.

### Estimate a hypothetical scenario

Create a private `usage.json` with mode `0600`:

```json
{
  "input_tokens": 10000,
  "output_tokens": 1000,
  "cached_input_tokens": 2000,
  "cache_write_tokens": 0,
  "reasoning_output_tokens": 600
}
```

```bash
ithrl cost --scenario usage.json --pricing pricing.json --output scenario-costs.json
```

Scenario mode needs no database or `recorded_source`. Its explicit token counts remain separate
from recorded usage and never fill telemetry gaps. For duration-specific cache writes, supply
`cache_write_5m_tokens` and `cache_write_1h_tokens`; these contribute to the inclusive input total.

## Data and privacy

Cases can contain prompts, source code, command output, paths and credentials. Keep cases, traces,
observation stores and native output private. Never commit them or attach them to a public issue
without reviewing and sanitizing their contents.

Native traces can summarize ignored model telemetry with a byte count and digest. Behavioral
output, actions and usage evidence remain exact; a telemetry summary does not supply missing usage.

By default, observations are stored in the platform's standard user-data directory. Use
`--observation-store PATH` for a dedicated SQLite store inside a private directory. On POSIX
systems, ithrl refuses case files and directories with unexpected permissions.

Source evidence is snapshotted before native execution. The original session and workspace remain
protected; disposable candidate state is separate. Returned actions are observed and denied under
the current profiles.

Content digests detect inconsistent changes. Another process running as the same user can still
replace local files; ithrl assumes that has not happened. On POSIX, it checks ownership and write
permissions for source and case paths. A shared sticky parent is allowed only above a private
user-owned directory. On platforms without ownership checks, custom paths must stay inside the
active user profile.

## Research basis

The [feasibility study](research/feasibility.md#research-basis) explains the original research
motivations and source papers. [Reduction design](research/reduction-design.md),
[context fuzzing](research/context-fuzzing.md) and
[comparative debugging](research/comparative-debugging.md) describe the statistical methods,
measured usefulness and limits behind the result claims.
