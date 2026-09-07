"""Versioned deterministic behavior targets and observations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ithrl.context import content_digest

type BehaviorType = Literal["final-response", "shell-action"]


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FinalResponseTarget(_ImmutableContract):
    """Match a final response using at most one length-limited text condition."""

    schema_version: Literal[1] = 1
    type: Literal["final-response"] = "final-response"
    text_equals: Annotated[str | None, Field(min_length=1)] = None
    text_contains: Annotated[str | None, Field(min_length=1)] = None

    @model_validator(mode="after")
    def validate_text_predicate(self) -> FinalResponseTarget:
        if self.text_equals is not None and self.text_contains is not None:
            raise ValueError("final-response target accepts only one text predicate")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of this target contract."""

        return target_digest(self)


class ShellActionTarget(_ImmutableContract):
    """Match shell actions by tool identity and at most one command condition."""

    schema_version: Literal[1] = 1
    type: Literal["shell-action"] = "shell-action"
    tool_name_equals: Annotated[str | None, Field(min_length=1)] = None
    command_equals: Annotated[str | None, Field(min_length=1)] = None
    command_contains: Annotated[str | None, Field(min_length=1)] = None
    count: Annotated[int | None, Field(ge=1)] = None

    @model_validator(mode="after")
    def validate_command_predicate(self) -> ShellActionTarget:
        if self.command_equals is not None and self.command_contains is not None:
            raise ValueError("shell-action target accepts only one command predicate")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of this target contract."""

        return target_digest(self)


type BehaviorTarget = Annotated[
    FinalResponseTarget | ShellActionTarget,
    Field(discriminator="type"),
]

_TARGET_ADAPTER = TypeAdapter(BehaviorTarget, config=ConfigDict(strict=True))


class ShellAction(_ImmutableContract):
    """One normalized shell action retained alongside its native evidence."""

    schema_version: Literal[1] = 1
    tool_name: Annotated[str, Field(min_length=1)]
    command: str


class BehaviorObservation(_ImmutableContract):
    """The normalized fields used by the built-in v0.1 targets."""

    schema_version: Literal[1] = 1
    final_response: str | None = None
    shell_actions: tuple[ShellAction, ...] = ()


def parse_behavior_target(value: object) -> BehaviorTarget:
    """Validate an untrusted target object against the discriminated contract."""

    return _TARGET_ADAPTER.validate_python(value)


def target_digest(target: BehaviorTarget) -> str:
    """Return a deterministic SHA-256 digest for a behavior target."""

    return content_digest(target.model_dump(mode="json", exclude_none=True))


def evaluate_target(target: BehaviorTarget, observation: BehaviorObservation) -> bool:
    """Evaluate a target without model inference, regular expressions, or user code."""

    if isinstance(target, FinalResponseTarget):
        text = observation.final_response
        if text is None:
            return False
        if target.text_equals is not None:
            return text == target.text_equals
        if target.text_contains is not None:
            return target.text_contains in text
        return True

    matches = tuple(
        action for action in observation.shell_actions if _matches_shell_action(target, action)
    )
    if target.count is not None:
        return len(matches) == target.count
    return bool(matches)


def _matches_shell_action(target: ShellActionTarget, action: ShellAction) -> bool:
    if target.tool_name_equals is not None and action.tool_name != target.tool_name_equals:
        return False
    if target.command_equals is not None and action.command != target.command_equals:
        return False
    return target.command_contains is None or target.command_contains in action.command
