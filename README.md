# ithrl

`ithrl` extracts a decision from a saved coding-agent session and searches for a smaller context
that preserves its behavior. Use it to turn a long session into an inspectable debugging case.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ithrl
```

Or use `pipx install ithrl`. In an existing virtual environment, use `python -m pip install ithrl`.
Check the installation with `ithrl --help` and `ithrl --version`.

Tested on Python 3.12–3.14. Native runners require Linux. APIs and saved formats may change before
1.0; keep your source sessions and record the version used for each case.

## Try it

The bundled example needs no account, agent installation or model access:

```bash
mkdir -m 700 ithrl-demo
ithrl extract --example --output ithrl-demo/case
ithrl shrink ithrl-demo/case --observation-store ithrl-demo/observations.sqlite
```

The example starts with seven context markers, `A` through `G`. The result keeps `A` and `C`,
confirms the behavior, and reports zero provider calls and zero executed actions. Inspect it:

```bash
cat ithrl-demo/case/results/*/request.json
cat ithrl-demo/case/results/*/summary.json
```

All generated example data stays in `ithrl-demo`. Extraction refuses to overwrite an existing case;
use a new directory to repeat it. You can remove the directory and its contents when finished.

## Commands

- `ithrl extract` creates a case from a saved session. In a terminal, omit the source to browse
  available sessions and select a decision.
- `ithrl shrink CASE` tests context removals and saves the result. `--dry-run` inspects the plan;
  `--format json` produces machine-readable output. A stopped search prints its resume command.

For example, extract a saved session file:

```bash
ithrl extract path/to/session.jsonl --output .ithrl/case
```

Cases contain copied session content and should stay private. Extraction makes no provider calls
and never executes recorded actions. Native reduction makes model requests and requires an explicit
experiment; see the [native-run guide](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md).

### Supported inputs

| Input | Extracted decisions | Reduction |
| --- | --- | --- |
| Bundled synthetic example | Synthetic tool request | Deterministic local runner |
| Codex CLI rollouts | Final responses | Experimental native runner |
| Copilot CLI session events | Final responses and shell requests | Experimental final-response and denied `bash` runner |

Native compatibility is checked against the installed harness's protocol, capabilities and safety
controls. [Compatibility and measurements](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md#compatibility-and-measurements)
records the tested configurations and how to check another release.

## Documentation

- [Native runs](https://github.com/eberkut/ithrl/blob/main/docs/native-runs.md): session selection,
  experiment configuration, preflight, model execution and measured costs.
- [Results and privacy](https://github.com/eberkut/ithrl/blob/main/docs/results.md): saved files,
  preservation claims, research references, budgets, resume and sensitive data.
- [Contributing](https://github.com/eberkut/ithrl/blob/main/CONTRIBUTING.md): issue-first collaboration,
  checkout setup, checks and smoke tests.
- [Roadmap](https://github.com/eberkut/ithrl/blob/main/ROADMAP.md): current priorities and planned capabilities.
- [Architecture](https://github.com/eberkut/ithrl/blob/main/docs/architecture.md): implementation boundaries and decisions.
- [Research](https://github.com/eberkut/ithrl/blob/main/docs/research/index.md): feasibility studies, statistical methods and measurements.

For help, bugs or proposals, see [Support](https://github.com/eberkut/ithrl/blob/main/SUPPORT.md).
Report vulnerabilities through [private reporting](https://github.com/eberkut/ithrl/security/advisories/new).

## Name

Pronounce `ithrl` as **Ithuriel**, the angel whose spear reveals Satan in disguise in Milton's
[*Paradise Lost*](https://rpo.library.utoronto.ca/content/paradise-lost-book-iv-1674).

## License

Licensed under the [Apache License 2.0](https://github.com/eberkut/ithrl/blob/main/LICENSE).
