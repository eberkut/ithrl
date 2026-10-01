from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ithrl.cli.output import (
    CommandOutput,
    ErrorOutput,
    OutputFormat,
    SampledErrorOutput,
    SampledOutput,
    render_sampled,
    render_sampled_error,
)


def _report(**overrides: object) -> SampledOutput:
    values: dict[str, object] = {
        "status": "incomplete",
        "plan_digest": "a" * 64,
        "capability_digest": "b" * 64,
        "harness": "codex",
        "harness_version": "1.2.3",
        "target": "final-response",
        "controlled_surface": "ephemeral-thread-v1",
        "safety_policy_digest": "c" * 64,
        "max_attempts": 4,
        "max_provider_calls": 2,
        "max_runner_seconds": 60,
        "max_tokens": None,
        "provider_call_accounting": "started-unknown",
    }
    values.update(overrides)
    return SampledOutput.model_validate(values)


def test_sampled_json_is_versioned_and_keeps_unknown_bounds_and_calls_null(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_sampled(_report(), OutputFormat.JSON)

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["kind"] == "sampled"
    assert payload["max_tokens"] is None
    assert payload["provider_calls"] is None
    assert payload["provider_call_accounting"] == "started-unknown"


def test_sampled_human_does_not_claim_zero_calls_after_execution_starts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_sampled(_report(), OutputFormat.HUMAN)

    output = capsys.readouterr().out
    assert "provider calls  unknown (execution started)" in output
    assert "provider calls  0" not in output
    assert "token bound     unknown" in output


def test_sampled_human_reports_zero_calls_only_before_execution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_sampled(
        _report(status="ready", provider_call_accounting="not-started", provider_calls=0),
        OutputFormat.HUMAN,
    )

    assert "provider calls  0 (execution did not start)" in capsys.readouterr().out


def test_sampled_error_without_probe_has_content_safe_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = SampledErrorOutput(status="refused", exit_code=3, error_code="unsafe-action-category")
    render_sampled_error(error, OutputFormat.JSON)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "shrink",
        "diagnostic_path": None,
        "error_code": "unsafe-action-category",
        "exit_code": 3,
        "harness_started": None,
        "kind": "sampled",
        "next_action": None,
        "probe_exit_code": None,
        "probe_reason": None,
        "probe_stage": None,
        "provider_call_accounting": "started-unknown",
        "provider_calls": None,
        "schema_version": 1,
        "status": "refused",
    }
    assert "message" not in payload
    with pytest.raises(ValidationError):
        SampledOutput.model_validate(
            {
                **_report().model_dump(),
                "endpoint": "https://user:secret@example.invalid/api?token=x",
            }
        )


@pytest.mark.parametrize(
    ("changes"),
    [
        {"provider_call_accounting": "not-started", "provider_calls": 1},
        {"provider_call_accounting": "not-started", "provider_calls": None},
        {"provider_call_accounting": "started-unknown", "provider_calls": 1},
        {"provider_call_accounting": "accounted", "provider_calls": None},
        {"provider_call_accounting": "accounted", "provider_calls": 3},
        {"attempts_used": 5},
        {"status": "ready", "provider_call_accounting": "started-unknown"},
    ],
)
def test_sampled_output_rejects_contradictory_accounting(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _report(**changes)


@pytest.mark.parametrize(
    ("accounting", "calls"),
    [("not-started", None), ("not-started", 1), ("started-unknown", 0)],
)
def test_sampled_error_rejects_contradictory_accounting(accounting: str, calls: int | None) -> None:
    with pytest.raises(ValidationError):
        SampledErrorOutput.model_validate(
            {
                "status": "refused",
                "exit_code": 3,
                "error_code": "unsafe-action-category",
                "provider_call_accounting": accounting,
                "provider_calls": calls,
            }
        )


@pytest.mark.parametrize(
    "variant", ["deterministic-result", "deterministic-error", "sampled-result", "sampled-error"]
)
def test_output_variants_use_one_schema_and_explicit_kinds(variant: str) -> None:
    if variant == "deterministic-result":
        report = CommandOutput(command="shrink", status="ready", case_path="case", case_digest="a")
    elif variant == "deterministic-error":
        report = ErrorOutput(
            command="shrink", exit_code=2, error_code="invalid-input", message="Invalid input."
        )
    elif variant == "sampled-result":
        report = _report()
    else:
        report = SampledErrorOutput(status="incomplete", exit_code=4, error_code="incomplete")
    payload = report.model_dump(mode="json")
    assert payload["schema_version"] == 1
    assert payload["kind"] == variant.split("-")[0]
    with pytest.raises(ValidationError):
        type(report).model_validate({**payload, "schema_version": 2})
    with pytest.raises(ValidationError):
        type(report).model_validate({**payload, "kind": "wrong"})


def test_deterministic_output_cannot_claim_provider_execution() -> None:
    report = CommandOutput(command="shrink", status="ready", case_path="case", case_digest="a")
    assert report.provider_calls == 0
    assert report.provider_call_accounting == "not-started"
    for changes in [{"provider_calls": 1}, {"provider_call_accounting": "accounted"}]:
        with pytest.raises(ValidationError):
            CommandOutput.model_validate({**report.model_dump(), **changes})
