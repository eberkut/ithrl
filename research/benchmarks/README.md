# Public debugging tasks

Reproduce two pinned public Python defects and check repairs against independent verifiers.
[Issue #81](https://github.com/eberkut/ithrl/issues/81) tracks the study and native investigations.
The offline commands start no coding agent and make no provider calls.

## Findings and decision

Adopt iniconfig and its patch grader as a supplementary workload for bounded constructed
investigations. Codex and Copilot each ran a bounded CLI reduction and completed fresh confirmation.
Keep migration-linter as a baseline-calibration limiting case. These results establish neither a
general benchmark nor improved ordinary debugging.

| Task / harness | Fresh calibration | Remove unrelated context | Withhold incident | Changed-defect replay |
| --- | --- | --- | --- | --- |
| Iniconfig / Codex | 3/3 pass | 1 pass; 1,354 bytes removed | 1 nonpass | 1 pass |
| Iniconfig / Copilot | 3/3 pass | 1 pass; 1,354 bytes removed | 1 nonpass | 1 pass |
| Migration-linter / Copilot | 2/3 pass; 3 required | Not admitted | Not admitted | Not admitted |
| Migration-linter / Codex | Not run | Not run | Not run | Not run |

Pilot calibration used the normal installed CLI. Each control used one shared `observe_once`
observation. An iniconfig pass means 42 source and eight independent checks passed. A
migration-linter pass means 31 source checks and one independent test covering fourteen SQL cases
passed. These pilots supplied no reduction-search or fresh-confirmation observations.

Each pilot control is a single observation, establishing neither repeated-run preservation nor
field survival through coarse search. Changed-defect replays use distinct sources and graders and
do not confirm the original case. The CLI investigations below provide separate fresh confirmation.

The migration calibration produced two passing repairs and one patch whose context Git could not
apply to the frozen source. The latter passed the envelope checks but reached neither verifier. The
rejection leaves the intended diagnosis unscored. The prescribed three-of-three gate stopped the
run; no replacement observation or downstream control received credit. A separate retained repair
passes the corrected grader, but supplies no calibration credit.

The pilots used Codex CLI `0.169.0`, Copilot CLI `1.0.104` with SDK `1.0.28`, and Python `3.12.3`.
Provider/model routes differed; this is not a comparison of harnesses or models. Native profiles,
traces and responses remain private. [results.json](results.json) records the sanitized outcomes,
output contracts and complete inference-request accounting.

### Investigations through the normal CLI

Each bounded investigation used fresh calibration, one coarse comparison, one fine comparison and
separate fresh confirmation. Earlier source and pilot observations received zero sample credit.

Counts below are per harness; both runs produced the same outcomes.

| Phase | Original repairs passing | Candidate repairs passing | Outcome |
| --- | --- | --- | --- |
| Calibration | 3/3 | Not applicable | Baseline gate passed |
| Grouped record removal | 3/3 | 0/3 | Task field retained |
| Section removal | 3/3 | 3/3 | 1,354-byte edit selected |
| Fresh confirmation | 11/11 | 11/11 | `confirmed-preserving` |

Each coarse trial removed two records together, including the task field. Its three candidate
responses failed grading before either verifier ran; this does not isolate which record mattered.
Each selected fine edit removes 1,354 bytes of overload-declaration sections, exceeding the frozen
512-byte criterion. Every passing repair passed all 42 source and eight independent checks.

For each run, the valid-only Newcombe method 10 lower bound for candidate minus original is
−0.197405 against a −0.20 margin at nominal one-sided confidence 0.95. Each meets its frozen
criterion under independent, stationary valid observations; this is not an exact coverage or joint
confidence guarantee. The coarse comparison caps stopped traversal before the final sweeps; both
configured fine searches completed. Both overall results therefore also report `search-incomplete`.

A developer can omit these overload-declaration sections when reproducing the tested repair
behavior. This is a confirmed smaller input for each declared grader/profile. All context units
remain: 18 for Codex and 10 for Copilot. Minimality and causal attribution remain unproven. The
audits found one visible copy of each original field, absent from the coarse candidate and edited in
the fine candidate. Seven Codex and four Copilot opaque nodes remain frozen, so other information
carriers cannot be excluded.

Native cleanup was verified for every attempt, protected inputs were unchanged, disposable
residuals were zero and no actions or tool-network requests executed. Both retained-context
inspections succeeded through the installed CLI with network denied and zero provider calls. See
[recorded cost](#budgets-and-recorded-cost) for requests, timing and usage.

Maintain the task source and dependency pins, reviewed correct/negative grader fixtures and native
profile bindings. Source or grader changes require fixture revalidation; changed native profiles
need their own qualification. The optional `python-patch-tests` integration grades repairs rather
than keyword mentions or response shape. Corpus expansion and additional harnesses require separate
evidence and execution decisions.

Constructed-task results supplement the unchanged
[ordinary-workflow limiting outcome](../../docs/research/reduction-design.md#prospective-ordinary-workflow-cohort).
The separate [statistical simulations](../reduction/README.md) assess specified rates and budgets.
Task-specific grades remain conditional on the reviewed tests; public-task contamination, opaque
state and ordinary-workflow usefulness remain unresolved.

## Tasks and provenance

| Task | Dataset instance | Debugging question |
| --- | --- | --- |
| Configuration API | `pytest-dev__iniconfig.16793ead.func_basic__4oga9t8l` | Why do section-level reads lose conversion or return the wrong missing-value default? |
| Migration analysis | `3yourmind__django-migration-linter-186` | Why does creating an index on a new table warn, and how can the correction retain warnings for existing tables? |

`tasks.json` binds dataset revisions, repository commits, source archives, adapted replay commands,
container-image references and every reviewed repair variant. `data/*-row.json` preserves the
selected source records. SWE-smith's patch **introduces** its defect; reversing it repairs the
original source. SWE-rebench's patch repairs an existing defect. Keep those orientations distinct.

The source archives are small: 8,130 bytes for iniconfig and 41,565 bytes for migration-linter.
Their SHA-256 values identify the bytes used here. Dataset image digests are retained for
provenance; those images were not pulled or executed. The focused environment below is an explicit
adaptation, not a reproduction of either complete container environment.

The two tasks and exclusion rules were frozen before paid observations. Selection examined one
bounded shard from each dataset. Funcy's prefix-slicing mutation was
less substantive than iniconfig's forwarding contract. The real-task alternatives added an async
race/extension build or unresolved issue references and C++ fixtures. These choices establish
small feasibility cases, not representative sampling or a population success rate.

Sources: [SWE-smith-py](https://huggingface.co/datasets/SWE-bench/SWE-smith-py),
[SWE-rebench V2.1](https://huggingface.co/datasets/ibragim-bad/SWE-rebench-V2.1),
[iniconfig source](https://github.com/pytest-dev/iniconfig/tree/16793eaddac67de0b8d621ae4e42e05b927e8d67)
and
[migration-linter source](https://github.com/3YOURMIND/django-migration-linter/tree/aef3db3e4198d06c38bc4b0874e72ed657891eea).
See [NOTICE](NOTICE) for attribution and licenses.

## Reproduce

Run from the repository root on Linux with usable Bubblewrap namespaces, `/usr/bin/bwrap`,
`/usr/bin/git`, GNU patch, curl and uv. The isolated replay mounts `/usr`, `/lib` and `/lib64` and
requires system Python **3.12.3** at `/usr/bin/python3.12`. Verify that version before setup:

```sh
/usr/bin/python3.12 --version
```

The research environment is separate from ithrl's development environment. It imports the pinned
source trees directly; no upstream setup script, database service or native coding agent is needed.

Create disposable directories and install the hash-locked research requirements:

```sh
task_cache=$(mktemp -d)
task_output=$(mktemp -d)
uv venv --python /usr/bin/python3.12 "$task_cache/env"
uv pip install --python "$task_cache/env/bin/python" --only-binary :all: \
  --require-hashes --no-deps -r research/benchmarks/requirements.txt
```

Download the pinned source archives. Downloads require network access; replay does not.

```sh
curl --fail --location --max-time 60 --max-filesize 10485760 \
  --output "$task_cache/iniconfig-source.tar.gz" \
  https://codeload.github.com/pytest-dev/iniconfig/tar.gz/16793eaddac67de0b8d621ae4e42e05b927e8d67
curl --fail --location --max-time 60 --max-filesize 10485760 \
  --output "$task_cache/migration-source.tar.gz" \
  https://codeload.github.com/3YOURMIND/django-migration-linter/tar.gz/aef3db3e4198d06c38bc4b0874e72ed657891eea
```

Run the replay with the explicit research interpreter:

```sh
/usr/bin/python3.12 -I -S research/benchmarks/reproduce.py \
  --cache "$task_cache" --python "$task_cache/env/bin/python" \
  --output "$task_output/replay"
```

The replay checks source hashes and the complete installed-package/version map, then runs the
reviewed fixtures with network access denied, a fixed environment, read-only source/environment
mounts and private temporary filesystems. The host checkout, home directory and credentials are
not mounted. Each suite has a 30-second timeout; an import failure or missing test count cannot
stand in for a reproduced behavioral failure. Logs and a semantic result receipt remain in the
new output directory.

Expect fifteen reference variants across the two tasks, matching
[verification.json](verification.json). The replay accepts only reviewed fixtures from `tasks.json`.
The product grader below separately checks seventeen repairs across four bundles, using an
independent test-reporting path to expose implementation disagreements.

The control and budget helpers import ithrl. First prepare its development environment using
the [contributor setup](../../CONTRIBUTING.md#development-setup):

```sh
uv sync --locked --all-groups --all-extras
```

That setup can download dependencies. Then bind prospective paragraph-removal controls offline:

```sh
uv run --locked --offline --no-sync python research/benchmarks/controls.py \
  --replay "$task_output/replay"
```

`controls.json` records the expected incident hashes and source ranges. These are provider-free
prototype bindings; actual extracted cases need native identities and byte-equivalence checks.

## Grader checks

The iniconfig source suite has 42 parameterized test instances. The injected fault fails
`test_section_get`; reversing it restores the suite. Eight independent checks cover conversion,
unconverted present values, missing-default identity, converter calls, empty values and conversion
errors. A positional-argument repair also passes. An incomplete repair that converts a missing
default passes the upstream suite but fails these additional checks.

The receipt's `official` field means source/dataset-origin assertions under this adapted replay.
Iniconfig runs its complete source test file, beyond the dataset's named selection. Migration-linter
runs its source unittest file instead of the dataset's pytest command. The preserved rows retain
those original declarations; these results are not full benchmark-container scores.

Migration-linter's added source regression produces one failure among 31 SQL-analyser tests.
Its supplied repair passes that suite but fails an independent mixed-index check: a later index on
an existing table can be missed after an index on a newly created table. The adapted reference
checks every relevant index. A second implementation also passes. Disabling the warning or
exempting every transaction containing any table creation fails the reviewed checks.

The additional SQL controls cover fourteen quoted/unquoted table and index cases, including leading
whitespace, column lists without preceding whitespace and names that share a prefix. They check
whether a `CREATE_INDEX` warning remains, not its count or table attribution. They do not qualify a
complete PostgreSQL parser. Preserve the original dataset result separately from this strengthened,
adapted task.

## Native pilot design

Tasks own their source, expected behavior and grader; harnesses own native evidence, reconstruction
and permissions. The pilots test repairs from supplied incident captures, not autonomous repository
exploration. Later harnesses or file/tool-output profiles need separately qualified delivery.

Preserve original dataset statements for provenance. Adapted incident prompts supply buggy source,
API requirements and symptoms, excluding diagnoses, reference repairs, test patches, graders and
dataset metadata. Keep those materials and this checkout out of the agent's workspace and input. The
final question requests a repair without repeating case facts. Returned actions are observations;
native profiles deny tool execution.

The migration capture includes an unrelated filesystem-utility module; removing it leaves the SQL
analyser and transaction-rule example intact. The changed-defect iniconfig control retains the
visible symptom but moves the fault to the underlying getter.

Freeze these conditions before model observations:

- Original evidence and three fresh calibration responses.
- A prospective removal of overload declarations or an unrelated filesystem-utility snapshot. Its
  expected invariance is a hypothesis to check, not an established fact about model use.
- Removal of all supplied case evidence. Inspect assistant history, duplicate carriers and opaque
  state before interpreting any surviving correct answer.
- A separately generated changed-defect source: conversion is lost in the underlying iniconfig
  getter, or the migration-linter's concurrent-drop rule is inverted. These controls require
  different corrections. They are additional synthetic variants, not original dataset instances
  or supported substitution edits.

Require the first source response to be exactly `ACK`, apart from surrounding whitespace, as
requested by the source-generation prompt. Stop before requesting a repair if it differs.
Keep source generation separate from immutable-original calibration. Changed-defect sources have
their own identities and grading expectations. Irrelevant/withheld evidence removals use supported
source-bound edits after extraction. Verify native identities, input visibility and materialization
before execution. Do not protect a field artificially or change search to keep it available.

### Grader integration

The `python-patch-tests` grader binds a task's source, verifier files, edit allowlist and prepared
environment. A sampled experiment can attach this grader to an unconstrained final-response
target. The normal workflow then grades the proposed repair after capturing the native response;
patch equality, keyword mentions and model-provided success claims do not establish task success.

The [grading contract](../../docs/architecture.md#measurement-search-and-confirmation) defines
content-only Git application, fixed verifiers, isolation and operational-error handling.

Prepare the four task bundles and replay the reviewed repairs through the product grader:

```sh
uv run --locked --offline --no-sync python research/benchmarks/prepare_grading.py \
  --cache "$task_cache" --python "$task_cache/env/bin/python" \
  --output "$task_output/grading"
```

Use the prepared system-Python environment and pinned package map above. The helper writes private
bundle/configuration files, unified repair patches and a grading receipt. Expect seventeen checks:
the fifteen reviewed variants plus a correction against each changed-defect source. Empty patches
are rejected before testing. The upstream migration repair's changelog edit is omitted from its
proposed patch and recorded as a non-code adaptation; its source repair and original dataset patch
remain preserved.

Generated Python can inspect the read-only verifiers and its test process. These task scores
assume ordinary repair attempts; deliberate grader manipulation is outside their validity claim.
Native input visibility and effective permissions need separate controls before paid execution.

### Budgets and recorded cost

Regenerate the original provider-free proposal arithmetic with:

```sh
uv run --locked --offline --no-sync python research/benchmarks/budget.py
```

That four-cell design permits at most 40 attempts, conditional on one-request enforcement. It is a
proposal, not an execution authorization or a measured total. Each actual package needs its own
bound runtime, whole-run deadline, stopping rules and approval. The migration-linter/Codex cell
was not executed.

| Task / harness | Recorded inference requests | Included unsuccessful work |
| --- | ---: | --- |
| Iniconfig / Codex pilots | 12 | Two source-preparation failures before the ten-request pilot |
| Iniconfig / Copilot pilots | 14 | Six requests before a native-protocol stop, then an eight-request continuation |
| Migration-linter / Copilot | 7 | Two requests per source-eligibility attempt, then three calibration requests |
| Iniconfig / Codex CLI investigation | 37 | Includes three nonmatching coarse candidate responses |
| Iniconfig / Copilot CLI investigation | 37 | Includes three nonmatching coarse candidate responses |
| **Total** | **107** | **All completed, unsuccessful and superseded attempts** |

The Codex CLI investigation used its full 37-request allowance in 377.17 seconds of CLI wall time,
including 251.122 seconds of native runner time and 84.261 seconds of grading. All 37 attempts
reported usage: 266,584 input and 5,265 output tokens, with zero reported cached input tokens.

The Copilot CLI investigation used 37 requests in 839.94 seconds, including 740.641 seconds
of native runner time and 70.094 seconds of grading. All 37 attempts reported 159,721 input,
10,054 output and 153,697 cached input tokens. These are provider-reported fields; different
cache semantics prevent a direct cost comparison with Codex.

The final migration-linter/Copilot calibration used three of eight approved requests; five
downstream requests were not used. Its three-observation CLI calibration took 291.617 seconds; that
excludes earlier preparation and is not the complete investigation time. All dispatch reservations
were reconciled, with no additional possible or pending requests. Source responses and earlier
calibration observations received no fresh-sample credit.

The first migration source repair added a file outside the allowlist and was rejected. The second
was rejected by the frozen GNU patch backend despite correct hunk counts and matching source text;
the unchanged response passes strict Git application and both fixed suites. That historical grade
stays separate from replays under the guarded Git backend. The guarded Git backend reported a
context mismatch on a different calibration response; that response remains rejected without manual
repair.

CLI timings exclude preparation and authoring. Request counts are not billed charges. Retained
native usage has incomplete coverage and different cache semantics; no combined token estimate is
reported. Billing, complete preparation/authoring effort and some authentication/metadata traffic
remain unknown. These records cannot establish the total cost or time of an investigation.
