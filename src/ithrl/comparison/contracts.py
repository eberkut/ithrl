"""Frozen inputs and progress for one directed two-context comparison."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.case.filesystem import lstat_regular_file, read_safe_text, reject_symlink_components
from ithrl.context import content_digest
from ithrl.execution.contracts import MAX_WALL_TIME_SECONDS
from ithrl.experiment import (
    MAX_SAMPLING_FRESHNESS_SECONDS,
    CodexReplayConfig,
    SampledChangeConfirmation,
    SampledComparisonPlan,
    SamplingArmTarget,
    SamplingEligibilityPolicy,
    SamplingPairTargets,
)
from ithrl.mutation import ContextSelection
from ithrl.runner.contracts import Candidate

MAX_COMPARISON_ATTEMPTS = 1000
MAX_COMPARISON_RESULT_BYTES = 64 * 1024 * 1024


class ComparisonConfigurationError(ValueError):
    """Private comparison inputs are unavailable or invalid."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ComparisonLimits(_Contract):
    max_provider_calls: Annotated[int, Field(ge=0)]
    max_wall_time_seconds: Annotated[int, Field(ge=1)]
    attempt_timeout_seconds: Annotated[int, Field(ge=1, le=MAX_WALL_TIME_SECONDS)]
    inter_attempt_delay_seconds: Annotated[int, Field(ge=0, le=86400)] = 0


class ComparisonCalibration(_Contract):
    reference: SamplingArmTarget
    candidate: SamplingArmTarget
    positive_minimum_matches: Annotated[int, Field(ge=1)]
    negative_maximum_matches: Annotated[int, Field(ge=0)]


class ComparisonConfirmation(_Contract):
    reference: SamplingArmTarget
    candidate: SamplingArmTarget
    alpha: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]


class ComparisonConfiguration(_Contract):
    schema_version: Literal[1] = 1
    kind: Literal["comparison"] = "comparison"
    objective: Literal["repair", "degradation"]
    reference: ContextSelection
    candidate: ContextSelection
    calibration: ComparisonCalibration
    confirmation: ComparisonConfirmation
    minimum_effect: Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)]
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)]
    eligibility_policy: SamplingEligibilityPolicy
    freshness_seconds: Annotated[int, Field(ge=1, le=MAX_SAMPLING_FRESHNESS_SECONDS)]
    limits: ComparisonLimits
    codex_replay: CodexReplayConfig | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> ComparisonConfiguration:
        # Reuse the bound procedure's directional sample/gate validation.
        _ = self.procedure
        if self.reference == self.candidate:
            raise ValueError("comparison requires two distinct context selections")
        attempts = sum(
            arm.max_attempts
            for arm in (
                self.calibration.reference,
                self.calibration.candidate,
                self.confirmation.reference,
                self.confirmation.candidate,
            )
        )
        if attempts > MAX_COMPARISON_ATTEMPTS:
            raise ValueError("comparison supports at most 1000 scheduled attempts")
        return self

    @property
    def policy_digest(self) -> str:
        return content_digest(self.model_dump(mode="json", exclude={"limits", "codex_replay"}))

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

    @property
    def procedure(self) -> SampledComparisonPlan:
        return SampledComparisonPlan(
            comparison_digest=self.policy_digest,
            objective=self.objective,
            seed=self.seed,
            reference=self.reference,
            candidate=self.candidate,
            calibration=SamplingPairTargets(
                original=self.calibration.reference,
                candidate=self.calibration.candidate,
            ),
            positive_minimum_matches=self.calibration.positive_minimum_matches,
            negative_maximum_matches=self.calibration.negative_maximum_matches,
            confirmation=SampledChangeConfirmation(
                original=self.confirmation.reference,
                candidate=self.confirmation.candidate,
                alpha=self.confirmation.alpha,
            ),
            minimum_effect=self.minimum_effect,
            eligibility_policy=self.eligibility_policy,
            freshness_seconds=self.freshness_seconds,
        )

    @classmethod
    def from_toml(cls, source: str) -> ComparisonConfiguration:
        try:
            return cls.model_validate_json(json.dumps(tomllib.loads(source)))
        except (ValueError, TypeError):
            raise ComparisonConfigurationError("invalid private comparison configuration") from None


def load_comparison_configuration(path: Path) -> ComparisonConfiguration:
    try:
        reject_symlink_components(path.absolute())
        metadata = lstat_regular_file(path, label="comparison configuration")
        if metadata.st_nlink != 1 or (
            os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ComparisonConfigurationError(
                "comparison configuration must be private single-link"
            )
        return ComparisonConfiguration.from_toml(
            read_safe_text(path, max_bytes=1024 * 1024, label="comparison configuration")
        )
    except (OSError, ValueError):
        raise ComparisonConfigurationError("cannot read private comparison configuration") from None


class BoundComparison(_Contract):
    procedure: SampledComparisonPlan
    reference: Candidate = Field(repr=False)
    candidate: Candidate = Field(repr=False)

    @model_validator(mode="after")
    def validate_pair(self) -> BoundComparison:
        if self.reference.digest == self.candidate.digest:
            raise ValueError("comparison contexts materialize the same candidate")
        for name in (
            "case_digest",
            "execution_plan_digest",
            "source_artifact_digests",
            "materialization",
        ):
            if getattr(self.reference, name) != getattr(self.candidate, name):
                raise ValueError("comparison contexts must share immutable execution bindings")
        for selection, candidate in (
            (self.procedure.reference, self.reference),
            (self.procedure.candidate, self.candidate),
        ):
            if (
                not set(selection.removed_context_ids) <= set(candidate.removed_context_ids)
                or selection.mutation != candidate.mutation
            ):
                raise ValueError("comparison candidate differs from its selected context")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class ComparisonOutcome(StrEnum):
    CONFIRMED_IMPROVEMENT = "confirmed-improvement"
    CONFIRMED_DEGRADATION = "confirmed-degradation"
    INCONCLUSIVE = "inconclusive"
    CALIBRATION_FAILED = "calibration-failed"
    INCOMPLETE = "incomplete"


@lru_cache(maxsize=1)
def comparison_implementation_digest() -> str:
    """Bind continuation to comparison orchestration and shared inference code."""

    package = Path(__file__).parent
    shared = package.parent
    sources = {
        **{name: package / name for name in ("contracts.py", "engine.py", "results.py")},
        **{
            name: shared / name
            for name in ("sampling.py", "observations.py", "run_state.py", "statistics.py")
        },
    }
    return content_digest(
        {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
    )


class ComparisonProgress(_Contract):
    binding: BoundComparison = Field(repr=False)
    implementation_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = Field(
        default_factory=comparison_implementation_digest
    )
    selected_at: datetime | None = None
    terminal: ComparisonOutcome | None = None
    stop_reason: Annotated[
        str | None, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9-]*$")
    ] = None
    stopped_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("selected_at", "stopped_at")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("comparison time must include a UTC offset")
            return value.astimezone(UTC)
        return None

    @model_validator(mode="after")
    def validate_terminal(self) -> ComparisonProgress:
        timed_stop = self.stop_reason in {"freshness-expired", "wall-time-exhausted"}
        if (self.stopped_at is not None) != timed_stop or (
            timed_stop and self.terminal is not ComparisonOutcome.INCOMPLETE
        ):
            raise ValueError("comparison timing refusal requires its terminal stop time")
        if (
            self.terminal
            in {
                ComparisonOutcome.CONFIRMED_IMPROVEMENT,
                ComparisonOutcome.CONFIRMED_DEGRADATION,
                ComparisonOutcome.INCONCLUSIVE,
            }
            and self.selected_at is None
        ):
            raise ValueError("comparison confirmation outcome requires final selection")
        if self.terminal is ComparisonOutcome.CONFIRMED_IMPROVEMENT and (
            self.binding.procedure.objective != "repair"
        ):
            raise ValueError("improvement outcome requires the repair objective")
        if self.terminal is ComparisonOutcome.CONFIRMED_DEGRADATION and (
            self.binding.procedure.objective != "degradation"
        ):
            raise ValueError("degradation outcome requires the degradation objective")
        return self

    @property
    def selection_digest(self) -> str:
        if self.selected_at is None:
            raise ValueError("comparison contexts have not been selected for confirmation")
        return content_digest(
            {
                "binding_digest": self.binding.digest,
                "implementation_digest": self.implementation_digest,
                "selected_at": self.selected_at.isoformat(),
            }
        )
