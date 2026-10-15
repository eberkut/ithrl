"""Strict experiment variants and canonical TOML rendering."""

from __future__ import annotations

import json
import tomllib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from ithrl.behavior import (
    BehaviorObservation,
    BehaviorTarget,
    FinalResponseTarget,
    ShellAction,
    ShellActionTarget,
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

# Runner variants remain distinct independently of the persisted schema version.
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
    kind: Literal["deterministic"] = "deterministic"
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

        experiment = parse_experiment_toml(source)
        if not isinstance(experiment, cls):
            raise ExperimentFormatError("invalid experiment: kind must be deterministic")
        return experiment

    def to_toml(self) -> str:
        """Render canonical TOML whose round trip preserves the experiment digest."""

        return _render_experiment(self)


# One explicit local calibration may make at most 16 attempts, including invalid retries.
MAX_CALIBRATION_ATTEMPTS = 16

# Finite contract ceilings bound a block's persisted attempts and freshness window.
# They do not recommend practical sample counts or a useful sampling duration.
MAX_SAMPLING_ATTEMPTS = 10_000
MAX_SAMPLING_FRESHNESS_SECONDS = 24 * 60 * 60


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


class SamplingArmTarget(_ImmutableContract):
    """Frozen valid-observation target and total-attempt ceiling for one arm."""

    required_valid: Annotated[int, Field(ge=1, le=MAX_SAMPLING_ATTEMPTS)]
    max_attempts: Annotated[int, Field(ge=1, le=MAX_SAMPLING_ATTEMPTS)]

    @model_validator(mode="after")
    def validate_sample_counts(self) -> SamplingArmTarget:
        if self.required_valid > self.max_attempts:
            raise ValueError("sampling arm requires required_valid <= max_attempts")
        return self


class SampledSearchPhase(_ImmutableContract):
    """Fixed independent arms used only for exploratory point screening."""

    role: Literal["search"] = "search"
    original: SamplingArmTarget
    candidate: SamplingArmTarget
    screen_rule: Literal["point-difference"] = "point-difference"


class SampledConfirmationPhase(_ImmutableContract):
    """Fresh independent arms with an explicit nominal one-sided confidence."""

    role: Literal["confirmation"] = "confirmation"
    original: SamplingArmTarget
    candidate: SamplingArmTarget
    method: Literal["newcombe-method-10"] = "newcombe-method-10"
    one_sided_confidence: Annotated[float, Field(gt=0.5, lt=1, allow_inf_nan=False)]


type SamplingEligibilityPolicy = Literal[
    "independent-safe-preaccept-only", "safe-provider-overload"
]


class SampledReductionPlan(_ImmutableContract):
    """Complete strict sampled search and separate final-confirmation policy."""

    search: SampledSearchPhase
    confirmation: SampledConfirmationPhase
    margin: Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)]
    baseline_policy: Literal["strict-fresh-original"] = "strict-fresh-original"
    estimand: Literal["valid-only"] = "valid-only"
    eligibility_policy: SamplingEligibilityPolicy = "independent-safe-preaccept-only"
    schedule_policy: Literal["seeded-interleaving"] = "seeded-interleaving"
    freshness_seconds: Annotated[int, Field(ge=1, le=MAX_SAMPLING_FRESHNESS_SECONDS)]


class SampledExperimentDefinition(_ImmutableContract):
    """Native calibration with an optional complete sampled reduction plan."""

    schema_version: Literal[1] = 1
    kind: Literal["sampled"] = "sampled"
    target: BehaviorTarget
    oracle: StructuralOracleConfig
    runner: NativeRunnerConfig
    calibration: SampledCalibrationPhase
    reduction: SampledReductionPlan | None = None

    @property
    def digest(self) -> str:
        """Return the content identity of the complete resolved experiment."""

        return content_digest(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_toml(cls, source: str) -> SampledExperimentDefinition:
        """Parse a strict sampled native experiment document."""

        experiment = parse_experiment_toml(source)
        if not isinstance(experiment, cls):
            raise ExperimentFormatError("invalid experiment: kind must be sampled")
        return experiment

    def to_toml(self) -> str:
        """Render canonical TOML whose round trip preserves the experiment digest."""

        return _render_experiment(self)


type AnyExperimentDefinition = ExperimentDefinition | SampledExperimentDefinition
_EXPERIMENT_ADAPTER = TypeAdapter(Annotated[AnyExperimentDefinition, Field(discriminator="kind")])


def parse_experiment_toml(source: str) -> AnyExperimentDefinition:
    """Select a strict variant by its explicit kind in the current schema."""

    try:
        raw = tomllib.loads(source)
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ExperimentFormatError("invalid experiment: schema_version must be 1")
        if raw.get("kind") not in ("deterministic", "sampled"):
            raise ExperimentFormatError("invalid experiment: kind must be deterministic or sampled")
        return _EXPERIMENT_ADAPTER.validate_python(_normalize_toml(raw))
    except (tomllib.TOMLDecodeError, ValidationError, TypeError, ValueError) as error:
        if isinstance(error, ExperimentFormatError):
            raise
        raise ExperimentFormatError(_format_error(error)) from error


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


def _render_experiment(experiment: AnyExperimentDefinition) -> str:
    lines = ["schema_version = 1", f"kind = {_toml_string(experiment.kind)}", "", "[target]"]
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
    elif isinstance(runner, UnavailableRunnerConfig):
        lines.append(f"harness = {_toml_string(runner.harness)}")
        lines.append(f"reason_code = {_toml_string(runner.reason_code)}")
    else:
        lines.append(
            f"required_capability_digest = {_toml_string(runner.required_capability_digest)}"
        )

    if isinstance(experiment, ExperimentDefinition):
        for name, phase in (
            ("calibration", experiment.calibration),
            ("search", experiment.search),
            ("confirmation", experiment.confirmation),
        ):
            lines.extend(("", f"[{name}]", f"role = {_toml_string(phase.role)}", "runs = 1"))
    else:
        lines.extend(
            (
                "",
                "[calibration]",
                'role = "calibration"',
                f"required_valid = {experiment.calibration.required_valid}",
                f"minimum_matches = {experiment.calibration.minimum_matches}",
                f"max_attempts = {experiment.calibration.max_attempts}",
            )
        )
        if experiment.reduction is not None:
            _render_sampled_reduction(lines, experiment.reduction)
    return "\n".join(lines) + "\n"


def _render_sampled_reduction(lines: list[str], reduction: SampledReductionPlan) -> None:
    lines.extend(("", "[reduction]", f"margin = {reduction.margin!r}"))
    for name in ("baseline_policy", "estimand", "eligibility_policy", "schedule_policy"):
        lines.append(f"{name} = {_toml_string(getattr(reduction, name))}")
    lines.append(f"freshness_seconds = {reduction.freshness_seconds}")
    for name, phase in (("search", reduction.search), ("confirmation", reduction.confirmation)):
        lines.extend(("", f"[reduction.{name}]", f"role = {_toml_string(phase.role)}"))
        if isinstance(phase, SampledSearchPhase):
            lines.append(f"screen_rule = {_toml_string(phase.screen_rule)}")
        else:
            lines.append(f"method = {_toml_string(phase.method)}")
            lines.append(f"one_sided_confidence = {phase.one_sided_confidence!r}")
        for arm_name, arm in (("original", phase.original), ("candidate", phase.candidate)):
            lines.extend(
                (
                    "",
                    f"[reduction.{name}.{arm_name}]",
                    f"required_valid = {arm.required_valid}",
                    f"max_attempts = {arm.max_attempts}",
                )
            )


def _append_optional_string(lines: list[str], name: str, value: str | None) -> None:
    if value is not None:
        lines.append(f"{name} = {_toml_string(value)}")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: tuple[Any, ...]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_error(error: Exception) -> str:
    if isinstance(error, tomllib.TOMLDecodeError):
        return "invalid experiment: malformed TOML"
    if isinstance(error, ValidationError):
        known_fields = {"deterministic", "sampled", "fake", "unavailable"}
        for contract in (
            ExperimentDefinition,
            SampledExperimentDefinition,
            FinalResponseTarget,
            ShellActionTarget,
            StructuralOracleConfig,
            FakeRunnerConfig,
            UnavailableRunnerConfig,
            NativeRunnerConfig,
            DeterministicPhase,
            SampledCalibrationPhase,
            SamplingArmTarget,
            SampledSearchPhase,
            SampledConfirmationPhase,
            SampledReductionPlan,
        ):
            known_fields.update(contract.model_fields)
        details = "; ".join(
            f"{'.'.join(str(part) if isinstance(part, int) or part in known_fields else '<field>' for part in issue['loc'])}: "
            + (
                "unsupported contract variant"
                if issue["type"] in ("union_tag_invalid", "union_tag_not_found")
                else issue["msg"]
            )
            for issue in error.errors(include_url=False, include_input=False)
        )
        return f"invalid experiment: {details}"
    return "invalid experiment: invalid contract"
