# Research

These studies evaluate whether controlled context interventions produce useful, inspectable agent
experiments at a defensible cost. Each records its workload, method, evidence and limits. Start with
the study that matches the question below; use the executable packages to check synthetic results
or repair graders.

Research evidence does not establish product support. The
[supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs)
defines current capabilities, and [native measurements](../measurements.md) record qualification
for specific workloads and profiles.

| Study | Question and evidence |
| --- | --- |
| [Feasibility and portability](feasibility.md) | Historical evidence and reconstruction mechanisms across four harnesses, including negative and unassessed paths |
| [Reduction design](reduction-design.md) | Statistical choices, failed cost/usefulness combinations, fresh confirmation, baseline-sharing alternatives and remaining measurement limits |
| [Prospective ordinary-workflow cohort](reduction-design.md#prospective-ordinary-workflow-cohort) | Selection funnel, exclusions, field-loss outcomes, recorded inference usage and cost gaps |
| [Native measurements](../measurements.md) | Synthetic qualification, saved-session outcomes, confirmation, execution failures and measured costs |
| [Ordinary-session investigation](ordinary-session-investigation.md) | A sanitized Codex debugging question, paragraph intervention, fresh confirmation, cost and limits of interpretation |
| [Passive compaction investigation](passive-compaction-investigation.md) | Ordinary Codex/Copilot repeated and changed captures, source-bound interpretation and provider-free installed-artifact qualification |
| [Context fuzzing](context-fuzzing.md) | A bounded synthetic discovery/simplification/confirmation design, exact inference and validity sensitivities |
| [Public debugging tasks](https://github.com/eberkut/ithrl/tree/main/research/benchmarks) | Pinned tasks, offline repair graders and bounded Codex/Copilot CLI reductions with fresh confirmation |
| [Comparative debugging](comparative-debugging.md) | Source-bound contexts, directed fresh confirmation, constructed repair tasks and ordinary Codex/Copilot investigations, with costs and interpretation limits |
| [Localized tool output interventions](tool-output-interventions.md) | Source-bound field edits, native delivery controls, ordinary final-response confirmation, practical review and complete-study costs |

## Reproduce the synthetic studies

Two source packages contain the models, configurations and reference results:

- [`research/reduction/`](https://github.com/eberkut/ithrl/tree/main/research/reduction): selected
  sampling decisions, baseline sharing, failure/freshness scenarios and native-layout budgets.
- [`research/fuzzing/`](https://github.com/eberkut/ithrl/tree/main/research/fuzzing): numerical
  confirmation checks, discovery, mutation simplification and validity sensitivities.

Each package documents a short check and an opt-in full replay, including their coverage. Both
run without credentials or provider calls. Native measurements are reported separately;
synthetic replay does not repeat model execution.

Analytic guarantees, simulations, fixture checks and live measurements answer different questions.
Read each result with its tested domain, criteria, seeds, complete-workflow costs and limitations.
[Issues](https://github.com/eberkut/ithrl/issues) track follow-up work;
[decision records](../decisions/index.md) explain adopted designs.
