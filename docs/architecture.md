# Architecture

`ithrl` tests how context changes affect one selected coding-agent decision. It preserves the
source evidence, constructs candidates that respect context dependencies, and records the
configured runner's observations. Results record retained context, observations and confirmation
status. They do not explain the cause of the original decision or reproduce the original agent
runtime.

The
[README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines the public capability claim. The public commands are `extract`, `shrink`, `inspect retained`
and `fuzz`. Retained-context inspection reads saved evidence without resolving runners or opening
an observation store. Fuzzing
has a credential-free local walkthrough and qualified Codex and Copilot append-text profiles for
eligible prior user input. See [native runs](native-runs.md) for the operator
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

The shared `text_sections.py` contract describes removal from one eligible prior-user text field.
It is separate from native reconstruction and from search. Field discovery uses the existing
adapter-owned editable-text surface: fixed decision input, instructions, protected nodes and opaque
records remain outside it. A content block is a field; sections never cross block boundaries.

`paragraph-v1` partitions the original string using physical LF, CRLF or CR line endings. A blank
line contains only ASCII spaces or tabs before its ending. Each run of nonblank lines owns its
following blank lines; leading blank lines form their own section. A whitespace-only field has one
section, and an empty field has none. Other Unicode line separators are ordinary text. Offsets are
half-open Python Unicode code-point positions; UTF-8 byte counts are reported separately. No
trimming, newline conversion, separator insertion or Markdown interpretation occurs. Removing all
sections leaves the existing field empty and preserves its enclosing record and content block.

Catalogs contain metadata, not the original text. Section identities bind the immutable case,
context node, content-block index, original-text digest, policy and exact range. A removal stores
ordered section IDs rather than caller-supplied ranges. Validation re-derives the catalog from the
immutable eligible field, refusing stale or foreign IDs, duplicates and noncanonical order. Fields
are limited to 1 MiB of UTF-8 text and 1,024 sections; a field exceeding either limit is unavailable
under this policy rather than partially catalogued.
Case-level discovery omits fields beyond these limits while preserving complete catalogs for
other eligible fields. Explicit validation still refuses an edit to an unavailable field.

The candidate's existing mutation envelope distinguishes `append-text` from `remove-text-sections`.
Empty and append-only candidates retain their existing serialization and identities. A section edit
can accompany dependency-closed whole-record removal only when its parent survives; removing the
same field through its parent or dependencies is refused. Appending still requires unchanged
context. Exact-request materialization addresses the immutable original message before accounting
for removed input positions and changes a defensive copy. A combined section edit refuses any
dependency-closed removal intersecting the original last input, including a nested field, even
when graph flags would allow coarse removal. The fixed input remains unchanged.
Loading `Candidate` JSON validates its shape. Binding it to a `PreparedAttempt` invokes
`prepare_candidate`, which re-derives its source bindings against immutable case evidence;
well-formed JSON and digests alone do not authorize an edit. Project-owned schema tags remain `1`
during pre-1.0 development.

Retained-context inspection exposes metadata-only catalogs with a 4,096-unit report allowance;
over-budget fields have no partial catalog. [Results](results.md#inspect-retained-context) explains
availability, byte measures and interpretation. Original text remains behind the existing bounded
opt-in.

Codex and Copilot final-response materializers apply section removals to copied native input.
Codex changes only the selected prior-user text block. Copilot changes the effective
`transformedContent` when present, otherwise `content`, preserving the surrounding event and
unedited prefix records. Copilot shell targets and fuzzing refuse section removal.

Sampled reduction opts in through `reduction.section_search`. Its ordered field descriptors list
eligible original section IDs; omitted sections remain protected during fine search. Separate coarse
and fine comparison ceilings do not transfer unused capacity. The checkpoint freezes the exact
coarse candidate and stopping state before independent per-field searches. Each fine candidate
combines that same coarse removal with one field's cumulative section selection. It never combines
edits across fields. The largest admitted editable-byte removal wins, with the first candidate
retained on a tie; the usefulness threshold is reported separately from selection and confirmation.
Fine search first tests removal of all declared eligible sections in a field. If that candidate
is refused or rejected, search partitions that same selection into smaller removals. Protected
sections remain untouched, and whole-record candidate order is unchanged.
An optional frozen `require_criterion_for_confirmation` gate stops before durable selection and
confirmation when the additional-byte criterion is unmet. Run-state validation also rejects a
selection that violates that gate; disabling it preserves independent threshold reporting.
The frozen `stop_when_no_retained_fields` setting additionally ends coarse search at a completed
comparison boundary if all selected fields have been removed. It requires the confirmation gate,
preserves the accepted coarse edit and makes no new sampling reservation.

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

Fuzz campaigns record a fixed mutation order, exact bounded payloads, counts of examined proposals
and the identity of the generator and traversal code in the collector's private sampled state.
Calibration completes and is charged before campaign initialization. On resume, the campaign
reconstructs traversal from the saved mutation order and validated exploratory observations without
executing runner attempts or making provider requests. Pure candidate-admission checks may still
run. Candidate-local admission refusals consume work without allocating a sampling block. An
unavailable comparison ends the campaign as incomplete.

Before final confirmation starts, the run state freezes the selected mutation and its complete
comparison policy. Resume reconstructs completed confirmation and terminal outcomes from the
observations belonging to each block. An unresolved reservation remains charged and cannot be
replayed. Resume stops if implementation, generation or evidence identities no longer match;
regenerated campaigns require a new run. The CLI persists a private frozen session
and immutable sampling receipts. Fuzz dry-run reads saved inputs without a harness; explicit
structural detection saves a private profile, and acknowledged execution rechecks its identity.
The fake runner uses the same collector with zero provider calls and truthful deterministic
capabilities. Native text mutation requires detected support for the exact editable seam and a
separately qualified execution profile. Codex overlays eligible prior user `input_text` fields;
Copilot overlays eligible prior `user.message` content in the copied native session prefix. Both
keep the selected decision's input outside the editable surface.

## Execution and privacy boundary

Native reduction preparation reuses a private receipt exported by an explicit successful dry-run.
The receipt carries the resolved execution plan, oracle identity and original-workspace path.
`prepare` validates these recorded facts against the case, copies its target and oracle, and binds
explicit statistical settings to the existing sampled experiment format. Section selectors use
case catalogs to derive source hashes. Preparation starts no native detection and performs no
authentication lookup or provider request. Its budget calculation is shared with normal preflight;
administrative limits remain arguments in the generated inspection command. A receipt does not
authenticate its author or establish current runtime readiness. The subsequent native dry-run
checks the installed runtime and configuration before execution can be acknowledged.

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

Codex reconstructs copied history through the installed CLI's app-server protocol. A tool-output
checkpoint includes the original output record and its captured call identity in the injected
history, then continues with empty new input. User-message triggers remain new user input. The
`codex-app-server-inject-checkpoint-v1` method binds this reconstruction contract; older
reconstruction identities cannot resume under it. Compacted source prefixes remain unsupported. An
explicit configuration-bound tool-output limit and conservative text-output admission prevent
reliance on a model-specific truncation default. Non-text tool-output content remains unsupported. A
bounded Responses relay forwards to the fixed configured destination and records physical request
counts. Native request and stream retries are disabled; a completed turn can still require several
Responses requests. The caller selects a per-attempt cap from 1 to 16 (default 16). Capability, plan
and saved-run identities retain this administrative limit. The relay rejects requests beyond the cap
and records a `provider-call-limit` error. A completed HTTP response alone does not establish model
acceptance or a completed native turn.

Copilot's Python SDK drives the installed CLI over stdio. It resumes the copied native prefix at
the selected model-turn boundary without injecting a user message, including decisions after tool
results. Captured system guidance is restored when present and verified before inference; current
custom-instruction and skill retrieval are disabled. A managed-request gate permits one inference
request, verifies effective policy and denies shell execution before a continuation can dispatch.
The session-independent model-catalogue GET permits up to three connection attempts within the
outer attempt timeout. Its attempts are counted separately from inference; certificate failures
remain terminal, and inference requests receive no retries.
The controlled prefix and policy define the experiment; uncaptured historical guidance and
harness-added context remain reconstruction limits.

Copilot fingerprints the Python executable, its shared library when present, and the installed SDK
dependency files. It mounts the standard library read-only as a trusted host dependency and records
its directory metadata. Standard-library file contents are outside the content fingerprint.

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
| `cli/` | Public command selection, native setup, progress, continuation and presentation |
| `path_safety.py`, `timestamps.py` | Shared private-path/lock primitives and timestamp validation |

Before 1.0, project-owned schema tags remain `1` while formats evolve directly. Strict structure,
kind, evidence identity and safety validation still apply. Breaking changes may require artifact
regeneration; the project does not add pre-1.0 migrations or compatibility readers. Captured
upstream versions remain evidence facts. Revisit format-version policy for 1.0.
