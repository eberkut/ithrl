# ithrl

`ithrl` is a differential debugging tool for agent context. It extracts one model decision from a
saved agent session and records the preceding context and the dependencies between its parts. With
a compatible runner, it searches for a smaller context that preserves the selected behavior.

Pronounce `ithrl` as **Ithuriel**. The name refers to the angel in John Milton's
[*Paradise Lost*](https://rpo.library.utoronto.ca/content/paradise-lost-book-iv-1674) whose spear
reveals Satan in disguise.

A `search-1-minimal` result means that, under the recorded runner, behavior test, search settings,
and budget, ithrl tested every valid single-group removal from the final context and none passed the
preservation test. It does not mean that the result is the smallest possible context, the only
context that would work, or the cause of the behavior.

## Available workflows

| Input | `extract` | `shrink` |
| --- | --- | --- |
| Included synthetic trace format | Yes | Yes, with the deterministic local runner |
| Codex CLI rollout JSONL | Yes | Experimental final-response reduction; see below |
| Copilot CLI event schema `1` | Yes | Experimental final-response and denied shell-action reduction; see below |

`extract` can discover the default Codex and Copilot session stores or read an explicit session
file. It does not start the coding agent, contact a provider, execute a tool, or change the saved
session. Experimental native reduction requires an explicit sampled experiment, local capability
checks and automatic original-context calibration. Its tested scope, configuration and execution
boundaries are described below. The opt-in smoke tests exercise bounded full-context calibration
separately from reduction.

The synthetic workflow covers extraction and reduction without provider credentials or network
access. Its deterministic runner does not execute tool calls.

## Set up a checkout

Prerequisites:

- [uv](https://docs.astral.sh/uv/)
- Python 3.12, 3.13, or 3.14; Python 3.14 is the primary development version

From the repository root, synchronize the locked development environment:

```bash
uv sync --locked --all-groups
```

Run ithrl from the checkout with `uv run`:

```bash
uv run ithrl --help
```

## Run the synthetic workflow

The included synthetic trace is deterministic and does not call a model. Extract its selected
decision into a local case, then reduce it:

```bash
uv run ithrl extract tests/fixtures/synthetic-trace.json \
  --inference synthetic-ac-001 \
  --output .ithrl/demo-case
uv run ithrl shrink .ithrl/demo-case
```

The expected result retains the synthetic markers `A` and `C`, reports zero provider calls, and
saves an inspectable result under `.ithrl/demo-case/results/`. Extraction never overwrites an
existing case, so remove `.ithrl/demo-case` before repeating the walkthrough.

Useful options:

```bash
# Validate the case and show the execution plan without running it.
uv run ithrl shrink .ithrl/demo-case --dry-run

# Test only the original context.
uv run ithrl shrink .ithrl/demo-case --calibrate-only

# Return the command result as one JSON object on stdout.
uv run ithrl shrink .ithrl/demo-case --format json
```

Every completed run prints its result directory. Start with `summary.json` for the outcome,
evidence identifiers, counts, and `next_action` value. `request.json` contains the original request
after a calibration-only run or the reduced request after a search.
`retained-context.jsonl` lists the retained context entries. `checkpoint.json` contains the search
state needed to verify or resume the reduction. Other result files record the experiment settings,
execution plan, and capture source.

Print the installed version when reporting a problem:

```bash
uv run ithrl --version
```

The synthetic trace defines one behavior for each recorded decision. The synthetic workflow does
not offer interactive behavior selection.

When `shrink` reaches the candidate limit, it saves a partial result and prints a command to resume
the search. Use `--max-candidates` to set the limit; `uv run ithrl shrink --help` shows the accepted
range.

## Extract a saved Codex or Copilot session

Run `extract` without a source in a terminal to discover supported sessions and choose a
completed decision and behavior:

```bash
uv run ithrl extract
```

You can also pass a native session file directly:

```bash
uv run ithrl extract path/to/session.jsonl --output .ithrl/native-case
```

For non-interactive use, pass the IDs reported by `extract`:

```bash
uv run ithrl extract \
  --harness codex \
  --session SESSION_ID \
  --decision DECISION_ID \
  --target TARGET_ID \
  --output .ithrl/native-case \
  --format json
```

When a required selection is missing, the command lists available IDs. Selection and discovery
output exclude prompts, responses, commands, tool arguments, and source paths.

The Codex parser recognizes rollout JSONL by its first `session_meta` record, then validates record
structure and recognized tool relationships. The
[accepted Codex structure](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/protocol.rs)
has no schema-version field. ithrl records the CLI version in the case but checks the file structure
instead. The Copilot parser accepts event schema `1` and rejects other schema versions. Both parsers
preserve unrecognized records without interpreting them.

Codex extraction supports completed final responses. Copilot extraction supports top-level final
responses and recognized `bash` or `shell` requests.

A native case contains the exact saved-session bytes, separate decision metadata, and an index of
context records and their dependencies.

## Experimental native sampled reduction

The validated profiles are Codex `0.162.1` final response and Copilot `1.0.95` final response or
visible-but-denied native `bash` action, using recorded model `gpt-6.1-sol`. Qualification used
harmless four-history-group cases of approximately 10k locally estimated context tokens. Each
profile removed 75% of editable history bytes and completed fresh confirmation with 100 matching
observations per arm. These are bounded workload results; arbitrary-session success rates,
100k contexts, token savings and monetary cost remain unqualified.

The workflow screens candidates against fresh original samples, freezes one selected candidate,
then compares it with the immutable original using separate fresh observations. A confirmed result
estimates behavior preservation among valid observations. Errors stay outside behavioral
denominators and execution reliability is reported separately. Inference assumes independent
valid observations and stationarity within each comparison. Search-sweep evidence and final
preservation are separate claims; sampled confirmation does not establish global or confirmed
minimality, causality, or campaign-wide confidence.

Prerequisites are a supported installed harness, Linux/Bubblewrap isolation prerequisites for the
current profiles, existing local harness authentication, an extracted case and the original
workspace to protect. Model/provider selection comes from captured evidence and the supported
local harness configuration. No `--model` override is provided. Keep experiment files, observation
stores and results private; use a dedicated private directory (mode `0700` on POSIX) outside
the case and original workspace for the observation store.

1. Obtain approval for local structural discovery, then inspect the current profile without a
   provider call. Native `--dry-run` can start bounded, network-denied local harness probes;
   it does not start a model turn. Use the returned `capability_digest` in the experiment below.

   ```bash
   uv run ithrl shrink CASE --original-workspace ORIGINAL \
     --required-valid 3 --minimum-matches 2 --max-attempts 7 \
     --attempt-timeout 120 --dry-run --format json
   ```

2. Create a separate private TOML experiment (mode `0600` on POSIX). Copy `[target]` and `[oracle]` from
   `CASE/experiments/default.toml`, preserving the behavior predicates and protected IDs. Combine
   those tables with the following Codex configuration; replace `APPROVED_DIGEST` with the digest
   from discovery. This is user configuration; leave the extracted case and internal JSON/SQLite
   state unchanged.

   ```toml
   schema_version = 1
   kind = "sampled"

   [runner]
   type = "codex"
   required_capability_digest = "APPROVED_DIGEST"

   [calibration]
   required_valid = 3
   minimum_matches = 2
   max_attempts = 7

   [reduction]
   margin = 0.20
   freshness_seconds = 86400
   baseline_policy = "strict-fresh-original"
   eligibility_policy = "safe-provider-overload"

   [reduction.search.original]
   required_valid = 4
   max_attempts = 8

   [reduction.search.candidate]
   required_valid = 5
   max_attempts = 9

   [reduction.confirmation]
   one_sided_confidence = 0.95

   [reduction.confirmation.original]
   required_valid = 100
   max_attempts = 124

   [reduction.confirmation.candidate]
   required_valid = 100
   max_attempts = 124
   ```

   For Copilot, use `runner.type = "copilot"`, its target-specific capability digest, and
   `eligibility_policy = "independent-safe-preaccept-only"`. Keep the extracted target unchanged.
   The Codex overload policy permits a fresh bounded attempt only for a verified structured
   overload without captured output and with unchanged protected state and verified cleanup.
   It retains the failed observation, unknown acceptance and full call charge. Its valid-only
   interpretation assumes independent, stationary attempts with prespecified validity.

   This starting plan permits a **20-percentage-point decrease** in valid-observation match
   probability, with nominal 95% one-sided Newcombe method 10 confirmation. Choose the margin
   deliberately; changing sample counts or the margin requires reconsidering usefulness and
   the complete budget together. These settings are explicit configuration, not automatic defaults.

3. Preflight the complete plan and inspect its exact provider, model, destination, safety policy
   and budgets before approving provider execution:

   ```bash
   uv run ithrl shrink CASE --experiment PRIVATE_EXPERIMENT \
     --original-workspace ORIGINAL --observation-store PRIVATE_STORE \
     --max-candidates 14 --max-provider-calls 493 --max-wall-time 86400 \
     --attempt-timeout 120 --inter-attempt-delay 15 --dry-run --format json
   ```

   For Copilot, omit `--inter-attempt-delay 15`; shell-action profiles require
   `--max-provider-calls 986`. The complete plan protects up to 248 confirmation attempts within
   493 total attempt slots. Copilot shell conservatively allows two provider calls per attempt.
   Returned actions are observed and denied, never executed; tool-side network, hosted tools and
   MCP remain forbidden. These profiles refuse an unenforceable hard input-token limit.

4. After approving the displayed limits, use the complete-plan preflight's `next_command`, which
   includes `--acknowledge-model-execution`. Calibration, search and fresh confirmation run within
   that allowance. Successful qualification commands took about 1h41m for paced Codex and
   36–37 minutes for Copilot. The 24-hour limit is a conservative administrative ceiling, not a
   duration estimate. Subscription quota is not predictable from these call limits.

Inspect the printed private result and `summary.json`. They report calibration/search/confirmation,
eligibility, reliability, known usage and actual/possible/pending/charged provider calls. Copilot
token usage remains unavailable, and denied shell actions may leave possible continuation calls;
unknown usage is never reported as zero consumption. Follow the printed continuation to resume
an incomplete run with its frozen experiment and pacing. Administrative budgets may increase;
completed confirmation is never repeated and unresolved dispatch is never automatically replayed.

## Data and privacy

Cases can contain prompts, source code, command output, paths, and other sensitive data. Never
commit unsanitized cases, traces, observation databases, or credentials. On POSIX systems, ithrl
refuses case files and directories that do not have their expected private permissions.

By default, observations are stored in the standard user-data directory for the host platform.
Use `--observation-store PATH` for a dedicated SQLite store. A custom store must be inside a
directory that only the user running ithrl can access.

Content digests detect accidental or inconsistent changes; they are not signatures. ithrl assumes
that another process running as the same user has not replaced the local case, result, run state,
or observation database. On POSIX systems, it checks directory ownership and write permissions
for source and case paths; a shared sticky parent is allowed only above a private user-owned
directory. On platforms where directory ownership cannot be checked with standard Python APIs,
custom paths must stay inside the active user profile.

## Development checks

Run the same validation entry points used by CI:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run radon cc src/ithrl --min B --show-complexity --total-average
uv run radon mi src/ithrl --min B --show
uv run pyrefly check
uv run pytest --ignore=tests/packaging
uv build --sdist --wheel --no-sources
ITHRL_DIST_DIR=dist uv run pytest -o addopts= tests/packaging
```

The non-packaging tests and synthetic walkthrough do not use provider credentials or make network
requests. Packaging checks may download build and runtime dependencies.

### Credential-free sampled CLI walkthrough

The credential-free sampled CLI walkthrough uses a mock runner
through the real calibration, search, fresh confirmation, observation store and private result
paths:

```bash
uv run pytest -o addopts= tests/integration/test_sampled_cli.py -q
```

The walkthrough exercises `shrink --experiment` with a private TOML plan, `--dry-run`, explicit
`--max-provider-calls` and `--max-wall-time`, acknowledgement, human/JSON output, and `--resume`
from an active run or saved result. Resumption preserves the statistical plan and completed
confirmation; administrative limits may increase. Optional `--inter-attempt-delay SECONDS`
defaults to zero, persists across resume and consumes the overall wall-time allowance.
Test sample sizes are fixture settings.

The mock-native integration suite runs Codex final-response and Copilot final-response and
visible-but-denied shell-action profiles through isolated mock harness processes:

```bash
uv run pytest -o addopts= tests/integration/test_native_calibration.py -q
```

It exercises context-sensitive reduction, fresh confirmation, independent candidate state,
immutable inputs, action denial, cleanup, accounting and budget refusal without provider calls.
The operator walkthrough also covers interrupted dispatch, private session metadata and result
publication, including exact resume without repeating completed confirmation.

The experimental native profiles and their measured limits are described above. Mock tests alone
do not establish native experimental support.

### Opt-in native smoke tests

The three native smoke profiles are `codex-final`, `copilot-final`, and `copilot-shell`.
They are skipped by default and refuse opt-in when `CI` or `GITHUB_ACTIONS` is set. Passing mock
tests does not change the availability table above.

Before running either live mode, obtain approval for the exact profile, model, provider, and
private evidence parent outside the checkout. The installed harness and Bubblewrap must pass the
local isolation and capability checks. Use an existing private evidence parent and configured
harness authentication; do not put credentials in arguments or case content.

Run structural preflight first. This starts bounded local harness probes but no model turn or
provider call. Native structural commands run with network access denied, the host filesystem and
harness executable read-only, and writable state restricted to private disposable directories:

```bash
uv run pytest -o addopts= tests/live -m native_smoke -s \
  --native-smoke preflight --native-profile PROFILE \
  --native-model MODEL --native-provider PROVIDER \
  --native-smoke-root /absolute/private/evidence-parent
```

Inspect the printed plan and private `preflight.json`, then obtain separate approval for provider
execution using its exact `capability_digest`. For Copilot, `PROVIDER` must be `github-copilot`.

```bash
uv run pytest -o addopts= tests/live -m native_smoke -s \
  --native-smoke run --native-profile PROFILE \
  --native-model MODEL --native-provider PROVIDER \
  --native-smoke-root /absolute/private/evidence-parent \
  --native-capability-digest APPROVED_DIGEST
```

Each invocation uses a fresh private directory, harmless synthetic session content, and an empty
original workspace. Calibration requires two matching independent attempts, permits no more than
two attempts, and bounds each attempt at 120 seconds plus the profile's cancellation grace.
Final-response profiles permit at most two provider calls; Copilot shell-action permits at most
four, with `pwd` visible but denied. Returned actions never execute; hosted tools, MCP, and
tool-side network effects remain forbidden. These profiles cannot enforce an input-token cap.
The private evidence records exact counts where known and uncertainty where acceptance cannot
be established. Unsupported or unsuccessful profiles fail the test rather than skip or retry.

## Research basis

The design draws on published research and preprints:

- [*Same Model, Different Harness: Different Coding-Agent Results*](https://arxiv.org/abs/2608.26218)
  by Sydney Lewis reports that harness-level context management can change coding-agent results
  when the model and task stay fixed.
- [*What Does Context Compression Cost an Agent? Interaction Costs Unrevealed by Task-Completion
  Metrics*](https://arxiv.org/abs/2608.16370) by Shuyu Liu examines information that an agent must
  retrieve again after context is removed.
- [*Semantic Invariance in Agentic AI*](https://arxiv.org/abs/2603.13173) by I. de Zarzà, J. de
  Curtò, Jordi Cabot, Pietro Manzoni, and Carlos T. Calafate studies whether behavior remains stable
  under meaning-preserving changes.
- [*Causal Agent Replay: Counterfactual Attribution for LLM-Agent
  Failures*](https://arxiv.org/abs/2606.08275) by Jaineet Shah describes counterfactual
  interventions for failures across agent runs. `ithrl` studies one decision and does not claim to
  identify the cause of an entire failed run.
- [*Interval estimation for the difference between independent proportions: comparison of eleven
  methods*](https://pubmed.ncbi.nlm.nih.gov/9595617/) by R. G. Newcombe informs the statistical
  design for comparisons between stochastic runs.

The publications inform the design but do not validate `ithrl`.

## License

Licensed under the [Apache License 2.0](LICENSE).
