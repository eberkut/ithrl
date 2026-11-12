# Troubleshooting

Record `ithrl --version`, the error code, `next_action` and any private evidence location. Check
the failing command's `--help`; `--format json` returns structured output with progress on stderr.
Review output, including local paths, before sharing it. Keep raw sessions and diagnostics private.

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

Check the [native-run prerequisites](native-runs.md#native-prerequisites) before authorizing
execution. Native dry-run can start bounded, network-denied local probes without provider calls.

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

Do not bypass a refusal by disabling permission checks or editing internal state. New harness
versions use the same capability checks; unsupported protocol or safety behavior needs
investigation.

`runner_error_reason_code = "protected-input-snapshot"` means the runner could not snapshot the
source or original workspace before starting that attempt. Check read permissions, unsupported
file types or links, and tree size or nesting limits.

### Authentication and certificate trust

For Codex custom CA trust, set `SSL_CERT_FILE` to a certificate bundle. The runner copies it into
private disposable state with verification enabled. A custom `SSL_CERT_DIR` hidden by isolation
is unavailable inside the runner.

Copilot chooses token environment variables before saved credentials, in this order:
`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`. To use saved credentials, prefix each detection,
execution or resume command with
`env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN`. This changes only that command's
environment. Authenticate through Copilot; another CLI's token may lack Copilot access.

Copilot preserves supported proxy settings and copies bounded `SSL_CERT_FILE` and
`NODE_EXTRA_CA_CERTS` bundles into private disposable state. For certificate refusals, check file
permissions and trust configuration; keep verification enabled. SDK HTTP forwarding uses ithrl's
Python runtime, whose certificate trust is not established by a successful CLI login.

### Copilot model catalogue

| Reason code | Next action |
| --- | --- |
| `model-catalog-connection` | Check network and proxy connectivity after connection recovery is exhausted. |
| `model-catalog-tls` | Check certificate trust. |
| `model-catalog-failed` | Check account access and the private diagnostic for another catalogue refusal. |

Read-only catalogue recovery allows three connection attempts, delayed by 0.5 then 1 second,
within the attempt timeout. It preserves SDK proxy/certificate settings and never retries
inference. HTTP status errors and streaming-body failures are outside that loop.

Recognized pre-turn startup warnings are metadata, not proof of authentication or provider
acceptance. If execution fails, inspect the refusal and private diagnostic.

## Preparing a reduction

Follow the [preparation procedure](reduction.md) to export a receipt,
supply settings and generate an experiment. `prepare` is offline; its returned inspection command
runs native dry-run, which can start bounded local probes.

| Error code | Next action |
| --- | --- |
| `invalid-preflight-receipt` | Supply the private file exported by `shrink --dry-run --save-preflight`, rather than its normal JSON summary. |
| `preflight-evidence-mismatch` / `preflight-target-mismatch` / `preflight-oracle-mismatch` | Use the unchanged case and behavior specification bound to the receipt. Changed inputs need a new explicit preflight receipt. |
| `preflight-limits-mismatch` | Fixed native limits differ from those recorded. Export a new receipt with the current installation. |
| `invalid-preparation-settings` | Check the preparation TOML's schema, required statistical choices and section selectors. Do not supply target/oracle blocks, runner bindings or section hashes. |
| `section-selection-mismatch` | Select an eligible field from `inspect sections CASE`; keep section IDs in source order. |
| `unsupported-section-search` | Use section removal only for a supported final-response profile. Shell-action reduction accepts whole-record removal. |
| `preparation-contract-mismatch` | Check the selected profile, behavior specification, protected IDs and section choices against the case. Keep the source evidence unchanged. |
| `preparation-invalid` / `preflight-output-invalid` | Check private path permissions and input bounds. Choose a new output outside the case and original workspace; existing files are never overwritten. |

Preparation also checks sampling budgets and freshness. Success validates recorded facts; it
neither replaces current native preflight nor authorizes model execution.

## Sampling and budget refusals

| Error code | Next action |
| --- | --- |
| `calibration-settings-required` / `sampling-options-required` | Supply all three calibration settings: `--required-valid`, `--minimum-matches` and `--max-attempts`. |
| `sampled-reduction-plan-required` | Supply a complete private experiment TOML with search and fresh-confirmation samples. |
| `sampling-budget-required` / `sampling-budget-too-small` | Inspect the minimum call/time requirements. Budget calibration, a complete comparison, protected fresh confirmation and local overhead together. |
| `unsupported-provider-call-limit` | Copilot requires a per-attempt provider-call cap of 1. Use that cap when creating the plan. |
| `profile-provider-call-limit-mismatch` | Use the cap frozen in the saved profile. A different cap requires a new profile and run. |
| `sampling-freshness-too-small` | Choose deadlines and a declared freshness window that admit a complete comparison, including cancellation time. |
| `confirmation-unattainable` | Reconsider sample counts, confidence and the scientifically meaningful margin together. Even all matching observations cannot establish preservation under this plan. |
| `acknowledgement-required` / `acknowledgement-declined` | Inspect the complete dry-run plan. Add `--acknowledge-model-execution` only when ready to authorize those model requests. |
| `sampled-resume-mismatch` | Use the original case, workspace, observation store and frozen statistical plan. Only supported administrative limits may increase. |

## Fuzz setup and resume

The [fuzzing walkthrough](fuzzing.md) covers the fake-runner example and native
setup. Detection makes no provider requests; inspecting a saved profile with `--dry-run` starts no
harness.

| Error code | Next action |
| --- | --- |
| `fuzz-configuration-required` | Supply the private TOML with `--config`; see the walkthrough for a complete example. |
| `detection-required` | Create a private structural profile with `--detect` and the original workspace, then review it with `--profile ... --dry-run`. Detection uses bounded local harness probes without provider requests. |
| `unsupported-context-operation` | The detected runner does not implement the requested mutation. Check the [supported inputs](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs) before choosing a profile; structural detection alone does not qualify a mutation workflow. |
| `profile-input-mismatch` | Use the case, configuration and original workspace bound to the saved profile. Changed inputs require a new profile. |
| `model-execution-not-acknowledged` | Inspect the complete plan and set an explicit provider-call budget before authorizing model requests with `--acknowledge-model-execution`. Acknowledgement cannot enable an unsupported operation. |
| `resume-input-conflict` | Use `ithrl fuzz RUN_OR_RESULT --resume` without overriding configuration, profile, workspace or observation store. Resume preserves the original limits. |

Use `--dry-run --resume` to inspect a saved fuzz campaign without starting a harness. See
[fuzz results](results.md#fuzz-results) for terminal outcomes, saved evidence and publication
recovery.

## Provider errors and interruption

`runner_error_category = "provider-call-limit"` means the local per-attempt cap blocked another
request. It is a budget stop, not throttling, transport failure or a behavioral nonmatch; no retry
or continuation is offered. Inspect retained usage before starting a new run with a different cap.
Codex can need several successful requests per turn even with retries disabled. See
[plan limits](reduction.md#3-generate-and-inspect-the-complete-plan-and-budget).

For authentication or quota failures, inspect the selected harness's account status privately.
Provider-call limits do not predict subscription allowance. Repeated full-run attempts can spend
calls without completing confirmation; preserve every failed run and its charge.

Sampled reduction and fuzz reports expose `runner_error_category` and `runner_error_reason_code`
without copying native messages, bodies or request identifiers. Structured Copilot failures use:

| Reason code | Next action |
| --- | --- |
| `provider-quota-exceeded` | Check the account's remaining quota and reset date. Continue only after capacity is available and the saved run remains eligible for resume. |
| `provider-authentication-required` | Authenticate through Copilot and check the credential selection described above. |
| `provider-payment-required` | Check billing and plan entitlement before making another request. |
| `provider-access-denied` | Check account, organization and model access through Copilot. |
| `provider-rate-limited` | Wait for the provider's rate limit to clear; inspect the saved run before continuing. |
| `provider-unavailable` | Check provider service status and retain the failed observation. |
| `provider-failure` | Inspect the private native diagnostic for an unclassified failure; share only sanitized evidence. |

These codes classify evidence; they neither retry attempts, establish provider acceptance nor
change charges. A quota refusal alone does not establish a harness compatibility failure.

Errors stay outside behavioral denominators. `started-unknown` means execution started without a
complete provider-call count. Inspect actual, additional possible, pending and charged counts
separately; unknown usage is not zero. See [sampled results](results.md#sampled-results) for
interpretation and the opt-in structured-overload replacement policy.

When interrupted, use the printed continuation command if one is supplied. An unresolved dispatch
must be reconciled before execution can continue. Completed confirmation is never repeated.
When no continuation is available, follow `next_action` and retain the private evidence rather
than modifying JSON/SQLite files or replaying the same attempt.

If the problem remains, use the
[support routes](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md). Include versions, platform,
safe reproduction steps, sanitized error codes and expected/actual outcomes. Keep source sessions,
raw diagnostics, credentials and case contents private.
