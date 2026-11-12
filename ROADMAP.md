# Roadmap

`ithrl` tests which context preserves a selected agent decision and which changes alter it.
Development priorities depend on useful results, reliable measurement and measured execution cost.

The [supported-inputs table](README.md#supported-inputs) describes current public capabilities.
The roadmap records completed work and future direction. Future version labels establish an
intended sequence, with scope and release dates decided from evidence.

## Harness priorities

Codex and GitHub Copilot are the primary harness integrations. Closing feature gaps between them
is the current priority.

## Completed: v0.2 context fuzzing

The `ithrl fuzz` command explores bounded context changes, simplifies interventions and
independently confirms their effects.

The released fuzzing capability supports **one confirmed finding per campaign**: a declared decrease
in the existing target's match probability among valid observations. A finding describes the
selected experiment, including execution reliability and assumptions. It does not establish a
model defect, a unique cause or semantic equivalence of the original and changed context.

The Codex and Copilot prior-user append workflows have completed synthetic qualification, including
simplification and fresh confirmation. See
[measurements](docs/measurements.md#native-context-fuzzing) for their workloads, results and costs,
and [fuzzing research](docs/research/context-fuzzing.md) for the method and assumptions.

[Practical settings and public claims](https://github.com/eberkut/ithrl/issues/10) cover
complete-workflow usefulness and measured costs.
[Copilot qualification](https://github.com/eberkut/ithrl/issues/29) tracks its reconstruction,
simplification and fresh-confirmation acceptance checks.

See the [fuzzing capability issue](https://github.com/eberkut/ithrl/issues/5) and
[`v0.2.0` milestone](https://github.com/eberkut/ithrl/milestone/1) for scope and acceptance
criteria.

[Version 0.2.0](https://github.com/eberkut/ithrl/releases/tag/v0.2.0) is published, with
[release preparation and publication](https://github.com/eberkut/ithrl/issues/11) complete.
Broader mutation families and multiple-finding campaigns need their own evidence and prospective
error allocation.

## Completed: v0.3 deeper reduction

Provider-free retained-context inspection and finer removal within supported prior-user text are
implemented, with Codex and Copilot final-response workflows. Ordinary saved-session qualification
and a [worked investigation](docs/research/ordinary-session-investigation.md) are complete; the
[measurements](docs/measurements.md#ordinary-session-paragraph-removal) record outcomes, costs and
limits. [Version 0.3.0](https://github.com/eberkut/ithrl/releases/tag/v0.3.0) is published.
[Issue #12](https://github.com/eberkut/ithrl/issues/12) records the milestone's scope, acceptance
and release evidence.

Ablation, alternative reproducers, interaction analysis and statistically confirmed single-removal
minimality remain deferred beyond v0.3.

## Current work: v0.4 preparation and passive compaction inspection

[Experiment preparation](https://github.com/eberkut/ithrl/issues/48) is implemented in
[PR #66](https://github.com/eberkut/ithrl/pull/66), keeping statistical choices, source identities
and execution limits explicit. It is merged on main and awaits release in
[v0.4.0](https://github.com/eberkut/ithrl/milestone/3).

The
[provider-free feasibility assessment](https://github.com/eberkut/ithrl/issues/15#issuecomment-6782516915)
supports adding passive compaction inspection for ordinary Codex and Copilot evidence. Its parent
is #15, with implementation in #64 and qualification in #65. The passive command has completed
[ordinary-session qualification](docs/research/passive-compaction-investigation.md) through the
CLI and both installed-artifact origins. It reports repeated recorded
operations and equal or changed captured results without inferring avoidable work, causal effects or
savings.

Comparative debugging remains a candidate for later scope decisions. Its implementation is not a
v0.4 requirement. Other candidates need useful evidence and a bounded proposal before assignment.

| Scope | Issue | Expected outcome |
| --- | --- | --- |
| Completed on main; awaiting v0.4 | [#48: experiment preparation](https://github.com/eberkut/ithrl/issues/48) | Generate and inspect private native reduction configurations without copying target/oracle blocks or digests by hand |
| Completed on main; awaiting v0.4 | [#15: passive compaction inspection](https://github.com/eberkut/ithrl/issues/15) | Useful provider-free reports for both harnesses, with explicit operation/result relations and interpretation limits |
| Completed on main; awaiting v0.4 | [#64: implement passive inspection](https://github.com/eberkut/ithrl/issues/64) | Bounded source projections, matching/report contracts and `inspect reacquire`; experimental reconstruction support is unchanged |
| Qualification complete; awaiting v0.4 | [#65: qualify passive inspection](https://github.com/eberkut/ithrl/issues/65) | Ordinary sources, counterexamples and both installed-artifact origins validated; worked interpretation recorded |
| Closing v0.4 review | [#67: performance and simplification](https://github.com/eberkut/ithrl/issues/67) | Project-wide performance fixes, permanent local-workload benchmarks and durable coding guidance |
| Closing v0.4 review | [#68: documentation and writing guidance](https://github.com/eberkut/ithrl/issues/68) | Research technical-writing practices and examples, review all documentation using Diátaxis and establish concise writing guidance |
| Candidate for additional v0.4 scope | [#13: comparative debugging](https://github.com/eberkut/ithrl/issues/13) | A comparability contract, paired workloads, interaction example and bounded validation proposal; decide whether to implement or defer |
| Deferred; no release assigned | [#14: additional harnesses](https://github.com/eberkut/ithrl/issues/14) | Assess Claude Code, OpenCode and Pi before choosing an integration order and qualification scope |
| Deferred; no release assigned | [#18: portable archives and privacy](https://github.com/eberkut/ithrl/issues/18) | Define a useful sharing workflow, archive format and deliberate privacy review before implementation |
| Research; no release assigned | [#16: broader workloads and costs](https://github.com/eberkut/ithrl/issues/16) | Measure ordinary-workflow usefulness, eligibility, reliability and full costs beyond selected published cases |
| Research; no release assigned | [#17: baseline sharing](https://github.com/eberkut/ithrl/issues/17) | Reconsider reuse only if useful complete-workflow savings justify the statistical and recovery complexity |
| Deferred research; no release assigned | [#61: ablation](https://github.com/eberkut/ithrl/issues/61) | Define retained-reproducer and full-original ablation, with distinct baselines and justified claims |
| Deferred research; no release assigned | [#62: alternative reproducers and interactions](https://github.com/eberkut/ithrl/issues/62) | Assess useful differences between preserving contexts, redundancy and joint effects, with bounded discovery and confirmation |
| Deferred research; no release assigned | [#63: confirmed single-removal minimality](https://github.com/eberkut/ithrl/issues/63) | Define the neighbor family, statistical claim, error allocation and complete comparison budget |

## Release history and current scope

| Stage | Outcome | Candidate features |
| --- | --- | --- |
| **[v0.2: context fuzzing](https://github.com/eberkut/ithrl/issues/5)** | Find small context changes that alter a selected behavior | Bounded seeded mutations, discovery and mutation simplification, one fresh confirmed finding, exact recovery and explicit cost/reliability reporting |
| **[v0.3: deeper reduction](https://github.com/eberkut/ithrl/issues/12)** | Inspect retained context and reduce within supported prior-user text | Provider-free inspection, text-section removal and qualified Codex/Copilot final-response workflows |
| **[v0.4: preparation and passive compaction inspection](https://github.com/eberkut/ithrl/milestone/3)** | Prepare native experiments and inspect recorded retrievals around compaction | Private preflight receipts, configuration generation, source section catalogs and qualified passive Codex/Copilot compaction inspection |

### Deeper reduction

Reduction within a message must preserve its native structure and protocol dependencies. Retained
context inspection should distinguish protected or frozen material, dependency constraints,
rejected removal trials, incomplete evidence and untested work. Trial explanations must identify
the tested context; a rejected sampled screen does not establish behavioral necessity.

[Ablation](https://github.com/eberkut/ithrl/issues/61) of a retained reproducer and of the full
original answers different questions and must report its baseline.
[Alternative reproducer and interaction research](https://github.com/eberkut/ithrl/issues/62) can
expose several preserving contexts and joint effects; none is automatically a unique explanation.
Deterministic single-removal minimality and
[statistically confirmed minimality](https://github.com/eberkut/ithrl/issues/63) are distinct
claims. The latter requires additional complete comparisons, error allocation and a budget that
covers all advertised checks.

### Comparative debugging and another integration

`compare GOOD_CASE BAD_CASE` should search supported context differences with dependency-valid
interventions. It must establish comparability of target, oracle, model/provider, request surface,
environment and effective runner policy before attributing a regression to context. Differences in
model, harness or execution environment remain explicit experimental factors.

Claude Code, OpenCode and Pi are planned additional integrations. Claude Code and OpenCode have
promising portability evidence in the [feasibility study](docs/research/feasibility.md); Pi needs an
initial applicability assessment. Each requires an adapter, faithful reconstruction, isolation,
calibration and a useful live workflow before becoming supported. Their order and release scope
remain open; [the integration issue](https://github.com/eberkut/ithrl/issues/14) tracks assessment,
prioritization and implementation. Add them incrementally while maintaining Codex and Copilot
parity.

### Passive reacquisition analysis

The `inspect reacquire` implementation compares recorded operations and captured results
across explicit compaction boundaries in one saved Codex or Copilot session. It starts with exact
comparisons, keeps content and display scopes separate, and reports unknown state and completeness.
Default output excludes private paths, arguments and text; detail requires bounded opt-in. It
starts no harness, opens no referenced resources and makes no provider calls.

Resume, fork, handoff and cross-harness matching remain later directions. Any normalized or
heuristic matcher needs its own policy and qualification. Repeated operations or equal captured
text do not prove avoidable retrieval, forgetting or effects caused by compaction. Passive
inspection leaves active Codex compaction reconstruction unsupported; implementing agent handoffs
is separate work.

## Further capability candidates

- Further mutation operations: supported whitespace changes, independent reordering and adding
  stale or irrelevant observations outside prior-user text. State whether each tests expected
  invariance or introduces intentional stress; a mutator name cannot prove semantic equivalence.
- Optional `record` adapters for future sessions when ordinary saved evidence is insufficient.
  Prior instrumentation must remain unnecessary for supported post-hoc extraction.
- Trajectory runners where faithful execution seams and safe action policies exist. The current
  single-decision experiment does not claim to reproduce a complete agent trajectory.
- Custom command/Python runners and additional provider integrations with the same observation,
  safety, identity and accounting contracts.
- [Portable export/import and deliberate redaction](https://github.com/eberkut/ithrl/issues/18).
  Exported experiments must state
  external state requirements and omitted sensitive evidence; portable evidence does not require
  a universal agent-session format.
- Optional editor/agent integrations and handoff analysis that compose the CLI rather than
  introducing separate experimental semantics.

These candidates have no assigned release.
[Broader native workloads and costs](https://github.com/eberkut/ithrl/issues/16) and
[baseline-sharing strategies](https://github.com/eberkut/ithrl/issues/17) remain research
opportunities. Synthetic qualification covers a planted approximately 10k workload;
[saved-session experiments](docs/measurements.md#saved-session-experiments) add selected larger
contexts, including incomplete and inconclusive outcomes. Neither establishes a general 100k
workload envelope. Qualification at that scale remains an optional expansion with a separate
cost proposal.

## Planning and release policy

GitHub issues track implementation scope, acceptance criteria and dependencies. Release milestones
group this work, and the [public Project](https://github.com/users/eberkut/projects/1/views/4) shows
status. See [decision records](docs/decisions/index.md) for rationale,
[research](docs/research/index.md) for methods and evidence, and
[architecture](docs/architecture.md) for the current design.

Before 1.0, project-owned schema tags remain at `1` while formats evolve directly. Breaking changes
are acceptable; current-format validation and evidence identity remain strict. A future stable
release needs stable case/runner/oracle contracts, useful adapters, documented statistics,
portable artifacts, reviewed export privacy, migrations and practical ordinary-case cost evidence.
There is no fixed 1.0 feature list or release date.
