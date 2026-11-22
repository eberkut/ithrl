# Localized tool output interventions

These investigations tested whether removing captured trust-policy text changes a role-assumption
answer and whether removing the first error excerpt changes diagnosis of a CI retry. Both completed
fresh final-response confirmation and practical review. The trust-policy result received narrower
acceptance because its literal predicate did not always agree with practical review; the CI result
passed both checks. [Issue #74](https://github.com/eberkut/ithrl/issues/74) records scope and
acceptance.

## Source and intervention

Both arms share one immutable session, fixed task/trigger, predicate, rubric, execution settings
and dependency-closed partition. The reference is unmutated; the candidate deletes one span.
Common omissions condition the effect on that retained context.

| Investigation | Tested profile | Candidate deletion | Common omissions |
| --- | --- | --- | --- |
| Configured direct trust | Codex 0.171.0; OpenAI `gpt-6-luna`; Python 3.12 | Complete 2,036-byte text field in an eight-block tool output | Ten records: two earlier answers and four call/result pairs |
| Initial CI failure | Copilot 1.0.104; SDK 1.0.28; GitHub Copilot `gpt-6.1-sol`; Python 3.12 | 1,479-byte excerpt from a 2,230-byte field | Seventy dependency-closed records, including duplicate occurrences of the error code |

| Execution setting | Configured direct trust | Initial CI failure |
| --- | --- | --- |
| Per-attempt request/time ceiling | 3 requests; 120 seconds + 3 seconds cleanup | 1 request; 180 seconds + 3 seconds cleanup |
| Run request/time ceiling | 138 requests; 3 hours | 50 requests; 4 hours |
| Observation freshness | 3 hours | 4 hours |
| Eligibility policy | `independent-safe-preaccept-only` | `safe-provider-transport` |
| Confirmation valid target / maximum attempts, per arm | 20 / 20 | 20 / 22 |

Neither run set a reasoning-effort override; both native input controls delivered `high`.

The access rubric requires the old role's configured direct grant and its absence from the
replacement role. The literal predicate checks for the captured old-role name verbatim, not the
explanation of its grant. Matching attached permissions does not establish that grant or live
access. Execution omitted historical reasoning from both inputs, as recorded in the profile.

The CI rubric distinguishes the first Azure polling `InternalServerError` from the retry's
already-exists/import error. The excerpt contains both initial error-code copies.
Correctness requires their ordering without inventing an underlying platform cause; the literal
predicate checks only the code.

## Native delivery and safety

Provider-free controls tested the full source, reference, candidate and candidate with the edit
undone. They forwarded zero inference requests and verified fixed input, call identity, policy,
protected state and cleanup. Authentication and
catalogue connectivity remained available. Action-refusal controls left canaries absent; native
dispatch refusal was not counted as a permission-denial event.

Deleting the complete field caused reconstruction to omit its empty content block; the other seven
blocks retained their order. Exact content mapping used an explicit projection for regenerated
prefix identities and session/cache metadata.
Historical call IDs, other content, generation/policy fields and undo remained exact.

For the excerpt, seventy source omissions mapped to 61 delivered-item removals. Only the expected
splice differed between arms; task, fixed input, surrounding text, envelope and undo were exact.
The candidate retained zero explicit copies of the selected error code.

## Sampling and practical review

Calibration requires three valid observations per arm: reference at least 2/3, candidate at most
1/3. Confirmation requires twenty fresh per arm, degradation direction, `alpha=.05` and minimum
effect `.10`. The bound concerns reference-minus-candidate literal-match probability under
independent, stationary valid observations. Earlier trials and calibration supply no confirmation
credit. Arm-blinded ratings freeze before unblinding; disagreements leave automatic counts and
ratings intact.

| Run | Fresh confirmation: literal reference / candidate | Practical reference / candidate | Reviewed disposition |
| --- | --- | --- | --- |
| Configured direct trust | 19/20 / 0/20; difference .95; lower bound .5828337662448868 | 18/20 / 0/20 correct; three proxy disagreements | Original agreement gate failed; explicit narrower acceptance |
| Initial CI, interrupted run | 16/16 / 0/18 valid; one SDK failure; no bound | All 34 valid classifications agree | Incomplete; no confirmation claim |
| Initial CI, fresh run | 20/20 / 0/20; difference 1; lower bound .6631330580338293 | 20/20 / 0/20 correct; 40/40 agreement; none unassessable | Complete; both gates passed |

The accepted access finding supports retaining this field for configured-grant diagnosis. The
failed agreement gate and three disagreements remain recorded: two incorrect reference answers
matched the name, while one correct answer abbreviated it and missed the literal predicate. The
literal bound supplies no semantic-grader guarantee.

The CI finding supports preserving the first failure excerpt when diagnosing retries. The earlier
incomplete block supplied no confirmation. Separate effort pilots had no practical positive,
despite low-effort literal reference matches of 3/3; their observations were not pooled.

## Complete study cost and limiting outcomes

Localized edits to the
[isolated-test-command recipe](comparative-debugging.md#ordinary-investigation-selecting-an-isolated-test-command)
also failed calibration and remain in the cost ledger.

| Paid scope | Attempts / valid | Actual requests | Reported input + output tokens | Outcome |
| --- | ---: | ---: | ---: | --- |
| Access, original 800-byte span | 6 / 6 | 6 | 728,556 | Candidate 3/3; agreement 5/6; failed |
| Isolated command, 463-byte recipe | 6 / 6 | 6 | 491,736 | Reference 1/3; failed |
| Access, one-request cap | 1 / 0 | 1 | 85,055 | Native continuation blocked; no behavioral credit |
| Access, conditioned whole-field pilot | 6 / 6 | 10 | 860,198 | Calibration passed |
| Isolated command, full recipe/rationale field | 6 / 6 | 6 | 490,707 | Reference 1/3; failed |
| CI excerpt, original calibration | 6 / 6 | 6 | 205,799 | Calibration passed |
| Access, fresh calibration + confirmation | 46 / 46 | 77 | 6,629,481 | Scoped acceptance above |
| Interrupted CI confirmation only | 35 / 34 | 35 | 1,161,107; one unknown-usage attempt | Incomplete |
| CI / Copilot `gpt-6-luna`, initial capture | 1 / 0 | 1 | Unknown | Capture limit reached; no valid response |
| CI / Copilot `gpt-6-luna`, native high effort | 6 / 5 | 6 | 178,446; one unknown | Timeout; 0/2 valid reference diagnoses correct |
| CI / Copilot `gpt-6-luna`, explicit low effort | 6 / 6 | 6 | 191,317 | 0/3 reference diagnoses correct |
| Fresh CI calibration + confirmation | 46 / 46 | 46 | 1,575,833 | Both gates passed; no spare attempts used |

Deduplicated totals are **206 actual/charged requests, 171 attempts, 167 valid observations,
12,598,235 known input/output tokens and three unknown-token attempts**, with zero additional
possible or pending requests. Summed runner time is 9,370,232 ms (156.2 minutes). The recorded CI
run window was 3,278.9 seconds. Calibration and confirmation commands took 469.7 and 2,728.0
seconds, respectively. Interrupted CI excludes six calibration calls already counted above. The
cap-one failure's possible upper bound is covered by its known call, not charged twice.

Synthetic control tokens are not paid usage. Preparation/review time, historical case selection and
billing remain outside known runner/token totals. Unsuccessful investigations and operational
failures remain included in the totals. The
[opt-in eligibility policy](../comparison.md#choose-direction-and-samples) permits only evidenced
output-free failures within frozen spare slots; its conditional validity assumptions do not turn
errors into behavioral losses. The final CI workflow needed no replacement.

## Execution evidence and scope

Both investigations used the normal CLI and bound installed artifacts. Completed-run resume left
observations and result/state identities unchanged, started no harness and made zero calls.
Offline inspection verified source selections. Every paid scope retained unchanged protected
sources/workspaces, verified cleanup and zero executed actions.

The [comparison guide](../comparison.md), [results reference](../results.md) and
[earlier bundle study](comparative-debugging.md) own workflow, interpretation and source selection.
Qualification covers the declared final-response edits; denied-shell bundle comparisons remain
separate.
