# CLI reference

Test how agent context affects a model decision.

**Usage**:

```console
$ ithrl [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--version`: Show the installed version and exit.
* `--install-completion`: Install completion for the current shell.
* `--show-completion`: Show completion for the current shell, to copy it or customize the installation.
* `--help`: Show this message and exit.

**Commands**:

* `fuzz`: Find text appends that decrease the...
* `compare`: Test whether a context change improves or...
* `prepare`: Generate a native reduction configuration...
* `cost`: Estimate recorded or hypothetical token...
* `extract`: Create a case from a saved session or...
* `shrink`: Find a smaller context that preserves the...
* `inspect`: Inspect saved sessions, cases and reports...

## `ithrl fuzz`

Find text appends that decrease the selected behavior&#x27;s match rate.

Requires the fuzz extra. Use --config to choose mutations, samples and limits,
then inspect with --dry-run. A selected append receives separate fresh
confirmation. For native cases, first save a profile with --detect; execution
requires --acknowledge-model-execution.

**Usage**:

```console
$ ithrl fuzz [OPTIONS] {CASE_OR_RUN}
```

**Arguments**:

* `CASE_OR_RUN`: Case, or saved run/result with --resume.  [required]

**Options**:

* `--config <path>`: Private TOML with explicit mutation, sampling and resource limits.
* `--profile <path>`: Private structural profile; output path with --detect.
* `--original-workspace <path>`: Protected original workspace for a native case.
* `--max-provider-calls-per-attempt <int range>`: Hard provider-call limit per native attempt. Codex default: 16; Copilot requires 1. Resume preserves the saved limit. This budget does not guarantee turn completion.  [1&lt;=x&lt;=16]
* `--observation-store <path>`: Private SQLite store outside protected state.
* `--dry-run`: Inspect saved inputs and limits without starting a harness or provider.
* `--detect`: Probe local capabilities without provider requests; save a profile.
* `--resume`: Continue exactly the saved campaign, without changing its inputs or budget.
* `--acknowledge-model-execution`: Approve the inspected native execution within its frozen limits.
* `--format <human|json>`: Output format.  [default: human]
* `--help`: Show this message and exit.

## `ithrl compare`

Test whether a context change improves or degrades the selected behavior.

Requires the compare extra. Both contexts come from one case and keep the same
triggering input. Use --config to select the pair, direction, samples and
limits, then inspect with --dry-run. Native execution requires a detected
profile and --acknowledge-model-execution. Calibration precedes fresh
confirmation of the declared effect.

**Usage**:

```console
$ ithrl compare [OPTIONS] {CASE_OR_RUN}
```

**Arguments**:

* `CASE_OR_RUN`: Case, or canonical saved run with --resume.  [required]

**Options**:

* `--config <path>`: Private TOML selecting both contexts, direction, samples and limits.
* `--experiment <path>`: Existing experiment with the fixed target, oracle and optional grader.
* `--profile <path>`: Private structural profile; output path with --detect-capabilities.
* `--original-workspace <path>`: Protected original workspace for native execution.
* `--max-provider-calls-per-attempt <int range>`: Hard native request ceiling per attempt; resume preserves its saved value.  [1&lt;=x&lt;=16]
* `--observation-store <path>`: Private SQLite store outside protected inputs.
* `--dry-run`: Inspect inputs and limits without starting a native harness or provider.
* `--detect-capabilities, --detect`: Probe local capabilities without provider requests; save a profile.
* `--resume`: Continue the same frozen comparison without redispatching completed work.
* `--calibration-only`: Stop after calibration; the plan must still reserve confirmation capacity.
* `--acknowledge-model-execution`: Approve the inspected native execution within its frozen limits.
* `--format <human|json>`: Output format.  [default: human]
* `--help`: Show this message and exit.

## `ithrl prepare`

Generate a native reduction configuration from a saved preflight receipt.

Combine --preflight with statistical choices in --settings and explicit
call/time limits. Writes a new private experiment TOML and prints its
dry-run command without starting a harness or making provider requests.

**Usage**:

```console
$ ithrl prepare [OPTIONS] {case}
```

**Arguments**:

* `case`: Extracted native case directory.  [required]

**Options**:

* `--preflight <path>`: Private native preflight receipt.  [required]
* `--settings <path>`: Explicit reduction preparation TOML.  [required]
* `--output <path>`: New private experiment TOML; never overwritten.  [required]
* `--max-candidates <int range>`: Maximum candidate tests across coarse and fine search.  [1&lt;=x&lt;=10000; required]
* `--max-provider-calls <int range>`: Maximum total provider calls for calibration, search and confirmation.  [x&gt;=1; required]
* `--max-wall-time <int range>`: Total elapsed-time limit in seconds, including calibration.  [x&gt;=1; required]
* `--inter-attempt-delay <int range>`: Seconds between attempts; included in admission bounds.  [default: 0; 0&lt;=x&lt;=86400]
* `--observation-store <path>`: Forward a store path without opening it.
* `--format <human|json>`: Output format.  [default: human]
* `--help`: Show this message and exit.

## `ithrl cost`

Estimate recorded or hypothetical token costs using reference rates.

Supply an observation database with --case-digest and --experiment-digest,
or use --scenario for hypothetical token counts. --pricing selects reference
prices or custom rates; estimates do not establish billed or subscription charges.
Prices are resolved offline, without provider requests.

**Usage**:

```console
$ ithrl cost [OPTIONS] [observations]
```

**Arguments**:

* `observations`: Existing observation database; omit with --scenario.

**Options**:

* `--pricing <path>`: Private JSON reference-pricing specification.  [required]
* `--output <path>`: New private cost report; existing files are not overwritten.  [required]
* `--case-digest <str>`: Exact case digest for recorded observations.
* `--experiment-digest <str>`: Exact experiment digest for recorded observations.
* `--scenario <path>`: Explicit hypothetical token counts as JSON.
* `--max-observations <int range>`: Maximum recorded observations; larger selections are refused, not truncated.  [default: 10000; 1&lt;=x&lt;=10000]
* `--format <human|json>`: [default: human]
* `--help`: Show this message and exit.

## `ithrl extract`

Create a case from a saved session or supported synthetic trace.

Omit SOURCE to browse saved sessions in a terminal. Without a terminal, supply
the IDs listed by the command. Extraction copies source evidence into a new
private case and makes no network requests.

**Usage**:

```console
$ ithrl extract [OPTIONS] [SOURCE]
```

**Arguments**:

* `SOURCE`: Saved native session or supported synthetic trace. Omit to discover sessions.

**Options**:

* `--example`: Use the bundled deterministic example; no harness or provider calls.
* `--inference <str>`: Synthetic trace inference ID.
* `--harness codex|copilot`: Native session harness.
* `--session <str>`: Native session ID.
* `--decision <str>`: Completed decision ID.
* `--target <str>`: Target ID or unique target type.
* `--text-contains <str>`: Literal text required in a native final response.
* `--text-contains-any <str>`: Alternative literal text for a native final response. Repeat for each alternative.
* `--command-contains <str>`: Literal text required in a native shell command.
* `-o, --output <path>`: Path for the new case. Existing paths are not overwritten.
* `--format <human|json>`: Output format.  [default: human]
* `--help`: Show this message and exit.

Saved session: ithrl extract path/to/session.jsonl
Synthetic trace: ithrl extract trace.json --inference inference-001

## `ithrl shrink`

Find a smaller context that preserves the selected behavior.

Use --dry-run to inspect the plan. Native cases require explicit sampling
settings, the original workspace and --acknowledge-model-execution. A native
dry-run can start local harness probes but makes no provider requests.

**Usage**:

```console
$ ithrl shrink [OPTIONS] {CASE_OR_RESULT}
```

**Arguments**:

* `CASE_OR_RESULT`: Case directory. With --resume, use a result or active run directory.  [required]

**Options**:

* `--max-candidates <int range>`: Maximum candidate tests across the active run and any resumed runs. Default: 100 for a new search; sampled resume preserves the saved limit. Deterministic resume defaults to 100 or twice the saved count, whichever is larger, capped at 10,000.  [1&lt;=x&lt;=10000]
* `--resume`: Resume a saved search.
* `--calibrate-only`: Test and save the original context without searching for a smaller one.
* `--dry-run`: Check the case and execution plan without running the experiment.
* `--acknowledge-model-execution`: Allow the selected native runner to send case context to its provider.
* `--original-workspace DIRECTORY`: Original workspace to protect during native execution.
* `--required-valid <int range>`: Number of valid baseline samples required for native calibration.  [1&lt;=x&lt;=16]
* `--minimum-matches <int range>`: Required matching baseline samples. Set with both other sample limits.  [1&lt;=x&lt;=16]
* `--max-attempts <int range>`: Total calibration attempt limit, including operational failures.  [1&lt;=x&lt;=16]
* `--max-provider-calls <int range>`: Maximum total provider calls allowed for the native run.  [x&gt;=1]
* `--max-provider-calls-per-attempt <int range>`: Hard provider-call limit per native attempt. Codex default: 16; Copilot requires 1. Resume preserves the saved limit. This budget does not guarantee turn completion.  [1&lt;=x&lt;=16]
* `--max-wall-time <int range>`: Total elapsed-time limit in seconds for a sampled run, including calibration.  [x&gt;=1]
* `--inter-attempt-delay SECONDS`: Pause between attempts during sampled reduction. Default: 0; resume preserves the saved delay. Counts toward the overall wall-time limit.  [0&lt;=x&lt;=86400]
* `--experiment TOML`: Private sampled experiment TOML, including search and fresh confirmation samples (0600 on POSIX).
* `--attempt-timeout <int range>`: Per-attempt wall-time deadline in seconds. Native default: 300.  [1&lt;=x&lt;=86400]
* `--max-input-tokens <int range>`: Hard per-attempt input-token cap; refused unless the runner can enforce it.  [x&gt;=1]
* `--format <human|json>`: Output format.  [default: human]
* `--verbose`: Show detailed sampled limits, assumptions and accounting.
* `--observation-store <path>`: SQLite observation store. Default: platform user data directory.
* `--save-preflight <path>`: Export a private native preparation receipt; requires --dry-run and a new case run.
* `--help`: Show this message and exit.

Example: ithrl shrink .ithrl/case
Resume:  ithrl shrink RESULT_DIRECTORY --resume

## `ithrl inspect`

Inspect saved sessions, cases and reports without running a harness.

**Usage**:

```console
$ ithrl inspect [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `costs`: Inspect a saved token-cost report.
* `comparison`: Inspect a saved context comparison.
* `tool`: List captured tool output fields or...
* `reacquire`: Compare recorded operations and results...
* `sections`: List editable prior-user fields and...
* `retained`: Explain retained context and removal...

### `ithrl inspect costs`

Inspect a saved token-cost report.

Validates the saved identities and arithmetic without resolving prices or
opening the source observation database.

**Usage**:

```console
$ ithrl inspect costs [OPTIONS] {report}
```

**Arguments**:

* `report`: Saved token-cost report.  [required]

**Options**:

* `--format <human|json>`: [default: human]
* `--help`: Show this message and exit.

### `ithrl inspect comparison`

Inspect a saved context comparison.

Reads the snapshot without a harness or observation database. Add --case to
verify both selections against their source; --show-text includes retained
tool output text and requires --case.

**Usage**:

```console
$ ithrl inspect comparison [OPTIONS] {result}
```

**Arguments**:

* `result`: Saved comparison evidence.  [required]

**Options**:

* `--case <path>`: Revalidate both context selections against this source case.
* `--show-text`: Include bounded retained tool output text; requires --case.
* `--max-text-bytes <int range>`: Total escaped text byte limit.  [default: 4096; 0&lt;=x&lt;=65536]
* `--format <human|json>`: [default: human]
* `--help`: Show this message and exit.

### `ithrl inspect tool`

List captured tool output fields or describe a text deletion.

Supply --field, --start and --end to create a source-bound edit descriptor;
the case remains unchanged. Offsets select a nonempty half-open range in
original Unicode codepoints; sizes are UTF-8 bytes. --show-text displays the
retained field when a range is selected.

**Usage**:

```console
$ ithrl inspect tool [OPTIONS] {case}
```

**Arguments**:

* `case`: Case containing captured tool output.  [required]

**Options**:

* `--field <str>`: Discovered source-bound tool output text field ID.
* `--start <int range>`: Inclusive original Unicode codepoint offset.  [x&gt;=0]
* `--end <int range>`: Exclusive original Unicode codepoint offset.  [x&gt;=1]
* `--show-text`: Include bounded escaped text; requires --field. Keep private.
* `--max-text-bytes <int range>`: Escaped text byte limit.  [default: 4096; 0&lt;=x&lt;=65536]
* `--format <human|json>`: Human-readable or JSON output.  [default: human]
* `--help`: Show this message and exit.

### `ithrl inspect reacquire`

Compare recorded operations and results across compaction.

Omit --boundary to list compaction IDs, then select one to compare adjacent
intervals. Reads saved JSONL without starting a harness or making model requests.

**Usage**:

```console
$ ithrl inspect reacquire [OPTIONS] {session}
```

**Arguments**:

* `session`: Explicit saved Codex or Copilot JSONL source.  [required]

**Options**:

* `--boundary <str>`: Boundary ID; omit to list compactions.
* `--max-groups <int range>`: Maximum reported groups or boundaries.  [default: 100; 1&lt;=x&lt;=1000]
* `--show-text`: Include bounded escaped detail; keep output private.
* `--max-text-bytes <int range>`: Total escaped detail byte limit.  [default: 4096; 1&lt;=x&lt;=65536]
* `--format <human|json>`: Human-readable or JSON output.  [default: human]
* `--help`: Show this message and exit.

### `ithrl inspect sections`

List editable prior-user fields and paragraph section IDs.

Reports source-bound selectors, half-open Unicode codepoint offsets and
UTF-8 byte sizes without displaying text or collecting observations.

**Usage**:

```console
$ ithrl inspect sections [OPTIONS] {case}
```

**Arguments**:

* `case`: Case containing the original evidence.  [required]

**Options**:

* `--format <human|json>`: Human-readable or JSON output.  [default: human]
* `--help`: Show this message and exit.

### `ithrl inspect retained`

Explain retained context and removal trials from a saved reduction.

Supply the original case and --result directory. Reads saved evidence without
a harness or observation database. Default output omits private text; use
--show-text for bounded retained content.

**Usage**:

```console
$ ithrl inspect retained [OPTIONS] {case}
```

**Arguments**:

* `case`: Case containing the original evidence.  [required]

**Options**:

* `--result <path>`: Saved deterministic or sampled reduction result directory.  [required]
* `--format <human|json>`: Human-readable or JSON output.  [default: human]
* `--show-text`: Include bounded retained content; keep output private.
* `--text-node <str>`: Display text only for this retained unit ID; requires --show-text.
* `--max-text-bytes <int range>`: Maximum total UTF-8 bytes of escaped retained content with --show-text.  [default: 4096; 1&lt;=x&lt;=65536]
* `--help`: Show this message and exit.
