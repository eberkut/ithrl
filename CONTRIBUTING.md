# Contributing

Start with an [issue](https://github.com/eberkut/ithrl/issues) describing the problem or proposed
change. Pull requests are limited to collaborators; discuss the work before preparing one.

For bug reports, include the ithrl version, platform, sanitized error code and steps to reproduce.
Keep full sessions, cases, proprietary source and credentials out of public reports. Report
suspected vulnerabilities through
[private vulnerability reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## Development setup

Use [uv](https://docs.astral.sh/uv/) and Python 3.12, 3.13 or 3.14. Python 3.14 is the primary
development version. From the repository root:

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

Non-packaging tests and the synthetic example need no provider credentials or network requests.
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

Recognized Markdown-only PRs skip Python linting, typing, unit-test matrices and dependency audits.
They retain Markdown formatting/linting, documentation tests, the strict site build, workflow and
dependency review, and all package build/install checks. Mixed changes, unknown paths, configuration
changes and PRs with 3,000 or more changed files run full validation. Rename detection considers
both paths; deletions participate. Failed path detection fails the required Workflows check.
Main-branch, scheduled, manual and release runs always perform full validation.

The required `Python tests` check summarizes the test matrix. It accepts a skipped matrix only
after successful Markdown-only classification. Failed classification, missing decisions, test
failures and unexpected skips fail this check.

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

The hashed export is temporary pipeline input. The audit queries PyPI's vulnerability service for
package names and versions without installing those packages, resolving alternatives or applying
fixes. CI evaluates markers on each supported Python version. This covers locked versions rather
than every version permitted by package dependency ranges.

GitHub's dependency graph and Dependency Review use the inventory GitHub has for each commit.
Before relying on them for `uv.lock` coverage, compare the hosted package/version inventory with
the locked export on each supported Python version. The PR dependency-review job rejects newly
introduced known vulnerabilities at any severity across runtime, development and unknown scopes.
It uses a read-only token and does not check out or execute PR code. If it reports incomplete
dependency snapshots, wait for GitHub's graph update and rerun the job. No license allowlist is
enforced by this check. Keep the lockfile audit above as the full-inventory CI gate; Dependabot
version updates alone do not establish dependency-graph coverage.

CI runs weekly on Monday at 06:17 UTC to detect new advisories. Dependabot checks Python, Actions
and pre-commit dependencies weekly, groups minor/patch updates and leaves majors individually
reviewable. Ordinary updates have a seven-day cooldown; security updates have none. Updates are
never automatically merged. Hosted dependency-graph coverage is a separate verification gate.

## Documentation

Use `uv run rumdl fmt .` to format Markdown, then `uv run rumdl check .` to lint it. The same
version and `pyproject.toml` configuration apply to pre-commit and CI. Prose wraps at 100
characters; commands and tables retain their layout. MkDocs syntax is recognized within `docs/`. The
generated CLI reference has narrow exceptions for Typer's prompts, option alignment and wrapping;
its byte-for-byte generation check remains enforced. The generated changelog is checked under the
shared rules.

Keep installation and first-use instructions in README, and detailed user guides and reference in
`docs/`. Support, security and conduct policies live at the repository root. Keep private sessions,
cases and credentials out of documentation and generated output.

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

The optional `docs` dependency group pins MkDocs and the shadcn theme. Search runs in the browser;
fonts and scripts are served with the site. GitHub star-count requests and raw Markdown copying
are disabled. A local hook admits only the packaged theme assets used by this configuration.
When changing the theme or enabled extensions, check that asset list and the
[third-party notices](docs/third-party-notices.txt) alongside the rendered site.
To preview the site locally:

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
[measurement notes](docs/native-runs.md#compatibility-and-measurements).

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
workspace. Calibration requires two matching attempts and permits at most two, each bounded at
120 seconds plus cancellation grace. Final-response profiles allow up to two provider calls;
Copilot shell allows up to four, with `pwd` visible but denied.

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

For every feature, aim for parity across supported harnesses. Any exception must be clearly
justified and documented before release.

Python Semantic Release, pinned in the `release` dependency group, calculates versions from
Conventional Commits. `pyproject.toml` is the version source; installed CLI and Codex client
metadata read that distribution version. The tool generates `CHANGELOG.md` from the full release
history, omitting routine build, CI, documentation, style and test commits. Do not edit the
generated changelog by hand; review its contents before approving a release.

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
- Both environments admit only `main`. `pypi` requires the maintainer's approval, permits
  self-review and disallows administrative approval bypass. Environment declarations in YAML
  do not configure these protections; verify the actual settings before dispatch.
- Release-tag rules permit the maintainer's signed-tag creation and prevent unapproved tag
  replacement or deletion. The workflow never creates a tag.

The dispatch gate admits the repository's current maintainer identity and requires the full source
SHA and exact final `X.Y.Z` version. Replace the placeholders with the reviewed values:

```bash
gh workflow run release.yml --repo eberkut/ithrl --ref main \
  -f version=X.Y.Z -f source-sha=FULL_APPROVED_COMMIT_SHA
```

1. Inspect that run's checks and successful **Publish TestPyPI rehearsal** job. Its summary records
   the source, version, immutable artifact ID and checksum-manifest digest. The production job
   waits at the `pypi` environment. TestPyPI files, logs and artifacts are public.
2. Download that run's `release-distributions` artifact into a new review directory. For example:

   ```bash
   gh run download RUN_ID --repo eberkut/ithrl \
     --name release-distributions --dir /absolute/new/release-review
   ```

   Match `SHA256SUMS` to the digest in the run summary, then run `sha256sum --check SHA256SUMS`
   from the review directory. Inspect the release notes and confirm TestPyPI serves the same
   distribution hashes. Complete a clean TestPyPI installation/example rehearsal before approval;
   resolve dependencies only from the normal PyPI index, without mixing indexes.
3. After the rehearsal passes and tag creation is approved, create and verify the signed tag at
   the frozen source commit, then push only that tag through the permitted tag-rules path:

   ```bash
   git tag -s vX.Y.Z FULL_APPROVED_COMMIT_SHA -m 'Release vX.Y.Z'
   git verify-tag vX.Y.Z
   git push origin refs/tags/vX.Y.Z
   ```

4. Review and approve the `pypi` deployment on the run's GitHub page. The job verifies the existing
   annotated tag targets the frozen source, rechecks the files, and uploads to PyPI. The final job
   rechecks the tag/files and creates the GitHub Release with those distributions, notes and
   checksums. Signature verification belongs to the preceding maintainer tag check; the workflow
   checks the tag object and target commit.
5. Verify the published versions, hashes, attestations and installed example. A successful upload
   alone does not complete release acceptance.

Complete review within 30 days: release artifacts are retained for 30 days, and GitHub limits the
total workflow lifetime, including approval waits. Start only one release at a time; concurrency
preserves the active run but GitHub may replace an older pending run when another is queued.
Do not rerun the whole workflow to promote a completed rehearsal or cancel a run during upload.
For failure or partial publication, inspect the index and existing release first; preserve the
original files and hashes. Any recovery upload needs explicit approval and must use identical
missing files. Never rebuild and overwrite a published version or suppress conflicts with
`skip-existing`.
