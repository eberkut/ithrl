"""Explicit private operator settings for a bounded fuzz campaign."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ithrl.case.filesystem import lstat_regular_file, read_safe_text, reject_symlink_components
from ithrl.execution.contracts import MAX_WALL_TIME_SECONDS
from ithrl.fuzz.contracts import FuzzCampaignConfig, FuzzCampaignSettings
from ithrl.mutation import MAX_MUTATION_TEXT_BYTES
from ithrl.runner import Candidate


class FuzzConfigurationError(ValueError):
    """The supplied private configuration cannot describe a supported campaign."""


class FuzzCommandLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    max_provider_calls: Annotated[int, Field(ge=0)]
    max_wall_time_seconds: Annotated[int, Field(ge=1)]
    attempt_timeout_seconds: Annotated[int, Field(ge=1, le=MAX_WALL_TIME_SECONDS)]
    inter_attempt_delay_seconds: Annotated[int, Field(ge=0, le=86400)] = 0


class FuzzConfiguration(FuzzCampaignSettings):
    limits: FuzzCommandLimits
    stale_text: Annotated[str, Field(max_length=MAX_MUTATION_TEXT_BYTES, repr=False)] = ""

    @field_validator("stale_text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MUTATION_TEXT_BYTES:
            raise ValueError("stress text exceeds its UTF-8 byte limit")
        return value

    def bind(self, original: Candidate) -> FuzzCampaignConfig:
        return FuzzCampaignConfig.model_validate(
            {**self.model_dump(exclude={"stale_text", "limits"}), "original": original}
        )

    @classmethod
    def from_toml(cls, source: str) -> FuzzConfiguration:
        try:
            raw = tomllib.loads(source)
            if isinstance(raw.get("mutators"), list):
                raw["mutators"] = tuple(raw["mutators"])
            return cls.model_validate(raw)
        except (ValueError, TypeError) as error:
            raise FuzzConfigurationError(
                "invalid fuzz configuration; check the declared settings"
            ) from error


def load_fuzz_configuration(path: Path) -> FuzzConfiguration:
    try:
        reject_symlink_components(path.absolute())
        metadata = lstat_regular_file(path, label="fuzz configuration")
        import os
        import stat

        if metadata.st_nlink != 1 or (
            os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise FuzzConfigurationError(
                "fuzz configuration must be a private, single-link file (0600 on POSIX)"
            )
        return FuzzConfiguration.from_toml(
            read_safe_text(path, max_bytes=1024 * 1024, label="fuzz configuration")
        )
    except (OSError, ValueError) as error:
        raise FuzzConfigurationError("cannot read private fuzz configuration") from error
