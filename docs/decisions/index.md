# Architecture decisions

These records explain consequential design choices and their alternatives. Update the current
architecture reference when implementation changes; supersede an accepted decision explicitly
when new evidence changes its rationale. Issue and pull-request links record the work itself.

| Record | Decision |
| --- | --- |
| [0001](0001-native-evidence-and-controlled-runners.md) | Preserve native evidence and qualify controlled execution separately |
| [0002](0002-fixed-samples-and-durable-dispatch.md) | Use fixed complete samples, fresh final confirmation and durable dispatch reconciliation |
| [0003](0003-strict-fresh-baselines.md) | Retain strict fresh-original baselines; keep sharing conditional on useful cost/freshness evidence |
| [0004](0004-single-finding-fuzz-confirmation.md) | Confirm one selected fuzz intervention with fresh exact-binomial bounds |

These records consolidate the decisions behind the initial reduction implementation and current
fuzz services. They were written retrospectively from development evidence and checked against
the implementation. Research dates identify the underlying studies; record numbers do not imply
that the files existed at those dates.

See [architecture](../architecture.md) for current ownership, [research](../research/index.md)
for supporting evidence and the [roadmap](https://github.com/eberkut/ithrl/blob/main/ROADMAP.md)
for future direction.
