from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ithrl.cli.output import ExitCode, OutputFormat
from ithrl.cli.sampled_output import (
    SampledReductionOutput,
    render_sampled_reduction,
    sampled_reduction_exit_code,
)
from ithrl.statistics import BinomialSample, classify_fixed_sample


def _report(**changes: object) -> SampledReductionOutput:
    data: dict[str, object] = {
        "status": "ready",
        "phase": "calibration",
        "plan_digest": "a" * 64,
        "capability_digest": "b" * 64,
        "harness": "codex",
        "harness_version": "1.2.3",
        "target": "final-response",
        "safety_policy_digest": "c" * 64,
        "max_attempts": 20,
        "max_provider_calls": 20,
        "max_runner_seconds": 600,
        "max_wall_time_seconds": 600,
        "max_candidate_comparisons": 2,
        "candidate_capacity": 2,
        "minimum_provider_calls": 16,
        "minimum_wall_time_seconds": 480,
        "confirmation_provider_calls": 8,
        "confirmation_runner_seconds": 240,
        "confirmation_capacity": "protected",
        "margin": 0.2,
        "one_sided_confidence": 0.95,
        "search_original_required_valid": 2,
        "search_original_max_attempts": 3,
        "search_candidate_required_valid": 2,
        "search_candidate_max_attempts": 3,
        "confirmation_original_required_valid": 3,
        "confirmation_original_max_attempts": 4,
        "confirmation_candidate_required_valid": 3,
        "confirmation_candidate_max_attempts": 4,
        "original_context_count": 4,
        "retained_context_count": 4,
        "removed_context_count": 0,
        "provider_call_accounting": "not-started",
        "provider_calls": 0,
    }
    data.update(changes)
    return SampledReductionOutput.model_validate(data)


def test_preflight_json_contains_explicit_fixed_samples_limits_and_assumptions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report()
    render_sampled_reduction(report, OutputFormat.JSON)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload == report.model_dump(mode="json")
    assert payload["kind"] == "sampled"
    assert payload["baseline_policy"] == "strict-fresh-original"
    assert payload["estimand"] == "valid-only"
    assert payload["confirmation_result"] == "not-run"
    assert payload["provider_calls"] == 0
    assert payload["confirmation_capacity"] == "protected"
    assert payload["confirmation_original_required_valid"] == 3
    assert payload["search_candidate_max_attempts"] == 3
    assert "valid-only-does-not-estimate-execution-reliability" in payload["assumptions"]
    assert "no-campaign-wide-error-guarantee" in payload["assumptions"]


def test_human_and_json_report_same_unknown_accounting_and_exact_continuation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(
        status="interrupted",
        phase="search",
        calibration_result="measurable",
        search_result="search-incomplete",
        reliability_status="unknown",
        provider_call_accounting="started-unknown",
        provider_calls=None,
        actual_provider_calls=4,
        possible_provider_calls=1,
        pending_provider_calls=2,
        charged_provider_calls=7,
        unknown_provider_attempts=1,
        attempts_used=5,
        matches=3,
        no_matches=1,
        invalid_observations=1,
        stop_reason="user-interrupt",
        next_action="Resume the frozen run with its remaining budget.",
        next_command="ithrl shrink /private/case --resume /private/run --format json",
        next_command_shell="posix",
    )
    render_sampled_reduction(report, OutputFormat.HUMAN)
    human = capsys.readouterr()
    render_sampled_reduction(report, OutputFormat.JSON)
    structured = capsys.readouterr()
    payload = json.loads(structured.out)
    assert human.err == structured.err == ""
    for phrase in (
        "Sampled shrink: interrupted",
        "phase: search",
        "search: search-incomplete",
        "confirmation: not-run",
        "provider calls: unknown actual; 7 charged / 20 maximum",
        "unknown call accounting: 1 attempts; 1 possible additional calls; 2 pending",
        "confirmation budget: 8 calls; 240s; protected",
        "stop reason: user-interrupt",
        str(payload["next_command"]),
        str(payload["next_action"]),
    ):
        assert phrase in human.out
    assert payload["provider_calls"] is None
    assert payload["charged_provider_calls"] == 7
    assert payload["matches"] == 3
    assert payload["invalid_observations"] == 1


def test_verbose_adds_diagnostics_without_changing_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report()
    render_sampled_reduction(report, OutputFormat.HUMAN)
    compact = capsys.readouterr().out
    render_sampled_reduction(report, OutputFormat.HUMAN, verbose=True)
    verbose = capsys.readouterr().out
    assert "plan digest:" not in compact
    assert "search original valid target:" not in compact
    assert "assumption:" not in compact
    assert "plan digest:" in verbose
    assert "search original valid target: 2" in verbose
    assert "assumption: fresh-independent-confirmation" in verbose
    assert "nominal one-sided confidence 0.95" in compact
    assert "execution reliability is reported separately" in compact
    assert "20 maximum" in compact
    assert "minimum budget: 16 calls; 480s" in compact
    render_sampled_reduction(report, OutputFormat.JSON)
    compact_json = capsys.readouterr().out
    render_sampled_reduction(report, OutputFormat.JSON, verbose=True)
    assert capsys.readouterr().out == compact_json


@pytest.mark.parametrize(
    "changes",
    [
        {"original_context_count": 5},
        {"provider_calls": 1},
        {"charged_provider_calls": 1},
        {"attempts_used": 1},
        {"confirmation_provider_calls": 21},
        {"confirmation_runner_seconds": 601},
        {"search_original_required_valid": 4},
        {"confirmation_candidate_required_valid": 5},
        {"provider_call_accounting": "accounted", "provider_calls": None},
        {
            "status": "incomplete",
            "provider_call_accounting": "started-unknown",
            "provider_calls": 0,
        },
        {"status": "ready", "provider_call_accounting": "started-unknown", "provider_calls": None},
        {"status": "confirmed", "confirmation_result": "not-run"},
        {
            "status": "confirmed",
            "confirmation_result": "confirmed-preserving",
            "evidence_eligibility": "refused",
        },
        {"status": "confirmation-failed", "confirmation_result": "incomplete"},
        {"next_command": "ithrl shrink /case --resume /run"},
    ],
)
def test_output_rejects_contradictory_claims(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _report(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"context_ids": ["private context"]},
        {"native_output": "private native response"},
        {"endpoint": "https://user:secret@example.invalid/api"},
        {"endpoint": "https://example.invalid/api?token=secret"},
        {"harness": "codex\nprivate response"},
        {"requested_model": "model\x1b[31m"},
        {"stop_reason": "private provider error message"},
        {"assumptions": ["private context"]},
        {"result_path": "/private/result\nsecret"},
        {"next_action": "resume\x1b[31m"},
        {"next_command": "ithrl shrink\nsecret", "next_command_shell": "posix"},
        {"safety_effects": ["private native response"]},
        {"plan_digest": "private native response"},
    ],
)
def test_output_has_no_native_content_channels_or_unsafe_terminal_metadata(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _report(**changes)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ExitCode.SUCCESS),
        (
            {
                "status": "confirmed",
                "confirmation_result": "confirmed-preserving",
                "evidence_eligibility": "eligible",
            },
            ExitCode.SUCCESS,
        ),
        ({"status": "confirmation-failed", "confirmation_result": "loses"}, ExitCode.INCONCLUSIVE),
        ({"status": "inconclusive", "confirmation_result": "inconclusive"}, ExitCode.INCONCLUSIVE),
        ({"status": "incomplete", "stop_reason": "provider-call-budget"}, ExitCode.BUDGET),
        ({"status": "incomplete", "stop_reason": "unsafe-prior-attempt"}, ExitCode.INCONCLUSIVE),
        ({"status": "refused", "confirmation_result": "refused"}, ExitCode.UNSUPPORTED),
        ({"status": "interrupted", "stop_reason": "user-interrupt"}, ExitCode.CANCELLED),
    ],
)
def test_exit_codes_separate_statistical_and_operational_outcomes(
    changes: dict[str, object],
    expected: ExitCode,
) -> None:
    assert sampled_reduction_exit_code(_report(**changes)) == expected


def test_accounted_calls_require_exact_known_usage() -> None:
    report = _report(
        status="running",
        provider_call_accounting="accounted",
        provider_calls=4,
        actual_provider_calls=4,
        charged_provider_calls=4,
        attempts_used=4,
    )
    assert report.provider_calls == 4
    with pytest.raises(ValidationError):
        SampledReductionOutput.model_validate(
            {**report.model_dump(), "unknown_provider_attempts": 1}
        )


def test_fresh_confirmation_counts_and_interval_have_human_json_parity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    classification = classify_fixed_sample(
        BinomialSample(3, 3),
        BinomialSample(2, 3),
        required_candidate=3,
        required_original=3,
        confidence=0.95,
        margin=0.2,
    )
    assert classification.interval is not None
    original = {
        "dispatched": 4,
        "valid": 3,
        "matches": 2,
        "no_matches": 1,
        "invalid_observations": 1,
        "runner_errors": 0,
    }
    candidate = {
        "dispatched": 4,
        "valid": 3,
        "matches": 3,
        "no_matches": 0,
        "invalid_observations": 0,
        "runner_errors": 1,
    }
    report = _report(
        status="inconclusive",
        phase="finished",
        calibration_result="measurable",
        confirmation_result="inconclusive",
        evidence_eligibility="eligible",
        confirmation_original=original,
        confirmation_candidate=candidate,
        confirmation_difference=1 / 3,
        confirmation_lower_bound=classification.interval.lower,
        confirmation_upper_bound=classification.interval.upper,
    )
    render_sampled_reduction(report, OutputFormat.HUMAN)
    human = capsys.readouterr().out
    render_sampled_reduction(report, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmation_original"] == original
    assert payload["confirmation_candidate"] == candidate
    assert payload["confirmation_difference"] == 1 / 3
    assert payload["confirmation_lower_bound"] == classification.interval.lower
    assert payload["confirmation_upper_bound"] == classification.interval.upper
    assert (
        "fresh confirmation original: 3 valid (2 matches, 1 no matches); 1 invalid; 0 runner errors"
        in human
    )
    assert (
        "fresh confirmation candidate: 3 valid (3 matches, 0 no matches); 0 invalid; 1 runner errors"
        in human
    )
    assert "fresh confirmation difference (candidate - original): 0.333333" in human
    assert (
        f"fresh confirmation interval: [{classification.interval.lower:g}, {classification.interval.upper:g}]"
        in human
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmation_lower_bound": -0.2},
        {"confirmation_lower_bound": 0.2, "confirmation_upper_bound": -0.2},
        {
            "confirmation_original": {
                "dispatched": 3,
                "valid": 3,
                "matches": 3,
                "no_matches": 0,
                "invalid_observations": 0,
                "runner_errors": 0,
            }
        },
    ],
)
def test_fresh_confirmation_rejects_partial_arm_or_interval_metadata(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _report(**changes)
