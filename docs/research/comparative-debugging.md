# Comparative debugging: explicit contexts and directed effects

Comparative debugging asks whether a particular context difference changes a fixed behavior under
one execution profile. An earlier successful answer and a later unsuccessful answer supply a
hypothesis; their difference alone does not explain the regression.

The implemented workflow compares two explicitly selected contexts from one immutable source. It
supports repair and degradation objectives, using the shared collector for calibration and fresh
confirmation. Constructed repair tasks and ordinary Codex and Copilot investigations qualified the
tested profiles. The ordinary results support retaining a diagnostic batch for test-command
selection and restoring a policy-read bundle for a role-assumption answer. They establish useful
effects of those complete bundles under their declared profiles.

This study records the method, controls, outcomes, costs and interpretation limits for
[#13](https://github.com/eberkut/ithrl/issues/13). The
[comparison guide](../comparison.md) owns command usage and configuration.

## Comparison contract

Keep two questions separate:

- **Historical contrast:** what differs between the captured decisions, and what can the saved
  evidence establish about their inputs and outcomes?
- **Prospective intervention:** does a source-bound edit change the probability of a fixed target
  when both arms use one immutable case and the same current execution profile?

The second question can remain useful when the historical contrast is confounded. Report that
change of question explicitly. It does not explain an earlier model or runtime's behavior.

Bind both contexts to one preserved source, target and execution profile, with a separate candidate
digest for each context. The source contains the full captured evidence; either selected context
may omit supported material. Restoration selects bytes already present before the decision's input
boundary. It does not insert arbitrary native records or add knowledge obtained after that decision.
The prospective predicate must keep the captured target category, but need not match the historical
response. Its identity is bound independently of the preserved decision and remains fixed in both
arms.

A second historical capture is optional proposal evidence. Preserve its identity and map any used
difference explicitly, but do not require its source digest to equal the anchor's. Different digests
establish provenance, not a behavioral confounder by themselves. Historical outputs receive no
confirmation credit. Direct comparison of two independent source bundles would need a separate
contract; do not merge their identities or weaken the existing collector's checks.

| Factor | Requirement for an isolated context comparison | When it is unmet |
| --- | --- | --- |
| Task and target | Same debugging question and deterministic success predicate, with the same normalizer or fixed grading bundle | A changed task or grader is a separate experiment; do not count its response as the opposite arm |
| Trigger | Identical fixed triggering input, including a completed-tool continuation where applicable | Refuse the isolated-context claim; do not rewrite the trigger to make a pair |
| Provider and model | Both experimental arms bind the same routing and requested model; verify returned identities where reported | Refuse drift during the experiment; a capture-time model change remains a historical confounder |
| Runtime and request surface | One verified current native runtime, reconstruction method and capability profile for both arms | Reconstructing both captures with a newer runtime tests that runtime, not historical equivalence |
| Instructions, tools and policy | Same effective instructions, tool schemas and action-denial policy, except for an explicitly supported intervention | Frozen differences outside the edit domain cannot be silently discarded or treated as tested edits |
| Environment | Same protected workspace snapshot and other declared readable inputs; no uncontrolled tool or external-state access | Refuse execution if required inputs or protection cannot be established; a matching workspace path is insufficient |
| Visible context | Every difference in the delivered reconstruction is accounted for by the edit or explicitly excluded native bookkeeping | Refuse an exact-mapping claim if extra assistant history, tool outputs, images or transformed text differ |
| Opaque state | Preserve the anchor's opaque records, or declare one supported common reconstruction projection | Different opaque state prevents exact historical alignment; a declared projection tests the resulting execution profile |

Requested model names may denote mutable deployments. Matching names do not prove immutable weights
or stationarity. Bind observable identities, bound the collection window and retain that assumption.
Native IDs and timestamps can differ between captures; do not erase them from source evidence.
Any comparison projection must be owned by the relevant harness and explain each excluded field.
It must never equate opaque data or meaning on the basis of normalized text.
When the selected Codex model or provider differs from capture, reconstruction omits complete
historical reasoning records, including summaries, from both execution copies. The source retains
those records. The comparison tests the resulting visible context; it does not replay historical
reasoning state.

Pure admission does not establish native resume eligibility. Validate the immutable original before
attributing a candidate failure to its edit. Copilot's
[`permission.completed` event](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/streaming-events#permissioncompleted)
records a resolved permission request, not a completed tool execution. Check the tool request and
result against the selected boundary; preserve unexplained bookkeeping rather than removing it to
make a replay succeed.

Apply a refusal to the claim it invalidates. A changed historical trigger, model or opaque state
prevents attributing the full captured contrast to one edit. It need not prevent a narrower
prospective hypothesis on a frozen anchor if both experimental arms satisfy the contract. Missing
historical information is unknown, not equality. Neither finding authorizes native or provider
execution. A report can identify the conflicting factor and the next evidence check.

## Method

A comparison starts with one private proposal attached to an existing case:

1. Snapshot the source and any donor capture. Record the debugging question, observed failure or
   success and substantive predicate before live observations. A historical opposite-outcome
   counterpart is helpful but optional; an identifier mention is not correctness.
2. Freeze the reference and candidate contexts, their source-bound partitions and edits, and the
   intended direction. Keep the trigger, dependencies, target and unedited inputs fixed. Record
   donor provenance separately.
3. Check reconstruction admission for both contexts before reserving sampling work. Require
   input-visibility, protected-state and action-denial controls for the exact live package before
   requesting paid execution.
4. Calibrate the two arms under one frozen profile. Historical outputs receive no sample credit.
   Stop if the contrast fails its gate, evidence violates the frozen eligibility policy, or the
   package changes.
5. Durably select the frozen pair, collect fresh independent fixed samples and report the
   prespecified directed rate difference. Any preceding search or simplification must use separate
   observations and finish before selection.

Declare the objective before calibration and keep the target's meaning unchanged:

| Objective | Directed difference | Positive arm for the lower bound |
| --- | --- | --- |
| Repair | `p(candidate) - p(reference)` | Candidate |
| Degradation | `p(reference) - p(candidate)` | Reference |

The same exact one-sided method computes `lower(positive) - upper(negative)`. Swap the samples and
their required counts when changing direction; negating a verdict or a point estimate does not
reverse a one-sided confidence bound. A confirmed repair increases the fixed target's valid-match
probability. Do not turn arbitrary nonmatches into a repair predicate.

Record removal, paragraph removal, one-field append and
[source-bound tool output text spans](../comparison.md#select-tool-output-text) define the
implemented edit surface, subject to each profile's admission rules. A coarse-removal context and
the full source can supply a restoration test. Removing either record from a bundle and restoring
both are distinct tests; report the complete tested change before attempting component isolation.

For a prefix-preserving append, exact mapping requires the donor's effective prior-user field to be
the anchor's exact text plus the declared suffix, and all other relevant delivered inputs to match.
Supplying a phrase copied from an unrelated failure is a stress test, not reproduction of the
captured difference. The [localized tool output study](tool-output-interventions.md) reports exact
field edits and their separate native and practical qualification. Instruction changes require
their own intervention design; prospective execution settings are bound before collection through
the [shared configuration](../comparison.md#choose-execution-settings).

Sampled fuzzing already tests and simplifies supplied append text. Use it when its generation and
selection behavior answer the question. Exact-context comparison instead freezes the two contexts
directly. Fuzzing calibrates the original, generates placements and simplifies a selected mutation;
comparison calibrates both selected contexts and confirms their declared contrast.

The comparison procedure binds the reference selection explicitly; reduction and fuzzing retain
their full-source baseline. Both contexts have source-bound materialization, two-arm calibration
and comparison progress in shared sampled state. The comparison identity binds both context
digests, target, profile, objective and frozen statistical policy; changing arm labels cannot
reinterpret retained observations. [Architecture](../architecture.md#explicit-context-comparison)
maps materialization, collection, accounting and result ownership.

## Qualification method

Validation combines provider-free contract checks, constructed repair tasks and ordinary engineering
investigations. Contract checks cover source/plan bindings, fixed-trigger protection,
materialization, directional statistics, invalid evidence, recovery and report interpretation.
Native controls verify the delivered inputs, action-denial boundary, protected state and cleanup for
the selected profile. Reuse requires matching identities or a reviewed equivalence proof for the
changed components.

Each qualified investigation freezes its question, predicate, complete intervention, direction and
practical review criteria before collecting its qualifying observations. Exploratory outcomes can
suggest a new hypothesis; they remain recorded and receive no sample credit for that hypothesis.
A historical successful counterpart is optional. Both experimental arms must use the same current
execution profile, even when that profile differs from capture-time conditions.

The reported qualifications use three calibration observations per arm, requiring at least two
positive-arm matches and at most one negative-arm match. Successful calibration precedes durable
selection and twenty fresh observations per arm. Any invalid or erroneous attempt stops these
packages; no overload replacement, repeated confirmation or search is included. Their private
execution packages bind physical-request ceilings, attempt limits, cleanup grace, command deadlines
and freshness. Admission uses the implemented budget calculation, including any pacing.

Confirmation uses the [exact change bounds](context-fuzzing.md#confirmation-method-and-assumptions)
at `alpha=.05` and minimum effect `.10`. Twenty matches versus zero gives a lower difference bound
of `0.6631330580`. Under independent, stationary valid outcomes, exact enumeration gives 98.47%
confirmation probability at rates `.90/.10` and 59.59% at `.80/.20`. Including the calibration gate,
those probabilities are 93.03% and 47.84%, assuming controls and all attempts remain valid. These
sample counts target a strong contrast rather than every directed effect above `.10`.

The error level applies to each fixed comparison; it supplies no combined guarantee across cases.
Operational errors remain outside behavioral denominators and retain their usage and conservative
call charges. Finite token or monetary caps require an enforceable estimator. Recorded usage and
reference-price estimates do not establish billed charges or complete investigation effort.

The constructed and ordinary investigations below supply the qualification evidence for the
implemented workflow. They establish useful bundle effects under their bound profiles, not
performance rankings between harnesses or a frequency estimate across ordinary sessions.
Implementation and qualification are tracked in [#88](https://github.com/eberkut/ithrl/issues/88)
and [#89](https://github.com/eberkut/ithrl/issues/89).

## Constructed controls and repair tasks

An ordered pair of paragraphs in one eligible field supplies the minimum planted interaction:

| Added paragraphs | Deterministic fixture target |
| --- | --- |
| None | Match |
| `ONE` | Match |
| `TWO` | Match |
| `ONE`, then `TWO` | Nonmatch |
| `TWO`, then `ONE` | Match |

The existing ordered-interaction fixture materializes source-bound appends through the deterministic
evaluator and keeps the full ordered pair during simplification. It also checks that a runner
error does not become a changed-behavior result. These are constructed software controls, not
model observations or evidence of an ordinary regression.

The report must preserve the whole tested bundle and order. Non-significant singleton tests would
not establish absence of a singleton effect in native observations. A confirmed bundle effect is
not a statistically established interaction; that stronger claim needs a separate factorial
estimand, sample plan and multiplicity allocation. The constructed controls keep singleton checks
provider-free; native confirmation tests the selected bundle. Native singleton investigations need a
separate budget and claim.

The
[iniconfig investigations](https://github.com/eberkut/ithrl/tree/main/research/benchmarks#investigations-through-the-normal-cli)
provide a concrete constructed contrast for each harness: three original repairs passed grading,
while three coarse-candidate responses were rejected before either verifier ran. The coarse edit
removed two records, including the task field. Those exploratory observations motivate a restoration
proposal; they neither confirm a repair effect nor identify which record mattered. The later
11-per-arm confirmation concerns preservation by a different, fine-removal candidate and supplies no
repair-confirmation credit.

The restoration comparisons used the exact retained coarse partition and full source, preserving
the trigger and grader. The fine candidate also removes text and would test a different change.
Changed-defect inputs have different source/grading bindings and remain comparability controls.

Fresh native comparisons tested the coarse/full pair through the installed `ithrl compare` CLI,
with one bound model and execution profile per harness:

| Harness | Coarse calibration | Restored calibration | Coarse fresh confirmation | Restored fresh confirmation |
| --- | ---: | ---: | ---: | ---: |
| Codex | 0/3 | 3/3 | 0/20 | 20/20 |
| Copilot | 0/3 | 3/3 | 0/20 | 20/20 |

Each cell shows successful repairs over valid attempts. Both directed lower bounds were `0.663` at
`alpha=0.05`, exceeding the prespecified `0.10` minimum effect. Calibration received no confirmation
credit. All 92 inference requests completed with known usage, totalling 496,673 recorded input and
output tokens; these are usage counts, not billed charges. A separate Copilot transport failure
stopped before inference and received no sample credit. No returned actions executed;
source/workspace protection and cleanup checks passed. Offline inspection revalidated both results
against their source cases.

This confirms restoration of the complete tested bundle under those profiles. It does not isolate
a component, compare the harnesses' performance or establish ordinary-workflow usefulness.

### Provider-free validation

The [local comparison example](../comparison.md#try-without-a-provider) uses the installed CLI and
bundled marker fixture. Restoring both required inputs matches the target; either singleton does
not. The planted outcomes demonstrate the workflow and supply no native qualification evidence.

Installed-package checks exercise calibration, fresh confirmation, resume and offline inspection.
Focused regressions cover directional bounds, invalid evidence, source/plan bindings and fixed-input
protection. Exact native input mapping requires separate controls for the selected profile.

## Ordinary investigation: selecting an isolated test command

An ordinary Copilot session supplied a test-fixing request and an earlier diagnostic batch that
documented an isolated `uv run` procedure for two affected test modules. The debugging question was
whether that batch changed selection of the documented procedure. The full reference retained it;
the candidate removed the complete 15-record batch. Both contexts preserved the same completed-tool
checkpoint, task, native profile and protected workspace. Input controls accounted for the exact
batch difference and verified that the recipe reached the reference's model input.

The frozen target matched a native shell action containing ` --isolated `. A separate practical
rubric required an actual `uv run` invocation with `--isolated` before the executable and `pytest`
covering both affected modules. Quoted mentions, misplaced options and different test selections did
not qualify. An arm-blinded reviewer classified all six calibration and 40 confirmation responses
before seeing their arm or automated outcome; every classification agreed with the target.

The installed CLI used Copilot 1.0.104 with `gpt-6.1-sol`:

| Context | Calibration | Fresh confirmation |
| --- | ---: | ---: |
| Full diagnostic batch | 2/3 | 17/20 |
| Batch removed | 0/3 | 0/20 |

All attempts were valid. The directed difference was `0.85`; its one-sided lower bound was
`0.45264` at `alpha=0.05`, exceeding the frozen `0.10` minimum effect. Calibration received no
confirmation credit. The 46-request qualification recorded 3,713,767 input and output tokens,
1,045 runner seconds and 1,185 elapsed seconds. Including 12 preliminary exploratory requests,
this ordinary Copilot investigation used 58 requests and 4,682,835 recorded tokens. Billing and
complete preparation effort remain unknown; no token or monetary cap was claimed.

All 186 returned actions were denied, source/workspace protection and cleanup checks passed, and
offline inspection plus a completed-run resume verified retained results without redispatch.
No returned command ran, so the result establishes selection of the procedure, not its execution,
test success or actual environment protection.

Retain the complete diagnostic batch when preparing this investigation. A native diff locates the
recipe; the comparison establishes the batch's effect on the continuation under the bound profile.
It does not isolate a single error or record, prove the necessity of `--isolated`, or make other
test commands incorrect. This one ordinary Copilot case does not establish how often comparison
helps across sessions.

## Ordinary investigation: reporting a role-assumption restriction

An ordinary Codex session asked whether replacing a domain role would change the permissions used
by an engineering tool. The roles had matching attached policies, while a captured trust statement
granted the old role a direct assumption path to an elevated role. The replacement role was absent
from that grant. Other trust paths existed. The finding concerned this configured direct
role-assumption grant; effective access in a deployed account remained untested.

Both contexts removed earlier answers and preserved the question and completed-tool checkpoint.
The candidate restored six policy-read pairs omitted from the reference. Both retained an earlier
partial excerpt. Native input controls verified the exact 12-record difference and fixed input.
The experiment therefore tested the detailed evidence bundle's effect on reporting the restriction.

The prospective profile used Codex `0.171.0`, Python `3.12.3` and OpenAI `gpt-6-luna`. It omitted
the same 13 historical reasoning records from both execution copies, including their summaries,
while preserving the source. This declared projection tests the visible context under the
prospective profile; it establishes no historical reasoning replay equivalence.

The frozen target matched the elevated role's identifier. An independent arm-blinded rubric required
the correct old-role versus replacement-role trust finding, with no unsupported claim about live
access or all workflows. All six calibration and 40 confirmation classifications agreed with the
target; none was incorrect or ambiguous.

| Context | Calibration | Fresh confirmation |
| --- | ---: | ---: |
| Detailed policy reads omitted | 0/3 | 0/20 |
| Detailed policy reads restored | 3/3 | 20/20 |

All 46 attempts were valid. The directed difference was `1.0`, with a one-sided lower bound of
`0.66313` at `alpha=0.05`, exceeding the frozen `0.10` minimum effect. Calibration received no
confirmation credit. The qualification used 56 physical provider requests, 1,814 runner seconds
and 2,087 elapsed seconds. No actions executed; source/workspace protection and cleanup passed.
Offline inspection verified source bindings, and completed-run resume reused the terminal evidence
without redispatch. The integrated artifact reproduced the same native inputs and effective policy.

Selection and unsuccessful execution remain part of the cost:

| Scope | Model attempts | Recorded request starts | Known input and output tokens |
| --- | ---: | ---: | ---: |
| Qualified role-assumption investigation | 46 | 56 | 5,303,866 |
| Other ordinary Codex eligibility and execution attempts | 11 | 15 | 1,254,608 |
| Total ordinary Codex work | 57 | 71 | 6,558,474 |

The other attempts include a parameter hypothesis with no contrast, an unmet secret-warning
criterion, request-limit failures and a TLS transport failure. They receive no qualification
credit. One failed attempt has unknown token usage and two conservatively charged possible
additional requests. Billing and complete preparation effort remain unknown.

Retain the detailed policy-read bundle when reproducing this answer. A manual source diff
identifies the grant; the experiment establishes the bundle's effect on the
agent's reporting under the declared profile. It does not establish component necessity or
effective permissions in a deployed account.

## Reporting and interpretation

Use the [result guide](../results.md#inspect-a-context-comparison) to interpret outcomes, phase
counts and accounting. Report the exact intervention, matched or unknown execution factors and
recorded native evidence alongside the effect. Include unsuccessful work and distinguish known
usage from unknown effort or billing; keep source text private.

A confirmed restoration supports retaining the tested bundle for that investigation. An
inconclusive result leaves the proposed explanation unestablished under the tested conditions.

Prospective usefulness requires a concrete next action supported by the tested difference or a
specific comparability refusal that prevents a misleading investigation. Record whether a native
diff and manual inspection would already supply that conclusion. Byte reduction alone is not the
comparison's success criterion.

## Research basis

[Zeller's cause-effect isolation](https://www.cs.umd.edu/~atif/zeller.pdf) motivates testing
differences between passing and failing executions, while requiring that the oracle distinguish
the intended failure from another failure. Its deterministic program-state assumptions do not
establish native-model repeatability or recover missing historical inputs.
[ContextCite](https://arxiv.org/abs/2409.00729) studies attribution through context interventions;
redundant information and pretrained knowledge limit interpretation of removal effects. Neither
source establishes that a selected native field will affect the target. The source-identity,
validity and fresh-confirmation contracts remain those documented in
[architecture](../architecture.md#measurement-search-and-confirmation).

[DoVer](https://arxiv.org/html/2512.06749v3#S4) tests interventions on failed agent runs and
evaluates recovery or progress, recognizing that multiple repairs may succeed. Its
framework-specific benchmarks and three-run/LLM-assessed criteria motivate repair objectives, not
ithrl's inference contract.
[LangSmith's evaluation workflows](https://docs.langchain.com/langsmith/evaluation-types) likewise
use fixed cases and explicit evaluators; they provide a workflow analogy, not evidence of native
reconstruction or debugging benefit here.
