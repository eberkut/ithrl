# ADR 0005: Grade cooperative Python repairs with fixed task tests

Status: accepted, 2026-11-19.

## Context

A response can mention the expected command or identifier without repairing the underlying defect.
Different patches can also solve the same task. Investigating repair behavior therefore needs an
observable outcome beyond response wording or equality with one reference patch.

In the public-task study, an incomplete iniconfig repair passed the source suite but failed an
independent missing-default check. Both suites contribute to the grading outcome.

Executing a proposed repair introduces a separate trust boundary. Native action denial does not
establish that returned code is safe to execute or that its reported test results are trustworthy.
The public debugging tasks need a bounded grading path with explicit assumptions about that code.

## Decision

Allow optional test grading for sampled final-response experiments without a text-matching
predicate. Bind the task source, editable files, fixed source tests, separately authored verifier
tests, prepared Python environment and grading implementation before execution. Associate each
grade with its captured response and attempt.

Apply repairs in disposable state and run the fixed suites with network access denied and
protected inputs read-only. Reserve grading and cleanup within the existing attempt deadline.
Native actions remain observations whose execution is denied by the runner; configuring a grader
does not grant those actions permission.

Assume **cooperative repairs that leave the verifier and its reporting intact**. Patched code runs
in the interpreter that executes test assertions and reports counts. Read-only verifier files
protect their stored bytes but remain readable, and candidate code can interfere with assertions
or reporting in memory. File permissions and report association do not authenticate grades against
hostile code.

A passing grade supplies a behavioral match; a rejected or failing repair supplies a nonmatch.
Keep operational grading errors separate: retain the completed attempt evidence, response digest
and provider charge, exclude the error from behavioral denominators and stop without automatically
requesting another response. The sampling and fresh-confirmation rules in
[ADR 0002](0002-fixed-samples-and-durable-dispatch.md) continue to apply.

## Alternatives

| Alternative | Limitation for this purpose |
| --- | --- |
| Response-text predicates | Establish specified wording, without checking whether a repair passes the task's tests. |
| Equality with a reference patch | Rejects alternative repairs that satisfy the same task. |
| Grading resistant to hostile candidate code | Requires a separate trust design and validation of verifier execution and result reporting. The shared-interpreter design does not provide that guarantee. |

## Consequences

Task grades support investigations of the declared test outcome under the cooperative-repair
assumption. Passing fixed source and independent suites does not establish correctness for all
inputs. Fresh confirmation concerns the selected task, context and execution profile; constructed
task results do not establish ordinary-workflow usefulness.

Changes to bound grading inputs require a new identity; historical responses and grades retain
their original conditions.

This extends the evidence and execution boundaries in
[ADR 0001](0001-native-evidence-and-controlled-runners.md) without replacing them. The
[architecture reference](../architecture.md#measurement-search-and-confirmation) owns grading
mechanics and error classification. [PR #83](https://github.com/eberkut/ithrl/pull/83) implements
this decision; the
[public-task study](https://github.com/eberkut/ithrl/tree/main/research/benchmarks) owns workload
evidence, costs and qualification limits.
