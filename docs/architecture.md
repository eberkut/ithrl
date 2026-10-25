# Architecture

`ithrl` studies one selected coding-agent decision under controlled context interventions.
It preserves the evidence needed to locate that decision, constructs dependency-valid candidates,
and records what a configured runner actually observed. A smaller reproducer is evidence about
the configured experiment; it is not a causal explanation or a reconstruction of the original
agent runtime.

The [README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines the public capability claim. The public commands are `extract`, `shrink` and `fuzz`.
Fuzzing has a credential-free local walkthrough; native text-mutation support requires separate
qualification. See [native runs](native-runs.md) for the qualified execution profiles and
[results](results.md) for interpreting their output.

## Evidence and execution have different owners

Adapters discover and parse harness-native evidence, identify completed decisions and compile an
edit graph. They do not construct case directories, invoke models or enforce execution policy.
`extraction.py` composes those outputs into a case. Codex and Copilot retain separate parsers,
decision selectors and context compilers because their event formats and decision boundaries
have different meanings.

Runners reconstruct candidates and execute through a controlled harness interface. Each concrete
runner owns native materialization, isolation, effective policy, protocol handling and output
normalization. Application workflow selects runners through a registry rather than importing
concrete harnesses into shared reduction code.

There is no universal agent-session representation. Native records can include tool identities,
reasoning state, compaction, provider references and lifecycle metadata that a portable message
list would lose. Shared contracts describe edits, experiments and observations while preserving
the original native evidence. Unknown records remain opaque and frozen until the adapter and
runner can support an intervention safely. [ADR 0001](decisions/0001-native-evidence-and-controlled-runners.md)
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

Content addressing keeps incompatible evidence separate:

- The case digest identifies immutable capture facts, source evidence and edit graph.
- The experiment digest identifies the resolved experimental definition, including its target,
  oracle, runner configuration and statistical settings.
- The execution-plan digest binds the selected capability profile, safety policy, resource limits
  and reconstruction boundary to the case and experiment.
- The candidate digest binds the retained/removed partition and any supported text mutation to
  that plan and source identities.
- The runner fingerprint identifies behavior-relevant runtime configuration. Observation identity
  additionally includes target and algorithm digests; each attempt has its own binding.

Administrative sampled-run allowances are recorded separately from frozen statistical identity.
Increasing an allowance does not change sample targets, freshness, effective policy or per-attempt
limits. Observation reuse requires compatible identities and roles; a matching candidate digest
alone is insufficient.

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

The shared collector in `sampling.py` admits and collects a complete frozen block sequentially.
It owns pacing, bounded attempts, eligibility, confirmation reserves and durable dispatch
reconciliation. It returns evidence and accounting, not candidate-selection or statistical
verdicts. Reduction owns exploratory admission and search coverage. Fuzz components own
mutation generation, discovery, simplification and change classification. Sharing a collector
does not make preservation and change the same statistical question.

Application workflow requires calibration, durably selects one candidate and then collects a
separate fresh final confirmation against the immutable original. Calibration and exploratory
observations never enter that holdout. Once selection is final, search cannot resume or use the
holdout to select another candidate. [ADR 0002](decisions/0002-fixed-samples-and-durable-dispatch.md)
explains fixed samples, eligibility and recovery; [results](results.md#sampled-results) describes
the resulting claims and assumptions.

Fuzz campaigns freeze generated mutation order, exact bounded payloads, consumed proposal
work and generator/traversal implementation identity in the same private sampled state used by the
collector. Calibration is completed and charged before campaign initialization. On recovery,
campaign traversal is reconstructed from the frozen sequence and authoritative exploratory
observations; this replay performs no runner calls. Candidate-local admission refusals consume
work without allocating a sampling block. An unavailable screen stops with an incomplete outcome.

One state replacement freezes the selected mutation and its complete final-comparison policy
before holdout dispatch. Completed confirmation and terminal outcomes are reconstructed from
their owned observations rather than trusted cached verdicts. An unresolved reservation remains
charged and cannot be replayed. Implementation, generation or evidence mismatches refuse
continuation; regenerated campaigns require a new run. The CLI persists a private frozen session
and immutable sampling receipts. Fuzz dry-run reads saved inputs without a harness; explicit
structural detection saves a private profile, and acknowledged execution rechecks its identity.
The fake runner uses the same collector with zero provider calls and truthful deterministic
capabilities. Native profiles must separately qualify text mutation.

## Execution and privacy boundary

A capability profile states what a runner can do; a safety policy bounds permitted effects.
Preflight resolves both for the exact source format, target, installed runtime and configuration.
Structural capability detection is separate from provider-backed calibration and live-use
qualification. A version string, fixture or working parser alone does not establish support.
Reconstruction deviations remain visible in the plan, and calibration measures the controlled
surface rather than proving historical equivalence.

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

The package uses a `src` layout. The following names describe the implemented ownership map;
they do not promise public Python APIs or additional CLI commands.

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
