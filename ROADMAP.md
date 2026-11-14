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

The `ithrl fuzz` command explores bounded context changes, simplifies interventions and freshly
confirms **at most one finding per campaign**: a declared decrease in the target's match probability
among valid observations. This is conditional experimental evidence, not proof of a model defect,
unique cause or semantic equivalence.

Codex and Copilot prior-user append workflows have completed synthetic qualification. Their
[measurements](docs/measurements.md#native-context-fuzzing) cover workloads, results and costs;
[fuzzing research](docs/research/context-fuzzing.md) owns the method and assumptions.
[Practical settings](https://github.com/eberkut/ithrl/issues/10) and
[Copilot qualification](https://github.com/eberkut/ithrl/issues/29) record acceptance evidence.

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

## Awaiting release: v0.4 preparation and passive compaction inspection

[Experiment preparation](https://github.com/eberkut/ithrl/issues/48) is implemented in
[PR #66](https://github.com/eberkut/ithrl/pull/66), keeping statistical choices, source identities
and execution limits explicit. It is merged on main and awaits release in
[v0.4.0](https://github.com/eberkut/ithrl/milestone/3).

Passive compaction inspection for ordinary Codex and Copilot evidence is implemented under #15,
with implementation in #64 and qualification in #65. Its
[provider-free feasibility assessment](https://github.com/eberkut/ithrl/issues/15#issuecomment-6782516915)
led to the current scope. The command has completed
[ordinary-session qualification](docs/research/passive-compaction-investigation.md) through the
CLI and both installed-artifact origins. It reports repeated recorded
operations and equal or changed captured results without inferring avoidable work, causal effects or
savings.

The project-wide documentation review is complete in
[PR #72](https://github.com/eberkut/ithrl/pull/72). All assigned v0.4 issues are complete on main;
release preparation and publication remain. The investigations below have no release assignment and
do not expand v0.4 scope.

| Scope | Issue | Expected outcome |
| --- | --- | --- |
| Completed on main; awaiting v0.4 | [#48: experiment preparation](https://github.com/eberkut/ithrl/issues/48) | Generate and inspect private native reduction configurations without copying target/oracle blocks or digests by hand |
| Completed on main; awaiting v0.4 | [#15: passive compaction inspection](https://github.com/eberkut/ithrl/issues/15) | Useful provider-free reports for both harnesses, with explicit operation/result relations and interpretation limits |
| Completed on main; awaiting v0.4 | [#64: implement passive inspection](https://github.com/eberkut/ithrl/issues/64) | Bounded source projections, matching/report contracts and `inspect reacquire`; experimental reconstruction support is unchanged |
| Qualification complete; awaiting v0.4 | [#65: qualify passive inspection](https://github.com/eberkut/ithrl/issues/65) | Ordinary sources, counterexamples and both installed-artifact origins validated; worked interpretation recorded |
| Completed on main; awaiting v0.4 | [#67: performance and simplification](https://github.com/eberkut/ithrl/issues/67) | Project-wide performance fixes, permanent local-workload benchmarks and durable coding guidance, merged in [PR #71](https://github.com/eberkut/ithrl/pull/71) |
| Completed on main; awaiting v0.4 | [#68: documentation and writing guidance](https://github.com/eberkut/ithrl/issues/68) | Project-wide documentation corrections and concise writing guidance, merged in [PR #72](https://github.com/eberkut/ithrl/pull/72) |
| Research; no release assigned | [#16: broader workloads and costs](https://github.com/eberkut/ithrl/issues/16) | Measure source eligibility, useful developer conclusions, reliability and full costs, including unsuccessful sources and runs |
| Candidate; no release assigned | [#13: comparative debugging](https://github.com/eberkut/ithrl/issues/13) | A comparability contract, paired workloads, interaction example and bounded validation proposal; decide whether to implement or defer |
| Candidate; no release assigned | [#73: sampled inspection metadata](https://github.com/eberkut/ithrl/issues/73) | Persist reconstruction and policy metadata bound to plan identity for offline reports, distinguishing declarations from recorded attempt evidence |
| Assessment; no release assigned | [#74: tool-output interventions](https://github.com/eberkut/ithrl/issues/74) | Assess native output visibility, source bindings and a useful localized intervention before proposing a new edit domain |
| Deferred; no release assigned | [#14: additional harnesses](https://github.com/eberkut/ithrl/issues/14) | Assess Claude Code, OpenCode and Pi before choosing an integration order and qualification scope |
| Deferred; no release assigned | [#18: portable archives and privacy](https://github.com/eberkut/ithrl/issues/18) | Define a useful sharing workflow, archive format and deliberate privacy review before implementation |
| Research; no release assigned | [#17: baseline sharing](https://github.com/eberkut/ithrl/issues/17) | Reconsider reuse only if useful complete-workflow savings justify the statistical and recovery complexity |
| Deferred research; no release assigned | [#61: ablation](https://github.com/eberkut/ithrl/issues/61) | Define retained-reproducer and full-original ablation, with distinct baselines and justified claims |
| Deferred research; no release assigned | [#62: alternative reproducers and interactions](https://github.com/eberkut/ithrl/issues/62) | Assess useful differences between preserving contexts, redundancy and joint effects, with bounded discovery and confirmation |
| Deferred research; no release assigned | [#63: confirmed single-removal minimality](https://github.com/eberkut/ithrl/issues/63) | Define the neighbor family, statistical claim, error allocation and complete comparison budget |

## Next investigations

Start with #16's provider-free workload preparation for both harnesses. Freeze debugging questions
and selection criteria before choosing successful examples; retain exclusions and unsuccessful
investigations.
[ctx](https://github.com/ctxrs/ctx/blob/33ba4645b620fd89d138c6db1fabf33096a0ee16/docs/search.md) may
help locate sessions, but ithrl must independently snapshot and validate native evidence. Measure
useful developer conclusions alongside reduction, reliability and cost.

Use the initial cohort to inform #13's good/bad-case comparison design and #74's tool-output
assessment; neither must wait for the entire study. #74 must distinguish original output,
delivered representation and saved evidence before proposing edits. #73's reporting improvement
is independent. Execution and release scope remain separate decisions in the owning issues.

The review of
[SkillOpt's training method](https://github.com/microsoft/SkillOpt/blob/45f65ca04f5f6713bca7b9797b495696ba812751/docs/guide/training-loop.md)
and
[Sift's tool-output benchmarks](https://github.com/ctxrs/sift/blob/c22fb1b3747352dba8f8323c057d60e7afd92bf5/benchmarks/results/2026-09-21-release-v0.4.0/README.md)
motivated better candidate selection and tool-output investigation. Their task-performance and
output-size results do not establish ithrl's debugging usefulness. No search engine or skill
optimizer is planned. Fuzzing already accepts supplied `stale_text`; #16 should establish a need for
distinct proposals tied to specific fields before creating further work. Passive inspection retains
its qualified exact-comparison contract; decoded or normalized matching needs a separate policy.

## Release history and current scope

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

Keep each issue's problem, rationale, evidence, scope and stop point self-contained. Roadmap entries
explain direction and link to those owners; neither should depend on chat history or local plan
files.

Before 1.0, project-owned schema tags remain at `1` while formats evolve directly. Breaking changes
are acceptable; current-format validation and evidence identity remain strict. A future stable
release needs stable case/runner/oracle contracts, useful adapters, documented statistics,
portable artifacts, reviewed export privacy, migrations and practical ordinary-case cost evidence.
There is no fixed 1.0 feature list or release date.
