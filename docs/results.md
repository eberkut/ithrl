# Results and privacy

Start with `summary.json` in the printed result directory for the outcome, evidence identifiers,
counts and next action.

## Saved files

| File | Contents |
| --- | --- |
| `summary.json` | Outcome, phase results, context counts, usage and continuation |
| `retained-context.jsonl` | Retained context records, in order |
| `request.json` | Original or reduced request when exact-request materialization is available |
| `checkpoint.json` | Deterministic search checkpoint |
| `result.json` | Sampled candidate selection, search checkpoint and confirmation evidence |
| `sampling/sampling-state.json` | Sampled run state and observation references |

Other files record the experiment, execution plan and provenance. Native reconstruction may have
no `request.json`; use the retained-context records and recorded reconstruction method. Leave
internal JSON and SQLite state unchanged so validation and resume can verify it.

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
confidence. Keep the search result and final preservation result separate when reporting a run.

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
shows mutation type, byte count and digest without printing payload text. Keep the entire result
and database private.

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
Actual, possible, pending and charged provider calls are separate counts. Copilot token usage is
unavailable, and denied shell actions can leave possible continuation calls; unknown usage stays
explicit.

Actual calls record known starts; additional possible calls retain usage that the runner cannot
exclude. Charged calls retain the full per-attempt allowance whenever provider work started,
including completed turns, and include pending dispatch reservations. Codex's relay records
physical requests separately, so actual calls can be lower than the charged count. Reopening a
run reconciles saved
observations before deciding whether another complete sampling block fits; it never repeats an
unresolved dispatch.

When a search stops at a budget, follow its printed continuation command. Deterministic searches
can resume from a saved result or active run. Sampled resume preserves the experiment and completed
work. For sampled `shrink`, supported administrative limits may increase; fuzz resume keeps its
original limits. Neither repeats completed confirmation or automatically replays an unresolved
dispatch.

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
