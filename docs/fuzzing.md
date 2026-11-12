# Context fuzzing

With the `fuzz` extra (`ithrl[fuzz]`), `ithrl fuzz` explores bounded text appends and confirms at
most one decrease in the target's match rate. Codex and Copilot preserve the triggering input and
append only to eligible prior user text. Copilot restores the session prefix through its installed
CLI and SDK; its shell-action profiles support removal only. See
[measurements](measurements.md#native-context-fuzzing) for completed synthetic campaigns and limits.

## Try locally

To exercise the complete workflow without a harness or provider, create a fresh example case:

```bash
mkdir -m 700 ithrl-fuzz-demo
ithrl extract --example --output ithrl-fuzz-demo/case
```

Save the following as `ithrl-fuzz-demo/fuzz.toml`, with mode `0600`:

```toml
seed = 1
mutators = ["append-stale-text"]
stale_text = "An irrelevant earlier instruction."
max_candidates = 4
max_simplification_trials = 4
minimum_decrease = 0.1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400

[calibration]
required_valid = 2
minimum_matches = 2
max_attempts = 2

[screening]
minimum_decrease = 0.4

[screening.original]
required_valid = 2
max_attempts = 2

[screening.mutant]
required_valid = 2
max_attempts = 2

[confirmation]
alpha = 0.05

[confirmation.original]
required_valid = 20
max_attempts = 20

[confirmation.mutant]
required_valid = 20
max_attempts = 20

[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
inter_attempt_delay_seconds = 0
```

These small fixed counts are for the deterministic fake-runner example. The alternative mutator,
`duplicate-text`, appends a copy of an existing editable text item.
Both mutators preserve protected records and keep payloads bounded. `stale_text` is private input
and is never printed in normal output.

Inspect without execution, then run with a dedicated store:

```bash
ithrl fuzz ithrl-fuzz-demo/case --config ithrl-fuzz-demo/fuzz.toml \
  --observation-store ithrl-fuzz-demo/fuzz-observations.sqlite --dry-run
ithrl fuzz ithrl-fuzz-demo/case --config ithrl-fuzz-demo/fuzz.toml \
  --observation-store ithrl-fuzz-demo/fuzz-observations.sqlite
```

The example finishes with `confirmed-change`, zero provider calls and zero executed actions. It uses
fixed original/mutant samples and fresh confirmation. See [fuzz results](results.md#fuzz-results)
for saved evidence and resume behavior. Use `--format json` for one JSON report on stdout; execution
progress and the inspected plan go to stderr.

## Run the synthetic native campaign

Use a
[source checkout](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md#development-setup) with
the locked environment, `fuzz` extra and
[native prerequisites](native-runs.md#native-prerequisites). The inputs below are synthetic
response-token exercises. Choose one harness for extraction; subsequent steps apply to either.
Execution makes real provider requests and can produce different observations.

| Harness | Public source | Decision | Target |
| --- | --- | --- | --- |
| Codex | `examples/fuzz/codex.jsonl` | `decision-f5eaba307afd19db` | `final-response` |
| Copilot | `examples/fuzz/copilot.jsonl` | `decision-cf9cce503bc9fc40` | `final-response` |

From the checkout root, create private state outside the workspace and copy the public
configuration:

```bash
FUZZ_RUN=$(mktemp -d /tmp/ithrl-fuzz.XXXXXXXX)
mkdir -m 700 "$FUZZ_RUN/original"
install -m 600 examples/fuzz/campaign.toml "$FUZZ_RUN/campaign.toml"
```

The configuration uses 3 calibration samples, 8 per exploratory arm and 20 fresh per confirmation
arm. It requires a 40-percentage-point screening decrease and a final decrease greater than
10 percentage points at `alpha=.05`. It permits one candidate, ten simplification trials and
350 calls, with a six-hour deadline, 120-second attempt timeout, 24-hour freshness and opt-in
`safe-provider-overload` eligibility. These settings measured a large planted effect; 20 samples
have weak power for moderate effects. Read the
[effect-size limits](research/context-fuzzing.md#alternatives-and-effect-size-limits) before reuse.

Before detection, adjust the private configuration for the selected harness:

- **Codex:** set `max_provider_calls = 5600` and keep `inter_attempt_delay_seconds = 15`. At its
  default 16-request cap, confirmation reserves 1,152 calls and one exploratory block needs 576;
  350 calls cannot admit both. The higher limit retains comparable headroom under the conservative
  attempt bound. For a different cap, pass `--max-provider-calls-per-attempt N` during detection
  and recalculate the total allowance. Changing that saved cap requires a new profile and run.
- **Copilot:** set `max_provider_calls = 339` and `inter_attempt_delay_seconds = 0`. Its inference
  cap is one request per attempt. These match the measured native-prefix campaign, which shared
  a 350-call qualification allowance with its control and interrupted-run calls.

Pacing consumes wall time without changing sample targets. Call limits can stop exploration while
preserving the 72-attempt fresh-confirmation reserve; neither call nor six-hour limits predict
actual usage or guarantee completion. Throttling and ordinary-session costs vary by workload;
[measurements](measurements.md#calls-duration-and-resource-use) record completed durations.

Extract one case. For Codex:

```bash
uv run ithrl extract examples/fuzz/codex.jsonl --harness codex \
  --decision decision-f5eaba307afd19db --target final-response \
  --output "$FUZZ_RUN/case" --format json
```

For Copilot:

```bash
uv run ithrl extract examples/fuzz/copilot.jsonl --harness copilot \
  --decision decision-cf9cce503bc9fc40 --target final-response \
  --output "$FUZZ_RUN/case" --format json
```

The case retains captured routing and model evidence; there is no model override.

1. Detect structural capabilities without provider requests:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --detect --profile "$FUZZ_RUN/profile.json" \
      --original-workspace "$FUZZ_RUN/original" --format json
    ```

    Detection uses bounded, network-denied local harness probes. Inspect the detected append-text
    support, reconstruction deviations and effective safety policy.

2. Review the complete plan without starting a harness:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --profile "$FUZZ_RUN/profile.json" --original-workspace "$FUZZ_RUN/original" \
      --observation-store "$FUZZ_RUN/observations.sqlite" --dry-run --format json
    ```

    Check the provider, model, destination, isolation, action-denial policy and complete budget.
    The profile binds the case, configuration and original workspace. Changed inputs need a new
    profile. The native profile cannot enforce a hard input-token cap.

3. Authorize execution of the reviewed plan:

    ```bash
    uv run ithrl fuzz "$FUZZ_RUN/case" --config "$FUZZ_RUN/campaign.toml" \
      --profile "$FUZZ_RUN/profile.json" --original-workspace "$FUZZ_RUN/original" \
      --observation-store "$FUZZ_RUN/observations.sqlite" \
      --acknowledge-model-execution --format json
    ```

The same procedure accepts an eligible final-response case extracted from your own saved session.
Pass that session's original workspace instead of the empty example workspace, and keep its case and
configuration private. Fuzz `--dry-run` starts no harness or provider; a native case without a
profile reports `detection-required`. Resume from the printed run/result path as described in
[fuzz results](results.md#fuzz-results). See the [CLI reference](cli.md#ithrl-fuzz) for options and
[troubleshooting](troubleshooting.md#fuzz-setup-and-resume) for refusals.
