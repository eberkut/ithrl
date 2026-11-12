# Contributing

Open an [issue](https://github.com/eberkut/ithrl/issues) for a bug report, question or proposed
change. Pull requests are limited to collaborators. If you are not a collaborator and want to
implement a change, agree on scope and ask the maintainer about access in the issue before preparing
a PR.

For bug reports, include the ithrl version, platform, sanitized error code and steps to reproduce.
Keep full sessions, cases, proprietary source and credentials out of public reports. Report
suspected vulnerabilities through
[private vulnerability reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## First contribution

1. Find an existing issue or open one, and agree on scope with the maintainer.
2. Follow [development setup](#development-setup) to prepare the locked environment.
3. Make a focused change using [Preparing a change](#preparing-a-change), and run the relevant
   [checks](#checks).
4. Open a PR linking the issue and explaining the change and its validation. Follow the
   [review requirements](#pull-request-review).

For task-specific instructions, see [documentation](#documentation),
[workflows and dependencies](#workflow-and-dependency-checks),
[integration tests](#credential-free-integration-tests) or
[native smoke tests](#opt-in-native-smoke-tests).

## Development setup

Use [uv](https://docs.astral.sh/uv/) at the version required by `tool.uv.required-version` in
`pyproject.toml`, and Python 3.12, 3.13 or 3.14. CI reads the same uv pin through `setup-uv`'s
`version-file` input. Python 3.14 is the primary development version. From the repository root:

```bash
uv sync --locked --all-groups --all-extras
uv run ithrl --help
```

The [bundled example](README.md#try-it) also works from a checkout; prefix `ithrl` commands with
`uv run`. It does not depend on test fixtures.

## Checks

Run the same entry points used by CI:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run rumdl fmt --check .
uv run rumdl check .
uv run pyrefly check
uv run pytest --ignore=tests/packaging
uv run radon cc src/ithrl --min B --show-complexity --total-average
uv run radon mi src/ithrl --min B --show
uv build --sdist --wheel --no-sources
ITHRL_DIST_DIR=dist uv run pytest -o addopts= tests/packaging
```

Non-packaging tests and the synthetic example need no provider credentials or external network
access. Native-runner tests need Linux, Bubblewrap and OpenSSL; their HTTPS fixture runs locally.
Packaging checks may download build and runtime dependencies. Radon reports are review signals.

CI runs tests, dependency audits and installed-package checks on Python 3.12–3.14. It builds one
wheel/sdist pair; every Python version tests that wheel and rebuilds and installs a wheel from the
sdist outside the checkout. Version output must match artifact metadata. To test a local wheel as
an isolated tool, pass its path to `uv tool install` or `pipx install`.

For local matrix runs, set `UV_PYTHON` on every command. Use separate environments, test caches and
coverage files for parallel runs; otherwise run versions sequentially.

Install the local hooks with `uv run pre-commit install`; run them on the whole repository with
`uv run pre-commit run --all-files`. Ruff and rumdl format files in place. CI checks formatting
without modifying files.

Markdown-only PRs skip Python linting, typing, unit tests and dependency audits. They still run
Markdown, documentation, strict site, workflow, dependency-review and package checks. Mixed changes,
unknown paths, configuration changes or 3,000 or more changed files require full validation;
classification includes deleted files and both sides of renames. Failed detection fails Workflows.
Main, scheduled, manual and release runs always validate fully. The required `Python tests` summary
accepts a skipped matrix only after successful Markdown-only classification; missing decisions,
failures and unexpected skips fail it.

The logo sources, usage rules and regeneration commands are in [branding](docs/branding/README.md).

## Performance and maintainability

Measure the affected workflow before optimizing it. Keep sanitized input generation and setup
outside timings; record the revision, locked environment, input size, repeats and noise. Measure
allocations separately from elapsed time. Include index/cache construction and retained memory,
then repeat the same workload after the change. Provider latency and local implementation overhead
are separate measurements.

- Choose collections for their operations: a set for repeated membership, a dictionary for keyed
  lookup, and a sequence for authoritative order. For example, fuzz proposal membership can use a
  local set while seeded traversal retains the original sequence. Measure scaling near admitted
  limits as well as small inputs; Radon scores alone do not justify a refactor.
- Keep reusable derived facts with the owner of their immutable inputs. Define copy/invalidation
  behavior and memory bounds before adding a cache. A frozen model can still copy stale derived
  state. Do not expose mutable internal indexes or borrow mutable native records across owners.
- Bound both output and examined work. Count rejected and duplicate proposals where the work cap
  requires it. Avoid repeated whole-input scans when the required operation is a keyed lookup.
  Release large temporary buffers before processing the next item.
- Batch fresh persistence reads where their consistency contract permits it. Preserve schema,
  identity, chronology and corruption checks, locks, durable dispatch and accounting. Persisted
  files and external state need fresh validation; an in-memory cache is not their authority. Reuse
  validated facts within one synchronous operation; refresh after callbacks, waits or execution.
- Test configurable limits with small inputs at the exact boundary and one beyond; assert policy
  defaults separately when needed. Use sparse files for size-only filesystem checks. Share completed
  immutable fixture data, not mutable cases or stores. Retain real process, SQLite and distribution
  checks when they establish lifecycle, recovery or artifact behavior.
- Share an existing mechanism only when its ownership, errors and side effects fit. Require a
  concrete maintenance benefit from abstractions; keep different harness and statistical rules
  explicit. Compare behavior, failure paths and recovery as well as performance.

### Benchmarks

The permanent [pytest-benchmark](https://pytest-benchmark.readthedocs.io/en/latest/) workloads
live in `tests/benchmarks/`. They use sanitized inputs and no native harness or provider.
Ordinary test runs skip them. From the repository root, save a baseline before changing code:

```bash
uv run pytest tests/benchmarks --benchmark-only --no-cov --benchmark-save=before
```

After the change, compare against that saved run:

```bash
uv run pytest tests/benchmarks --benchmark-only --no-cov --benchmark-compare=0001 --benchmark-save=after
```

Replace `0001` with the baseline ID printed by the first command. The plugin keeps saved runs
under the ignored `.benchmarks/` directory. Compare identical workloads on the same interpreter
and machine with low competing load. Check the distributions and memory tradeoffs; do not infer
a regression from a small noisy difference. Keep setup outside the measured call and assert
outcomes. Stateful workloads need equivalent starting state for every round.

CI runs the benchmarks on Python 3.14 in the existing Quality job. Non-PR runs retain the generated
JSON as `benchmarks-python-3.14` artifacts for 90 days. Download comparable runs and inspect them
with `uv run pytest-benchmark compare FIRST.json SECOND.json`. There is no elapsed-time failure
threshold; benchmark assertions still fail on incorrect behavior. Saved JSON contains machine and
commit metadata; raw profiles and private-workflow inputs do not belong in these artifacts.

## Workflow and dependency checks

For GitHub Actions or Dependabot changes, use the jactionlint release pinned in
[CI](.github/workflows/ci.yml):

```bash
jactionlint --no-online --strict-exit
```

CI uses its default correctness/security profile, with online lookups and automatic fixes disabled.

Audit the resolved runtime dependencies, optional extras and every dependency group, including
development, docs and audit tooling, from `uv.lock`. Run this in Bash with the locked environment
synchronized:

```bash
set -o pipefail
uv export --locked --all-groups --all-extras --no-emit-project --format requirements.txt |
  uv run --locked --group audit pip-audit --strict --disable-pip --require-hashes -r /dev/stdin
```

The audit sends locked package names and versions to PyPI's vulnerability service. It installs
nothing, resolves no alternatives and applies no fixes. It covers locked versions, not entire
dependency ranges. For dependency changes, run it on each supported Python version to evaluate
markers. Dependabot and Dependency Review cannot replace it: GitHub's dependency graph may omit
lockfile packages.

The PR dependency-review job rejects newly introduced known vulnerabilities at any severity across
runtime, development and unknown scopes. It uses a read-only token and does not check out or execute
PR code. If it reports incomplete dependency snapshots, wait for GitHub's graph update and rerun
the job. License checks are disabled.

CI runs weekly on Monday at 06:17 UTC to detect new advisories. Dependabot checks Python, Actions
and pre-commit dependencies weekly, groups minor/patch updates and leaves majors individually
reviewable. Ordinary updates have a seven-day cooldown; security updates have none. Updates are
never automatically merged.

## Documentation

Use `uv run rumdl fmt .` to format Markdown, then `uv run rumdl check .` to lint it. The same
version and `pyproject.toml` configuration apply to pre-commit and CI. Prose wraps at 100
characters; commands and tables retain their layout. MkDocs syntax is recognized within `docs/`. The
generated CLI reference has narrow exceptions for Typer's prompts, option alignment and wrapping;
its byte-for-byte generation check remains enforced. The generated changelog keeps each entry on
one source line, with a line-length exception.

Keep installation and first-use instructions in README, and detailed user guides and reference in
`docs/`. Support, security and conduct policies live at the repository root. Keep private sessions,
cases and credentials out of documentation and generated output.

### Writing and review

Start with the reader's task. Use [Diátaxis](https://diataxis.fr/start-here/) to distinguish a
guided learning example, a task-specific how-to, reference facts and explanation. A README can
combine orientation and first use; do not split pages just to fill those categories.

- Put prerequisites before commands, conditions before dependent actions and the expected result
  after a step that needs verification. Use numbered steps for a sequence and tables for
  comparisons.
- State what the reader can do or conclude. Prefer concrete verbs and established terms; remove
  filler, promotional claims, rhetorical contrasts and sentences that repeat their heading.
  For example, replace “It is important to note that execution requires explicit acknowledgement”
  with “After reviewing the plan, pass `--acknowledge-model-execution` to allow provider calls.”
- Give each topic one owner. Keep options in the generated CLI reference, procedures in user guides,
  measurements in `docs/measurements.md`, rationale in decisions and methods in research. Link to
  detail instead of repeating it. Keep essential safety conditions beside the affected action.
- Shorten without widening claims. Preserve tested profiles, statistical conditions, costs,
  uncertainty, failed outcomes and historical context. “The screen passed” does not mean
  “preservation is confirmed.” Check commands against the current CLI and qualify future work.
- Follow [W3C's writing guidance](https://www.w3.org/WAI/tips/writing/): use descriptive link text,
  ordered headings and useful image alternatives. Do not rely on color alone. Keep code blocks
  executable or identify placeholders before the command.

Before review, check: can the intended reader find the task, run its steps and interpret the
result? Does each paragraph add information? Are terms, claims, links and generated content current?
Check the [documentation commands](#building-and-checking-documentation) below after edits.

These rules apply
[Google's guidance on clear, concise writing](https://developers.google.com/style/highlights) and
[Microsoft's procedural guidance](https://learn.microsoft.com/en-us/style-guide/procedures-instructions/writing-step-by-step-instructions).
Use judgment rather than word-count targets; explanations and research need enough detail to support
their conclusions.

### Documentation ownership

[ROADMAP.md](ROADMAP.md) describes future direction. GitHub issues own actionable scope,
dependencies, acceptance criteria and status; release milestones group scope and the public
[Project](https://github.com/users/eberkut/projects/1/views/4) provides views of those same issues.
Link implementation pull requests to their owning issues and record relevant validation before
closing them. Keep future plans separate from current support claims.

Use [architecture](docs/architecture.md) to locate current contracts,
[decision records](docs/decisions/index.md) for significant choices and alternatives, and
[research](docs/research/index.md) for methods, measurements and reproducibility. Amend the owning
document instead of maintaining a second plan. A new decision record should explain context,
decision, alternatives and consequences; supersede an old decision explicitly when needed.

Before evaluating a new study, declare its tested domain, criteria, seeds, budget and intended
claims. Report costs across complete workflows, including failed and incomplete work, and document
the alternatives and limitations needed to interpret the results.

### Building and checking documentation

The `docs` group pins MkDocs and the shadcn theme. Search runs in the browser; the site bundles
fonts and scripts. GitHub star-count requests and raw Markdown copying are disabled. When changing
the theme or extensions, review the asset allowlist in `scripts/docs_theme.py`, the
[third-party notices](docs/third-party-notices.txt) and the rendered site. To preview:

```bash
uv sync --locked --all-groups --all-extras
uv run --group docs mkdocs serve
```

`mkdocs.yml` admits public pages and Markdown files in the dedicated `docs/decisions/` and
`docs/research/` directories. These directories contain publication-ready material only. Add new
pages to navigation; a page within an admitted directory needs no per-file package/test entry.
When adding a public directory or asset type, update the site/sdist publication boundaries and
their tests together. Never set `docs_dir` to the repository root. Keep raw evidence and generated
research outputs outside the documentation sources.

After changing CLI options or help, regenerate the committed reference with Typer:

```bash
uv run typer ithrl.cli utils docs --name ithrl --title 'CLI reference' --output docs/cli.md
```

Generation reads command metadata without invoking the commands. Verify reference drift,
publication boundaries, issue forms, page links and anchors with:

```bash
uv run pytest -o addopts= tests/unit/test_documentation.py -q
uv run --group docs mkdocs build --strict
```

Build output stays in ignored `.local/site/`. Strict builds validate local documentation links;
they do not verify remote URL availability.

The [documentation workflow](.github/workflows/pages.yml) builds and deploys
`https://eberkut.github.io/ithrl/` on pushes to `main`, or when a maintainer runs it manually
from `main`. It checks the documentation before uploading only the generated site. Deployment
runs in a separate `github-pages` environment job without checking out or executing project code.
Pull requests receive read-only documentation checks through CI and cannot deploy.

Repository administrators must select **Settings → Pages → Source: GitHub Actions** and restrict
the `github-pages` environment to `main`. No custom domain, separate
site repository, `gh-pages` branch or personal access token is needed. After a successful
deployment, check the published pages, navigation and static assets at the URL above.

## Credential-free integration tests

The sampled CLI tests exercise calibration, search, fresh confirmation, observations, result
publication and exact resume with a mock runner:

```bash
uv run pytest -o addopts= tests/integration/test_sampled_cli.py -q
```

Mock-native tests exercise the three current native target profiles through isolated mock harness
processes. They cover context-sensitive reduction, immutable inputs, action denial, cleanup,
accounting and budget refusal:

```bash
uv run pytest -o addopts= tests/integration/test_native_calibration.py -q
```

Fuzz CLI tests cover harness-free inspection, consent, bounded collection, unsuccessful outcomes,
interruption and exact resume without provider requests:

```bash
uv run pytest -o addopts= tests/integration/test_fuzz_cli.py -q
```

These tests establish local contracts. Native qualification is recorded in the
[measurements](docs/measurements.md).

## Opt-in native smoke tests

Profiles are `codex-final`, `copilot-final` and `copilot-shell`. They are skipped by default and
refuse opt-in when `CI` or `GITHUB_ACTIONS` is set. Live mode makes real model requests.

Use a private evidence parent outside the checkout and existing harness authentication. The
installed harness and Bubblewrap must pass isolation/capability checks. Never put credentials in
arguments or case content. Inspect the exact profile, model, provider and limits before execution.

Start with structural preflight, which runs bounded local probes with network denied and no model
turn. The host filesystem and harness executable are read-only; only private disposable state is
writable:

```bash
uv run pytest -o addopts= tests/live -m native_smoke -s \
  --native-smoke preflight --native-profile PROFILE \
  --native-model MODEL --native-provider PROVIDER \
  --native-smoke-root /absolute/private/evidence-parent
```

Inspect the printed plan and private `preflight.json`. To authorize provider execution, use its
exact capability digest. For Copilot, `PROVIDER` must be `github-copilot`:

```bash
uv run pytest -o addopts= tests/live -m native_smoke -s \
  --native-smoke run --native-profile PROFILE \
  --native-model MODEL --native-provider PROVIDER \
  --native-smoke-root /absolute/private/evidence-parent \
  --native-capability-digest APPROVED_DIGEST
```

Every invocation creates fresh private state, harmless session content and an empty original
workspace. Calibration requires two matching attempts and permits at most two, each bounded at 120
seconds plus cancellation grace. Codex allows up to 32 provider calls across the two attempts;
either Copilot profile allows up to two. In the shell profile, `pwd` is visible but denied.

Returned actions never execute. Hosted tools, MCP and tool-side network effects remain forbidden. An
input-token cap cannot be enforced. Private evidence records known counts and unknown acceptance
explicitly. Unsupported or unsuccessful profiles fail the test without skipping or retrying.

## Preparing a change

Keep changes focused and add tests for changed behavior and failure paths. Update the relevant
user guide when commands or results change. Use Conventional Commit subjects, such as
`fix(extract): reject conflicting selectors`.

Changes to native protocols, reconstruction, permissions or provider accounting need review of
the affected safety boundary. Mock tests establish local contracts; claims of native support also
need capability, isolation, calibration and live-use evidence for the target profile.

### Pull request review

Pull requests are squash-merged, with the PR title becoming the commit subject. Use a Conventional
Commit title, such as `fix(codex): handle overload responses` or
`docs: explain confirmation results`. The title check reruns on title edits and new pushes; it reads
metadata without checking out PR code. Make sure the required CI and CodeQL checks pass and resolve
review conversations before merging.

CodeRabbit reviews PRs when they are ready for review and posts feedback in a walkthrough and
inline comments. Address relevant findings or explain why a suggestion does not apply. The
maintainer makes the final review and merge decisions.

CodeRabbit's optional planning and code-generation features are configured in
[`.coderabbit.yaml`](.coderabbit.yaml). A paid CodeRabbit account is not required to contribute.

## Preparing a release

Maintainers need repository and package-publishing access. Before release, justify and document
any feature-parity exception between supported harnesses.

Python Semantic Release, pinned in the `release` dependency group, calculates versions from
Conventional Commits. `pyproject.toml` is the version source; installed CLI and Codex client
metadata read that distribution version. The tool generates `CHANGELOG.md` from the full release
history, omitting routine build, CI, documentation, style and test commits. Its
[template](.github/changelog/CHANGELOG.md.j2) keeps lists compact, with one entry per source line.
Change the template to adjust formatting; do not edit generated entries by hand.

Review and merge generated version/notes changes through a PR. CI tests the prepared version;
publication builds that committed version.

Use `main` with complete history and all existing release tags available. Preview the next version
without changing files, the index, commits, tags or remote state:

```bash
uv run --locked --group release semantic-release --noop version --print
```

The preparation command below writes **and stages** the version and changelog, even with
`--no-commit`. Obtain approval for those local staging changes before running it. For worktree-only
review, run it in a disposable checkout and transfer the reviewed file changes. To prepare:

```bash
uv run --locked --group release semantic-release version \
  --no-commit --no-tag --no-push --no-vcs-release --skip-build
uv lock --upgrade-package ithrl
```

The lock update remains unstaged. Review all generated changes, confirm version consistency and
public-safe release notes, then run the affected checks before proposing the signed commit.
Preparation creates no commit, tag or hosted release and uploads no package. Publication requires
separate approval of the source commit, version, artifacts and destinations.

### Publishing a prepared release

The manually dispatched [release workflow](.github/workflows/release.yml) uses the existing CI
workflow to check the exact source commit, build one release wheel/sdist pair and test installation
on Python 3.12–3.14. It uploads to TestPyPI, waits for production approval, then promotes those
same distribution bytes to PyPI and GitHub Releases. Ordinary pushes and pull requests do not
publish packages. No native harness or model request is part of release automation.

Before dispatch, the source must be reviewed and committed on `main`, with successful CI and
the applicable hosted security checks for that commit. Verify account recovery readiness, package
ownership and the following repository/index settings:

- PyPI and TestPyPI Trusted Publishers bind `eberkut/ithrl`, `release.yml` and their respective
  `pypi` / `testpypi` environments. Publishing uses OIDC, without a stored PyPI API token.
- Both environments admit only `main`, require the maintainer's approval, permit self-review and
  disallow administrative approval bypass. Environment declarations in YAML do not configure these
  protections; verify the actual settings before dispatch.
- Release-tag rules permit the maintainer's signed-tag creation and prevent unapproved tag
  replacement or deletion. The workflow never creates a tag.

The release workflow accepts dispatches only from the repository maintainer. Supply the full source
commit SHA and exact final `X.Y.Z` version. Replace the placeholders with the reviewed values:

```bash
gh workflow run release.yml --repo eberkut/ithrl --ref main \
  -f version=X.Y.Z -f source-sha=FULL_APPROVED_COMMIT_SHA
```

1. After source verification and validation pass, approve the `testpypi` deployment on the run's
   GitHub page. This authorizes the public TestPyPI upload.
2. Inspect the successful **Publish TestPyPI rehearsal** job. Its summary records
   the source, version, immutable artifact ID and checksum-manifest digest. The production job
   waits at the `pypi` environment. TestPyPI files, logs and artifacts are public.
3. Download that run's `release-distributions` artifact into a new review directory. For example:

   ```bash
   gh run download RUN_ID --repo eberkut/ithrl \
     --name release-distributions --dir /absolute/new/release-review
   ```

   Match `SHA256SUMS` to the digest in the run summary, then run `sha256sum --check SHA256SUMS`
   from the review directory. Inspect the release notes and confirm TestPyPI serves the same
   distribution hashes. Complete a clean TestPyPI installation/example rehearsal before approval;
   resolve dependencies only from the normal PyPI index, without mixing indexes.
4. After the rehearsal passes and tag creation is approved, create and verify the signed tag at
   the frozen source commit, then push only that tag through the permitted tag-rules path:

   ```bash
   git tag -s vX.Y.Z FULL_APPROVED_COMMIT_SHA -m 'Release vX.Y.Z'
   git verify-tag vX.Y.Z
   git push origin refs/tags/vX.Y.Z
   ```

5. Review and approve the `pypi` deployment on the run's GitHub page. The job verifies the existing
   annotated tag targets the frozen source, rechecks the files, and uploads to PyPI. The final job
   rechecks the tag/files and creates the GitHub Release with those distributions, notes and
   checksums. Signature verification belongs to the preceding maintainer tag check; the workflow
   checks the tag object and target commit.
6. Verify the published versions, hashes, attestations and installed example. A successful upload
   alone does not complete release acceptance.

Complete review within the 30-day artifact retention and GitHub workflow lifetime, including
approval waits. Start one release at a time: a new queued run may replace an older pending run,
though the active run is preserved. Do not rerun the workflow to promote a completed rehearsal or
cancel during upload.

After failure or partial publication, inspect the index and release before recovery. Preserve
the original files and hashes; uploading identical missing files requires explicit approval.
Never overwrite a published version with rebuilt files or suppress conflicts with `skip-existing`.
