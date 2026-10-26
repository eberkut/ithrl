# Roadmap

`ithrl` tests which context preserves a selected agent decision and which changes alter it.
Development priorities depend on useful results, reliable measurement and measured execution cost.

The [supported-inputs table](README.md#supported-inputs) describes current public capabilities.
The directions below describe future work; their version labels establish an intended sequence,
with scope and release dates decided from evidence.

## Current work: context fuzzing

Version 0.1.0 ships `extract` and `shrink`. Development of `ithrl fuzz` adds bounded context
changes, simplification of an interesting intervention and independent confirmation of its effect.

The first fuzzing milestone aims for **one confirmed finding per campaign**: a declared decrease
in the existing target's match probability among valid observations. A finding describes the
selected experiment, including execution reliability and assumptions. It does not establish a
model defect, a unique cause or semantic equivalence of the original and changed context.

The feasibility study, mutation contracts, deterministic generation and reconstruction, mutation
simplification, sampled comparisons and durable campaign recovery are implemented. The
public command provides a local fake-runner walkthrough, and its composed workflow and recovery
checks have passed. Native text-mutation qualification remains open. See the
[fuzzing research](docs/research/context-fuzzing.md) and the
[fuzzing capability issue](https://github.com/eberkut/ithrl/issues/5) and
[`v0.2.0` milestone](https://github.com/eberkut/ithrl/milestone/1) for implementation work and
acceptance.

Remaining deliverables, in dependency order:

1. [Qualify one native text-mutation workflow](https://github.com/eberkut/ithrl/issues/9) through
   harmless controls and fresh confirmation.
2. [Derive practical settings and public claims](https://github.com/eberkut/ithrl/issues/10) from
   complete-workflow usefulness and measured costs.

[Release preparation and publication](https://github.com/eberkut/ithrl/issues/11) follow acceptance.
They remain separate from implementing the capability. Broader mutation families and multiple-
finding campaigns need their own evidence and prospective error allocation.

## Direction by version

| Stage | Outcome | Candidate features |
| --- | --- | --- |
| **[v0.2: context fuzzing](https://github.com/eberkut/ithrl/issues/5)** | Find small context changes that alter a selected behavior | Bounded seeded mutations, discovery and mutation simplification, one fresh confirmed finding, exact recovery and explicit cost/reliability reporting |
| **[v0.3: deeper reduction](https://github.com/eberkut/ithrl/issues/12)** | Examine how small and robust a reproducer is | Supported content-block/section removal, retained-context inspection, ablation, alternative reproducers, interactions and optional confirmed 1-minimality |
| **[v0.4: comparative debugging](https://github.com/eberkut/ithrl/issues/13)** | Isolate behavior-changing differences between compatible cases | `compare`, context-version delta debugging and another qualified harness integration |
| **[v0.5: trace forensics](https://github.com/eberkut/ithrl/issues/15)** | Explain what information was acquired again | Passive reacquisition analysis around compaction, resume, forks and handoffs |

### Deeper reduction

Reduction within a message must preserve its native structure and protocol dependencies. Retained
context inspection should distinguish dependencies retained for validity, unremovable state and
items whose removal changed the measured behavior.

Retained-item and full-original ablation answer different questions and must report their baseline.
Alternative reproducer search can expose several preserving contexts and interactions; none is
automatically a unique explanation. Deterministic single-removal minimality and statistically
confirmed minimality are distinct claims. The latter requires additional complete comparisons,
error allocation and a budget that covers all advertised checks.

### Comparative debugging and another integration

`compare GOOD_CASE BAD_CASE` should search supported context differences with dependency-valid
interventions. It must establish comparability of target, oracle, model/provider, request surface,
environment and effective runner policy before attributing a regression to context. Differences in
model, harness or execution environment remain explicit experimental factors.

Claude Code and OpenCode have promising portability evidence, described in the
[feasibility study](docs/research/feasibility.md). They require adapters, faithful reconstruction,
isolation, calibration and a useful live workflow before becoming supported integrations. Pi
remains unassessed. [Select the next integration](https://github.com/eberkut/ithrl/issues/14)
from evidence and user value.

### Passive reacquisition analysis

Planned `inspect reacquire` work examines information retrieved before and after compaction,
resume, fork or a cross-harness handoff. Reports should distinguish exact, normalized and heuristic
matches, and check whether source state changed before describing repeated retrieval as avoidable.
Passive analysis should work without provider calls; implementing agent handoffs is separate work.

## Further capability candidates

- Additional mutators: duplication, supported whitespace changes, independent reordering and
  stale/irrelevant observations. Label approximate-invariance expectations and intentional stress
  explicitly; a mutator name cannot prove semantic equivalence.
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
opportunities. The qualified reduction measurements cover a planted approximately 10k workload; 100k
qualification is an optional expansion with a separate cost proposal.

## Planning and release policy

Repository issues own executable scope, acceptance criteria and dependencies. Release milestones
group accepted scope; the [public Project](https://github.com/users/eberkut/projects/1/views/4)
provides views over those same issues. Design rationale lives in
[decision records](docs/decisions/index.md), methods and evidence in
[research](docs/research/index.md), and the current system model in
[architecture](docs/architecture.md). Keep task status in GitHub rather than a second checklist
here.

Before 1.0, project-owned schema tags remain at `1` while formats evolve directly. Breaking changes
are acceptable; current-format validation and evidence identity remain strict. A future stable
release needs stable case/runner/oracle contracts, useful adapters, documented statistics,
portable artifacts, reviewed export privacy, migrations and practical ordinary-case cost evidence.
There is no fixed 1.0 feature list or release date.
