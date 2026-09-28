"""Strict deterministic experiment contracts and canonical TOML rendering."""

from __future__ import annotations

import json
import tomllib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ithrl.behavior import (
    BehaviorObservation,
    BehaviorTarget,
    FinalResponseTarget,
    ShellAction,
    evaluate_target,
    parse_behavior_target,
)
from ithrl.context import content_digest


class ExperimentFormatError(ValueError):
    """An experiment document does not satisfy the supported schema."""


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StructuralOracleConfig(_ImmutableContract):
    """Mechanical oracle prerequisites protected during reduction."""

    type: Literal["structural"] = "structural"
    protected_context_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()

    @model_validator(mode="after")
    def validate_unique_context_ids(self) -> StructuralOracleConfig:
        if len(set(self.protected_context_ids)) != len(self.protected_context_ids):
            raise ValueError("protected_context_ids must not contain duplicates")
        return self


class FakeRunnerConfig(_ImmutableContract):
    """Complete deterministic behavior of the credential-free fake runner."""

    type: Literal["fake"] = "fake"
    required_markers: Annotated[tuple[str, ...], Field(min_length=1)]
    alternative_marker_sets: tuple[tuple[str, ...], ...] = ()
    tool_name: Annotated[str | None, Field(min_length=1)] = None
    command: str | None = None
    final_text: str | None = None

    @model_validator(mode="after")
    def validate_fake_behavior(self) -> FakeRunnerConfig:
        marker_sets = (self.required_markers, *self.alternative_marker_sets)
        if any(not marker_set for marker_set in marker_sets):
            raise ValueError("fake runner marker sets must not be empty")
        if any(any(not marker for marker in marker_set) for marker_set in marker_sets):
            raise ValueError("fake runner markers must not be empty")
        if any(len(set(marker_set)) != len(marker_set) for marker_set in marker_sets):
            raise ValueError("fake runner marker sets must not contain duplicates")
        normalized_sets = tuple(frozenset(marker_set) for marker_set in marker_sets)
        if len(set(normalized_sets)) != len(normalized_sets):
            raise ValueError("fake runner marker sets must be distinct")

        emits_shell = self.tool_name is not None
        emits_final = self.final_text is not None
        if emits_shell == emits_final:
            raise ValueError("fake runner must emit exactly one supported behavior family")
        if not emits_shell and self.command is not None:
            raise ValueError("fake runner command requires tool_name")
        return self


class UnavailableRunnerConfig(_ImmutableContract):
    """Explicit refusal used until a compatible harness runner is available."""

    type: Literal["unavailable"] = "unavailable"
    harness: Annotated[str, Field(min_length=1)]
    reason_code: Literal["runner-not-implemented"] = "runner-not-implemented"


class NativeRunnerConfig(_ImmutableContract):
    """A native runner bound to one exact, locally detected capability profile."""

    type: Literal["codex", "copilot"]
    required_capability_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


type RunnerConfig = Annotated[
    FakeRunnerConfig | UnavailableRunnerConfig,
    Field(discriminator="type"),
]

# Consumers accepting both persisted versions can use this; schema v1 remains
# restricted to RunnerConfig and cannot silently acquire native execution.
type AnyRunnerConfig = Annotated[
    FakeRunnerConfig | UnavailableRunnerConfig | NativeRunnerConfig,
    Field(discriminator="type"),
]


class DeterministicPhase(_ImmutableContract):
    """One observation per candidate is sufficient for a deterministic runner."""

    role: Literal["calibration", "search", "confirmation"]
    runs: Literal[1] = 1


class ExperimentDefinition(_ImmutableContract):
    """Versioned deterministic experiment persisted inside a case."""

    schema_version: Literal[1] = 1
    target: BehaviorTarget
    oracle: StructuralOracleConfig
    runner: RunnerConfig
    calibration: DeterministicPhase
    search: DeterministicPhase
    confirmation: DeterministicPhase

    @model_validator(mode="after")
    def validate_consistent_contract(self) -> ExperimentDefinition:
        actual_roles = (
            self.calibration.role,
            self.search.role,
            self.confirmation.role,
        )
        expected_roles = ("calibration", "search", "confirmation")
        if actual_roles != expected_roles:
            raise ValueError("experiment phase roles must match their sections")

        if isinstance(self.runner, FakeRunnerConfig):
            observation = _configured_observation(self.runner)
            if not evaluate_target(self.target, observation):
                raise ValueError("fake runner output cannot satisfy the behavior target")
        return self

    @property
    def digest(self) -> str:
        """Return the content identity of the complete resolved experiment."""

        return content_digest(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_toml(cls, source: str) -> ExperimentDefinition:
        """Parse and strictly validate a versioned experiment document."""

        try:
            raw = tomllib.loads(source)
            normalized = _normalize_toml(raw)
            return cls.model_validate(normalized)
        except (tomllib.TOMLDecodeError, ValidationError, TypeError, ValueError) as error:
            if isinstance(error, ExperimentFormatError):
                raise
            raise ExperimentFormatError(_format_error(error)) from error

    def to_toml(self) -> str:
        """Render canonical TOML whose round trip preserves the experiment digest."""

        return _render_experiment(self)


# One explicit local calibration may make at most 16 attempts, including invalid retries.
MAX_CALIBRATION_ATTEMPTS = 16


class SampledCalibrationPhase(_ImmutableContract):
    """Explicit bounded full-context sampling; no candidate search is implied."""

    role: Literal["calibration"] = "calibration"
    required_valid: Annotated[int, Field(ge=1, le=MAX_CALIBRATION_ATTEMPTS)]
    minimum_matches: Annotated[int, Field(ge=1, le=MAX_CALIBRATION_ATTEMPTS)]
    max_attempts: Annotated[int, Field(ge=1, le=MAX_CALIBRATION_ATTEMPTS)]

    @model_validator(mode="after")
    def validate_sample_counts(self) -> SampledCalibrationPhase:
        if not self.minimum_matches <= self.required_valid <= self.max_attempts:
            raise ValueError(
                "calibration requires minimum_matches <= required_valid <= max_attempts"
            )
        return self


class SampledExperimentDefinition(_ImmutableContract):
    """Schema-v2 native calibration experiment without search or confirmation."""

    schema_version: Literal[2]
    target: BehaviorTarget
    oracle: StructuralOracleConfig
    runner: NativeRunnerConfig
    calibration: SampledCalibrationPhase

    @property
    def digest(self) -> str:
        """Return the content identity of the complete resolved experiment."""

        return content_digest(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_toml(cls, source: str) -> SampledExperimentDefinition:
        """Parse a strict schema-v2 native calibration document."""

        try:
            return cls.model_validate(_normalize_toml(tomllib.loads(source)))
        except (tomllib.TOMLDecodeError, ValidationError, TypeError, ValueError) as error:
            if isinstance(error, ExperimentFormatError):
                raise
            raise ExperimentFormatError(_format_error(error)) from error

    def to_toml(self) -> str:
        """Render canonical TOML whose round trip preserves the experiment digest."""

        return _render_sampled_experiment(self)


type AnyExperimentDefinition = ExperimentDefinition | SampledExperimentDefinition


def parse_experiment_toml(source: str) -> AnyExperimentDefinition:
    """Select the strict experiment contract by its persisted schema version."""

    try:
        version = tomllib.loads(source).get("schema_version")
    except tomllib.TOMLDecodeError as error:
        raise ExperimentFormatError(_format_error(error)) from error
    if version == 2 and not isinstance(version, bool):
        return SampledExperimentDefinition.from_toml(source)
    return ExperimentDefinition.from_toml(source)


def _normalize_toml(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    target = normalized.get("target")
    if isinstance(target, dict):
        normalized["target"] = parse_behavior_target(target)

    oracle = normalized.get("oracle")
    if isinstance(oracle, dict):
        oracle = dict(oracle)
        if isinstance(oracle.get("protected_context_ids"), list):
            oracle["protected_context_ids"] = tuple(oracle["protected_context_ids"])
        normalized["oracle"] = oracle

    runner = normalized.get("runner")
    if isinstance(runner, dict):
        runner = dict(runner)
        if runner.get("type") == "fake" and isinstance(runner.get("required_markers"), list):
            runner["required_markers"] = tuple(runner["required_markers"])
        alternatives = runner.get("alternative_marker_sets")
        if isinstance(alternatives, list):
            runner["alternative_marker_sets"] = tuple(
                tuple(marker_set) if isinstance(marker_set, list) else marker_set
                for marker_set in alternatives
            )
        normalized["runner"] = runner
    return normalized


def _configured_observation(runner: FakeRunnerConfig) -> BehaviorObservation:
    if runner.tool_name is not None:
        return BehaviorObservation(
            shell_actions=(
                ShellAction(
                    tool_name=runner.tool_name,
                    command=runner.command or "",
                ),
            )
        )
    return BehaviorObservation(final_response=runner.final_text)


def _render_experiment(experiment: ExperimentDefinition) -> str:
    lines = ["schema_version = 1", "", "[target]"]
    target = experiment.target
    lines.append(f"type = {_toml_string(target.type)}")
    if isinstance(target, FinalResponseTarget):
        _append_optional_string(lines, "text_equals", target.text_equals)
        _append_optional_string(lines, "text_contains", target.text_contains)
    else:
        _append_optional_string(lines, "tool_name_equals", target.tool_name_equals)
        _append_optional_string(lines, "command_equals", target.command_equals)
        _append_optional_string(lines, "command_contains", target.command_contains)
        if target.count is not None:
            lines.append(f"count = {target.count}")

    lines.extend(("", "[oracle]", 'type = "structural"'))
    lines.append(f"protected_context_ids = {_toml_value(experiment.oracle.protected_context_ids)}")

    runner = experiment.runner
    lines.extend(("", "[runner]", f"type = {_toml_string(runner.type)}"))
    if isinstance(runner, FakeRunnerConfig):
        lines.append(f"required_markers = {_toml_value(runner.required_markers)}")
        lines.append(f"alternative_marker_sets = {_toml_value(runner.alternative_marker_sets)}")
        _append_optional_string(lines, "tool_name", runner.tool_name)
        _append_optional_string(lines, "command", runner.command)
        _append_optional_string(lines, "final_text", runner.final_text)
    else:
        lines.append(f"harness = {_toml_string(runner.harness)}")
        lines.append(f"reason_code = {_toml_string(runner.reason_code)}")

    for name, phase in (
        ("calibration", experiment.calibration),
        ("search", experiment.search),
        ("confirmation", experiment.confirmation),
    ):
        lines.extend(("", f"[{name}]", f"role = {_toml_string(phase.role)}", "runs = 1"))
    return "\n".join(lines) + "\n"


def _render_sampled_experiment(experiment: SampledExperimentDefinition) -> str:
    lines = ["schema_version = 2", "", "[target]"]
    target = experiment.target
    lines.append(f"type = {_toml_string(target.type)}")
    if isinstance(target, FinalResponseTarget):
        _append_optional_string(lines, "text_equals", target.text_equals)
        _append_optional_string(lines, "text_contains", target.text_contains)
    else:
        _append_optional_string(lines, "tool_name_equals", target.tool_name_equals)
        _append_optional_string(lines, "command_equals", target.command_equals)
        _append_optional_string(lines, "command_contains", target.command_contains)
        if target.count is not None:
            lines.append(f"count = {target.count}")

    lines.extend(("", "[oracle]", 'type = "structural"'))
    lines.append(f"protected_context_ids = {_toml_value(experiment.oracle.protected_context_ids)}")

    runner = experiment.runner
    lines.extend(
        (
            "",
            "[runner]",
            f"type = {_toml_string(runner.type)}",
            f"required_capability_digest = {_toml_string(runner.required_capability_digest)}",
            "",
            "[calibration]",
            'role = "calibration"',
            f"required_valid = {experiment.calibration.required_valid}",
            f"minimum_matches = {experiment.calibration.minimum_matches}",
            f"max_attempts = {experiment.calibration.max_attempts}",
        )
    )
    return "\n".join(lines) + "\n"


def _append_optional_string(lines: list[str], name: str, value: str | None) -> None:
    if value is not None:
        lines.append(f"{name} = {_toml_string(value)}")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: tuple[Any, ...]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_url=False, include_input=False)
        )
        return f"invalid experiment: {details}"
    return f"invalid experiment: {error}"
