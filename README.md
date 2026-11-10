# ithrl

[![PyPI](https://img.shields.io/pypi/v/ithrl.svg)](https://pypi.org/project/ithrl/)
[![CI](https://github.com/eberkut/ithrl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/eberkut/ithrl/actions/workflows/ci.yml)

`ithrl` turns saved Codex and GitHub Copilot sessions into debugging experiments. Search for a
smaller context that preserves a chosen response or tool request, or a small text change that alters
it.

[Documentation](https://eberkut.github.io/ithrl/) · [Try it](#try-it) ·
[Supported inputs](#supported-inputs)

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ithrl
```

You can also install with `pipx install ithrl` or, in an existing virtual environment,
`python -m pip install ithrl`. For context fuzzing, use `'ithrl[fuzz]'` in place of `ithrl` in
the installation command.

Tested on Python 3.12–3.14. Native runners require Linux. APIs and saved formats may change before
1.0; keep your source sessions and record the version used for each case.

## Try it

The bundled deterministic example needs no account, agent installation or model access:

```bash
mkdir -m 700 ithrl-demo
ithrl extract --example --output ithrl-demo/case
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

## Commands

- `ithrl extract` browses saved sessions and creates a case from a selected decision.
- `ithrl shrink CASE` searches for context removals that preserve the selected behavior.
- `ithrl inspect retained CASE --result RESULT` explains retained units and recorded removal trials
  from a saved reduction without model access. See
  [results and privacy](https://github.com/eberkut/ithrl/blob/main/docs/results.md#inspect-retained-context).
- `ithrl fuzz CASE --config PRIVATE_TOML` searches for and simplifies text appends that decrease how
  often the selected behavior occurs, with independent confirmation.

For example, extract a saved session file:

```bash
ithrl extract path/to/session.jsonl --output .ithrl/case
```

Extraction makes no provider calls. Native experiments require Linux, usable Bubblewrap, an
installed harness and model access. Review the provider, safety controls and call/time budget in the
[native-run guide](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md) before execution.
Returned actions are observed and denied. Cases contain copied session content; keep them private.

## Supported inputs

| Input | Extracted decisions | Reduction | Fuzzing |
| --- | --- | --- | --- |
| Bundled synthetic example | Synthetic tool request | Deterministic local runner | Local fake runner |
| Codex CLI rollouts | Final responses | Experimental native runner; [paragraph-removal measurements](https://github.com/eberkut/ithrl/blob/main/docs/measurements.md#ordinary-session-paragraph-removal) | Experimental prior-user text appends |
| Copilot CLI session events | Final responses and shell requests | Experimental final-response paragraph removal and denied `bash` runner | Experimental prior-user text appends for final-response targets |

Native Codex and Copilot final-response fuzzing supports appending text to eligible prior user
input while preserving the fixed triggering input. The
[fuzzing walkthrough](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md#context-fuzzing)
starts with a local example and covers native execution.

[Saved-session measurements](https://github.com/eberkut/ithrl/blob/main/docs/measurements.md#saved-session-experiments)
include ordinary Codex and Copilot final-response experiments that removed paragraphs from retained
prior-user text and passed fresh confirmation at a 20-percentage-point preservation margin. The
[worked investigation](https://github.com/eberkut/ithrl/blob/main/docs/research/ordinary-session-investigation.md)
explains what the smaller Codex context helped a developer inspect and what remains unresolved.
The measurements distinguish private runtime qualification from integrated support and report
resource usage and unsuccessful outcomes alongside completed runs. Check
[native compatibility](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md#compatibility-and-measurements)
for the installed harness's requirements.

## Documentation

Read the [documentation site](https://eberkut.github.io/ithrl/) or browse the guides below.

- [Native runs](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md): session selection,
  experiment configuration, preflight, model execution and measured resource usage.
- [Results and privacy](https://github.com/eberkut/ithrl/blob/main/docs/results.md): saved files,
  result interpretation, budgets, resume and sensitive data.
- [Contributing](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md): issue-first
  collaboration, checkout setup, checks and smoke tests.
- [Roadmap](https://github.com/eberkut/ithrl/blob/main/ROADMAP.md): current priorities and planned
  capabilities.
- [Architecture](https://github.com/eberkut/ithrl/blob/main/docs/architecture.md): implementation
  boundaries and decisions.
- [Research](https://github.com/eberkut/ithrl/blob/main/docs/research/index.md): feasibility
  studies, statistical methods and measurements.

For help, bugs or proposals, see [Support](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md).
Report vulnerabilities through
[private reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## Name

Pronounce `ithrl` as **Ithuriel**, the angel whose spear reveals Satan in disguise in Milton's
[*Paradise Lost*](https://rpo.library.utoronto.ca/content/paradise-lost-book-iv-1674).

## License

Licensed under the [Apache License 2.0](https://github.com/eberkut/ithrl/blob/main/LICENSE).
