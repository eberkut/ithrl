# Research

The research asks whether controlled context interventions can produce useful, inspectable agent
experiments at a defensible cost. Each study states its workload, method, evidence and limits.
The [README](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs) lists supported
capabilities. A research result can precede a supported integration.

| Study | What it establishes |
| --- | --- |
| [Feasibility and portability](feasibility.md) | Historical evidence and reconstruction mechanisms across four harnesses, including negative and unassessed paths |
| [Reduction design](reduction-design.md) | Statistical choices, failed cost/usefulness combinations, fresh confirmation, baseline-sharing alternatives and remaining measurement limits |
| [Native measurements](../measurements.md) | Synthetic qualification, saved-session outcomes, confirmation, execution failures and measured costs |
| [Context fuzzing](context-fuzzing.md) | A bounded synthetic discovery/simplification/confirmation design, exact inference and validity sensitivities |

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
Studies report tested domains, criteria, seeds, complete-workflow costs, alternatives and
limitations needed to interpret their results. [Issues](https://github.com/eberkut/ithrl/issues)
track follow-up work; [decision records](../decisions/index.md) explain adopted designs.
