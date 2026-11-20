# ADR 0006: Compare explicit contexts with directed fresh confirmation

Status: accepted, 2026-11-20.

## Context

Reduction asks whether a selected behavior survives context removal. Fuzzing discovers and
simplifies an intervention that decreases its match rate. Neither workflow directly tests two
caller-selected contexts for a declared repair or degradation objective.

An earlier successful answer and a later failure can suggest an intervention, but their requests,
models, tools or environment may differ. A useful prospective test must bind the inputs actually
compared without treating a transcript difference as a historical cause.

## Decision

Compare two explicitly selected contexts derived from one immutable source case, with the same
target or grader, fixed trigger, execution profile, policy and resource limits. Each context has its
own source-bound candidate identity. Either context may omit supported material. A historical donor
capture is optional proposal evidence; its identity remains separate from experimental observations.

Use existing supported context operations and native admission. Restoration selects material
already present before the captured decision input. It does not add native records, edit opaque
state or import later answers into the model input.

Keep capture-time provider and model identity in the source. An explicit prospective Codex profile
may select a different provider and model while omitting complete historical reasoning records
from both execution copies. Bind and report that common projection separately from the tested
context edit. It preserves the raw evidence and fixed trigger; it establishes no equivalence to
the historical reasoning state.

Declare the objective before calibration: repair estimates candidate minus reference match rate;
degradation estimates reference minus candidate. Order the exact one-sided bounds by that declared
direction. Keep the target's meaning fixed; negating an outcome is not a repair predicate.

Freeze both contexts and calibrate them separately from confirmation. After the calibration gate,
persist one final pair and collect fresh fixed samples. Reuse the shared collector, whole-block
admission, dispatch reservation, reconciliation and conservative accounting under an explicit
comparison procedure. Bind direction and both materializations in state and results. Preserve
existing reduction and fuzzing validation rather than interpreting their checkpoints as comparison
state.

Confirm one directed effect with the exact binomial bounds used for change confirmation, splitting
the declared error level between the positive-arm lower bound and negative-arm upper bound.
The guarantee assumes independent, stationary valid observations and a prespecified eligibility
policy. Calibration supplies no confirmation observations; incomplete samples and operational
errors do not establish an effect. Recovery finishes the same frozen work and never redispatches
an unresolved attempt or repeats completed confirmation.

## Alternatives

| Alternative | Limitation for this purpose |
| --- | --- |
| Use reduction or fuzzing unchanged | Their candidate selection, calibration and final-result semantics answer different questions. |
| Compare arbitrary session bundles | Differences in task, trigger, policy or hidden inputs can confound a context effect; separate source identities alone provide no comparability contract. |
| Build a separate sampling and accounting engine | Duplicates dispatch, budget and recovery rules that already have shared owners. |
| Require a matched historical success before testing a failure | Excludes useful prospective repair hypotheses; a common frozen experimental profile can answer a narrower question. |

## Consequences

Reports describe the complete tested intervention, directed effect and execution conditions.
A bundle effect does not establish a unique historical cause, component necessity, minimality or
statistical interaction. Other candidate changes and repeated user campaigns receive no shared
error guarantee.

Provider-free controls establish software behavior. Public experimental support still requires
useful ordinary Codex and Copilot investigations through the normal CLI and installed artifacts,
with input visibility, action denial, protected state, fresh confirmation and costs including
unsuccessful work and explicit accounting gaps.

This extends [ADR 0002](0002-fixed-samples-and-durable-dispatch.md) without introducing observation
sharing or replacing the strict fresh-baseline decision in
[ADR 0003](0003-strict-fresh-baselines.md). [Issue #13](https://github.com/eberkut/ithrl/issues/13)
owns the design, [#88](https://github.com/eberkut/ithrl/issues/88) implements it, and
[#89](https://github.com/eberkut/ithrl/issues/89) owns qualification. The
[comparison research](../research/comparative-debugging.md) records the method and evidence limits.
