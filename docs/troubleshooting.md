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

For custom Codex CA trust, configure `SSL_CERT_FILE` with a certificate bundle. The runner copies
it into private disposable state and preserves certificate verification. A custom `SSL_CERT_DIR`
under a path hidden by isolation is unavailable inside the runner.

## Sampling and budget refusals

| Error code | Next action |
| --- | --- |
| `calibration-settings-required` / `sampling-options-required` | Supply all three calibration settings: `--required-valid`, `--minimum-matches` and `--max-attempts`. |
| `sampled-reduction-plan-required` | Supply a complete private experiment TOML with search and fresh-confirmation samples. |
| `sampling-budget-required` / `sampling-budget-too-small` | Inspect the minimum call/time requirements. Budget calibration, a complete comparison, protected fresh confirmation and local overhead together. |
| `unsupported-provider-call-limit` | Use the per-attempt bound reported by detection for the selected harness and target. |
| `profile-provider-call-limit-mismatch` | Use the cap frozen in the saved profile. A different cap requires a new profile and run. |
| `sampling-freshness-too-small` | Choose deadlines and a declared freshness window that admit a complete comparison, including cancellation time. |
| `confirmation-unattainable` | Reconsider sample counts, confidence and the scientifically meaningful margin together. Even all matching observations cannot establish preservation under this plan. |
| `acknowledgement-required` / `acknowledgement-declined` | Inspect the complete dry-run plan. Add `--acknowledge-model-execution` only when ready to authorize those model requests. |
| `sampled-resume-mismatch` | Use the original case, workspace, observation store and frozen statistical plan. Only supported administrative limits may increase. |

## Fuzz setup and resume

The [fuzzing walkthrough](native-runs.md#context-fuzzing) covers the fake-runner example and native
setup. Detection makes no provider requests; inspecting a saved profile with `--dry-run` starts no
harness.

| Error code | Next action |
| --- | --- |
| `fuzz-configuration-required` | Supply the private TOML with `--config`; see the walkthrough for a complete example. |
| `detection-required` | Create a private structural profile with `--detect` and the original workspace, then review it with `--profile ... --dry-run`. Detection uses bounded local harness probes without provider requests. |
| `unsupported-context-operation` | The detected runner does not support the requested mutation. Codex supports append-text for eligible prior user input; Copilot does not. |
| `profile-input-mismatch` | Use the case, configuration and original workspace bound to the saved profile. Changed inputs require a new profile. |
| `model-execution-not-acknowledged` | Inspect the complete plan and set an explicit provider-call budget before authorizing model requests with `--acknowledge-model-execution`. Acknowledgement cannot enable an unsupported operation. |
| `resume-input-conflict` | Use `ithrl fuzz RUN_OR_RESULT --resume` without overriding configuration, profile, workspace or observation store. Resume preserves the original limits. |

Use `--dry-run --resume` to inspect a saved fuzz campaign without starting a harness. See
[fuzz results](results.md#fuzz-results) for terminal outcomes, saved evidence and publication
recovery.

## Provider errors and interruption

`runner_error_category = "provider-call-limit"` means the local per-attempt cap stopped another
request. The failed attempt retains its usage and conservative charge and remains outside
behavioral match rates. Inspect the saved evidence before starting a new run with a different
cap. Codex can make several successful Responses requests within one turn despite disabled
retries. See [plan limits](native-runs.md#3-inspect-the-complete-plan-and-budget) for configuration.

Sampled reduction and fuzz reports include `runner_error_category` and
`runner_error_reason_code` when available. These contain machine codes; native error messages and
response bodies stay in private evidence. Structural fuzz detection refusals also report the probe
stage, exit code and private diagnostic path when available.

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
