"""Frozen operator inputs and saved-state association for one sampled reduction run.

Recorded paths are descriptive metadata. This contract neither reads them nor grants
live continuation authority; the CLI validates canonical live state separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.execution import ResourceLimits
from ithrl.experiment import SampledExperimentDefinition
from ithrl.observations import ObservationIdentity
from ithrl.timestamps import validate_rfc3339_timestamp

if TYPE_CHECKING:
    from ithrl.run_state import SampledRunState


class SampledRunMetadata(BaseModel):
    """Frozen operator inputs for one exact sampled reduction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
    run_directory: Path = Field(repr=False)
    source_case: Path = Field(repr=False)
    original_workspace: Path = Field(repr=False)
    observation_store: Path = Field(repr=False)
    experiment: SampledExperimentDefinition = Field(repr=False)
    resource_limits: ResourceLimits
    original_identity: ObservationIdentity
    started_at: datetime = Field(strict=True)

    @field_validator("run_directory", "source_case", "original_workspace", "observation_store")
    @classmethod
    def absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or len(str(value)) > 4096:
            raise ValueError("session paths must be absolute and cannot contain '..'")
        return value

    @field_validator("started_at", mode="before")
    @classmethod
    def rfc3339_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(validate_rfc3339_timestamp(value, field="started_at"))
        return value

    @field_validator("started_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session start must include a UTC offset")
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("session start must be within the supported UTC range") from None

    @model_validator(mode="after")
    def bound_run(self) -> SampledRunMetadata:
        if self.run_directory != self.source_case / ".runs" / self.run_id:
            raise ValueError("session run must belong to its canonical source case")
        if self.experiment.reduction is None:
            raise ValueError("session requires a sampled reduction plan")
        if self.original_identity.experiment_digest != self.experiment.digest:
            raise ValueError("session experiment must match its original identity")
        return self

    def matches_state(self, state: SampledRunState) -> bool:
        """Compare frozen metadata with saved state without accessing recorded paths."""

        return not (
            state.run_id != self.run_id
            or Path(state.source_case) != self.source_case
            or Path(state.observation_store) != self.observation_store
            or state.started_at != self.started_at
            or state.original_identity != self.original_identity
            or (
                state.checkpoint is not None
                and state.checkpoint.reduction != self.experiment.reduction
            )
            or any(block.plan.procedure != self.experiment.reduction for block in state.blocks)
        )
