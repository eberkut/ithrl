# Agent skill

The `ithrl` skill guides an agent through session inspection, experiments and result interpretation
using the public guides and CLI. The agent invoking it need not be Codex or Copilot.

Install ithrl first, following the
[installation instructions](https://github.com/eberkut/ithrl/blob/main/README.md#install).
Choose one of the routes below for the skill. Neither installer installs or upgrades the ithrl CLI.

## From a Python project environment

Use [Library Skills](https://library-skills.io/use/) when ithrl is installed in the current
project's Python environment. Run from the project root. Discovery uses the project environment,
even though `uvx` runs Library Skills itself in isolation:

```bash
uvx library-skills scan --all --json
uvx library-skills install --skill ithrl --copy
```

Before installing, confirm the scan entry is named `ithrl`, comes from package `ithrl` and has the
expected package version. `--all` includes installed packages outside declared direct dependencies.
For Claude Code, add `--claude` when installing to also create `.claude/skills/ithrl`.

The default destination is the project's `.agents/skills/ithrl`. This route has no user-wide
installation option. Library Skills does not automatically discover an isolated
`uv tool install ithrl` environment; use the repository route for that installation.

## From the repository

Use [Vercel Skills](https://github.com/vercel-labs/skills) with Node.js 22.20 or later. Replace
`REF` with the tag matching the installed release, or the full source commit for a build from
unreleased source. A development build's version label may still name an older release.
List the available skill before installation:

```bash
npx skills add eberkut/ithrl@REF --list
npx skills add eberkut/ithrl@REF --skill ithrl --copy
```

Select the agent destinations in the installer. Project scope is the default; add `--global` for
user scope. Review the destination before installing: Vercel can replace an existing same-name
skill directory. A pinned tag or commit selects skill content; it does not verify CLI compatibility.

## Verify and update

Both routes copy the same skill and its references. After installation, ask the agent to use the
ithrl skill for the [local example](https://github.com/eberkut/ithrl/blob/main/README.md#try-it),
or for inspection of a saved result. Installing or activating the skill grants no provider-call
approval. Native execution still requires review of the provider, safety policy and call/time
limits described in [native runs](native-runs.md).

Use one installer per destination. Before an update, identify the existing copy and preserve any
local changes. Library Skills refuses to replace a real directory; remove only a known,
replaceable installed copy before running it again. Vercel can replace that directory during
installation.

Update ithrl and its skill together. Check `ithrl --version`, known source revision and command
help; the documentation site follows `main`, while the skill explains how to find guides at the
matching release tag. An unavailable command is a version mismatch to resolve before following that
workflow.

## Maintained source

[`skills/ithrl`](https://github.com/eberkut/ithrl/tree/main/skills/ithrl) is the single editable
source. Python wheels include it at `ithrl/.agents/skills/ithrl`; source distributions retain
`skills/ithrl` so rebuilt wheels carry the same files. Repository installers read the canonical
directory directly. Contributor guidance remains in `CONTRIBUTING.md` and `AGENTS.md`.
