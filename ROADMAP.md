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

## Completed: v0.4 preparation and passive compaction inspection

[Experiment preparation](https://github.com/eberkut/ithrl/issues/48) is implemented in
[PR #66](https://github.com/eberkut/ithrl/pull/66), keeping statistical choices, source identities
and execution limits explicit. It is published in
[v0.4.0](https://github.com/eberkut/ithrl/releases/tag/v0.4.0).

Passive compaction inspection for ordinary Codex and Copilot evidence is implemented under #15,
with implementation in #64 and qualification in #65. Its
[provider-free feasibility assessment](https://github.com/eberkut/ithrl/issues/15#issuecomment-6782516915)
led to the current scope. The command has completed
[ordinary-session qualification](docs/research/passive-compaction-investigation.md) through the
CLI and both installed-artifact origins. It reports repeated recorded
operations and equal or changed captured results without inferring avoidable work, causal effects or
savings.

The project-wide documentation review is complete in
[PR #72](https://github.com/eberkut/ithrl/pull/72). All assigned issues in the
[v0.4.0 milestone](https://github.com/eberkut/ithrl/milestone/3) are complete.

| Scope | Issue | Expected outcome |
| --- | --- | --- |
| Released in v0.4 | [#48: experiment preparation](https://github.com/eberkut/ithrl/issues/48) | Generate and inspect private native reduction configurations without copying target/oracle blocks or digests by hand |
| Released in v0.4 | [#15: passive compaction inspection](https://github.com/eberkut/ithrl/issues/15) | Useful provider-free reports for both harnesses, with explicit operation/result relations and interpretation limits |
| Released in v0.4 | [#64: implement passive inspection](https://github.com/eberkut/ithrl/issues/64) | Bounded source projections, matching/report contracts and `inspect reacquire`; experimental reconstruction support is unchanged |
| Released in v0.4 | [#65: qualify passive inspection](https://github.com/eberkut/ithrl/issues/65) | Ordinary sources, counterexamples and both installed-artifact origins validated; worked interpretation recorded |
| Released in v0.4 | [#67: performance and simplification](https://github.com/eberkut/ithrl/issues/67) | Project-wide performance fixes, permanent local-workload benchmarks and durable coding guidance, merged in [PR #71](https://github.com/eberkut/ithrl/pull/71) |
| Released in v0.4 | [#68: documentation and writing guidance](https://github.com/eberkut/ithrl/issues/68) | Project-wide documentation corrections and concise writing guidance, merged in [PR #72](https://github.com/eberkut/ithrl/pull/72) |

## Completed: v0.5 investigation reports and comparative debugging

Implementation and qualification for the
[v0.5.0 milestone](https://github.com/eberkut/ithrl/milestone/4) are complete. The milestone covers
broader ordinary-workflow evidence, public benchmark feasibility, self-describing sampled reports,
cost estimates, a first-party agent skill, comparative debugging and localized tool-output
interventions. The comparison workflow binds two explicit contexts to one immutable source, with
repair/degradation objectives and independent fresh confirmation in
[#88](https://github.com/eberkut/ithrl/issues/88). Constructed and ordinary Codex/Copilot
investigations qualified the tested profiles in [#89](https://github.com/eberkut/ithrl/issues/89);
the [worked comparisons](docs/research/comparative-debugging.md) record their costs and scope.

| Scope | Issue | Expected outcome |
| --- | --- | --- |
| Completed in [#82](https://github.com/eberkut/ithrl/pull/82) | [#16: broader workloads and costs](https://github.com/eberkut/ithrl/issues/16) | Published the frozen cohort's limiting outcome, selection funnel, developer conclusions, recorded costs and explicit gaps |
| Completed in [#83](https://github.com/eberkut/ithrl/pull/83) | [#81: public debugging benchmark](https://github.com/eberkut/ithrl/issues/81) | Published two reviewed tasks and a patch grader; Codex/Copilot reductions reached fresh confirmation, with limiting outcomes and costs recorded |
| Completed in [#84](https://github.com/eberkut/ithrl/pull/84) | [#73: sampled inspection metadata](https://github.com/eberkut/ithrl/issues/73) | Offline reports explain digest-bound reconstruction and policy declarations separately from recorded attempt evidence |
| Completed in [#86](https://github.com/eberkut/ithrl/pull/86) | [#77: model cost estimates](https://github.com/eberkut/ithrl/issues/77) | Offline pricing with genai-prices, usage/rate provenance and explicit coverage; estimates and scenarios remain distinct from actual billing |
| Completed in [#87](https://github.com/eberkut/ithrl/pull/87) | [#78: first-party agent skill](https://github.com/eberkut/ithrl/issues/78) | One portable skill source, Python-package and repository distribution, installed-copy validation and concise installation guidance |
| Completed in [#90](https://github.com/eberkut/ithrl/pull/90) | [#13: comparative debugging](https://github.com/eberkut/ithrl/issues/13) | Explicit source-bound contexts, repair/degradation objectives, constructed repair tasks and useful ordinary investigations |
| Completed in [#90](https://github.com/eberkut/ithrl/pull/90) | [#88: explicit-context comparison](https://github.com/eberkut/ithrl/issues/88) | Comparison CLI, two-arm calibration, fresh confirmation, accounting, recovery and offline reports |
| Completed in [#90](https://github.com/eberkut/ithrl/pull/90) | [#89: qualify comparative debugging](https://github.com/eberkut/ithrl/issues/89) | Constructed and ordinary Codex/Copilot investigations through the normal CLI and installed artifacts, with successful and unsuccessful costs and explicit limits |
| Completed in [#91](https://github.com/eberkut/ithrl/pull/91) | [#74: tool-output interventions](https://github.com/eberkut/ithrl/issues/74) | Exact span deletion for final-response comparisons, with native input controls, practical review and complete study costs in the [worked investigations](docs/research/tool-output-interventions.md) |

## Planned v0.6: context explanation and portable investigations

The [v0.6.0 milestone](https://github.com/eberkut/ithrl/milestone/5) groups ablation,
bounded alternative reproducers and two-component interaction investigations, optional confirmed
single-removal minimality, and portable case/result archives. These are planned capabilities;
the supported-inputs table remains the current public claim.

| Workstream | Design | Implementation |
| --- | --- | --- |
| Comparison preparation and full-original/retained-context ablation | [#61](https://github.com/eberkut/ithrl/issues/61) | [Preparation and ablation](https://github.com/eberkut/ithrl/issues/98) |
| Bounded alternative reproducers | [#62](https://github.com/eberkut/ithrl/issues/62) | [Discovery and confirmation](https://github.com/eberkut/ithrl/issues/99) |
| Declared two-component interactions | [#62](https://github.com/eberkut/ithrl/issues/62) | [Interaction investigation](https://github.com/eberkut/ithrl/issues/100) |
| Optional confirmed single-removal minimality | [#63](https://github.com/eberkut/ithrl/issues/63) | [Complete-neighborhood confirmation](https://github.com/eberkut/ithrl/issues/101) |
| Portable archives and deliberate privacy review | [#18](https://github.com/eberkut/ithrl/issues/18) | [Export, import and offline inspection](https://github.com/eberkut/ithrl/issues/102) |

Reviewed designs and cost proposals gate implementation. Interaction and minimality need early
statistical and cost decisions; a limiting result requires an explicit scope decision and does not
qualify a capability. [Native qualification](https://github.com/eberkut/ithrl/issues/103)
owns useful ordinary Codex/Copilot investigations through the normal CLI and installed artifacts.
Archives record external-state requirements and omitted evidence; portability does not imply replay.
Additional harnesses and baseline sharing remain outside v0.6.0. Provider execution and publication
require separate approval.

## Evidence guiding the next investigations

[#16's frozen cohort](https://github.com/eberkut/ithrl/issues/16) completed native controls and
approved measurements. Both primary workloads calibrated, but their selected fields were removed in
coarse search; neither reached fine search or fresh confirmation. The
[canonical report](docs/research/reduction-design.md#prospective-ordinary-workflow-cohort) is merged
in [#82](https://github.com/eberkut/ithrl/pull/82). Its retained evidence informed comparison and
tool-output workload selection; their own studies establish the qualified workflows.

[#81's public task package](research/benchmarks/README.md), merged in
[#83](https://github.com/eberkut/ithrl/pull/83), recommends iniconfig as a supplementary
constructed workload. Bounded Codex and Copilot CLI reductions each completed fresh confirmation;
both coarse sweeps remain incomplete. Migration-linter remains a baseline-calibration limiting
case. The report owns outcomes, costs and maintenance requirements. Broader tasks, changed profiles
and additional harnesses require separate evidence; constructed results do not establish
ordinary-workflow usefulness.

[ctx](https://github.com/ctxrs/ctx/blob/33ba4645b620fd89d138c6db1fabf33096a0ee16/docs/search.md)
can help locate further sources, but ithrl must independently snapshot and validate native
evidence. Future ordinary cohorts must freeze selection rules before inspection and measure useful
developer conclusions alongside reduction, reliability and cost.

The [comparative-debugging study](docs/research/comparative-debugging.md) establishes useful
context-bundle effects under the tested Codex and Copilot profiles. Broader ordinary cohorts can
assess how often comparison helps; historical contrasts alone cannot establish those effects.

The review of
[SkillOpt's training method](https://github.com/microsoft/SkillOpt/blob/45f65ca04f5f6713bca7b9797b495696ba812751/docs/guide/training-loop.md)
and
[Sift's tool-output benchmarks](https://github.com/ctxrs/sift/blob/c22fb1b3747352dba8f8323c057d60e7afd92bf5/benchmarks/results/2026-09-21-release-v0.4.0/README.md)
motivated better candidate selection and tool-output investigation. Their task-performance and
output-size results do not establish ithrl's debugging usefulness. No search engine or skill
optimizer is planned. Fuzzing already accepts supplied `stale_text`; further ordinary-workflow
evidence must establish a need for distinct proposals tied to specific fields before creating
further work. Passive inspection retains its qualified exact-comparison contract; decoded or
normalized matching needs a separate policy.

## Deferred work

Additional harnesses and baseline sharing remain outside v0.6.0 without a release assignment.

| Scope | Issue | Expected outcome |
| --- | --- | --- |
| Deferred; no release assigned | [#14: additional harnesses](https://github.com/eberkut/ithrl/issues/14) | Assess Claude Code, OpenCode and Pi before choosing an integration order and qualification scope |
| Research; no release assigned | [#17: baseline sharing](https://github.com/eberkut/ithrl/issues/17) | Reconsider reuse only if useful complete-workflow savings justify the statistical and recovery complexity |

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

### Additional harnesses

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

### Further capability candidates

- Further mutation operations: supported whitespace changes, independent reordering and adding
  stale or irrelevant observations outside prior-user text. State whether each tests expected
  invariance or introduces intentional stress; a mutator name cannot prove semantic equivalence.
- Optional `record` adapters for future sessions when ordinary saved evidence is insufficient.
  Prior instrumentation must remain unnecessary for supported post-hoc extraction.
- Trajectory runners where faithful execution seams and safe action policies exist. The current
  single-decision experiment does not claim to reproduce a complete agent trajectory.
- Custom command/Python runners and additional provider integrations with the same observation,
  safety, identity and accounting contracts.
- Optional editor/agent integrations and handoff analysis that compose the CLI rather than
  introducing separate experimental semantics.

These candidates have no assigned release. The completed
[broader-workload study](https://github.com/eberkut/ithrl/issues/16) records a bounded limiting
outcome. Further ordinary-workflow evidence and
[baseline-sharing strategies](https://github.com/eberkut/ithrl/issues/17) remain research
opportunities without an assigned release. Synthetic qualification covers a planted workload of
approximately 10,000 estimated input tokens;
[saved-session experiments](docs/measurements.md#saved-session-experiments) add selected larger
contexts, including incomplete and inconclusive outcomes. Neither establishes a general capacity at
100,000 input tokens. Qualification at that scale remains an optional expansion with a separate cost
proposal.

### Planning and release policy

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
