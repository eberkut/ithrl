"""Frozen campaign policy for one independently confirmed target-rate decrease."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import content_digest
from ithrl.experiment import (
    MAX_SAMPLING_FRESHNESS_SECONDS,
    SampledCalibrationPhase,
    SamplingArmTarget,
    SamplingEligibilityPolicy,
)
from ithrl.runner.contracts import Candidate

MAX_FUZZ_CANDIDATES = 10_000


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class FuzzScreening(_Contract):
    """Exploratory samples only; passing a screen never confirms a finding."""

    original: SamplingArmTarget
    mutant: SamplingArmTarget
    minimum_decrease: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class FuzzConfirmation(_Contract):
    """Fresh fixed samples, with alpha split equally between one-sided exact bounds."""

    original: SamplingArmTarget
    mutant: SamplingArmTarget
    method: Literal["clopper-pearson-union-bound"] = "clopper-pearson-union-bound"
    alpha: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]


class FuzzCampaignConfig(_Contract):
    """One original, explicit finite exploration, and one fresh final comparison.

    Sampling and work counts are explicit; these contracts install no practical
    defaults or execution authority. Administrative resource allowances remain
    separate from statistical policy. A later workflow must freeze the final
    candidate before confirmation and validate observed evidence before claiming
    any terminal outcome.
    """

    schema_version: Literal[1] = 1
    kind: Literal["fuzz"] = "fuzz"
    objective: Literal["decrease-target-rate"] = "decrease-target-rate"
    original: Candidate = Field(repr=False)
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)]
    mutators: Annotated[
        tuple[Literal["duplicate-text", "append-stale-text"], ...],
        Field(min_length=1, max_length=2),
    ]
    max_candidates: Annotated[int, Field(ge=1, le=MAX_FUZZ_CANDIDATES)]
    max_simplification_trials: Annotated[int, Field(ge=1, le=MAX_FUZZ_CANDIDATES)]
    max_findings: Literal[1] = 1
    calibration: SampledCalibrationPhase
    screening: FuzzScreening
    confirmation: FuzzConfirmation
    minimum_decrease: Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)]
    baseline_policy: Literal["strict-fresh-original"] = "strict-fresh-original"
    estimand: Literal["valid-only"] = "valid-only"
    eligibility_policy: SamplingEligibilityPolicy
    schedule_policy: Literal["seeded-interleaving"] = "seeded-interleaving"
    freshness_seconds: Annotated[int, Field(ge=1, le=MAX_SAMPLING_FRESHNESS_SECONDS)]

    @model_validator(mode="after")
    def validate_campaign(self) -> FuzzCampaignConfig:
        if self.original.removed_context_ids or not self.original.mutation.is_empty:
            raise ValueError("campaign baseline must be the unchanged original candidate")
        if len(set(self.mutators)) != len(self.mutators):
            raise ValueError("mutators must be unique")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class FuzzOutcome(StrEnum):
    """Terminal meanings; the workflow must establish the corresponding evidence.

    Inconclusive means complete eligible final samples did not confirm the
    declared effect. Incomplete means required work/evidence was unavailable.
    No candidate found describes bounded exploration, never proof of stability.
    """

    CONFIRMED_CHANGE = "confirmed-change"
    INCONCLUSIVE = "inconclusive"
    INCOMPLETE = "incomplete"
    NO_CANDIDATE_FOUND = "no-candidate-found"
