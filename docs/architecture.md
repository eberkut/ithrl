# Architecture

`ithrl` tests how context changes affect one selected coding-agent decision. It preserves source
evidence, constructs dependency-valid candidates and records runner observations and confirmation
status. These experiments do not identify the original decision's cause or reproduce its runtime.

The
[README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines public support. Use `extract` to capture a decision, `prepare` to generate a reduction
configuration, `shrink` to reduce context, `fuzz` to test mutations, and `inspect` to read retained
context or recorded operations across compaction. Inspection starts no runner or observation
store. See [native runs](native-runs.md) for procedures, [measurements](measurements.md) for
qualification evidence and [results](results.md) for interpretation.

## Adapters and runners

Adapters discover and parse native evidence, identify completed decisions and compile an edit
graph. `extraction.py` verifies the source, re-derives the decision and graph, and writes the case.
Adapters neither write cases nor invoke models or enforce execution policy. Codex and Copilot
have separate parsers, selectors and context compilers because their event formats and decision
boundaries differ.

Runners own native materialization, isolation, effective policy, protocol handling and output
normalization. Application workflow selects them through a registry; shared reduction code has
no concrete harness imports.

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
belongs to the runner and execution plan. Runners can perform an
[admission check](#execution-and-privacy-boundary) before sampling resources are allocated.

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

## Source-bound text sections

`text_sections.py` defines removal within one eligible prior-user text field, independently of
reconstruction and search. Adapters discover eligible fields; fixed decision input, instructions,
protected nodes and opaque records are excluded. Each content block is a separate field.

`paragraph-v1` partitions the original string using physical LF, CRLF or CR line endings. A blank
line contains only ASCII spaces or tabs before its ending. Each run of nonblank lines owns its
following blank lines; leading blank lines form their own section. A whitespace-only field has one
section, and an empty field has none. Other Unicode line separators are ordinary text. Offsets are
half-open Python Unicode code-point positions; UTF-8 byte counts are reported separately. No
trimming, newline conversion, separator insertion or Markdown interpretation occurs. Removing all
sections leaves the existing field empty and preserves its enclosing record and content block.

Catalogs contain metadata without original text. Section identities bind the immutable case,
context node, content-block index, original-text digest, policy and exact range. A removal stores
ordered section IDs rather than caller-supplied ranges. Validation re-derives the catalog from the
immutable eligible field, refusing stale or foreign IDs, duplicates and noncanonical order. Fields
are limited to 1 MiB of UTF-8 text and 1,024 sections; a field exceeding either limit is unavailable
under this policy rather than partially catalogued.
Case-level discovery omits fields beyond these limits while preserving complete catalogs for
other eligible fields. Explicit validation still refuses an edit to an unavailable field.

The mutation envelope distinguishes `append-text` and `remove-text-sections`; empty and append-only
candidates keep their serialization and identities. Appending requires unchanged context. Section
edits can accompany dependency-closed record removal only if the edited field's parent survives.

Exact-request materialization edits a defensive copy, locating the original message before adjusting
for removed positions. Combined section edits refuse removal touching the original last input,
including its nested fields, even if graph flags permit coarse removal. `Candidate` JSON validation
checks shape; `prepare_candidate` re-derives source bindings when creating a `PreparedAttempt`.
Neither valid JSON nor matching digests alone authorize an edit.

Retained-context inspection exposes metadata-only catalogs with a 4,096-unit report allowance;
over-budget fields have no partial catalog. [Results](results.md#inspect-retained-context) explains
availability, byte measures and interpretation. Original text remains behind the existing bounded
opt-in.

Codex and Copilot final-response materializers apply section removals to copied native input.
Codex changes only the selected prior-user text block. Copilot changes the effective
`transformedContent` when present, otherwise `content`, preserving the surrounding event and
unedited prefix records. Copilot shell targets and fuzzing refuse section removal.

`reduction.section_search` opts in with ordered original section IDs; omitted sections stay
protected during fine search. Coarse and fine comparison ceilings are separate, with no capacity
transfer. A checkpoint freezes the coarse candidate and stopping state before independent per-field
searches. Each fine candidate combines that coarse removal with cumulative section edits to one
field. Search first tries all eligible sections, then partitions a refused or rejected removal. It
never combines fields or changes whole-record candidate order. The largest admitted editable-byte
removal wins; ties retain the first candidate. Usefulness is reported separately from selection and
confirmation.

The frozen `require_criterion_for_confirmation` gate stops before selection and confirmation if
the additional-byte criterion is unmet; run-state validation rejects a violating selection.
Disabling the gate leaves threshold reporting independent. With this gate enabled,
`stop_when_no_retained_fields` can end coarse search at a completed comparison boundary once all
selected fields are gone, preserving the accepted edit without another sampling reservation.

Candidate, experiment and algorithm identities bind the source, policy and traversal. Resume
reconstructs the recorded coarse and fine decisions without executing attempts. One final durable
selection precedes fresh confirmation against the full immutable original, including when either
search stage has incomplete coverage. Results retain both stages and the selected mutation;
inspection revalidates its source bindings without loading a runner or observation database.

Provider-free tests cover these implementation contracts. The
[ordinary-session measurements](measurements.md#ordinary-session-paragraph-removal) record completed
qualification for the tested native profiles and their integration limits. The
[workload criteria](research/reduction-design.md#text-section-workload-preparation) preserve the
original preparation proposal separately from those measurements.

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

`sampling.py` admits complete blocks and collects observations sequentially under fixed sample
targets and attempt limits. It owns pacing, eligibility, confirmation reserves, dispatch
reconciliation and accounting. Reduction owns exploratory screening, search coverage and
preservation classification. Fuzz components own mutation generation, discovery, simplification
and change classification.

Application workflow requires calibration, durably selects one candidate and then collects a
separate fresh final confirmation against the immutable original. Calibration and exploratory
observations never enter that holdout. Once selection is final, search cannot resume or use the
holdout to select another candidate.
[ADR 0002](decisions/0002-fixed-samples-and-durable-dispatch.md) explains fixed samples, eligibility
and recovery; [results](results.md#sampled-results) describes the resulting claims and assumptions.

Fuzz campaigns record a fixed mutation order, exact bounded payloads, counts of examined proposals
and the identity of the generator and traversal code in the collector's private sampled state.
Calibration completes and is charged before campaign initialization. On resume, the campaign
reconstructs traversal from the saved mutation order and validated exploratory observations without
executing runner attempts or making provider requests. Pure candidate-admission checks may still
run. Candidate-local admission refusals consume work without allocating a sampling block. An
unavailable comparison ends the campaign as incomplete.

Before confirmation, run state freezes the mutation and comparison policy. Resume derives completed
confirmation and terminal outcomes from block observations without replaying unresolved charged
reservations. Changed implementation, generation or evidence identities require a new run. The CLI
saves a private frozen session and immutable sampling receipts.

Fuzz dry-run reads saved inputs without a harness. Explicit structural detection saves a private
profile; acknowledged execution rechecks its identity.
The fake runner uses this collector with deterministic capabilities and zero provider calls.
Native text mutation requires detected support and a qualified execution profile: Codex edits
eligible prior-user `input_text` fields, and Copilot edits prior `user.message` content in a copied
prefix. Neither edits the selected decision's input.

## Execution and privacy boundary

Passive compaction inspection has separate harness-owned projections over the bounded immutable
JSONL snapshot, dispatched through the existing adapter registry. These projections retain only
source anchors, recorded operations, captured result fields and compaction intervals needed by
the exact matcher in `reacquisition.py`. They neither construct experimental contexts nor relax
experimental parser or runner refusals. Unknown records stay opaque; copied nested history does
not become newly completed execution. The shared matcher partitions adjacent epochs and keeps
recorded-operation identity, captured-result equality, completeness and unknown resource state
separate. It opens no referenced resource and performs no native or provider operation. See
[passive report semantics](results.md#inspect-recorded-compaction).

Native reduction preparation consumes a private receipt exported by a successful explicit dry-run.
It records the execution plan, oracle identity and original-workspace path. `prepare` validates
them against the case, copies the target and oracle, and binds explicit sampling settings and
source-derived section hashes. It starts no native detection, authentication lookup or provider
request. Budget arithmetic is shared with preflight; administrative limits stay in the generated
inspection command. A receipt neither authenticates its author nor proves runtime readiness.
The next native dry-run checks the installed runtime and configuration before execution.

A capability profile describes available operations; a safety policy bounds permitted effects.
Preflight resolves both for the source format, target, installed runtime and configuration.
Structural detection does not replace calibration or live-use qualification. The plan records
reconstruction deviations; calibration measures that surface without proving historical equivalence.

Runners may expose a pure candidate-admission check. Preflight applies it to the unchanged original
before sampling resources are committed. An original reconstruction refusal is terminal; a
candidate-local refusal during search consumes no provider calls. Admission starts no process or
provider request; execution still validates the reconstructed candidate.

Codex reconstructs copied history through the installed CLI's app-server. Tool-output checkpoints
inject the original output and call identity, then continue with empty new input; user-message
triggers remain new input. `codex-app-server-inject-checkpoint-v1` binds this contract and refuses
resume from older reconstruction identities. Compacted prefixes and non-text tool outputs remain
unsupported. An explicit configuration-bound output limit and conservative text admission avoid
model-specific truncation defaults.

A Responses relay forwards to the fixed destination, counts physical requests and enforces the
caller's per-attempt cap: 1–16, default 16. Capability, plan and saved-run identities bind that
limit. Excess requests cause `provider-call-limit`. Native request/stream retries are disabled,
but a turn may need several requests. HTTP completion alone proves neither model acceptance nor
native turn completion.

Copilot's Python SDK drives the CLI over stdio, resuming the copied prefix at the selected model
turn, including after tool results, without a new user message. When present, captured system
guidance is restored and verified before inference; current custom-instruction and skill retrieval
are disabled. A managed-request gate
allows one inference request, verifies policy and denies shell execution before continuation.

The model-catalogue GET allows three connection attempts within the outer timeout, counted
separately from inference; certificate failures remain terminal. Inference is never retried.
Uncaptured historical guidance
and harness-added context remain limits of the controlled prefix and policy.

Copilot fingerprints the Python executable, its shared library when present, and the installed SDK
dependency files. It mounts the standard library read-only as a trusted host dependency and records
its directory metadata. Standard-library file contents are outside the content fingerprint.

`runner/runtime/` owns private state, bounded output, process-group termination, cancellation and
cleanup. Concrete runners supply filesystem/network isolation and effective action denial;
source/workspace snapshots provide secondary verification. Returned actions are observations.
A shell target requires native action visibility and denial through the effective permission
category. Tool-side network effects, hosted tools, MCP and web access need separately qualified
profiles.

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
| `context.py`, `mutation.py`, `text_sections.py` | Context graph, dependency closure, stable selectors and bounded text-edit contracts |
| `behavior.py`, `experiment.py` | Structural targets and frozen deterministic/sampled experimental definitions |
| `preparation.py`, `cli/preparation.py` | Recorded preflight receipts, offline settings binding and private experiment publication |
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
| `inspection.py`, `results/inspection.py`, `cli/inspect.py` | Offline result/case binding, retained-context interpretation, source section catalogs and CLI presentation |
| `reacquisition.py`, `adapters/reacquisition.py`, `adapters/codex/reacquisition.py`, `adapters/copilot/reacquisition.py` | Passive compaction projections, exact adjacent-epoch matching and bounded reports |
| `cli/` | Public command selection, native setup, progress, continuation and presentation |
| `path_safety.py`, `timestamps.py` | Shared private-path/lock primitives and timestamp validation |

Before 1.0, project-owned schema tags remain `1` while formats evolve directly. Strict structure,
kind, evidence identity and safety validation still apply. Breaking changes may require artifact
regeneration; the project does not add pre-1.0 migrations or compatibility readers. Captured
upstream versions remain evidence facts. Revisit format-version policy for 1.0.
