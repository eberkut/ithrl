from __future__ import annotations

import pytest

from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    ExperimentFormatError,
    FakeRunnerConfig,
    StructuralOracleConfig,
)


def _shell_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(
        target=ShellActionTarget(
            tool_name_equals="bash",
            command_equals="pwd",
        ),
        oracle=StructuralOracleConfig(protected_context_ids=("ctx-tool-bash",)),
        runner=FakeRunnerConfig(
            required_markers=("A", "C"),
            alternative_marker_sets=(("B", "D"),),
            tool_name="bash",
            command="pwd",
        ),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )


def test_experiment_toml_round_trip_is_canonical_and_digest_stable() -> None:
    experiment = _shell_experiment()

    rendered = experiment.to_toml()
    loaded = ExperimentDefinition.from_toml(rendered)

    assert loaded == experiment
    assert loaded.digest == experiment.digest
    assert loaded.target.digest == experiment.target.digest
    assert rendered == loaded.to_toml()
    assert '[calibration]\nrole = "calibration"\nruns = 1' in rendered
    assert '[search]\nrole = "search"\nruns = 1' in rendered
    assert '[confirmation]\nrole = "confirmation"\nruns = 1' in rendered


def test_final_response_experiment_round_trip() -> None:
    experiment = ExperimentDefinition(
        target=FinalResponseTarget(text_contains="ready"),
        oracle=StructuralOracleConfig(),
        runner=FakeRunnerConfig(required_markers=("A",), final_text="ready to continue"),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )

    assert ExperimentDefinition.from_toml(experiment.to_toml()) == experiment


def test_experiment_rejects_phase_role_mismatch_and_non_deterministic_run_count() -> None:
    source = _shell_experiment().to_toml()

    with pytest.raises(ExperimentFormatError, match="phase roles must match"):
        ExperimentDefinition.from_toml(source.replace('role = "search"', 'role = "calibration"'))
    with pytest.raises(ExperimentFormatError, match="Input should be 1"):
        ExperimentDefinition.from_toml(source.replace("runs = 1", "runs = 2", 1))


def test_experiment_rejects_fake_behavior_that_cannot_match_target() -> None:
    source = _shell_experiment().to_toml().replace('command = "pwd"', 'command = "ls"')

    with pytest.raises(ExperimentFormatError, match="cannot satisfy"):
        ExperimentDefinition.from_toml(source)

    original = _shell_experiment()
    with pytest.raises(ValueError, match="cannot satisfy"):
        ExperimentDefinition(
            target=ShellActionTarget(tool_name_equals="bash", count=2),
            oracle=original.oracle,
            runner=original.runner,
            calibration=original.calibration,
            search=original.search,
            confirmation=original.confirmation,
        )


def test_experiment_rejects_non_matching_final_response() -> None:
    experiment = ExperimentDefinition(
        target=FinalResponseTarget(text_equals="ready"),
        oracle=StructuralOracleConfig(),
        runner=FakeRunnerConfig(required_markers=("A",), final_text="ready"),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )
    source = experiment.to_toml().replace('final_text = "ready"', 'final_text = "not ready"')

    with pytest.raises(ExperimentFormatError, match="cannot satisfy"):
        ExperimentDefinition.from_toml(source)


def test_fake_runner_rejects_duplicate_or_ambiguous_behavior() -> None:
    with pytest.raises(ValueError, match="marker sets must be distinct"):
        FakeRunnerConfig(
            required_markers=("A", "B"),
            alternative_marker_sets=(("B", "A"),),
            tool_name="bash",
        )
    with pytest.raises(ValueError, match="exactly one supported behavior family"):
        FakeRunnerConfig(
            required_markers=("A",),
            tool_name="bash",
            final_text="ready",
        )


def test_experiment_parser_rejects_unknown_or_malformed_input() -> None:
    source = _shell_experiment().to_toml()

    with pytest.raises(ExperimentFormatError, match="Extra inputs are not permitted"):
        ExperimentDefinition.from_toml(source + "\nunknown = true\n")
    with pytest.raises(ExperimentFormatError, match="Invalid value"):
        ExperimentDefinition.from_toml("schema_version =")


def test_experiment_digest_changes_with_protected_context() -> None:
    original = _shell_experiment()
    changed = original.model_copy(
        update={"oracle": StructuralOracleConfig(protected_context_ids=("other",))}
    )

    assert original.digest != changed.digest
