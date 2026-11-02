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

## Saved-result provenance

The saved aggregates come from the completed study before public-source relocation. Its corrected
sources had these recorded SHA-256 identities:

| Source | SHA-256 |
| --- | --- |
| `programme.json` | `37c0a9b43c39f0e8f5c5bed89ae1295cd66ab980d0fe972b645fb718783b20b8` |
| `method.py` | `fb6c5d1ada3d6a2f49dd18f2f25704970665ae73bf9f619e66531da893c00b6e` |
| `study.py` | `75e2afac5112e865fce37905b9d0bda8666f8fd69002c1b634e3499593d9d8f6` |

Before sensitivity evaluation, filtering was corrected to use the mutant conditional rate `1/3`
at every placement. Reporting then excluded inapplicable paragraph-geometry metrics, and the same
sensitivity seed was replayed. Qualification parameters and draws were unchanged; twelve selected
qualification cells replayed identically after these corrections.

The historical source snapshot is not distributed with this reference; its recorded checksums alone
cannot support exact source-level reproduction. The commands below use the current public sources
and record their identities. The short replay compares its stated coverage with the saved
aggregates. The full qualification grid has not been rerun after public-source relocation.

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

The full replay simulates 7,128 qualification rows of 10,000 campaigns each (71,280,000 campaigns),
followed by 24 sensitivity cells and a drift block. Runtime depends on hardware. Run these commands
sequentially:

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
Each simulated attempt counts as one call; the study makes zero real provider requests.
The 512-call study bracket and selected 384-attempt allowance are simulation limits. Provider
quotas, token usage, monetary costs, model effect sizes and native runtime require measurements on
the relevant workload and runner profile. The [native measurements](../../docs/measurements.md)
report resource usage for completed Codex and Copilot experiments. This synthetic reference is
separate from the installed runtime and makes no native support claim.
