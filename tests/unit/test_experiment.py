from __future__ import annotations

import tomllib

import pytest
from pydantic import TypeAdapter

from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.experiment import (
    AnyRunnerConfig,
    DeterministicPhase,
    ExperimentDefinition,
    ExperimentFormatError,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    StructuralOracleConfig,
    parse_experiment_toml,
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


def _sampled_experiment() -> SampledExperimentDefinition:
    return SampledExperimentDefinition(
        schema_version=1,
        target=FinalResponseTarget(text_contains="ready"),
        oracle=StructuralOracleConfig(protected_context_ids=("ctx-1",)),
        runner=NativeRunnerConfig(type="codex", required_capability_digest="a" * 64),
        calibration=SampledCalibrationPhase(required_valid=3, minimum_matches=2, max_attempts=5),
    )


def test_experiment_toml_round_trip_is_canonical_and_digest_stable() -> None:
    experiment = _shell_experiment()

    rendered = experiment.to_toml()
    loaded = ExperimentDefinition.from_toml(rendered)

    assert loaded == experiment
    assert loaded.digest == experiment.digest
    assert loaded.target.digest == experiment.target.digest
    assert rendered == loaded.to_toml()
    assert rendered.startswith('schema_version = 1\nkind = "deterministic"\n')
    assert '[calibration]\nrole = "calibration"\nruns = 1' in rendered
    assert '[search]\nrole = "search"\nruns = 1' in rendered
    assert '[confirmation]\nrole = "confirmation"\nruns = 1' in rendered
    assert parse_experiment_toml(rendered) == experiment


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
    with pytest.raises(ExperimentFormatError, match="malformed TOML"):
        ExperimentDefinition.from_toml("schema_version =")


def test_experiment_digest_changes_with_protected_context() -> None:
    original = _shell_experiment()
    changed = original.model_copy(
        update={"oracle": StructuralOracleConfig(protected_context_ids=("other",))}
    )

    assert original.digest != changed.digest


def test_sampled_experiment_round_trip_is_canonical_and_digest_stable() -> None:
    experiment = _sampled_experiment()

    rendered = experiment.to_toml()
    loaded = parse_experiment_toml(rendered)

    assert isinstance(loaded, SampledExperimentDefinition)
    assert loaded == experiment
    assert loaded.digest == experiment.digest
    assert rendered == loaded.to_toml()
    assert rendered.startswith('schema_version = 1\nkind = "sampled"\n')
    assert "[search]" not in rendered
    assert "[confirmation]" not in rendered
    assert SampledExperimentDefinition.from_toml(rendered) == experiment
    assert (
        TypeAdapter(AnyRunnerConfig).validate_python(tomllib.loads(rendered)["runner"])
        == experiment.runner
    )


def test_sampled_copilot_shell_target_round_trip() -> None:
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=ShellActionTarget(tool_name_equals="bash", command_equals="pwd", count=1),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(type="copilot", required_capability_digest="b" * 64),
        calibration=SampledCalibrationPhase(required_valid=2, minimum_matches=1, max_attempts=3),
    )

    assert parse_experiment_toml(experiment.to_toml()) == experiment


def test_sampled_experiment_digest_binds_capability_and_budget() -> None:
    experiment = _sampled_experiment()
    changed_runner = experiment.model_copy(
        update={"runner": NativeRunnerConfig(type="codex", required_capability_digest="b" * 64)}
    )
    changed_calibration = experiment.model_copy(
        update={
            "calibration": SampledCalibrationPhase(
                required_valid=3, minimum_matches=2, max_attempts=6
            )
        }
    )

    assert experiment.digest != changed_runner.digest
    assert experiment.digest != changed_calibration.digest


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ('type = "codex"', 'type = "fake"', "Input should be 'codex' or 'copilot'"),
        (
            'required_capability_digest = "' + "a" * 64 + '"',
            'required_capability_digest = "' + "A" * 64 + '"',
            "String should match pattern",
        ),
        ("required_valid = 3", "required_valid = 0", "greater than or equal to 1"),
        ("required_valid = 3", "required_valid = true", "valid integer"),
        ("required_valid = 3", "required_valid = 17", "less than or equal to 16"),
        ("minimum_matches = 2", "minimum_matches = 4", "minimum_matches <= required_valid"),
        ("max_attempts = 5", "max_attempts = 2", "required_valid <= max_attempts"),
        ('role = "calibration"', 'role = "search"', "Input should be 'calibration'"),
        ("max_attempts = 5", "max_attempts = 5\nruns = 1", "Extra inputs"),
        ("max_attempts = 5", 'max_attempts = 5\n[search]\nrole = "search"', "Extra inputs"),
        (
            "max_attempts = 5",
            'max_attempts = 5\n[confirmation]\nrole = "confirmation"',
            "Extra inputs",
        ),
    ],
)
def test_sampled_experiment_rejects_invalid_contract(old: str, new: str, error: str) -> None:
    source = _sampled_experiment().to_toml().replace(old, new)

    with pytest.raises(ExperimentFormatError, match=error):
        parse_experiment_toml(source)


def test_sampled_experiment_requires_all_budget_fields_and_explicit_version() -> None:
    source = _sampled_experiment().to_toml()

    with pytest.raises(ExperimentFormatError, match="minimum_matches: Field required"):
        parse_experiment_toml(source.replace("minimum_matches = 2\n", ""))
    with pytest.raises(ExperimentFormatError, match="schema_version"):
        SampledExperimentDefinition.from_toml(source.replace("schema_version = 1\n", ""))
    with pytest.raises(ExperimentFormatError, match="schema_version"):
        parse_experiment_toml(source.replace("schema_version = 1", "schema_version = 3"))
    with pytest.raises(ExperimentFormatError, match="kind must be deterministic"):
        ExperimentDefinition.from_toml(source)


@pytest.mark.parametrize("experiment", [_shell_experiment(), _sampled_experiment()])
@pytest.mark.parametrize("version", ["2", "3", "true", '"1"'])
def test_experiment_parser_rejects_unsupported_or_non_integer_version(
    experiment: ExperimentDefinition | SampledExperimentDefinition, version: str
) -> None:
    source = experiment.to_toml().replace("schema_version = 1", f"schema_version = {version}")

    with pytest.raises(ExperimentFormatError, match="schema_version must be 1"):
        parse_experiment_toml(source)


@pytest.mark.parametrize("experiment", [_shell_experiment(), _sampled_experiment()])
def test_persisted_experiment_requires_explicit_kind(
    experiment: ExperimentDefinition | SampledExperimentDefinition,
) -> None:
    source = experiment.to_toml()
    missing_kind = source.replace(f'kind = "{experiment.kind}"\n', "")

    with pytest.raises(ExperimentFormatError, match="kind must be deterministic or sampled"):
        parse_experiment_toml(missing_kind)
    with pytest.raises(ExperimentFormatError, match="kind must be deterministic or sampled"):
        parse_experiment_toml(source.replace(f'kind = "{experiment.kind}"', 'kind = "unknown"'))


def test_sampled_kind_never_invents_samples_or_accepts_deterministic_runner() -> None:
    source = _shell_experiment().to_toml().replace('kind = "deterministic"', 'kind = "sampled"')

    with pytest.raises(ExperimentFormatError, match="required_valid: Field required"):
        parse_experiment_toml(source)


def test_deterministic_kind_cannot_accept_native_runner_or_skip_reduction_phases() -> None:
    source = _sampled_experiment().to_toml().replace('kind = "sampled"', 'kind = "deterministic"')

    with pytest.raises(ExperimentFormatError, match="search: Field required"):
        parse_experiment_toml(source)
    with pytest.raises(ExperimentFormatError, match="kind must be sampled"):
        SampledExperimentDefinition.from_toml(_shell_experiment().to_toml())


@pytest.mark.parametrize(
    "private_source",
    [
        'kind = "private-secret-kind"',
        '[runner]\ntype = "private-secret-runner"',
        '[target]\ntype = "private-secret-target"',
        '[private-secret-field]\nvalue = "private-secret-content"',
    ],
)
def test_experiment_parser_errors_do_not_expose_untrusted_names_or_values(
    private_source: str,
) -> None:
    source = 'schema_version = 1\nkind = "sampled"\n'
    if private_source.startswith("kind"):
        source = "schema_version = 1\n"

    with pytest.raises(ExperimentFormatError) as caught:
        parse_experiment_toml(source + private_source)

    assert "private-secret" not in str(caught.value)
