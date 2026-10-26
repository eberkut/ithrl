# Synthetic reduction studies

Evaluate screening and sampling decisions, and reproduce the baseline-sharing and native-layout
budget studies using synthetic Bernoulli observations. These scripts use no harness, provider,
credentials or network service.
[Reduction design](../../docs/research/reduction-design.md) explains the methods, results and
decisions; this directory supplies executable models and reference data.

## Studies

| Replay | Question | Reference result |
| --- | --- | --- |
| `decisions` | Why separate exploratory screening from confirmation, and how should attempts be bounded? | An independent finite evaluation matches 44 documented summary values and checks sample sizes, failure tolerance and minimal completion ceilings. |
| `eager` | Does prefetching sibling candidates reduce complete-workflow cost? | Unused candidate work increases cost at some instruction positions; the cost criteria reject adoption. |
| `sharing` | Does lazy sharing help, and is a smaller fresh-original sample a simpler alternative? | Lazy groups of two save about 6.61% of calls per useful result in the primary synthetic workload. The 40/80 allocation saves more but misses the declared mildly degraded usefulness criterion. |
| `operational` | Does the selected lazy policy retain its benefit with failures, drift, expiry and resume? | Benefits depend on freshness; the declared expiry scenario reduces useful completion to 25%. |
| `native` | How many comparisons does the planted native-history layout need? | The ten-comparison configuration falls below the usefulness criterion; fourteen is the smallest tested passing cap. |

The models assume specified rates and validity rules. Synthetic call counts measure modeled work.
The [native measurements](../../docs/measurements.md) report observed harness costs and outcomes.
Baseline sharing remains outside supported execution.

## Contents

| Files | Purpose |
| --- | --- |
| `reference.py` | Fixed search order, candidate identity, exploratory admission and Newcombe confirmation algorithms |
| `decisions.py`, `data/decisions-checks.json` | Independent decision evaluation and documented summary checks with their rounding tolerances |
| `study.py` | Eager candidate sampling, fixed schedules, failure/drift models and cost criteria |
| `stationary.py`, `driver.py` | Finite-binomial lazy-sharing and allocation comparisons |
| `stress.py`, `operational.py` | Seeded lazy-policy simulation and operational summaries |
| `native_layout.py`, `native_budget.py` | Finite enumeration of the planted five-group layout and comparison-cap family |
| `data/*-programme.json` | Parameters, sample counts, seeds, workload weights and acceptance criteria |
| `data/*-results.json` | Per-cell summaries, aggregates, bounds and decisions |
| `data/search-tree.json` | All 64 decision paths for the four-group search model |
| `provenance.json` | Source identities and changes needed to make the reference portable |
| `reproduce.py` | Source freezing, reference checks and replay commands |

The reference algorithms are independent of the installed application. The synthetic byte
encoder is checked against all 32 recorded removal fixtures. Stationary terminal details are
regenerated during replay; the reference data retains every cell summary and aggregate.

## Reproduce

Run from the repository root on Linux with Python 3.12–3.14. Install the locked environment using
the [contributor setup](../../CONTRIBUTING.md#development-setup). SciPy and NumPy are included by
`uv sync --locked --all-groups --all-extras`; the commands below then run offline.

Start with the short verification:

```sh
uv run --locked --offline --no-sync python research/reduction/reproduce.py freeze --output research/reduction/outputs/check
uv run --locked --offline --no-sync python research/reduction/reproduce.py verify --output research/reduction/outputs/check
```

Verification checks:

- All 64 search paths and 32 byte fixtures.
- Every stationary and operational summary, plus the native-layout enumeration.
- Forty independent statistical reference vectors.
- The selected sampling decisions: 44 documented summaries, 48 independent workflow comparisons,
  24 rational admission calculations and 127 diagnostic rate pairs.

It prints the checked counts and writes `verification.json`.

To run all evaluations, freeze a new output directory and run the full replay:

```sh
uv run --locked --offline --no-sync python research/reduction/reproduce.py freeze --output research/reduction/outputs/full
uv run --locked --offline --no-sync python research/reduction/reproduce.py replay --output research/reduction/outputs/full
```

To run one evaluation, pass `--study decisions`, `--study eager`, `--study sharing`,
`--study operational` or `--study native` to the second command. The eager study simulates 4.8
million workflows. The operational study simulates 1.76 million workflows and scores each against
two byte layouts; those scores share outcomes and are not
independent samples. Runtime depends on hardware. The configured runtime limits leave sample counts
and criteria unchanged if a run cannot finish in time.

All generated files stay in the required directory under ignored `outputs/`. Choose a new
directory for each run. Freezing refuses an existing directory, result writes use exclusive
creation, and subsequent commands reject changed sources or numerical dependency versions.
Output paths that traverse symlinks are refused.

A freeze records the public reference used for a reproduction. It cannot establish that a new
experiment was declared before its outcomes were evaluated.

Replay compares every scientific result field with the reference data. Integer values must agree
exactly; floating-point comparisons allow absolute differences of `1e-10` or relative differences
of `1e-12`. Timing and source-identity metadata are recorded separately. These tolerances cover
numerical reproduction; they do not establish interval coverage or provider independence.

## Scope

The eager replay covers its executed stationary cost stage, with two plans, three policies and
four instruction positions. Its rejection ended the adoption evaluation before the full risk
family. The sharing replay covers all 280 stationary cells, and the operational replay covers
all 176 scored rows. The native replay covers the ten-comparison baseline and caps fourteen
and twenty at each declared position and failure rate.

The independent decision evaluation covers eight screening choices, five failure-tolerance plans at
four failure rates, and six confirmation sample sizes. Its 127 rate pairs follow the explicit recipe
in `decisions-programme.json`; rate differences and boundary labels use rational arithmetic. It
validates the selected documented figures without enumerating all configurations of the earlier
198-, 72-, 312- or 224-setting sweeps. Alternative score methods are described in the design
research and are outside this numerical evaluation.

The conditional screen comparison assumes successful calibration and valid observations. Its call
ceiling includes two calibration calls. The failure-tolerance comparison separately includes
calibration readiness and incomplete blocks under independent safe preaccept failures. Neither
sets a default for ordinary workloads. Synthetic efficiency does not establish native sharing
safety, representative session success, provider-token savings or monetary cost.
