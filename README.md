# ithrl

`ithrl` tests how changes to an agent's saved context affect a model decision. It extracts one
decision and searches for a smaller context that preserves the selected behavior without breaking
context dependencies.

Pronounce `ithrl` as **Ithuriel**. The name refers to the angel in John Milton's
[*Paradise Lost*](https://rpo.library.utoronto.ca/content/paradise-lost-book-iv-1674) whose spear
reveals Satan in disguise.

The project does not claim that a reduced context is the unique cause of a behavior or the
smallest possible context. A completed reduction is reproducible and search-1-minimal under the
recorded execution method, behavior test, search algorithm, and budget. This means that the search
tested every valid single-group removal from the final context and none preserved the behavior.

## Current status

The current pre-release version supports synthetic saved evidence and a deterministic local
runner. This workflow does not use provider credentials or make network requests.

Adapters and runners for real coding agents are not available yet. The synthetic workflow cannot
measure real-model reliability, suitability, or cost.

## Research basis

The project design draws on these papers and preprints:

- [*Same Model, Different Harness: Different Coding-Agent Results*](https://arxiv.org/abs/2608.26218)
  by Sydney Lewis reports that harness-level context management can change coding-agent results
  when the model and task stay fixed.
- [*What Does Context Compression Cost an Agent? Interaction Costs Unrevealed by Task-Completion
  Metrics*](https://arxiv.org/abs/2608.16370) by Shuyu Liu motivates the planned analysis of
  information that an agent must retrieve again after context is removed.
- [*Semantic Invariance in Agentic AI*](https://arxiv.org/abs/2603.13173) by I. de Zarzà, J. de
  Curtò, Jordi Cabot, Pietro Manzoni, and Carlos T. Calafate motivates testing whether
  meaning-preserving context changes alter behavior.
- [*Causal Agent Replay: Counterfactual Attribution for LLM-Agent
  Failures*](https://arxiv.org/abs/2606.08275) by Jaineet Shah is related intervention-based work.
  `ithrl` studies one decision and does not claim to identify the cause of an entire failed run.
- [*Interval estimation for the difference between independent proportions: comparison of eleven
  methods*](https://pubmed.ncbi.nlm.nih.gov/9595617/) by R. G. Newcombe provides the confidence
  interval method planned for comparisons between stochastic runs.

These papers motivate the design; they do not validate `ithrl`. The current synthetic workflow
does not yet implement stochastic comparison, context fuzzing, or retrieval analysis.

## Develop from a checkout

Prerequisites:

- [uv](https://docs.astral.sh/uv/)
- Python 3.12, 3.13, or 3.14; Python 3.14 is the primary development version

Clone the repository, replacing `REPOSITORY_URL` with its Git URL, then synchronize the locked
development environment:

```bash
git clone REPOSITORY_URL ithrl
cd ithrl
uv sync --locked --all-groups
```

Run the same validation entry points used by CI:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run radon cc src/ithrl --min B --show-complexity --total-average
uv run radon mi src/ithrl --min B --show
uv run pyrefly check
uv run pytest
uv build --sdist --wheel --no-sources
```

The default test suite and walkthrough do not use provider credentials or make network requests.

## Try the synthetic workflow

Extract the fixture's selected decision into a local case, then reduce it:

```bash
uv run ithrl extract tests/fixtures/synthetic-trace.json \
  --inference synthetic-ac-001 \
  --output .ithrl/demo-case
uv run ithrl shrink .ithrl/demo-case
```

The expected result retains the synthetic markers `A` and `C`, reports zero provider calls,
and saves an inspectable result under `.ithrl/demo-case/results/`. Remove `.ithrl/demo-case`
before repeating the walkthrough because extraction never overwrites an existing case.

Use these options when you need a narrower operation or machine-readable output:

```bash
# Validate the case and show the execution plan without running it.
uv run ithrl shrink .ithrl/demo-case --dry-run

# Test only the original context.
uv run ithrl shrink .ithrl/demo-case --calibrate-only

# Return the same command result as one JSON object on stdout.
uv run ithrl shrink .ithrl/demo-case --format json
```

Every completed run prints its result directory. Start with `summary.json` for the outcome,
evidence identifiers, counts, and next action. `request.json` is the request saved for that result:
the original request after calibration-only runs or the current reduced request after a search.
`retained-context.jsonl` records the retained context entries, and
`checkpoint.json` contains the search evidence needed to verify or resume the reduction. The
remaining files preserve the experiment settings, execution plan, and capture source.

Print the installed version when reporting a problem:

```bash
uv run ithrl --version
```

The synthetic trace defines one behavior to test for each recorded decision. This pre-release
workflow does not let you choose a different behavior interactively. By default, observations are
stored in the standard user-data directory for the current platform. Use
`--observation-store PATH` for a dedicated SQLite store. Place a custom store in a directory that
only the current user can access.

`shrink` tests at most 100 candidate contexts by default. If the limit is reached, it saves a
partial result and prints a command that resumes with a larger total limit. The supported hard
limit is 10,000 candidate contexts.

Cases can contain prompts, source code, command output, paths, and other sensitive data. Treat
local ithrl evidence as private. Never commit unsanitized cases, traces, observation databases,
or credentials. On POSIX systems, ithrl refuses case files and directories that do not have their
expected private permissions.

Content digests detect accidental or inconsistent changes. They are not signatures. ithrl assumes
that another process running as the same user has not replaced the local case, result, run state,
or observation database.

## License

Licensed under the [Apache License 2.0](LICENSE).
