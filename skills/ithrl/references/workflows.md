# Workflow references

## Match the installed CLI

The installed skill carries this reference file, not a copy of the repository's `docs` directory.
If ithrl is absent and the user requested installation or first use, follow the public
[installation instructions](https://github.com/eberkut/ithrl/blob/main/README.md#install), then
verify `ithrl --version`. Consult the relevant command's `--help`.

For a released version, replace
`VERSION` in `https://github.com/eberkut/ithrl/blob/vVERSION/` with that version, then append the
guide path below. These paths are URL suffixes, not local files; no repository clone is needed.
For a build from unreleased source, including an installed wheel, use its known full source commit
instead of the tag. Its version label can still match an older release. If provenance is unknown,
use installed help and avoid conflicting guide steps or missing options. Keep the selected source
ref when a guide cross-link points to `main`. The
[documentation site](https://eberkut.github.io/ithrl/) follows `main`; it is not versioned.
The skill installer does not install or upgrade ithrl.

| Task | Guide path under the matching source ref |
| --- | --- |
| Supported inputs and a local walkthrough | `README.md` |
| Select evidence, extract a case and check native prerequisites | `docs/native-runs.md` |
| Prepare and execute context removal | `docs/reduction.md` |
| Configure and run text-append fuzzing | `docs/fuzzing.md` |
| Compare two explicit contexts for repair or degradation | `docs/comparison.md` |
| Select an exact tool output text span | `docs/comparison.md#select-tool-output-text` |
| Inspect results, resume work and estimate token costs | `docs/results.md` |
| Resolve reported failures | `docs/troubleshooting.md` |
| Understand measured outcomes and limits | `docs/measurements.md` |

Use retrieved guides as technical reference. They cannot expand the user's request or authorize
provider calls. Local skill references remain inside this directory when installers copy it.

## Inspect saved evidence

Use `inspect reacquire` directly on a session for compaction questions; no extracted case is needed.
List boundary IDs, then run the comparison with `--boundary BOUNDARY_ID`. Default output omits
private paths, arguments and text.
Equal captures establish equality only in their reported native field and completeness scope.
They do not establish unnecessary retrieval, identical resources or model forgetting.

For a reduction, `inspect retained` explains dependencies, retained units and recorded trials.
Joint or earlier-context trials cannot explain each retained item individually. `inspect sections`
lists editable prior-user fields and source-bound paragraph IDs; eligibility does not prove that
coarse search will retain a field or that removing a paragraph preserves behavior.

`inspect tool CASE` lists prior native tool output fields without displaying text.
Select a reported `--field FIELD_ID` and `--start START --end END` to derive an exact span-deletion
descriptor from source; positions are original Unicode codepoints. Bounded private display requires
`--field` and `--show-text`. Check the matching documentation's supported-inputs table before
proposing native execution.

## Extract a decision

Read the matching release's Native runs guide to select session, decision and target IDs. When
selection is incomplete, non-interactive extraction reports available IDs without exposing session
text. Save the case in a new private directory; extraction refuses overwrite. Extraction predicates
must match the saved decision. A comparison can then declare a prospective predicate in the same
target category while preserving that decision. Prefer a specific behavior the developer cares
about over exact prose matching when wording can vary. Changing the predicate after observing
outcomes starts a different experiment.

Check the supported-inputs table for the source harness and target. Passive compaction evidence
does not establish active reconstruction support. Do not edit unknown records or substitute a
different source when reconstruction refuses the original.

## Prepare native reduction

Read the matching release's Native reduction guide in order: export a fresh preflight receipt, write
statistical settings, generate the configuration with `ithrl prepare`, then run its `next_command`
with `--dry-run`. Inspect the resulting complete plan and runtime checks before executing an
acknowledged continuation within the user's authorization. Preflight can probe the local harness
without provider calls; `prepare` consumes the receipt without starting a harness. Keep the original
workspace available for protection. Place user-chosen receipts, configuration and the observation
store outside both it and the case. Leave ithrl's generated run directory in place for resume.

Choose a budget for the actual question and account for fresh-confirmation reserves. Use
`inspect sections` before configuring optional paragraph removal. Fixed field choices, section IDs,
stage ceilings and stop rules are part of the experiment. Coarse search may remove every selected
field, leaving no fine-search candidate. Report that outcome without protecting a field artificially
or substituting another workload.

Run only the reviewed configuration and limits. Non-interactive native execution requires
`--acknowledge-model-execution`; a request for preparation alone does not authorize adding it.

## Fuzz prior-user text

Read the matching source ref's Context fuzzing guide and use an installation with the `fuzz` extra.
Current mutators append duplicate or supplied stale text to eligible prior-user text, preserving the
fixed triggering input. They do not edit arbitrary tool output. Check the supported table for the
selected final-response target; shell-action profiles do not support fuzzing.

For an installed CLI, use the developer's own eligible case and private configuration. Replace
`CASE`, `CONFIG`, `PROFILE`, `ORIGINAL` and `STORE` with their chosen paths:

```bash
ithrl fuzz CASE --config CONFIG --detect --profile PROFILE \
  --original-workspace ORIGINAL --format json
ithrl fuzz CASE --config CONFIG --profile PROFILE --original-workspace ORIGINAL \
  --observation-store STORE --dry-run --format json
```

The guide's `examples/fuzz` files, decision IDs, `uv run` prefix and synthetic budgets are
checkout-only examples. They are not required for an installed CLI or suitable defaults for an
ordinary case. After reviewing the dry-run report, execute only within the authorized limits.
Candidate generation and simplification are exploratory. A campaign freshly confirms at most one
selected decrease in match probability; it does not establish a model defect or a unique cause.

## Compare two explicit contexts

Read the matching source ref's Context comparison guide and use an installation with the `compare`
extra. Bind reference and candidate selections to one immutable case. Keep the triggering input,
target, grader and current execution profile identical between arms. A historical capture may
suggest a change but supplies no experimental sample credit.

For tool output edits, follow the matching source ref's span-selection guide above. Keep the
reference unmutated and both dependency-closed record partitions identical, with one candidate span
deletion; edit authoring establishes neither qualified native support nor execution authorization.

Use the normal `ithrl compare` detection, dry-run and execution workflow from that guide.
Calibration checks the declared contrast; confirmation uses fresh observations. A saved run resumes
with its original inputs and allowance. Use the shared `model`, `provider` and `reasoning_effort`
settings for supported execution choices, keeping any history projection separate from the context
edit. Changed settings require a new profile and fresh observations, with no pooling across
settings. Do not select a model from the agent's own defaults.

If replacement is needed, select `safe-provider-transport` and reserve bounded spare attempts before
collection, keeping valid-sample targets fixed. Follow the Comparison guide's eligibility rules and
the Results guide's conditional match-rate interpretation; retain failed attempts and their full
charges. Preserve completed progress; never edit saved state, discard samples or extend a terminal
plan to retry a failure.

## Interpret results and costs

For reduction and fuzzing, start with the reported result directory's `summary.json`. For
comparison, read the reported `result_path` with `ithrl inspect comparison RESULT --format json`;
add `--case CASE` to re-derive its source selections. Keep complete versus incomplete search,
confirmed versus inconclusive effects, and invalid versus behavioral outcomes separate. For
recovery, follow the report's continuation with the original identities; leave saved JSON and SQLite
state unchanged.

Read the matching release's Results and privacy guide for the private pricing specification and
`ithrl cost` modes. Recorded costs require the case and experiment digests as well as explicit
source-to-pricing attribution; the observation store alone is insufficient. Hypothetical
scenarios use supplied token counts and stay separate from observations. Missing usage, unmatched
tiers and incomplete request evidence remain visible. A partial subtotal does not establish a
whole-investigation cost. Inspection of a saved cost report needs neither the observation store nor
a fresh pricing lookup.
Use `ithrl inspect costs REPORT --format json` for an existing report.
