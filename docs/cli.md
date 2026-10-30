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

* `fuzz`: Find and independently confirm a bounded...
* `extract`: Create a case from a saved agent session...
* `shrink`: Find a smaller context that preserves the...

## `ithrl fuzz`

Find and independently confirm a bounded decrease in the selected target rate.

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
* `--max-provider-calls-per-attempt <int range>`: Hard provider-call limit per native attempt. Codex default: 16; Copilot uses its detected target limit. Resume preserves the saved limit. This budget does not guarantee turn completion.  [1&lt;=x&lt;=16]
* `--observation-store <path>`: Private SQLite store outside protected state.
* `--dry-run`: Inspect saved inputs and limits without starting a harness or provider.
* `--detect`: Explicit credential-free structural harness detection; save a profile.
* `--resume`: Continue exactly the saved campaign, without changing its inputs or budget.
* `--acknowledge-model-execution`: Approve the inspected native execution within its frozen limits.
* `--format <human|json>`: Output format.  [default: human]
* `--help`: Show this message and exit.

## `ithrl extract`

Create a case from a saved agent session or supported synthetic trace.

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

Saved session: ithrl extract --harness codex --session SESSION --decision DECISION --target TARGET
Synthetic trace: ithrl extract trace.json --inference inference-001

## `ithrl shrink`

Find a smaller context that preserves the selected behavior.

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
* `--max-provider-calls-per-attempt <int range>`: Hard provider-call limit per native attempt. Codex default: 16; Copilot uses its detected target limit. Resume preserves the saved limit. This budget does not guarantee turn completion.  [1&lt;=x&lt;=16]
* `--max-wall-time <int range>`: Total elapsed-time limit in seconds for a sampled run, including calibration.  [x&gt;=1]
* `--inter-attempt-delay SECONDS`: Pause between attempts during sampled reduction. Default: 0; resume preserves the saved delay. Counts toward the overall wall-time limit.  [0&lt;=x&lt;=86400]
* `--experiment TOML`: Private sampled experiment TOML, including search and fresh confirmation samples (0600 on POSIX).
* `--attempt-timeout <int range>`: Per-attempt wall-time deadline in seconds. Native default: 300.  [1&lt;=x&lt;=86400]
* `--max-input-tokens <int range>`: Hard per-attempt input-token cap; refused unless the runner can enforce it.  [x&gt;=1]
* `--format <human|json>`: Output format.  [default: human]
* `--verbose`: Show detailed sampled limits, assumptions and accounting.
* `--observation-store <path>`: SQLite observation store. Default: platform user data directory.
* `--help`: Show this message and exit.

Example: ithrl shrink .ithrl/case
Resume:  ithrl shrink RESULT_DIRECTORY --resume
