"""Output-boundary and comparison regressions; no simulation or provider execution."""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import decisions
import pytest
import reproduce

# Assertions express test expectations.
# ruff: noqa: S101


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "research"
    path.mkdir()
    monkeypatch.setattr(reproduce, "ROOT", path)
    monkeypatch.setattr(reproduce, "OUTPUTS", path / "outputs")
    monkeypatch.setattr(reproduce, "source_hashes", lambda: {"reference.py": "fixed"})
    return path


def test_output_path_requires_a_child_of_the_output_tree(root: Path) -> None:
    for invalid in (root, root / "outputs", root / "elsewhere", root / "outputs/../../escape"):
        with pytest.raises(ValueError, match="run directory"):
            reproduce.output_directory(str(invalid))
    assert reproduce.output_directory(str(root / "outputs/run")) == root / "outputs/run"


@pytest.mark.parametrize("nested", [False, True])
def test_output_path_rejects_symlink_components(root: Path, nested: bool) -> None:
    outside = root.parent / "outside"
    outside.mkdir()
    outputs = root / "outputs"
    if nested:
        outputs.mkdir()
        (outputs / "linked").symlink_to(outside, target_is_directory=True)
        candidate = outputs / "linked/run"
    else:
        outputs.symlink_to(outside, target_is_directory=True)
        candidate = outputs / "run"
    with pytest.raises(ValueError, match="symlink"):
        reproduce.output_directory(str(candidate))
    assert list(outside.iterdir()) == []


def test_freeze_refuses_existing_directory_and_changed_sources(root: Path, monkeypatch) -> None:
    output = reproduce.output_directory(str(root / "outputs/run"))
    reproduce.freeze(output)
    original = (output / "freeze.json").read_bytes()
    reproduce.verify_freeze(output)
    with pytest.raises(FileExistsError):
        reproduce.freeze(output)
    assert (output / "freeze.json").read_bytes() == original
    monkeypatch.setattr(reproduce, "source_hashes", lambda: {"reference.py": "changed"})
    with pytest.raises(ValueError, match="changed after freeze"):
        reproduce.verify_freeze(output)


def test_result_write_refuses_an_existing_file(root: Path) -> None:
    reproduce.write(root, "result.json", {"count": 1})
    original = (root / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        reproduce.write(root, "result.json", {"count": 2})
    assert (root / "result.json").read_bytes() == original


@pytest.mark.parametrize(
    ("actual", "expected"),
    [(2, 1), (0.21, 0.2), (True, 1.0), ({"n": 1}, {"n": 1, "valid": 1})],
)
def test_comparison_rejects_changed_scientific_results(actual, expected) -> None:
    with pytest.raises(AssertionError):
        reproduce.compare(actual, expected)


def test_comparison_accepts_numeric_encoding_and_metadata_differences() -> None:
    reproduce.compare(
        {"count": 1.0, "probability": 0.2 + 1e-13, "elapsed_seconds": 5},
        {"count": 1, "probability": 0.2, "elapsed_seconds": 1},
    )


@pytest.mark.parametrize("endpoint", ["lower", "upper"])
def test_formula_reference_rejects_drift_above_absolute_tolerance(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = reproduce.reference.newcombe_interval

    def perturbed(*args, **kwargs):
        interval = original(*args, **kwargs)
        bound = getattr(interval, endpoint)
        if 0.2 <= abs(bound) <= 0.9:
            return replace(interval, **{endpoint: bound + 1e-10})
        return interval

    monkeypatch.setattr(reproduce.reference, "newcombe_interval", perturbed)
    with pytest.raises(AssertionError):
        reproduce.check_saved_results()


def test_rational_point_screen_has_known_probability() -> None:
    assert decisions.rational_point_probability(
        1, 1, Fraction(1, 2), Fraction(1, 2), Fraction()
    ) == Fraction(3, 4)
    assert decisions.rational_point_probability(
        1, 1, Fraction(1, 2), Fraction(), Fraction()
    ) == Fraction(1, 2)


def test_attempt_ceiling_is_minimal_and_rejects_impossible_targets() -> None:
    assert decisions.attempt_ceiling(1, 0.5, 0.75) == 2
    for required, failure, target in ((0, 0.1, 0.9), (1, 1.0, 0.9), (1, 0.1, 1.0)):
        with pytest.raises(ValueError, match="positive target"):
            decisions.attempt_ceiling(required, failure, target)


def test_independent_workflow_multiplies_fresh_phase_probabilities() -> None:
    tree = {
        "nodes": {"": {"effective": ["g1", "g2"]}},
        "terminals": {
            "0": {"complete": True, "retained": ["g0", "g1", "g2"], "removed": []},
            "1": {"complete": True, "retained": ["g0"], "removed": ["g1", "g2"]},
        },
    }
    result = decisions.workflow(
        tree,
        instruction_position=0,
        retained_admission=0.75,
        removed_admission=0.25,
        block_completion=0.8,
        calibration_ready=0.9,
        confirmation_complete=0.7,
        confirmation_preserves=0.6,
    )
    assert result["useful_probability"] == pytest.approx(0.2268)
    assert result["search_incomplete_sampling_given_calibration"] == pytest.approx(0.2)


def test_diagnostic_rate_domain_keeps_exact_boundaries_and_reflections() -> None:
    programme = reproduce.read("decisions-programme.json")
    pairs = decisions.rate_pairs(programme["confirmation_grid"])
    assert len(pairs) == len(set(pairs)) == 127
    assert (Fraction(1), Fraction(4, 5)) in pairs
    assert (Fraction(1, 5), Fraction()) in pairs
    assert all(0 <= p <= 1 and 0 <= q <= 1 for p, q in pairs)
