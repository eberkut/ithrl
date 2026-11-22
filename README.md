<!-- rumdl-disable MD033 -->
<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/eberkut/ithrl/main/docs/branding/ithrl-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/eberkut/ithrl/main/docs/branding/ithrl-logo-light.svg">
    <img src="https://raw.githubusercontent.com/eberkut/ithrl/main/docs/branding/ithrl-logo.png" alt="ithrl" width="320" height="158">
  </picture>
</h1>
<!-- rumdl-enable MD033 -->

[![PyPI](https://img.shields.io/pypi/v/ithrl.svg)](https://pypi.org/project/ithrl/)
[![CI](https://github.com/eberkut/ithrl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/eberkut/ithrl/actions/workflows/ci.yml)

`ithrl` turns saved Codex and GitHub Copilot sessions into debugging experiments. Find a smaller
context that preserves a chosen response or tool request, test text changes that alter it, or
compare two selected contexts from the same source.

[Documentation](https://eberkut.github.io/ithrl/) · [Try it](#try-it) ·
[Supported inputs](#supported-inputs)

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install 'ithrl[all]'
```

This installs all features. You can also use `pipx install 'ithrl[all]'` or, in an existing virtual
environment, `python -m pip install 'ithrl[all]'`. For a smaller installation, use `ithrl` without
extras, or select `'ithrl[fuzz]'` for context fuzzing and `'ithrl[compare]'` for context comparison.

Tested on Python 3.12–3.14. Native runners require Linux. APIs and saved formats may change before
1.0; keep your source sessions and record the version used for each case.

## Try it

The bundled deterministic example needs no account, agent installation or model access:

```bash
mkdir -m 700 ithrl-demo &&
  ithrl extract --example --output ithrl-demo/case &&
  ithrl shrink ithrl-demo/case --observation-store ithrl-demo/observations.sqlite
```

The result removes half the context items and confirms that the target is preserved. Output excerpt:

```text
Reduced and confirmed
  context items   10 -> 5
  original test   target observed
  search          1-minimal; all valid single removals tested
  confirmation    target preserved
  observations    15
  No network requests or provider calls. No actions executed.
```

Inspect the saved result in `ithrl-demo/case/results/`; the
[results guide](https://github.com/eberkut/ithrl/blob/main/docs/results.md) explains its files.
Use a new directory to repeat the example; extraction refuses to overwrite an existing case.

## Use with a coding agent

The `ithrl` agent skill guides your coding agent through inspecting saved sessions, preparing
experiments and interpreting results. The agent using the skill need not be the harness that
recorded the session.

Install it through [Library Skills](https://library-skills.io/) when ithrl is installed in your
Python project environment, or through [Vercel Skills](https://github.com/vercel-labs/skills) from
the repository. The
[installation guide](https://github.com/eberkut/ithrl/blob/main/docs/agent-skill.md) covers both
routes and matching the skill to your installed CLI.

After installation, try:

> Use the ithrl skill to run the bundled local example and explain the result.

For a saved session, provide its path and ask:

> Use the ithrl skill to prepare a context-removal experiment for this session. Show me the
> experiment plan and provider-call budget before execution.

## Commands

Choose a workflow for your debugging question:

| Task | Command | Guide |
| --- | --- | --- |
| Create a case from a saved decision | `ithrl extract` | [Select a decision](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md#select-a-saved-decision) |
| Find a smaller context that preserves the behavior | `ithrl prepare`, then `ithrl shrink` | [Context reduction](https://github.com/eberkut/ithrl/blob/main/docs/reduction.md) |
| Find text appends that decrease the target's match rate | `ithrl fuzz` | [Context fuzzing](https://github.com/eberkut/ithrl/blob/main/docs/fuzzing.md) |
| Test a selected context change | `ithrl compare` | [Context comparison](https://github.com/eberkut/ithrl/blob/main/docs/comparison.md) |
| Inspect saved context, compaction or results without model calls | `ithrl inspect` | [Results and privacy](https://github.com/eberkut/ithrl/blob/main/docs/results.md) |
| Estimate costs from recorded or hypothetical token usage | `ithrl cost` | [Token-cost estimates](https://github.com/eberkut/ithrl/blob/main/docs/results.md#estimate-token-costs) |

Inspection covers retained context and removal trials (`inspect retained`), editable paragraph
boundaries (`inspect sections`), source-bound tool output (`inspect tool`) and recorded operations
across compaction (`inspect reacquire`). Default inspection output omits private text.

For example, extract a saved session file:

```bash
ithrl extract path/to/session.jsonl --output .ithrl/case
```

Extraction makes no provider calls. Native experiments require Linux, usable Bubblewrap, an
installed harness and model access. Review the provider, safety controls and request/time limits in
the [native-run guide](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md) before
execution. Returned actions are observed and denied. Cases contain copied session content; keep them
private.

## Supported inputs

| Input | Extracted decisions | Reduction | Fuzzing | Comparison | Passive inspection |
| --- | --- | --- | --- | --- | --- |
| Bundled synthetic example | Synthetic tool request | Deterministic local runner | Local fake runner | Local fake runner | — |
| Codex CLI rollouts | Final responses | Experimental native runner; [paragraph-removal measurements](https://github.com/eberkut/ithrl/blob/main/docs/measurements.md#ordinary-session-paragraph-removal) | Experimental prior-user text appends | Experimental final-response [context bundles](https://github.com/eberkut/ithrl/blob/main/docs/research/comparative-debugging.md) and [tool output text spans](https://github.com/eberkut/ithrl/blob/main/docs/research/tool-output-interventions.md) under their qualified profiles | Exact recorded-operation and captured-result comparisons across compaction |
| Copilot CLI session events | Final responses and shell requests | Experimental final-response paragraph removal and denied `bash` runner | Experimental prior-user text appends for final-response targets | Experimental final-response and denied `bash` [context bundles](https://github.com/eberkut/ithrl/blob/main/docs/research/comparative-debugging.md); final-response [tool output text spans](https://github.com/eberkut/ithrl/blob/main/docs/research/tool-output-interventions.md) under their qualified profiles | Exact recorded-operation and captured-result comparisons across compaction |

Passive inspection reads saved evidence without a harness or model access. The
[worked investigation](https://github.com/eberkut/ithrl/blob/main/docs/research/passive-compaction-investigation.md)
records the qualified native profiles, ordinary examples and interpretation limits.

Native fuzzing appends text to eligible prior user input while preserving the fixed triggering
input. The
[fuzzing walkthrough](https://github.com/eberkut/ithrl/blob/main/docs/fuzzing.md)
covers a local example and native execution.

[Worked comparisons](https://github.com/eberkut/ithrl/blob/main/docs/research/comparative-debugging.md)
test complete context changes in ordinary Codex and Copilot investigations. For localized changes,
[select a source-bound tool output text span](https://github.com/eberkut/ithrl/blob/main/docs/comparison.md#select-tool-output-text);
the
[tool output study](https://github.com/eberkut/ithrl/blob/main/docs/research/tool-output-interventions.md)
records qualified final-response profiles and outcomes.

Ordinary Codex and Copilot paragraph-removal experiments passed fresh confirmation at a
20-percentage-point preservation margin. See the
[measurements](https://github.com/eberkut/ithrl/blob/main/docs/measurements.md#saved-session-experiments)
for completed and unsuccessful outcomes, costs and qualification limits, and the
[worked investigation](https://github.com/eberkut/ithrl/blob/main/docs/research/ordinary-session-investigation.md)
for a developer's interpretation. Check
[native compatibility](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md#compatibility-and-measurements)
before using an installed harness.

## Documentation

The [documentation site](https://eberkut.github.io/ithrl/) provides the guides above,
[CLI reference](https://eberkut.github.io/ithrl/cli/) and
[troubleshooting](https://eberkut.github.io/ithrl/troubleshooting/).
For development, see [Contributing](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md) and
[architecture](https://github.com/eberkut/ithrl/blob/main/docs/architecture.md).
The [roadmap](https://github.com/eberkut/ithrl/blob/main/ROADMAP.md) records direction;
[research](https://github.com/eberkut/ithrl/blob/main/docs/research/index.md) records methods and
evidence.

For help, bugs or proposals, see [Support](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md).
Report vulnerabilities through
[private reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## Name

Pronounce `ithrl` as **Ithuriel**, the angel whose spear reveals Satan in disguise in Milton's
[*Paradise Lost*](https://rpo.library.utoronto.ca/content/paradise-lost-book-iv-1674).

## License

Licensed under the [Apache License 2.0](https://github.com/eberkut/ithrl/blob/main/LICENSE).
