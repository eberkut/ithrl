"""Cost reports retain recorded work without changing or replaying an investigation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.cli import app
from ithrl.context import content_digest
from ithrl.cost_reporting import CostReport, load_cost_report, recorded_report, write_cost_report
from ithrl.costs import PriceBook, PricingSpec, RecordedSource
from ithrl.observations import (
    _INSERT_OBSERVATION,
    Observation,
    ObservationArm,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
    ObservationStoreError,
    _to_row,
)
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting, TokenUsage

NOW = datetime(2026, 11, 19, tzinfo=UTC)
SOURCE = RecordedSource(
    provider="private-provider", model="private-model", endpoint="https://private.invalid/inference"
)


def observation(
    index: int, *, failed: bool = False, calls: int = 1, usage: bool = True
) -> Observation:
    return Observation(
        observation_id=f"observation-{index}",
        recorded_at=NOW,
        case_digest="a" * 64,
        candidate_digest="b" * 64,
        experiment_digest="c" * 64,
        runner_fingerprint="private-fingerprint",
        execution_plan_digest="d" * 64,
        target_digest="e" * 64,
        algorithm_digest="algorithm",
        role=ObservationRole.SEARCH,
        arm=ObservationArm.CANDIDATE,
        attempt=index,
        outcome=ObservationOutcome.RUNNER_ERROR if failed else ObservationOutcome.MATCH,
        error_category="timeout" if failed else None,
        attempt_evidence=AttemptEvidence(
            attempt_id=f"attempt-{index:032x}",
            attempt_digest=f"{index:064x}",
            termination_reason="timeout" if failed else "completed",
            provider_acceptance_state=ProviderAcceptanceState.ACCEPTED
            if calls
            else ProviderAcceptanceState.NOT_STARTED,
            effective_policy_digest="f" * 64,
            provider=SOURCE.provider,
            model_returned=SOURCE.model,
            endpoint=SOURCE.endpoint,
            accounting=RunAccounting(provider_calls_started=calls, provider_calls_completed=calls),
            usage=TokenUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=200)
            if usage
            else None,
        ),
    )


def book() -> PriceBook:
    return PriceBook(
        PricingSpec(
            provider="openai",
            model="gpt-6.1-sol",
            as_of=NOW,
            recorded_source=SOURCE,
            custom_rates={
                "input_mtok": Decimal(2),
                "output_mtok": Decimal(10),
                "cache_read_mtok": Decimal("0.1"),
            },
        )
    )


def test_read_only_report_preserves_errors_and_counts_each_resumed_attempt_once(
    tmp_path: Path,
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append_many(
        (
            observation(1),
            observation(2, failed=True),
            observation(3, calls=0, usage=False),
            observation(4, usage=False),
        )
    )
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    assert report.summary()["subtotal_usd"] == "0.00204"
    assert report.summary()["priced_observations"] == 0
    assert report.summary()["partially_priced_observations"] == 2
    assert report.summary()["known_zero_provider_observations"] == 1
    assert report.summary()["unpriced_observations"] == 1
    assert report.summary()["attempts_with_unknown_additional_calls"] == 1
    assert report.summary()["pending_provider_calls"] is None
    assert (
        recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
        == report
    )
    assert "private" not in report.model_dump_json()


def test_repeated_physical_attempt_is_not_charged_twice(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    first = observation(1)
    store.append_many((first, first.model_copy(update={"observation_id": "reused", "attempt": 2})))
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert len(report.observations) == 1
    assert report.summary()["subtotal_usd"] == "0.00102"


@pytest.mark.parametrize("failed", [False, True])
def test_conflicting_outcomes_or_errors_for_one_attempt_refuse(
    tmp_path: Path, failed: bool
) -> None:
    first = observation(1, failed=failed)
    changes = {"observation_id": "conflict", "attempt": 2}
    if failed:
        changes["error_category"] = "transport-error"
    else:
        changes["outcome"] = ObservationOutcome.NO_MATCH
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append_many((first, first.model_copy(update=changes)))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="conflicting evidence"):
        recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("tier", [None, "priority", "default", "private-tier-secret"])
def test_recorded_service_tier_is_bound_without_disclosing_native_values(
    tmp_path: Path, tier: str | None
) -> None:
    first = observation(1)
    evidence = first.attempt_evidence.model_copy(update={"service_tier": tier})
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(first.model_copy(update={"attempt_evidence": evidence}))
    pricing = PriceBook(book().spec.model_copy(update={"context": {"service_tier": "priority"}}))
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=pricing)
    row = report.observations[0]
    assert row.service_tier_digest == (content_digest(tier) if tier is not None else None)
    assert row.usage is not None
    if tier in (None, "priority"):
        assert row.charges is not None
        assert row.reason is None
        data = report.model_dump(mode="json")
        data["observations"][0]["service_tier_digest"] = content_digest("default")
        with pytest.raises(ValueError, match="single-call attribution"):
            CostReport.model_validate_json(json.dumps(data))
    else:
        assert row.charges is None
        assert row.reason == "service-tier-unmatched"
    assert "private-tier-secret" not in report.model_dump_json()


@pytest.mark.parametrize("tier", [None, "default", "priority"])
def test_implicit_catalog_tier_is_checked_and_persisted(tmp_path: Path, tier: str | None) -> None:
    first = observation(1)
    evidence = first.attempt_evidence.model_copy(update={"service_tier": tier})
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(first.model_copy(update={"attempt_evidence": evidence}))
    pricing = PriceBook(book().spec.model_copy(update={"custom_rates": None}))
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=pricing)
    assert report.pricing.context == {"service_tier": "default"}
    assert report.observations[0].reason == (
        "service-tier-unmatched" if tier == "priority" else None
    )
    path = tmp_path / "costs.json"
    write_cost_report(path, report)
    assert load_cost_report(path) == report
    if tier != "priority":
        data = report.model_dump(mode="json")
        data["observations"][0]["service_tier_digest"] = content_digest("priority")
        with pytest.raises(ValueError, match="single-call attribution"):
            CostReport.model_validate_json(json.dumps(data))


def test_multiple_requests_are_unpriced_without_a_request_level_breakdown(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(observation(1, calls=2))
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert report.observations[0].reason == "multiple-provider-requests"
    assert report.summary()["subtotal_usd"] is None
    assert report.summary()["provider_calls_started_known"] == 2
    assert report.observations[0].usage is not None
    assert report.observations[0].usage.input_tokens == 1000


@pytest.mark.parametrize(
    ("acceptance", "possible", "started"),
    [
        (ProviderAcceptanceState.ACCEPTED, 0, 1),
        (ProviderAcceptanceState.UNKNOWN, 4, None),
        (ProviderAcceptanceState.NOT_STARTED, 0, 0),
    ],
)
def test_missing_accounting_preserves_acceptance_and_separate_possible_calls(
    tmp_path: Path, acceptance: ProviderAcceptanceState, possible: int, started: int | None
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    row = observation(1, failed=True)
    evidence = row.attempt_evidence.model_dump()
    evidence.update(
        accounting=None, provider_acceptance_state=acceptance, provider_calls_possible=possible
    )
    store.append(
        row.model_copy(update={"attempt_evidence": AttemptEvidence.model_validate(evidence)})
    )
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    saved = report.observations[0]
    assert saved.provider_calls_started == started
    assert saved.provider_calls_possible == possible
    assert saved.usage is not None
    assert saved.provider_calls_started_exact == (acceptance is ProviderAcceptanceState.NOT_STARTED)
    assert saved.charges is None
    assert report.summary()["subtotal_usd"] == ("0" if started == 0 else None)


def test_empty_selection_and_missing_store_are_not_zero_costs(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    with pytest.raises(FileNotFoundError):
        recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert not store.path.exists()
    store.initialize()
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert report.summary()["subtotal_usd"] is None


@pytest.mark.parametrize(("started", "possible"), [(0, 4), (1, 1)])
def test_uncertain_acceptance_does_not_discard_other_priced_attempts(
    tmp_path: Path, started: int, possible: int
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    uncertain = observation(2, failed=True)
    fields = uncertain.attempt_evidence.model_dump()
    fields.update(
        accounting=RunAccounting(provider_calls_started=started, provider_calls_completed=started),
        provider_acceptance_state=ProviderAcceptanceState.UNKNOWN,
        provider_calls_possible=possible,
    )
    store.append_many(
        (
            observation(1),
            uncertain.model_copy(
                update={"attempt_evidence": AttemptEvidence.model_validate(fields)}
            ),
        )
    )
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert report.summary()["subtotal_usd"] == "0.00102"
    assert report.observations[1].reason == "incomplete-provider-request"
    assert report.observations[1].usage is not None
    assert report.observations[1].provider_calls_possible == possible
    assert report.observations[1].provider_calls_started == started
    assert not report.observations[1].provider_calls_started_exact
    data = report.model_dump(mode="json")
    data["observations"][1]["provider_calls_started_exact"] = True
    with pytest.raises(ValueError, match="exact started-call count"):
        CostReport.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    ("calls", "has_usage", "mismatch", "uncertain", "reason"),
    [
        (0, False, False, False, "no-provider-calls"),
        (2, False, True, False, "usage-unavailable"),
        (2, True, True, False, "attribution-unmatched"),
        (2, True, False, True, "multiple-provider-requests"),
    ],
)
def test_refusal_precedence_when_an_attempt_has_multiple_limitations(
    tmp_path: Path, calls: int, has_usage: bool, mismatch: bool, uncertain: bool, reason: str
) -> None:
    row = observation(1, failed=True, calls=calls, usage=has_usage)
    fields = row.attempt_evidence.model_dump()
    if mismatch:
        fields["model_returned"] = "another-private-model"
    if uncertain:
        fields.update(
            provider_acceptance_state=ProviderAcceptanceState.UNKNOWN, provider_calls_possible=calls
        )
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(
        row.model_copy(update={"attempt_evidence": AttemptEvidence.model_validate(fields)})
    )
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert report.observations[0].reason == reason


def test_conflicting_physical_attempt_refuses_without_rewriting_source(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    first = observation(1)
    store.append_many(
        (
            first,
            first.model_copy(
                update={"observation_id": "conflict", "attempt": 2, "candidate_digest": "f" * 64}
            ),
        )
    )
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="conflicting evidence"):
        recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["scope", "duplicate", "reason", "attribution", "zero"])
def test_saved_recorded_report_rejects_inconsistent_evidence(tmp_path: Path, change: str) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(observation(1))
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    data = report.model_dump(mode="json")
    row = data["observations"][0]
    if change == "scope":
        data["case_digest"] = None
    elif change == "duplicate":
        data["observations"].append(row.copy())
    elif change == "reason":
        row["reason"] = "usage-unavailable"
    elif change == "attribution":
        row["attribution_digest"] = "f" * 64
    else:
        row.update(charges=None, reason="no-provider-calls")
    with pytest.raises(ValueError, match=r"scope|repeats|refused|attribution|zero-call"):
        CostReport.model_validate_json(json.dumps(data))


def test_read_only_snapshot_rejects_links_and_limits(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append_many((observation(1), observation(2)))
    with pytest.raises(ObservationStoreError, match="row or byte limit"):
        store.read_snapshot(case_digest="a" * 64, experiment_digest="c" * 64, limit=1)
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(store.path)
    with pytest.raises(ObservationStoreError, match="symbolic link"):
        ObservationStore(linked).read_snapshot(case_digest="a" * 64, experiment_digest="c" * 64)


def test_read_only_snapshot_refuses_wal_before_creating_sidecars(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(observation(1))
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    before = {p.name for p in tmp_path.iterdir()}
    with pytest.raises(ObservationStoreError, match="rollback-journal"):
        store.read_snapshot(case_digest="a" * 64, experiment_digest="c" * 64)
    assert {p.name for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    ("column", "value"),
    [(5, b"blob"), (11, 1.5), (13, b"blob")],
)
def test_read_only_snapshot_refuses_noncanonical_column_types(
    tmp_path: Path, column: int, value: bytes | float
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    row: list[object] = list(_to_row(observation(1, failed=True)))
    row[column] = value
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute(_INSERT_OBSERVATION, row)
        connection.commit()
    before = store.path.read_bytes()
    files = set(tmp_path.iterdir())
    with pytest.raises(ObservationStoreError, match="invalid column types"):
        store.read_snapshot(case_digest="a" * 64, experiment_digest="c" * 64)
    assert store.path.read_bytes() == before
    assert set(tmp_path.iterdir()) == files


@pytest.mark.parametrize(
    "rate_json",
    [
        "-1e-999",
        "1e-999",
        "1000000.000000000001",
        "0",
        '{"base":"2","tiers":[{"start":100,"price":1e-999}]}',
        '"not-a-rate"',
        '"1e-999"',
        '"1000000.000000000001"',
    ],
)
def test_cli_requires_exact_decimal_rate_strings(tmp_path: Path, rate_json: str) -> None:
    pricing, usage, output = (
        tmp_path / name for name in ("pricing.json", "usage.json", "out.json")
    )
    pricing.write_text(
        book().spec.model_dump_json().replace('"input_mtok":"2"', f'"input_mtok":{rate_json}')
    )
    usage.write_text(
        '{"input_tokens":100,"output_tokens":0,"cached_input_tokens":0,"cache_write_tokens":0}'
    )
    for path in (pricing, usage):
        path.chmod(0o600)
    result = CliRunner().invoke(
        app,
        [
            "cost",
            "--pricing",
            str(pricing),
            "--scenario",
            str(usage),
            "--output",
            str(output),
            "--format",
            "json",
        ],
    )
    if rate_json == '"1000000.000000000001"':
        assert result.exit_code == 0, result.output
        report = load_cost_report(output)
        assert report.scenario is not None
        assert report.scenario.components[0].usd_per_million == Decimal("1000000.000000000001")
        assert report.scenario.subtotal_usd == Decimal("100.0000000000000001")
    else:
        assert result.exit_code == 2
        assert json.loads(result.output)["error_code"] == "cost-pricing-invalid"
        assert not output.exists()


def test_normal_cli_saves_and_inspects_without_the_store_or_price_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.append(observation(1))
    pricing = tmp_path / "pricing.json"
    pricing.write_text(book().spec.model_dump_json())
    pricing.chmod(0o600)
    destination = tmp_path / "costs.json"
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "cost",
            str(store.path),
            "--case-digest",
            "a" * 64,
            "--experiment-digest",
            "c" * 64,
            "--pricing",
            str(pricing),
            "--output",
            str(destination),
            "--format",
            "json",
        ],
    )
    assert created.exit_code == 0, created.output
    data = json.loads(created.output)
    assert data["summary"]["subtotal_usd"] == "0.00102"
    assert data["provider_calls"] == 0
    assert "private" not in created.output
    store.path.unlink()
    pricing.unlink()

    def no_lookup(*args: object, **kwargs: object) -> None:
        pytest.fail("inspection attempted to resolve current prices")

    monkeypatch.setattr(PriceBook, "__init__", no_lookup)
    inspected = runner.invoke(app, ["inspect", "costs", str(destination), "--format", "json"])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output) == data
    human = runner.invoke(app, ["inspect", "costs", str(destination)])
    assert human.exit_code == 0
    assert "custom-rate-equivalent" in human.output


def test_invalid_private_cost_input_has_a_content_safe_cli_error(tmp_path: Path) -> None:
    pricing = tmp_path / "private-secret-pricing.json"
    pricing.write_text('{"provider":"private-secret"}')
    pricing.chmod(0o600)
    result = CliRunner().invoke(
        app,
        [
            "cost",
            "--scenario",
            str(tmp_path / "private-secret-usage.json"),
            "--pricing",
            str(pricing),
            "--output",
            str(tmp_path / "out.json"),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["error_code"] == "cost-pricing-invalid"
    assert "private-secret" not in result.output


def test_cli_scenario_is_explicit_and_cannot_mix_with_recorded_inputs(tmp_path: Path) -> None:
    pricing, usage = tmp_path / "pricing.json", tmp_path / "usage.json"
    pricing.write_text(book().spec.model_dump_json())
    usage.write_text('{"input_tokens":1000,"output_tokens":100,"cached_input_tokens":200}')
    for path in (pricing, usage):
        path.chmod(0o600)
    common = ["--pricing", str(pricing), "--output", str(tmp_path / "out.json"), "--format", "json"]
    runner = CliRunner()
    missing = runner.invoke(app, ["cost", *common])
    assert missing.exit_code == 2
    assert json.loads(missing.output)["error_code"] == "cost-input-mode"
    mixed = runner.invoke(
        app, ["cost", str(tmp_path / "absent.sqlite"), "--scenario", str(usage), *common]
    )
    assert mixed.exit_code == 2
    assert json.loads(mixed.output)["error_code"] == "cost-input-mode"
    assert not (tmp_path / "absent.sqlite").exists()
    result = runner.invoke(app, ["cost", "--scenario", str(usage), *common])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == "hypothetical"
    assert json.loads(result.output)["summary"]["subtotal_usd"] == "0.00102"
    assert (
        runner.invoke(
            app, ["inspect", "costs", str(tmp_path / "missing.json"), "--format", "json"]
        ).exit_code
        == 2
    )


@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("stage", ["pricing", "timestamp", "mapping", "usage", "source", "output"])
def test_cli_diagnostics_identify_the_failing_stage_without_private_values(
    tmp_path: Path, stage: str, output_format: str
) -> None:
    pricing = tmp_path / "private-sentinel-pricing.json"
    usage = tmp_path / "private-sentinel-usage.json"
    output = tmp_path / "private-sentinel-output.json"
    store = ObservationStore(tmp_path / "private-sentinel-observations.sqlite")
    store.append(observation(1))
    pricing.write_text(book().spec.model_dump_json())
    usage.write_text('{"input_tokens":1000,"output_tokens":100,"cached_input_tokens":200}')
    for path in (pricing, usage):
        path.chmod(0o600)
    if stage == "pricing":
        pricing.write_text('{"provider":"private-sentinel"}')
    elif stage == "timestamp":
        fields = book().spec.model_dump(mode="json")
        fields["as_of"] = "0001-01-01T00:00:00+01:00"
        pricing.write_text(json.dumps(fields))
    elif stage == "mapping":
        pricing.write_text(
            book().spec.model_copy(update={"recorded_source": None}).model_dump_json()
        )
    elif stage == "usage":
        usage.write_text('{"input_tokens":1,"cached_input_tokens":2}')
    elif stage == "source":
        store.path.write_text("private-sentinel-database-content")
    else:
        output.write_text("private-sentinel-do-not-overwrite")
        output.chmod(0o600)
    inputs = (
        [str(store.path), "--case-digest", "a" * 64, "--experiment-digest", "c" * 64]
        if stage in ("source", "mapping")
        else ["--scenario", str(usage)]
    )
    result = CliRunner().invoke(
        app,
        [
            "cost",
            *inputs,
            "--pricing",
            str(pricing),
            "--output",
            str(output),
            "--format",
            output_format,
        ],
    )
    assert result.exit_code == 2
    assert "private-sentinel" not in result.output
    assert "private-provider" not in result.output
    expected = f"cost-{'pricing' if stage in ('mapping', 'timestamp') else stage}-invalid"
    if output_format == "json":
        assert json.loads(result.output)["error_code"] == expected
    else:
        assert {
            "pricing": "--pricing",
            "timestamp": "reference time",
            "mapping": "recorded_source",
            "usage": "--scenario",
            "source": "recorded usage",
            "output": "--output",
        }[stage] in result.output
    if stage == "output":
        assert output.read_text() == "private-sentinel-do-not-overwrite"
    else:
        assert not output.exists()


def test_human_report_aggregates_unpriced_dimensions_beyond_the_json_view(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite")
    rows = []
    for index in range(1, 22):
        row = observation(index)
        evidence = row.attempt_evidence.model_copy(
            update={"usage": TokenUsage(input_tokens=1000, output_tokens=100)}
        )
        rows.append(row.model_copy(update={"attempt_evidence": evidence}))
    store.append_many(rows)
    report = recorded_report(store, case_digest="a" * 64, experiment_digest="c" * 64, book=book())
    destination = tmp_path / "costs.json"
    write_cost_report(destination, report)
    result = CliRunner().invoke(app, ["inspect", "costs", str(destination)])
    assert result.exit_code == 0, result.output
    assert "input_tokens: usage-unknown (observations: 21)" in result.output
    assert "cached_input_tokens: usage-unknown (observations: 21)" in result.output
    assert "output_tokens: usage-unknown" not in result.output
    assert "private" not in result.output


def test_human_scenario_reports_a_missing_duration_rate(tmp_path: Path) -> None:
    pricing, usage = tmp_path / "pricing.json", tmp_path / "usage.json"
    pricing.write_text(book().spec.model_dump_json())
    usage.write_text(
        '{"input_tokens":100,"output_tokens":0,"cached_input_tokens":0,"cache_write_5m_tokens":50,"cache_write_1h_tokens":0}'
    )
    for path in (pricing, usage):
        path.chmod(0o600)
    result = CliRunner().invoke(
        app,
        [
            "cost",
            "--scenario",
            str(usage),
            "--pricing",
            str(pricing),
            "--output",
            str(tmp_path / "costs.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "cache_write_5m_tokens: rate-unavailable" in result.output
    assert "observations:" not in result.output
