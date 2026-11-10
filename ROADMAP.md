# Roadmap

`ithrl` tests which context preserves a selected agent decision and which changes alter it.
Development priorities depend on useful results, reliable measurement and measured execution cost.

The [supported-inputs table](README.md#supported-inputs) describes current public capabilities.
The directions below describe future work; their version labels establish an intended sequence,
with scope and release dates decided from evidence.

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

## Current work: v0.3 deeper reduction

Provider-free retained-context inspection and finer removal within supported prior-user text are
implemented, with Codex and Copilot final-response workflows. Ordinary saved-session qualification
and a [worked investigation](docs/research/ordinary-session-investigation.md) are complete; the
[measurements](docs/measurements.md#ordinary-session-paragraph-removal) record outcomes, costs and
limits. Version 0.3 is not yet released.
[Issue #12](https://github.com/eberkut/ithrl/issues/12) owns the milestone's acceptance and scope.

Choose section boundaries, workload-specific usefulness criteria and execution budgets before
provider-backed evaluation. Finer native removal needs its own reconstruction, safety and live-use
evidence. Ablation, alternative reproducers, interaction analysis and statistically confirmed
single-removal minimality are deferred; they are not v0.3 release requirements.

## Direction by version

| Stage | Outcome | Candidate features |
| --- | --- | --- |
| **[v0.2: context fuzzing](https://github.com/eberkut/ithrl/issues/5)** | Find small context changes that alter a selected behavior | Bounded seeded mutations, discovery and mutation simplification, one fresh confirmed finding, exact recovery and explicit cost/reliability reporting |
| **[v0.3: deeper reduction](https://github.com/eberkut/ithrl/issues/12)** | Inspect retained context and reduce within supported prior-user text | Provider-free inspection, text-section removal and qualified Codex/Copilot final-response workflows |
| **[v0.4: comparative debugging](https://github.com/eberkut/ithrl/issues/13)** | Isolate behavior-changing differences between compatible cases | `compare`, context-version delta debugging and another qualified harness integration |
| **[v0.5: trace forensics](https://github.com/eberkut/ithrl/issues/15)** | Explain what information was acquired again | Passive reacquisition analysis around compaction, resume, forks and handoffs |

### Deeper reduction

Reduction within a message must preserve its native structure and protocol dependencies. Retained
context inspection should distinguish protected or frozen material, dependency constraints,
rejected removal trials, incomplete evidence and untested work. Trial explanations must identify
the tested context; a rejected sampled screen does not establish behavioral necessity.

Deferred retained-item and full-original ablation answer different questions and must report their
baseline.
Alternative reproducer search can expose several preserving contexts and interactions; none is
automatically a unique explanation. Deterministic single-removal minimality and statistically
confirmed minimality are distinct claims. The latter requires additional complete comparisons,
error allocation and a budget that covers all advertised checks.

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

Planned `inspect reacquire` work examines information retrieved before and after compaction,
resume, fork or a cross-harness handoff. Reports should distinguish exact, normalized and heuristic
matches, and check whether source state changed before describing repeated retrieval as avoidable.
Passive analysis should work without provider calls; implementing agent handoffs is separate work.

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
