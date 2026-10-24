# ADR 0001: Native evidence and controlled runners

Status: Accepted.

## Context

Coding-agent session stores are not portable conversations. Their records encode harness-specific
tool identities, reasoning and compaction state, provider references and execution metadata.
Normalizing them into one message format would discard distinctions required to reconstruct a
decision and enforce its safety policy. Conversely, preserving an event does not establish that
an experiment can safely execute or edit it.

An extracted decision also has a different lifetime from an experiment. Evidence records what
was captured. Targets, reconstruction settings and statistical procedures describe what to test.
Observations record what happened during attempts. Combining these would allow a runtime support
decision or later outcome to change the meaning of captured facts.

## Decision

Preserve harness-native source bytes and keep four separate contracts: immutable evidence and
edit metadata, experimental definitions, resolved execution plans, and append-only observations.
Result snapshots derive their claims from those contracts.

Adapters own discovery, bounded parsing, native decision selection and compilation of an ordered
context graph. Application extraction writes the case. Each graph node refers to captured source
or an authoritative exact request; stable selectors and content digests protect its identity.
Dependencies and atomic groups constrain legal interventions without creating a universal
agent-session representation. Unknown records remain opaque and frozen until supported.

Concrete runners own reconstruction, isolation, effective policy, native protocols and normalized
results. Shared workflow, graph, reduction and result code remain independent of concrete
harnesses. Runner selection uses an application registry. A candidate describes an intervention
against immutable evidence; it does not carry mutable native runtime state.

Evidence classification, execution support and behavior target remain separate. A logical session
can support controlled native execution without becoming an exact provider request. The absence
of a safe runner does not erase the value of extraction. A controlled reconstruction defines a
runner-scoped experiment and reports its deviations; successful full-context calibration does
not prove equivalence to the historical runtime.

Qualify experimental support for the exact profile through compatible evidence, capability
detection, preventive isolation, effective action denial, calibration and bounded live-use
evidence. Capabilities describe available mechanisms and do not grant permission. A passing
fixture or installed version string cannot replace these gates. Current public support belongs
in the [README supported-inputs table](https://github.com/eberkut/ithrl/blob/main/README.md#supported-inputs),
with measured boundaries in [native runs](../native-runs.md#compatibility-and-measurements).

The shared process runtime owns bounded lifecycle and disposable state, not general filesystem or
network isolation. A concrete runner must prevent forbidden effects before execution and verify
protected state afterward. Returned actions are observations. A native shell-action profile must
retain the action's visibility while denying execution through the effective permission category;
a similarly named emitted tool or inert replacement does not prove that property. Hosted tools,
MCP, web and tool-side network effects need their own safe profiles.

## Consequences

The design retains native semantics and makes unsupported boundaries explicit. Harness-specific
complexity stays in paired adapters and runners, while experiments and results share a common
contract. Adding a new harness requires evidence interpretation and execution qualification
separately; a parser alone supplies extraction support.

Cases can be inspected independently of their source session store, but they are private evidence,
not generally executable portable sessions. Content digests detect inconsistent changes; they do
not protect against a malicious process with the same user identity. Native bodies and tool
arguments stay out of ordinary diagnostics, and authentication is not bundled with evidence.

Pre-1.0 project schemas evolve directly with tag `1`, strict validation and no migration or legacy
reader layer. Regenerating an incompatible artifact must not modify its captured source evidence.
Schema versioning is reconsidered for 1.0.

## Alternatives

A universal normalized transcript would simplify shared editing but hide native identity and
state requirements. Direct replay of every captured request would incorrectly treat logical
session evidence as an authoritative provider request. Executing or rewinding the original
session/workspace would make experimental state disposable evidence. These alternatives do not
meet the evidence and safety requirements.

See the [architecture reference](../architecture.md) for current module ownership and contract
lifecycles. Operational safety and contributor checks belong in
[AGENTS.md](https://github.com/eberkut/ithrl/blob/main/AGENTS.md) and
[CONTRIBUTING.md](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md).
