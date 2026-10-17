# Contributing

Start with an [issue](https://github.com/eberkut/ithrl/issues) describing the problem or proposed
change. Pull requests are limited to collaborators; discuss the work before preparing one.

For bug reports, include the ithrl version, platform, sanitized error code and steps to reproduce.
Keep full sessions, cases, proprietary source and credentials out of public reports. Report
suspected vulnerabilities through [private vulnerability reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## Development setup

Use [uv](https://docs.astral.sh/uv/) and Python 3.12, 3.13 or 3.14. Python 3.14 is the primary
development version. From the repository root:

```bash
uv sync --locked --all-groups
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

## Workflow and dependency checks

For GitHub Actions or Dependabot changes, use the jactionlint release pinned in
[CI](.github/workflows/ci.yml):

```bash
jactionlint --no-online --strict-exit
```

CI uses its default correctness/security profile, with online lookups and automatic fixes disabled.

Audit the resolved runtime dependencies and every dependency group, including development, docs
and audit tooling, from `uv.lock`. Run this in Bash with the locked environment synchronized:

```bash
set -o pipefail
uv export --locked --all-groups --no-emit-project --format requirements.txt |
  uv run --locked --group audit pip-audit --strict --disable-pip --require-hashes -r /dev/stdin
```

The hashed export is temporary pipeline input. The audit queries PyPI's vulnerability service for
package names and versions without installing those packages, resolving alternatives or applying
fixes. CI evaluates markers on each supported Python version. This covers locked versions rather
than every version permitted by package dependency ranges.

GitHub's dependency graph and Dependency Review use the inventory GitHub has for each commit.
Before relying on them for `uv.lock` coverage, compare the hosted package/version inventory with
the locked export on each supported Python version. Keep the lockfile audit above as the CI gate;
Dependabot version updates alone do not establish dependency-graph coverage.

CI runs weekly on Monday at 06:17 UTC to detect new advisories. Dependabot checks Python, Actions
and pre-commit dependencies weekly, groups minor/patch updates and leaves majors individually
reviewable. Ordinary updates have a seven-day cooldown; security updates have none. Updates are
never automatically merged. Hosted dependency-graph coverage is a separate verification gate.

## Documentation

README owns the quick start; `docs/` owns user guides and reference. Support, security and conduct
policies live at the repository root. Keep private sessions, cases and credentials out of
documentation and generated output.

The optional `docs` dependency group pins MkDocs and the shadcn theme. Search runs in the browser;
fonts and scripts are served with the site. GitHub star-count requests and raw Markdown copying
are disabled. A local hook admits only the packaged theme assets used by this configuration.
When changing the theme or enabled extensions, check that asset list and the
[third-party notices](docs/third-party-notices.txt) alongside the rendered site.
To preview the site locally:

```bash
uv sync --locked --all-groups
uv run --group docs mkdocs serve
```

`mkdocs.yml` admits only the named public files under `docs/`. When adding a page, update its
navigation and file allowlist, the sdist include list in `pyproject.toml`, and the documentation
manifests in the documentation and packaging tests. Never set `docs_dir` to the repository root.

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
the `github-pages` environment to `main` before the first deployment. No custom domain, separate
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

Returned actions never execute. Hosted tools, MCP and tool-side network effects remain forbidden.
An input-token cap cannot be enforced. Private evidence records known counts and unknown
acceptance explicitly. Unsupported or unsuccessful profiles fail the test without skipping or retrying.

## Preparing a change

Keep changes focused and add tests for changed behavior and failure paths. Update the relevant
user guide when commands or results change. Use Conventional Commit subjects, such as
`fix(extract): reject conflicting selectors`.

Changes to native protocols, reconstruction, permissions or provider accounting need review of
the affected safety boundary. Mock tests establish local contracts; claims of native support also
need capability, isolation, calibration and live-use evidence for the target profile.

## Preparing a release

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
