from __future__ import annotations

import tomllib

import pytest
from pydantic import TypeAdapter, ValidationError

from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.experiment import (
    MAX_CALIBRATION_ATTEMPTS,
    MAX_SAMPLING_ATTEMPTS,
    MAX_SAMPLING_FRESHNESS_SECONDS,
    AnyRunnerConfig,
    DeterministicPhase,
    ExperimentDefinition,
    ExperimentFormatError,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
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


def _sampled_reduction() -> SampledReductionPlan:
    return SampledReductionPlan(
        search=SampledSearchPhase(
            original=SamplingArmTarget(required_valid=4, max_attempts=8),
            candidate=SamplingArmTarget(required_valid=5, max_attempts=9),
        ),
        confirmation=SampledConfirmationPhase(
            original=SamplingArmTarget(required_valid=20, max_attempts=24),
            candidate=SamplingArmTarget(required_valid=21, max_attempts=25),
            one_sided_confidence=0.95,
        ),
        margin=0.2,
        freshness_seconds=600,
    )


def _sampled_reduction_experiment() -> SampledExperimentDefinition:
    return _sampled_experiment().model_copy(update={"reduction": _sampled_reduction()})


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


def test_sampled_calibration_legacy_render_and_digest_are_unchanged() -> None:
    experiment = _sampled_experiment()
    expected = (
        'schema_version = 1\nkind = "sampled"\n\n'
        '[target]\ntype = "final-response"\ntext_contains = "ready"\n\n'
        '[oracle]\ntype = "structural"\nprotected_context_ids = ["ctx-1"]\n\n'
        '[runner]\ntype = "codex"\nrequired_capability_digest = "' + "a" * 64 + '"\n\n'
        '[calibration]\nrole = "calibration"\nrequired_valid = 3\n'
        "minimum_matches = 2\nmax_attempts = 5\n"
    )

    assert experiment.reduction is None
    assert experiment.to_toml() == expected
    assert experiment.digest == "86769eb3ef8f265b1f65efe80b39fc63cf41491538b524b1c7f840c687ca7e1d"
    assert "reduction" not in experiment.model_dump(mode="json", exclude_none=True)
    assert MAX_CALIBRATION_ATTEMPTS == 16


def test_sampled_reduction_round_trip_has_only_nested_phase_sections() -> None:
    experiment = _sampled_reduction_experiment()
    rendered = experiment.to_toml()
    raw = tomllib.loads(rendered)
    loaded = parse_experiment_toml(rendered)

    assert loaded == experiment
    assert loaded.digest == experiment.digest
    assert loaded.to_toml() == rendered
    assert set(raw) == {
        "schema_version",
        "kind",
        "target",
        "oracle",
        "runner",
        "calibration",
        "reduction",
    }
    assert raw["schema_version"] == 1
    assert raw["kind"] == "sampled"
    assert experiment.reduction is not None
    assert raw["reduction"] == experiment.reduction.model_dump()
    for phase_name in ("search", "confirmation"):
        assert f"[reduction.{phase_name}]" in rendered
        for arm_name in ("original", "candidate"):
            assert f"[reduction.{phase_name}.{arm_name}]" in rendered
    assert "[search]" not in rendered
    assert "[confirmation]" not in rendered
    assert "one_sided_confidence" not in raw["reduction"]["search"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("margin",), 0.3),
        (("freshness_seconds",), 601),
        (("search", "original", "required_valid"), 3),
        (("search", "candidate", "max_attempts"), 10),
        (("confirmation", "original", "max_attempts"), 26),
        (("confirmation", "candidate", "required_valid"), 22),
        (("confirmation", "one_sided_confidence"), 0.99),
    ],
)
def test_sampled_reduction_digest_binds_the_complete_plan(
    path: tuple[str, ...], value: object
) -> None:
    experiment = _sampled_reduction_experiment()
    data = _sampled_reduction().model_dump()
    current = data
    for name in path[:-1]:
        current = current[name]
    current[path[-1]] = value
    changed = experiment.model_copy(update={"reduction": SampledReductionPlan.model_validate(data)})

    assert changed.digest != experiment.digest
    assert _sampled_experiment().digest != experiment.digest
    assert parse_experiment_toml(changed.to_toml()).digest == changed.digest


@pytest.mark.parametrize("field", ["required_valid", "max_attempts"])
@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 1.0, "1", float("nan"), float("inf"), 10_001]
)
def test_sampling_arm_rejects_invalid_counts(field: str, value: object) -> None:
    data = {"required_valid": 1, "max_attempts": MAX_SAMPLING_ATTEMPTS}
    with pytest.raises(ValidationError):
        SamplingArmTarget.model_validate({**data, field: value})


def test_sampling_arm_rejects_missing_counts_and_target_above_attempt_ceiling() -> None:
    with pytest.raises(ValueError, match="required_valid <= max_attempts"):
        SamplingArmTarget(required_valid=2, max_attempts=1)
    for field in ("required_valid", "max_attempts"):
        with pytest.raises(ValueError, match="Field required"):
            SamplingArmTarget.model_validate({field: 1})
    assert SamplingArmTarget(
        required_valid=MAX_SAMPLING_ATTEMPTS, max_attempts=MAX_SAMPLING_ATTEMPTS
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("margin",), True),
        (("margin",), "0.2"),
        (("margin",), -0.01),
        (("margin",), 1),
        (("margin",), float("nan")),
        (("margin",), float("inf")),
        (("margin",), float("-inf")),
        (("freshness_seconds",), True),
        (("freshness_seconds",), 0),
        (("freshness_seconds",), -1),
        (("freshness_seconds",), 600.0),
        (("freshness_seconds",), "600"),
        (("freshness_seconds",), MAX_SAMPLING_FRESHNESS_SECONDS + 1),
        (("search", "role"), "confirmation"),
        (("search", "screen_rule"), "newcombe-method-10"),
        (("confirmation", "role"), "search"),
        (("confirmation", "method"), "wald"),
        (("confirmation", "one_sided_confidence"), True),
        (("confirmation", "one_sided_confidence"), "0.95"),
        (("confirmation", "one_sided_confidence"), 0.5),
        (("confirmation", "one_sided_confidence"), 1),
        (("confirmation", "one_sided_confidence"), float("nan")),
        (("confirmation", "one_sided_confidence"), float("inf")),
        (("confirmation", "one_sided_confidence"), float("-inf")),
        (("baseline_policy",), "last-accepted"),
        (("estimand",), "attempt-success"),
        (("eligibility_policy",), "low-error-rate"),
        (("schedule_policy",), "original-first"),
        (("search", "candidate"), None),
        (("confirmation",), None),
    ],
)
def test_sampled_reduction_rejects_invalid_policy_and_parameters(
    path: tuple[str, ...], value: object
) -> None:
    data = _sampled_reduction().model_dump()
    current = data
    for name in path[:-1]:
        current = current[name]
    current[path[-1]] = value

    with pytest.raises(ValidationError):
        SampledReductionPlan.model_validate(data)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "search"),
        ((), "confirmation"),
        ((), "margin"),
        ((), "freshness_seconds"),
        (("search",), "original"),
        (("search",), "candidate"),
        (("confirmation",), "original"),
        (("confirmation",), "candidate"),
        (("confirmation",), "one_sided_confidence"),
    ],
)
def test_sampled_reduction_never_invents_missing_practical_parameters(
    path: tuple[str, ...], field: str
) -> None:
    data = _sampled_reduction().model_dump()
    current = data
    for name in path:
        current = current[name]
    del current[field]

    with pytest.raises(ValueError, match="Field required"):
        SampledReductionPlan.model_validate(data)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("reduction", "max_provider_calls"),
        ("reduction", "mode"),
        ("reduction", "schema_version"),
        ("reduction", "epoch_size"),
        ("reduction.search", "one_sided_confidence"),
        ("reduction.search", "confidence"),
        ("reduction.confirmation", "confidence"),
        ("reduction.search.original", "minimum_matches"),
        ("reduction.confirmation.candidate", "unknown"),
    ],
)
def test_sampled_reduction_toml_rejects_unknown_and_misplaced_fields(
    section: str, field: str
) -> None:
    source = _sampled_reduction_experiment().to_toml()
    source = source.replace(f"[{section}]", f"[{section}]\n{field} = 1", 1)
    with pytest.raises(ExperimentFormatError, match="Extra inputs"):
        parse_experiment_toml(source)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("required_valid = 4", "required_valid = true"),
        ("required_valid = 4", "required_valid = 9"),
        ("max_attempts = 8", "max_attempts = 10001"),
        ("margin = 0.2", "margin = nan"),
        ("margin = 0.2", "margin = inf"),
        ("one_sided_confidence = 0.95", "one_sided_confidence = -inf"),
        ("freshness_seconds = 600", "freshness_seconds = true"),
        ('role = "search"', 'role = "calibration"'),
        ('role = "confirmation"', 'role = "search"'),
        ("[reduction.confirmation]", "[confirmation]"),
        ("[reduction.search]", "[search]"),
    ],
)
def test_sampled_reduction_toml_rejects_invalid_nested_contract(old: str, new: str) -> None:
    source = _sampled_reduction_experiment().to_toml().replace(old, new, 1)
    with pytest.raises(ExperimentFormatError):
        parse_experiment_toml(source)


def test_sampled_reduction_contracts_are_frozen_and_allow_boundary_values() -> None:
    reduction = _sampled_reduction()
    for contract, field in (
        (reduction, "margin"),
        (reduction.search, "role"),
        (reduction.confirmation, "one_sided_confidence"),
        (reduction.search.original, "required_valid"),
    ):
        with pytest.raises(ValueError, match="frozen"):
            setattr(contract, field, getattr(contract, field))
    data = reduction.model_dump()
    data["margin"] = 0
    data["freshness_seconds"] = MAX_SAMPLING_FRESHNESS_SECONDS
    assert SampledReductionPlan.model_validate(data).margin == 0


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
