# Architecture

`ithrl` tests how context changes affect one selected coding-agent decision. It preserves source
evidence, constructs dependency-valid candidates and records runner observations and confirmation
status. These experiments do not identify the original decision's cause or reproduce its runtime.

The
[README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines public support. Use `extract` to capture a decision, `prepare` to generate a reduction
configuration, `shrink` to reduce context, `fuzz` to test mutations and `compare` to test a declared
effect between two source-bound contexts. `inspect` reads retained context or recorded operations
across compaction without starting a runner or opening an observation store. See
[native runs](native-runs.md) and [comparison](comparison.md) for procedures,
[measurements](measurements.md) and the [comparison study](research/comparative-debugging.md) for
qualification evidence, and [results](results.md) for interpretation.

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

`execution/contracts.py` defines the full `ResolvedExecutionPlan` and its bounded,
content-safe `ExecutionPlanMetadata`. Their identities use canonical JSON:

```text
plan_content_digest = SHA256(canonical_json(resolved_execution_plan))
execution_plan_digest = SHA256(canonical_json(execution_plan_metadata))
```

The metadata includes `plan_content_digest`, capability and safety-policy digests, capture type,
candidate materialization, reconstruction method, `SafetyPolicyDeclarations` and `ResourceLimits`.
The declaration contract owns the whitelist of disclosed policy fields; private provider
destinations remain in the full plan. This binds readable declarations to all private plan inputs
without disclosing those inputs or requiring them to recompute the execution-plan digest.

A full-plan hash alone cannot validate a disclosed subset without the private contents. An
unbound projection could change while retaining the recorded plan identity.

`sampled_run_metadata.py` persists required `execution_metadata`, including per-attempt resource
limits, and verifies its digest against the original observation identity. Sampled snapshots
retain no full execution plan. `results/inspection.py` validates this binding offline;
`inspection.py` reports the metadata and bounded coverage of recorded effective-policy digests.
Declared policy and recorded attempt evidence remain separate, with interpretation in
[results](results.md#interpret-retained-units-and-trials).

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

Prior-user text edits use `append-text` or `remove-text-sections`. Appending requires unchanged
context. Section removal may accompany dependency-closed record removal when the edited field
survives.

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

Sampled experiments can attach `python-patch-tests` grading to an unconstrained final-response
target. `grading/` binds the implementation, reviewed task bundle and prepared system-Python
environment. Git applies content-only changes to existing allowlisted regular files with isolated
configuration and attributes. Paths, file types and modes are checked before compilation; fixed
source and separately authored verifier suites run in disposable namespaces without network access.
Each suite imports the patched code in the interpreter that runs assertions and reports counts.
Read-only mounts protect verifier files; they do not prevent the candidate from reading those files
or altering in-process reporting. Grades assume cooperative repairs that leave the verifier and its
reporting intact. The report nonce checks association, not authenticity against candidate code.
Preflight and pre-dispatch checks start an inert grader namespace with a timeout of at most two
seconds; they import no task code and make no provider calls.

An absolute native deadline reaches the runtime's prelaunch checks, reserving time for grading and
cleanup without changing the frozen attempt. The grade, response digest and suite counts stay with
the completed attempt. Rejected or failing repairs are nonmatches. Operational grading errors retain
the native request charge and stop collection without entering behavioral counts or authorizing
another inference. Under that cooperative assumption, task grades record the declared test
outcomes. Native input visibility and runtime qualification require separate evidence. Exceptions
in setup, teardown or cleanup are operational errors, including those caused by candidate code;
failures reported for test bodies are behavioral nonmatches. Skipped or expected-failure outcomes
and incomplete suites are operational grading errors.

[ADR 0005](decisions/0005-cooperative-repair-grading.md) records the grading outcome and trust
boundary.

Deterministic reduction and sampled reduction share dependency-valid candidate construction but
make different claims. The deterministic reducer can establish `search-1-minimal` through a
completed final single-group sweep. Sampled search uses exploratory screens and reports its
search coverage separately from final preservation; it does not establish sampled minimality.

`sampling.py` admits complete blocks and collects observations sequentially under fixed sample
targets and attempt limits. It owns pacing, eligibility, confirmation reserves, dispatch
reconciliation and accounting. Reduction owns exploratory screening, search coverage and
preservation classification. Fuzz components own mutation generation, discovery, simplification
and change classification.

Reduction and fuzzing require calibration, durably select one candidate and then collect a
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

### Explicit-context comparison

`comparison/` binds a reference and candidate to one immutable case, target and execution plan. Each
selection uses existing dependency-closed removals and optional source-bound text edits; either
context may omit supported material. Preparation admits the immutable original and both selections,
and protects the fixed triggering input independently of caller-supplied edit flags.

Capture-time model and provider remain source provenance. Shared `model`, `provider` and
`reasoning_effort` settings bind execution choices for both harnesses. Codex omits historical
reasoning from both execution copies when the effective model or provider differs from capture;
an effort-only change does not activate that projection. The plan and report declare the resolved
policy separately from context edits. Source evidence and fixed input remain intact.

The comparison procedure binds both selections, repair or degradation direction, calibration gates,
fixed confirmation counts, freshness and eligibility policy. Administrative ceilings remain
separate. The shared collector materializes its reference arm from the explicit selection only for
this procedure; reduction and fuzzing retain their full-original baseline.

Two-arm calibration checks the declared contrast before durable selection. Confirmation uses fresh
observations and the existing exact change bounds: repair orders candidate minus reference;
degradation orders reference minus candidate. A lower bound exceeding the frozen minimum effect
confirms the directed change. Incomplete evidence and a complete inconclusive comparison remain
distinct, and calibration supplies no confirmation credit.

Comparison progress uses the existing sampled state, observation store, block admission and
dispatch reconciliation. It excludes reduction and fuzz checkpoints. A private result snapshot
contains that state, referenced blocks and digest-bound execution metadata; summaries are derived
from them. Offline inspection validates their identities and chronology without opening the store
or starting a runner. Supplying the case additionally re-derives source partitions and text edits.
[ADR 0006](decisions/0006-explicit-context-comparison.md) records the decision; the
[comparison guide](comparison.md) owns configuration and recovery procedures.

## Source-bound tool output text

`tool_output_text.py` defines metadata-only native text fields and `remove-tool-output-text-span`.
Adapters enumerate field locations in verified logical-session evidence; callers select a
digest-bound field ID rather than supply a native write path. The field identity binds the case,
context node, native field path, complete text digest and original sizes. Case-level discovery is
cached against the exact immutable owner and evidence inputs; copied or rebound evidence revalidates
discovery.

The one-field edit stores a nonempty half-open Unicode codepoint range and the removed substring's
digest. Validation re-derives the field and exact substring from source, enforcing the 1 MiB
UTF-8 field limit. Materialization copies the native record and splices the string without
decoding embedded content or changing retained text. Complete source-field deletion preserves an
empty string and its enclosing record; native reconstruction can elide an empty content block.
Each runner owns native path resolution, call/result pairing, fixed trigger protection and existing
reconstruction limits.

This operation is confined to explicit-context comparison: the reference must be unmutated, the
candidate contains one span deletion, and both actual dependency-closed record partitions must
match. Configuration and saved bindings validate that contract; result loading additionally
checks source eligibility when a case is supplied. Existing paragraph and append rules remain
separate. The active-resume implementation digest includes the span and mutation algorithms.

`inspect tool` exposes bounded field metadata and can author a deletion from immutable source
bytes. Optional text is escaped under a shared byte allowance. Comparison reports retain the edit
descriptor and can re-derive its sizes and retained text with the source case. The
[supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines qualified experimental support; discovery and reconstruction support alone do not establish
native input visibility or behavioral qualification.

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
turn, including after tool outputs, without a new user message or resume bookkeeping event.
Selected environment tokens are passed explicitly to the SDK session; managed login uses the
isolated configuration. When present, captured system
guidance is restored and verified before inference; current custom-instruction and skill retrieval
are disabled. A managed-request gate
allows one inference request, verifies policy and denies shell execution before continuation.

After policy verification, up to two same-session `/meta` GETs may precede model-context
verification. Their count is retained in native output. The model-catalogue GET allows three
connection attempts within the outer timeout, counted separately from inference; certificate
failures remain terminal. Inference is never retried.
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

Source snapshots, workspace roots and their ancestors refuse symlinks. Codex workspace snapshots
can preserve nested relative links to existing real files or directories inside the same tree.
Leading `..` steps may stay within that root; absolute targets, later `..` traversal, link chains,
dangling targets and special files are refused. The snapshot hashes each link's text without
following it; ordinary tree inspection covers the target data.

Attempt evidence preserves termination and cleanup state, protected-path effects, action counts,
provider acceptance, available token usage and native-output references. Actual, additional
possible and pending provider calls remain distinct and conservatively charged. Missing usage
is unknown, not zero. Synchronous local filesystem work and observed disposable-state limits do
not imply a hard filesystem quota or a hard completion deadline.

Post-run cost reporting reads a bounded, read-only observation snapshot and applies explicitly
selected reference prices through `costs.py`. `cost_reporting.py` keeps recorded usage separate
from hypothetical scenarios and saves counts, effective rates, arithmetic and pricing provenance
in a self-contained private report. Inspection needs neither the observation store nor a current
price lookup. Pricing does not participate in execution admission.
See [token-cost estimates](results.md#estimate-token-costs) for coverage and interpretation.

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
| `context.py`, `mutation.py`, `text_sections.py`, `tool_output_text.py` | Context graph, dependency closure, stable selectors and bounded text-edit contracts |
| `behavior.py`, `experiment.py` | Structural targets and frozen deterministic/sampled experimental definitions |
| `preparation.py`, `cli/preparation.py` | Recorded preflight receipts, offline settings binding and private experiment publication |
| `execution/contracts.py`, `execution/preflight.py`, `execution/budget.py` | Capabilities, policy, resolved plans, refusal and admission arithmetic |
| `runner/contracts.py`, `runner/registry.py`, `runner/validation.py`, `runner/evidence.py` | Candidate/attempt contracts, runner selection and returned-evidence validation |
| `runner/codex/`, `runner/copilot/` | Native capability detection, configuration, materialization, isolation, policy and protocol |
| `runner/runtime/` | Shared bounded process, state, protection and output lifecycle |
| `runner/fake.py`, `synthetic.py` | Provider-free deterministic runner and bundled example |
| `measurement.py`, `calibration.py`, `observations.py` | One-attempt measurement, calibration and append-only SQLite observation/block evidence |
| `costs.py`, `cost_reporting.py`, `cli/cost.py` | Offline token-price equivalents, usage/rate provenance and self-contained cost reports |
| `grading/` | Bound Python repair tasks, isolated patch application and test grading; no native action execution |
| `sampling.py`, `run_state.py` | Shared frozen-block collection, private run state, reservations, reconciliation and accounting |
| `sampled_run_metadata.py` | Frozen sampled-run metadata and execution-plan identity binding |
| `reduce/contracts.py`, `reduce/engine.py`, `reduce/search.py` | Deterministic and exploratory sampled reduction, checkpoints and final sweep coverage |
| `statistics.py` | Independent-arm preservation interval and confirmation classification |
| `fuzz/contracts.py`, `fuzz/generation.py`, `fuzz/deterministic.py`, `fuzz/simplify.py`, `fuzz/sampling.py`, `fuzz/statistics.py` | Internal bounded mutation, discovery, simplification and change-confirmation components |
| `cli/fuzz.py`, `cli/fuzz_run.py`, `cli/fuzz_output.py`, `cli/fuzz_session.py` | Fuzz preflight, consent, composition, content-safe reports and private resume/results |
| `fuzz/configuration.py` | Private operator policy and command limits, bound to an immutable original |
| `fuzz/progress.py`, `fuzz/campaign.py` | Frozen mutation order, authoritative traversal recovery, durable selection and terminal campaign evidence over shared sampled state |
| `comparison/contracts.py`, `comparison/engine.py`, `comparison/results.py` | Explicit source-bound selections, two-arm calibration, directed confirmation and offline result validation over shared sampled state |
| `cli/compare.py`, `cli/comparison_output.py`, `cli/comparison_session.py` | Comparison preflight, consent, progress, private sessions, recovery and content-safe reports |
| `workflow.py`, `results/` | Application phase composition and validated result models, summaries and snapshots |
| `inspection.py`, `results/inspection.py`, `cli/inspect.py` | Offline result/case binding, retained-context interpretation, source section catalogs and CLI presentation |
| `reacquisition.py`, `adapters/reacquisition.py`, `adapters/codex/reacquisition.py`, `adapters/copilot/reacquisition.py` | Passive compaction projections, exact adjacent-epoch matching and bounded reports |
| `cli/` | Public command selection, native setup, progress, continuation and presentation |
| `path_safety.py`, `timestamps.py` | Shared private-path/lock primitives and timestamp validation |

Before 1.0, project-owned schema tags remain `1` while formats evolve directly. Strict structure,
kind, evidence identity and safety validation still apply. Breaking changes may require artifact
regeneration; the project does not add pre-1.0 migrations or compatibility readers. Captured
upstream versions remain evidence facts. Do not edit captured source or retained
evidence to manufacture new metadata or a matching identity. Revisit format-version policy for 1.0.
