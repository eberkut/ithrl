# Architecture

`ithrl` tests how context changes affect one selected coding-agent decision. It preserves the
source evidence, constructs candidates that respect context dependencies, and records the
configured runner's observations. Results record retained context, observations and confirmation
status. They do not explain the cause of the original decision or reproduce the original agent
runtime.

The
[README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines the public capability claim. The public commands are `extract`, `shrink` and `fuzz`. Fuzzing
has a credential-free local walkthrough and a qualified Codex append-text profile for eligible
prior user input. Copilot remains removal-only. See [native runs](native-runs.md) for the operator
procedure, [measurements](measurements.md#native-context-fuzzing) for qualification evidence and
[results](results.md) for interpreting output.

## Adapters and runners

Adapters discover and parse harness-native evidence, identify completed decisions and compile an
edit graph. After source evidence is verified, extraction re-derives the selected native decision
and its context from that evidence. Adapters do not construct case directories, invoke models or
enforce execution policy. `extraction.py` composes those outputs into a case. Codex and Copilot
retain separate parsers, decision selectors and context compilers because their event formats and
decision boundaries have different meanings.

Runners reconstruct candidates and execute through a controlled harness interface. Each concrete
runner owns native materialization, isolation, effective policy, protocol handling and output
normalization. Application workflow selects runners through a registry rather than importing
concrete harnesses into shared reduction code.

There is no universal agent-session representation. Native records can include tool identities,
reasoning state, compaction, provider references and lifecycle metadata that a portable message list
would lose. Shared contracts describe edits, experiments and observations while preserving the
original native evidence. Unknown records remain opaque and frozen until the adapter and runner can
support an intervention safely. [ADR 0001](decisions/0001-native-evidence-and-controlled-runners.md)
records this boundary and its qualification requirements.

## Case and experiment lifecycle

| Contract | Purpose and lifetime |
| --- | --- |
| `case.toml`, `decision.json` | Immutable capture facts, selected behavior and native pre-decision boundary |
| `source/manifest.json` and source artifacts | Exact captured bytes with safe relative paths, sizes and content digests |
| `context.jsonl` | Immutable ordered edit graph referencing captured evidence |
| Optional `request.json` | Exact request only when authoritative evidence supplies one; logical sessions do not acquire an invented request |
| Experiment definition | Explicit target, structural oracle, runner configuration and deterministic or sampled procedure |
| Resolved execution plan | Capability profile, safety policy, limits, reconstruction method and identities bound before execution |
| Candidate and prepared attempt | Dependency-valid edit, then its binding to one phase, arm, policy and unique attempt |
| Observation store | Append-only outcomes and bounded attempt evidence, kept separately from immutable case evidence |
| Result snapshot | Derived search, selection, confirmation and accounting evidence referencing the preceding contracts |

A source-backed context node refers to a native record by source, line and available native
identity, with a digest of its canonical JSON. Case loading verifies the source manifest,
selectors, digests and ordering; application loading also re-derives the selected native decision
and graph. Nodes belong before the selected inference's input boundary, so the selected output
cannot become its own input. Exact-request nodes instead use JSON Pointers into the captured
request.

Stable node identities address original evidence even after earlier nodes are removed. Atomic
groups and dependency closure preserve relationships such as tool calls and their results.
Intrinsic editability belongs to the evidence; whether a particular operation can be reconstructed
belongs to the runner and execution plan. An optional runner admission check rejects a
candidate-local reconstruction failure before sampling resources are allocated. This check is
pure and follows validation of the immutable original; runtime validation still occurs at
execution.

Digests identify the inputs and configuration used for each measurement:

- The case digest identifies immutable capture facts, source evidence and edit graph.
- The experiment digest identifies the resolved experimental definition, including its target,
  oracle, runner configuration and statistical settings.
- The execution-plan digest binds the selected capability profile, safety policy, resource limits
  and reconstruction boundary to the case and experiment.
- The candidate digest binds the retained/removed partition and any supported text mutation to
  that plan and source identities.
- The runner fingerprint identifies behavior-relevant runtime configuration. Observation identity
  additionally includes target and algorithm digests; each attempt has its own binding.

Sampled-run resource limits are recorded separately from the statistical settings. Increasing a
run allowance leaves sample targets, freshness, effective policy and per-attempt limits unchanged.
Observation reuse requires compatible identities and roles; a matching candidate digest alone
is insufficient.

## Measurement, search and confirmation

`measurement.py` invokes one prepared attempt, validates its returned evidence, evaluates the
structural target and persists an observation. A valid match and a valid nonmatch are behavioral
outcomes. Invalid observations and runner errors retain execution evidence without becoming
behavioral losses. The oracle recognizes the configured final response or shell request; it does
not judge why the model produced it.

Deterministic reduction and sampled reduction share dependency-valid candidate construction but
make different claims. The deterministic reducer can establish `search-1-minimal` through a
completed final single-group sweep. Sampled search uses exploratory screens and reports its
search coverage separately from final preservation; it does not establish sampled minimality.

The shared collector in `sampling.py` checks the budget for a complete sampling block, then
collects its observations sequentially using fixed sample targets and attempt limits.
It owns pacing, bounded attempts, eligibility, confirmation reserves and durable dispatch
reconciliation. It returns evidence and accounting. Reduction owns exploratory admission, search
coverage and preservation classification. Fuzz components own mutation generation, discovery,
simplification and change classification.

Application workflow requires calibration, durably selects one candidate and then collects a
separate fresh final confirmation against the immutable original. Calibration and exploratory
observations never enter that holdout. Once selection is final, search cannot resume or use the
holdout to select another candidate.
[ADR 0002](decisions/0002-fixed-samples-and-durable-dispatch.md) explains fixed samples, eligibility
and recovery; [results](results.md#sampled-results) describes the resulting claims and assumptions.

Fuzz campaigns freeze generated mutation order, exact bounded payloads, consumed proposal
work and generator/traversal implementation identity in the same private sampled state used by the
collector. Calibration is completed and charged before campaign initialization. On recovery,
campaign traversal is reconstructed from the frozen sequence and authoritative exploratory
observations; this replay performs no runner calls. Candidate-local admission refusals consume
work without allocating a sampling block. An unavailable screen stops with an incomplete outcome.

Before final confirmation starts, the run state freezes the selected mutation and its complete
comparison policy. Resume reconstructs completed confirmation and terminal outcomes from the
observations belonging to each block. An unresolved reservation remains charged and cannot be
replayed. Resume stops if implementation, generation or evidence identities no longer match;
regenerated campaigns require a new run. The CLI persists a private frozen session
and immutable sampling receipts. Fuzz dry-run reads saved inputs without a harness; explicit
structural detection saves a private profile, and acknowledged execution rechecks its identity.
The fake runner uses the same collector with zero provider calls and truthful deterministic
capabilities. Native text mutation requires detected support for the exact editable seam and a
separately qualified execution profile.

## Execution and privacy boundary

A capability profile states what a runner can do; a safety policy bounds permitted effects.
Preflight resolves both for the exact source format, target, installed runtime and configuration.
Structural capability detection is separate from provider-backed calibration and live-use
qualification. A version string, fixture or working parser alone does not establish support.
Reconstruction deviations remain visible in the plan, and calibration measures the controlled
surface rather than proving historical equivalence.

Runners may expose a pure candidate-admission check. Preflight applies it to the unchanged original
before sampling resources are committed. An original reconstruction refusal is terminal; a
candidate-local refusal during search consumes no provider calls. Admission starts no process or
provider request; execution still validates the reconstructed candidate.

Codex reconstructs copied history through the installed CLI's app-server protocol. A bounded
Responses relay forwards to the fixed configured destination and records physical request counts.
Native request and stream retries are disabled; a completed turn can still require several
Responses requests. The caller selects a per-attempt cap from 1 to 16 (default 16). Capability,
plan and saved-run identities retain this administrative limit. The relay rejects requests beyond
the cap and records a `provider-call-limit` error. A completed HTTP response alone does not
establish model acceptance or a completed native turn.

The shared runtime in `runner/runtime/` owns fresh private state, bounded output, process-group
termination, cancellation and cleanup. It is not a filesystem or network sandbox. Concrete
runners supply preventive isolation and effective action denial; source/workspace snapshots
provide secondary verification. Returned actions remain observations. A shell target requires
both visibility of the native action and denial by the harness's effective permission category.
Tool-side network effects, hosted tools, MCP and web access require separately qualified profiles.

Attempt evidence preserves termination and cleanup state, protected-path effects, action counts,
provider acceptance, available token usage and native-output references. Actual, additional
possible and pending provider calls remain distinct and conservatively charged. Missing usage
is unknown, not zero. Synchronous local filesystem work and observed disposable-state limits do
not imply a hard filesystem quota or a hard completion deadline.

Cases and native output may contain prompts, source, credentials and tool arguments. Persistence
uses private paths, bounded input/output and content-safe diagnostics; authentication is not
copied into cases or results. `path_safety.py` centralizes controlling-directory and private-lock
validation. The filesystem model assumes no malicious process with the same user identity or
elevated privileges. See [results and privacy](results.md#data-and-privacy) for storage and sharing
guidance, and [AGENTS.md](https://github.com/eberkut/ithrl/blob/main/AGENTS.md) for contributor
safety constraints.

## Developer map

The package uses a `src` layout. The table maps internal implementation responsibilities;
Python module names are not public API commitments.

| Modules under `src/ithrl/` | Responsibility |
| --- | --- |
| `adapters/contracts.py`, `adapters/discovery.py`, `adapters/registry.py` | Bounded native evidence contracts, discovery and adapter selection |
| `adapters/codex/`, `adapters/copilot/` | Harness-specific `parser.py`, `discovery.py`, `decisions.py` and `context.py` |
| `extraction.py`, `decisions.py` | Selected-decision contracts and case construction/revalidation |
| `case/contracts.py`, `case/evidence.py`, `case/storage.py`, `case/source.py`, `case/source_context.py`, `case/source_storage.py`, `case/filesystem.py` | Immutable case evidence, source verification and private workspace persistence |
| `context.py`, `mutation.py` | Context graph, dependency closure, stable selectors and bounded text-edit contracts |
| `behavior.py`, `experiment.py` | Structural targets and frozen deterministic/sampled experimental definitions |
| `execution/contracts.py`, `execution/preflight.py`, `execution/budget.py` | Capabilities, policy, resolved plans, refusal and admission arithmetic |
| `runner/contracts.py`, `runner/registry.py`, `runner/validation.py`, `runner/evidence.py` | Candidate/attempt contracts, runner selection and returned-evidence validation |
| `runner/codex/`, `runner/copilot/` | Native capability detection, configuration, materialization, isolation, policy and protocol |
| `runner/runtime/` | Shared bounded process, state, protection and output lifecycle |
| `runner/fake.py`, `synthetic.py` | Provider-free deterministic runner and bundled example |
| `measurement.py`, `calibration.py`, `observations.py` | One-attempt measurement, calibration and append-only SQLite observation/block evidence |
| `sampling.py`, `run_state.py` | Shared frozen-block collection, private run state, reservations, reconciliation and accounting |
| `reduce/contracts.py`, `reduce/engine.py`, `reduce/search.py` | Deterministic and exploratory sampled reduction, checkpoints and final sweep coverage |
| `statistics.py` | Independent-arm preservation interval and confirmation classification |
| `fuzz/contracts.py`, `fuzz/generation.py`, `fuzz/deterministic.py`, `fuzz/simplify.py`, `fuzz/sampling.py`, `fuzz/statistics.py` | Internal bounded mutation, discovery, simplification and change-confirmation components |
| `cli/fuzz.py`, `cli/fuzz_run.py`, `cli/fuzz_output.py`, `cli/fuzz_session.py` | Fuzz preflight, consent, composition, content-safe reports and private resume/results |
| `fuzz/configuration.py` | Private operator policy and command limits, bound to an immutable original |
| `fuzz/progress.py`, `fuzz/campaign.py` | Frozen mutation order, authoritative traversal recovery, durable selection and terminal campaign evidence over shared sampled state |
| `workflow.py`, `results/` | Application phase composition and validated result models, summaries and snapshots |
| `cli/` | Public command selection, native setup, progress, continuation and presentation |
| `path_safety.py`, `timestamps.py` | Shared private-path/lock primitives and timestamp validation |

Before 1.0, project-owned schema tags remain `1` while formats evolve directly. Strict structure,
kind, evidence identity and safety validation still apply. Breaking changes may require artifact
regeneration; the project does not add pre-1.0 migrations or compatibility readers. Captured
upstream versions remain evidence facts. Revisit format-version policy for 1.0.
