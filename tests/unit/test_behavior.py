from __future__ import annotations

import pytest
from pydantic import ValidationError

from ithrl.behavior import (
    MAX_BEHAVIOR_TEXT_LENGTH,
    MAX_SHELL_ACTIONS,
    MAX_TOOL_NAME_LENGTH,
    BehaviorObservation,
    FinalResponseTarget,
    ShellAction,
    ShellActionTarget,
    evaluate_target,
    parse_behavior_target,
)


def test_final_response_target_uses_one_bounded_predicate() -> None:
    observation = BehaviorObservation(final_response="deprecated API: use v2")

    assert evaluate_target(FinalResponseTarget(), observation)
    assert evaluate_target(FinalResponseTarget(text_contains="deprecated API"), observation)
    assert not evaluate_target(FinalResponseTarget(text_equals="deprecated API"), observation)

    with pytest.raises(ValidationError, match="only one text predicate"):
        FinalResponseTarget(text_equals="one", text_contains="two")


def test_final_response_alternatives_are_explicit_literal_or_matches() -> None:
    target = FinalResponseTarget(text_contains_any=("No findings", "No actionable findings"))
    for text in ("No findings.", "No actionable findings after review."):
        assert evaluate_target(target, BehaviorObservation(final_response=text))
    assert not evaluate_target(target, BehaviorObservation(final_response="Found an issue."))
    assert not evaluate_target(target, BehaviorObservation(final_response="no findings"))
    assert parse_behavior_target(target.model_dump(mode="json")) == target
    assert "text_contains_any" not in FinalResponseTarget(text_contains="ready").model_dump()
    with pytest.raises(ValidationError, match="only one text predicate"):
        FinalResponseTarget(text_contains="one", text_contains_any=("two",))


@pytest.mark.parametrize(
    "alternatives", [(), ("",), ("same", "same"), ("x" * MAX_BEHAVIOR_TEXT_LENGTH, "extra")]
)
def test_text_alternatives_are_nonempty_unique_and_bounded(alternatives: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        FinalResponseTarget(text_contains_any=alternatives)


def test_shell_action_target_matches_tool_command_and_exact_count() -> None:
    observation = BehaviorObservation(
        shell_actions=(
            ShellAction(tool_name="bash", command="terraform apply -refresh=false"),
            ShellAction(tool_name="bash", command="terraform apply -refresh=false"),
            ShellAction(tool_name="other", command="terraform apply -refresh=false"),
        )
    )
    target = ShellActionTarget(
        tool_name_equals="bash",
        command_contains="terraform apply",
        count=2,
    )

    assert evaluate_target(target, observation)
    assert not evaluate_target(target.model_copy(update={"count": 1}), observation)


def test_shell_action_target_rejects_ambiguous_or_invalid_predicates() -> None:
    with pytest.raises(ValidationError, match="only one command predicate"):
        ShellActionTarget(command_equals="pwd", command_contains="pwd")
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ShellActionTarget(count=0)
    with pytest.raises(ValidationError, match="valid integer"):
        ShellActionTarget.model_validate({"count": "1"})


def test_target_parser_is_discriminated_strict_and_forbids_unknown_fields() -> None:
    target = parse_behavior_target(
        {
            "schema_version": 1,
            "type": "shell-action",
            "tool_name_equals": "bash",
        }
    )

    assert isinstance(target, ShellActionTarget)
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        parse_behavior_target({"schema_version": 1, "type": "unknown"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_behavior_target({"schema_version": 1, "type": "final-response", "regex": ".*"})


def test_target_digest_is_stable_and_content_sensitive() -> None:
    first = FinalResponseTarget(text_contains="ready")
    second = FinalResponseTarget(text_contains="ready")
    changed = FinalResponseTarget(text_equals="ready")

    assert first.digest == second.digest
    assert first.digest != changed.digest
    assert len(first.digest) == 64


def test_behavior_contracts_are_immutable() -> None:
    target = FinalResponseTarget()
    field = "text_equals"

    with pytest.raises(ValidationError, match="frozen"):
        setattr(target, field, "changed")


def test_behavior_contracts_bound_persisted_text_and_action_counts() -> None:
    with pytest.raises(ValidationError, match="at most"):
        FinalResponseTarget(text_equals="x" * (MAX_BEHAVIOR_TEXT_LENGTH + 1))
    with pytest.raises(ValidationError, match="at most"):
        ShellAction(tool_name="x" * (MAX_TOOL_NAME_LENGTH + 1), command="pwd")
    action = ShellAction(tool_name="shell", command="pwd")
    with pytest.raises(ValidationError, match="at most"):
        BehaviorObservation(shell_actions=(action,) * (MAX_SHELL_ACTIONS + 1))
