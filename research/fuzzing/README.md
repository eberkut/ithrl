# Synthetic fuzzing feasibility study

Reproduce the discovery, paragraph simplification and fresh-confirmation study using synthetic
Bernoulli responses. The scripts generate no prompts and invoke no harness, provider or network
service. The [research record](../../docs/research/context-fuzzing.md) owns the question, methods,
results, alternatives, validity assumptions and limitations.

| Artifact | Contents |
| --- | --- |
| `programme.json` | Frozen domain, seeds, acceptance gates and selection rule |
| `method.py` | Independent one-sided exact-binomial bounds and rejection probabilities |
| `study.py` | Numerical algorithm, complete-workflow simulation, analysis and sensitivities |
| `results.json` | Historical aggregate design analysis, method summaries, twelve replay cells and sensitivity results |
| `selected-cells.csv` | All 64 selected adaptive single-component cells, including rates, bounds, cost distributions and terminal counts |
| `validate_results.py` | Independent bookkeeping, method and same-seed replay checks |

Shared CSV configuration is in `results.json` under `selected_cells.common`. The
`selected_cells.by_invalid` summaries describe equally weighted cells; they are not pooled cost
quantiles. The numerical algorithm and seeded domain are preserved from the completed study.
The saved terminal counts use the full `calibration-not-measurable` label, correcting historical
string truncation without changing any counts or numerical results.

## Reproduce

Run from the repository root with Python 3.12–3.14 and the repository's locked `fuzz` extra.
SciPy supplies NumPy transitively; the recorded numerical versions are NumPy 2.5.4 and
SciPy 1.18.1. There is no separate requirements file. If dependencies are not installed,
prepare the environment with `uv sync --locked --extra fuzz`. This preparation may download
locked packages; the study itself is offline and needs no credentials.

The short replay checks:

- Exact bounds against SciPy and binomial tail equations, plus the finite operating-characteristic
  grids.
- Twelve selected cells across all three workflows and the outcome-dependent validity sensitivity:
  130,000 synthetic campaigns in total.
- All 64 retained cells against their per-invalid-rate summaries and selected-design aggregates,
  including count and budget consistency.

Saved cell aggregates must match exactly; numerical method summaries allow differences within
`1e-12`. The short replay checks the retained 64-cell data without resimulating the whole grid.

```sh
uv run --locked --offline --no-sync python research/fuzzing/study.py freeze --output research/fuzzing/outputs/replay
uv run --locked --offline --no-sync python research/fuzzing/validate_results.py --output research/fuzzing/outputs/replay
```

Every generated file belongs to the required `--output` directory under the ignored
`research/fuzzing/outputs/` tree. Choose a new directory for each run: freezing requires
a directory that does not exist, and writes use exclusive creation. The freeze records
the public programme, numerical sources, validator, saved results and dependency versions;
later commands verify them. Each reproduction records its own source and dependency identities.
The short replay covers the checks listed above; use the full replay to evaluate the complete
qualification grid.

The full replay is outside default CI. It simulates 7,128 qualification rows of 10,000 campaigns
each (71,280,000 campaigns), followed by 24 sensitivity cells and a drift block. Runtime depends
on hardware. Run these commands sequentially:

```sh
uv run --locked --offline --no-sync python research/fuzzing/study.py freeze --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py method --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py workflow --budget 256 --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py workflow --budget 384 --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py workflow --budget 512 --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py analyse --output research/fuzzing/outputs/full
uv run --locked --offline --no-sync python research/fuzzing/study.py sensitivity --output research/fuzzing/outputs/full
```

The generated `analysis.json` can be compared with `results.json`'s `analysis` object.
Synthetic attempted calls are bookkeeping units, with one simulated provider call per
attempt; there are zero real provider calls and zero provider charges. The 512-call maximum
study bracket and the selected 384-attempt allowance do not establish a subscription quota,
token cost, model effect size or native runtime. Those require separately approved,
budget-limited live studies for an exact safe runner profile. This research reference is
separate from the installed runtime and makes no native support claim.
