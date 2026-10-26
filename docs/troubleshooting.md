# Troubleshooting

Start with `ithrl --version` and the command's `--help`. For a refusal, keep the error code,
`next_action` and any printed private evidence location. `--format json` returns structured
output; progress is written to stderr. Review output before sharing it, including local paths.

## Selecting a decision

| Problem | Next action |
| --- | --- |
| `selection-required` | In a terminal, run `ithrl extract` to choose a session and decision. For automation, supply the IDs listed by the command. |
| `invalid-selection` | Choose an ID from the current selection output; IDs belong to the selected session. |
| `conflicting-options` with `--example` | Use `--example` without a source, harness, session, inference, decision or target selector. |
| `case-exists` | Choose a new output directory. Extraction never overwrites a case. |

Extraction accepts the record structures described in
[compatibility](native-runs.md#compatibility-and-measurements). If a new harness record is
unsupported, keep the source unchanged. Report the harness/version and sanitized refusal through the
[compatibility form](https://github.com/eberkut/ithrl/issues/new?template=compatibility.yml);
do not attach the session.

## Native setup and preflight

Native reduction needs Linux, usable Bubblewrap, an installed harness with existing local
authentication, and the original workspace. Follow the [native-run procedure](native-runs.md)
before acknowledging model execution. Native dry-run can start local, network-denied probes.

| Error code | Next action |
| --- | --- |
| `runtime-unavailable` | Check that the harness executable and required isolation runtime are installed and usable. Keep isolation enabled. |
| `original-workspace-required` / `original-workspace-unavailable` | Supply the existing original workspace through `--original-workspace`. It must be distinct from disposable runner state. |
| `codex-authentication-unavailable` / `copilot-authentication-unavailable` | Authenticate through the selected harness. Keep credentials out of cases, arguments and reports. |
| `codex-probe-failed` / `copilot-probe-failed` | Inspect `probe_stage`, `probe_exit_code`, `probe_reason` and the printed private diagnostic. Share only reviewed, sanitized details. |
| `capability-profile-mismatch` | A runtime or configuration change can change the capability digest. Preflight a new experiment against the current installation; preserve the old run and its pinned identity. |
| `runner-evidence-mismatch` | Use a runner that matches the harness recorded in the case. |
| `observation-store-unavailable` | Choose a writable store inside a private user-owned directory. |
| `storage-overlap` | Move disposable storage outside the protected case and original workspace. |
| `input-token-limit-unenforceable` | The current native profiles cannot enforce a hard input-token cap. Use a profile with that guarantee, or explicitly decide whether to proceed without the cap. |

Do not disable permission checks or edit internal state to bypass a refusal. A new harness version
can pass the same capability checks without a product update; unsupported protocol or safety
behavior requires investigation.

## Sampling and budget refusals

| Error code | Next action |
| --- | --- |
| `calibration-settings-required` / `sampling-options-required` | Supply all three calibration settings: `--required-valid`, `--minimum-matches` and `--max-attempts`. |
| `sampled-reduction-plan-required` | Supply a complete private experiment TOML with search and fresh-confirmation samples. |
| `sampling-budget-required` / `sampling-budget-too-small` | Inspect the minimum call/time requirements. Budget calibration, a complete comparison, protected fresh confirmation and local overhead together. |
| `sampling-freshness-too-small` | Choose deadlines and a declared freshness window that admit a complete comparison, including cancellation time. |
| `confirmation-unattainable` | Reconsider sample counts, confidence and the scientifically meaningful margin together. Even all matching observations cannot establish preservation under this plan. |
| `acknowledgement-required` / `acknowledgement-declined` | Inspect the complete dry-run plan. Add `--acknowledge-model-execution` only when ready to authorize those model requests. |
| `sampled-resume-mismatch` | Use the original case, workspace, observation store and frozen statistical plan. Only supported administrative limits may increase. |

## Fuzz setup and resume

The [fuzzing walkthrough](native-runs.md#context-fuzzing) uses a development checkout and the fake
runner. Current native profiles do not qualify text mutation.

| Error code | Next action |
| --- | --- |
| `fuzz-configuration-required` | Supply the private TOML with `--config`; see the walkthrough for a complete example. |
| `detection-required` | Native planning needs a capability profile from explicit `--detect`. A fuzz dry-run never starts a harness; detection can refuse an unsupported mutation profile. |
| `profile-input-mismatch` | Use the case, configuration and original workspace bound to the saved profile. Changed inputs require a new profile. |
| `model-execution-not-acknowledged` | Inspect the plan before authorizing model requests with `--acknowledge-model-execution`. Acknowledgement cannot enable an unsupported operation. |
| `resume-input-conflict` | Use `ithrl fuzz RUN_OR_RESULT --resume` without overriding configuration, profile, workspace or observation store. Resume preserves the original limits. |

Use `--dry-run --resume` to inspect a saved fuzz campaign without starting a harness. See
[fuzz results](results.md#fuzz-results) for terminal outcomes, saved evidence and publication
recovery.

## Provider errors and interruption

For authentication or quota failures, inspect the selected harness's account status privately.
Provider-call limits do not predict subscription allowance. Repeated full-run attempts can spend
calls without completing confirmation; preserve every failed run and its charge.

Errors remain outside behavioral denominators. `started-unknown` accounting means execution
started but the available evidence cannot establish the complete provider-call count. Inspect
actual, additional possible, pending and charged counts separately; unknown usage is not zero.
The [result guide](results.md#sampled-results) explains the conditional interpretation and the
opt-in structured-overload replacement policy.

When interrupted, use the printed continuation command if one is supplied. An unresolved dispatch
must be reconciled before execution can continue. Completed confirmation is never repeated.
When no continuation is available, follow `next_action` and retain the private evidence rather
than modifying JSON/SQLite files or replaying the same attempt.

If the problem remains, use the
[support routes](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md). Include versions, platform,
safe reproduction steps, sanitized error codes and expected/actual outcomes. Keep source sessions,
raw diagnostics, credentials and case contents private.
