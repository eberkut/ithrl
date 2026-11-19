"""Reference pricing, count semantics and saved-report arithmetic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ithrl.case import CaseExistsError
from ithrl.context import content_digest
from ithrl.cost_reporting import hypothetical_report, load_cost_report, write_cost_report
from ithrl.costs import CostComponent, CostUsage, PriceBook, PricingSpec, TieredRate

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def spec(**updates: object) -> PricingSpec:
    return PricingSpec.model_validate(
        {
            "provider": "openai",
            "model": "gpt-6.1-sol",
            "as_of": NOW,
            "custom_rates": {
                "input_mtok": Decimal(2),
                "output_mtok": Decimal(10),
                "cache_read_mtok": Decimal("0.1"),
            },
            **updates,
        }
    )


def test_cached_and_reasoning_subsets_are_not_charged_twice() -> None:
    result = PriceBook(spec()).estimate(
        CostUsage(
            input_tokens=1000,
            cached_input_tokens=200,
            cache_write_tokens=0,
            output_tokens=100,
            reasoning_output_tokens=70,
        )
    )
    assert result.subtotal_usd == Decimal("0.00262")
    assert result.complete
    assert [(c.dimension, c.tokens) for c in result.components] == [
        ("input_tokens", 800),
        ("output_tokens", 100),
        ("cached_input_tokens", 200),
        ("cache_write_tokens", 0),
    ]


@pytest.mark.parametrize(
    "counts",
    [
        {"input_tokens": 10, "cached_input_tokens": 11},
        {"input_tokens": 10, "cached_input_tokens": 4, "cache_write_tokens": 7},
        {"output_tokens": 10, "reasoning_output_tokens": 11},
        {"cache_write_tokens": 10, "cache_write_5m_tokens": 3, "cache_write_1h_tokens": 3},
        {"input_tokens": True},
        {"input_tokens": -1},
        {"input_tokens": 10, "cache_write_5m_tokens": 11},
        {"cache_write_tokens": 10, "cache_write_1h_tokens": 11},
    ],
)
def test_contradictory_or_invalid_counts_refuse(counts: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CostUsage.model_validate(counts)


def test_unknown_cache_counts_leave_input_unpriced_but_preserve_output_subtotal() -> None:
    result = PriceBook(spec()).estimate(CostUsage(input_tokens=1000, output_tokens=100))
    assert result.subtotal_usd == Decimal("0.001")
    assert not result.complete
    assert {c.dimension for c in result.components if c.reason == "usage-unknown"} == {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
    }


def test_missing_write_count_stays_unknown_when_custom_rates_omit_writes() -> None:
    book = PriceBook(spec())
    usage = CostUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=200)
    result = book.estimate(usage)
    assert result.subtotal_usd == Decimal("0.00102")
    assert not result.complete
    unknown = {c.dimension for c in result.components if c.reason == "usage-unknown"}
    assert unknown == {"input_tokens", "cache_write_tokens"}
    explicit = book.estimate(usage.model_copy(update={"cache_write_tokens": 0}))
    assert explicit.complete
    assert explicit.subtotal_usd == Decimal("0.00262")


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6.2-astra", "gpt-6.2-sol"])
def test_retention_specific_catalog_write_rate_needs_retention_evidence(model: str) -> None:
    book = PriceBook(spec(custom_rates=None, model=model))
    usage = CostUsage(
        input_tokens=1000, output_tokens=100, cached_input_tokens=100, cache_write_tokens=100
    )
    result = book.estimate(usage)
    write = next(c for c in result.components if c.dimension == "cache_write_tokens")
    assert write.reason == "cache-retention-unknown"
    assert write.tokens == 100
    assert write.amount_usd is None
    assert write.usd_per_million is None
    assert all(c.amount_usd is not None for c in result.components if c is not write)
    assert not result.complete
    zero = book.estimate(usage.model_copy(update={"cache_write_tokens": 0}))
    assert zero.complete
    assert (
        next(c for c in zero.components if c.dimension == "cache_write_tokens").usd_per_million
        is None
    )


def test_explicit_custom_write_rate_remains_an_operator_assumption() -> None:
    book = PriceBook(
        spec(
            model="gpt-6.1-terra",
            custom_rates={
                "input_mtok": Decimal(2),
                "output_mtok": Decimal(10),
                "cache_read_mtok": Decimal("0.1"),
                "cache_write_mtok": Decimal(3),
            },
        )
    )
    result = book.estimate(
        CostUsage(
            input_tokens=1000, output_tokens=100, cached_input_tokens=100, cache_write_tokens=100
        )
    )
    assert result.complete
    assert result.subtotal_usd == Decimal("0.00291")


def test_generic_write_remainder_does_not_establish_a_missing_duration() -> None:
    book = PriceBook(
        spec(
            custom_rates={
                "input_mtok": Decimal(2),
                "output_mtok": Decimal(10),
                "cache_read_mtok": Decimal("0.1"),
                "cache_write_5m_mtok": Decimal("2.5"),
                "cache_write_1h_mtok": Decimal(4),
            }
        )
    )
    result = book.estimate(
        CostUsage(
            input_tokens=100,
            output_tokens=0,
            cached_input_tokens=0,
            cache_write_tokens=50,
            cache_write_5m_tokens=20,
        )
    )
    hour = next(c for c in result.components if c.dimension == "cache_write_1h_tokens")
    assert hour.tokens is None
    assert hour.reason == "usage-unknown"
    assert not result.complete


def test_empty_usage_never_becomes_zero() -> None:
    result = PriceBook(spec()).estimate(CostUsage())
    assert result.subtotal_usd is None
    assert not result.complete


def test_explicit_zero_usage_and_zero_rates_remain_distinct_from_missing() -> None:
    result = PriceBook(spec()).estimate(
        CostUsage(input_tokens=0, output_tokens=0, cached_input_tokens=0)
    )
    assert result.subtotal_usd == 0
    assert result.complete
    free = PriceBook(
        spec(
            custom_rates={
                "input_mtok": Decimal(0),
                "output_mtok": Decimal(0),
                "cache_read_mtok": Decimal(0),
            }
        )
    )
    assert (
        free.estimate(
            CostUsage(
                input_tokens=10, output_tokens=10, cached_input_tokens=0, cache_write_tokens=0
            )
        ).subtotal_usd
        == 0
    )
    with pytest.raises(ValidationError, match="cannot be empty"):
        spec(custom_rates={})


def test_explicit_cache_write_durations_use_their_own_rates() -> None:
    book = PriceBook(
        spec(
            custom_rates={
                "input_mtok": Decimal(2),
                "output_mtok": Decimal(10),
                "cache_read_mtok": Decimal("0.1"),
                "cache_write_5m_mtok": Decimal("2.5"),
                "cache_write_1h_mtok": Decimal(4),
            }
        )
    )
    result = book.estimate(
        CostUsage(
            input_tokens=1000,
            cached_input_tokens=100,
            output_tokens=100,
            cache_write_5m_tokens=200,
            cache_write_1h_tokens=300,
        )
    )
    assert result.complete
    assert result.subtotal_usd == Decimal("0.00351")
    ambiguous = book.estimate(
        CostUsage(
            input_tokens=1000, cached_input_tokens=100, output_tokens=100, cache_write_tokens=500
        )
    )
    assert not ambiguous.complete
    assert ambiguous.subtotal_usd == Decimal("0.00181")


def test_context_threshold_changes_all_tokens_and_output_rate() -> None:
    price = TieredRate.model_validate_json('{"base":"2","tiers":[{"start":100,"price":"4"}]}')
    book = PriceBook(
        spec(
            custom_rates={"input_mtok": price, "output_mtok": price, "cache_read_mtok": Decimal(0)}
        )
    )
    before = book.estimate(
        CostUsage(input_tokens=100, output_tokens=10, cached_input_tokens=0, cache_write_tokens=0)
    )
    after = book.estimate(
        CostUsage(input_tokens=101, output_tokens=10, cached_input_tokens=0, cache_write_tokens=0)
    )
    assert before.subtotal_usd == Decimal("0.00022")
    assert after.subtotal_usd == Decimal("0.000444")
    unknown = book.estimate(CostUsage(output_tokens=10, cached_input_tokens=0))
    assert unknown.subtotal_usd == 0  # Only the explicitly zero cache-read component is priced.
    assert (
        next(c for c in unknown.components if c.dimension == "output_tokens").reason
        == "request-context-unknown"
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "private-deployment"},
        {"model": "unrecognized-alias"},
        {"context": {"service_tier": "typo"}},
        {"context": {"region": "unpriced-region"}},
        {"context": {"service_tier": "priority", "processing": "batch"}},
        {"context": {"service_tier": "flex"}, "as_of": datetime(2026, 10, 1, tzinfo=UTC)},
    ],
)
def test_bundled_selection_refuses_fallbacks(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"unavailable|unsupported|variant"):
        PriceBook(spec(custom_rates=None, **updates))


def test_bundled_model_service_tiers_and_data_identity_are_explicit() -> None:
    default = PriceBook(spec(custom_rates=None, context={"service_tier": "default"}))
    implicit = PriceBook(spec(custom_rates=None))
    priority = PriceBook(spec(custom_rates=None, context={"service_tier": "priority"}))
    usage = CostUsage(
        input_tokens=1000, output_tokens=100, cached_input_tokens=0, cache_write_tokens=0
    )
    assert priority.estimate(usage).subtotal_usd != default.estimate(usage).subtotal_usd
    assert implicit.provenance == default.provenance
    assert implicit.estimate(usage) == default.estimate(usage)
    assert default.provenance.source == "genai-prices-bundled"
    assert default.provenance.package_version == "0.2.4"
    assert len(default.provenance.data_digest) == 64
    assert default.provenance.timestamp_basis == "operator-selected-reference-time"
    with pytest.raises(ValidationError):
        spec(context={"service_tier": "auto"})


def test_default_tier_is_not_added_to_other_pricing_contexts() -> None:
    assert PriceBook(spec()).provenance.context == {}
    batch = PriceBook(spec(custom_rates=None, context={"processing": "batch"}))
    assert batch.provenance.context == {"processing": "batch"}
    anthropic = PriceBook(spec(provider="anthropic", model="claude-sonnet-5-5", custom_rates=None))
    assert anthropic.provenance.context == {}


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5"])
def test_current_anthropic_models_price_cache_durations(model: str) -> None:
    book = PriceBook(spec(provider="anthropic", model=model, custom_rates=None))
    result = book.estimate(
        CostUsage(
            input_tokens=1000,
            output_tokens=100,
            cached_input_tokens=100,
            cache_write_5m_tokens=200,
            cache_write_1h_tokens=300,
            reasoning_output_tokens=50,
        )
    )
    assert result.complete
    assert result.subtotal_usd is not None
    assert result.subtotal_usd > 0
    assert (
        next(c for c in result.components if c.dimension == "cache_write_5m_tokens").usd_per_million
        != next(
            c for c in result.components if c.dimension == "cache_write_1h_tokens"
        ).usd_per_million
    )


def test_zero_quantity_does_not_invent_an_unresolved_tier_rate() -> None:
    tier = TieredRate.model_validate_json('{"base":"2","tiers":[{"start":100,"price":"4"}]}')
    result = PriceBook(spec(custom_rates={"output_mtok": tier})).estimate(
        CostUsage(output_tokens=0)
    )
    output = next(c for c in result.components if c.dimension == "output_tokens")
    assert output.amount_usd == 0
    assert output.usd_per_million is None


def test_provable_zero_input_does_not_require_cache_telemetry() -> None:
    result = PriceBook(spec()).estimate(CostUsage(input_tokens=0, output_tokens=0))
    assert result.complete
    assert result.subtotal_usd == 0


def test_partial_write_counts_stay_visible_without_an_applicable_rate() -> None:
    result = PriceBook(spec()).estimate(
        CostUsage(
            input_tokens=100, output_tokens=0, cached_input_tokens=0, cache_write_5m_tokens=50
        )
    )
    by_dimension = {c.dimension: c for c in result.components}
    assert by_dimension["input_tokens"].tokens is None
    assert by_dimension["cache_write_5m_tokens"].tokens == 50
    assert by_dimension["cache_write_5m_tokens"].reason == "rate-unavailable"
    assert by_dimension["cache_write_1h_tokens"].reason == "usage-unknown"
    assert not result.complete


def test_generic_write_rate_does_not_price_an_explicit_duration() -> None:
    result = PriceBook(spec(custom_rates={"cache_write_mtok": Decimal(3)})).estimate(
        CostUsage(
            input_tokens=100,
            output_tokens=0,
            cached_input_tokens=0,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=100,
        )
    )
    component = next(c for c in result.components if c.dimension == "cache_write_1h_tokens")
    assert component.tokens == 100
    assert component.reason == "rate-unavailable"
    assert component.amount_usd is None


@pytest.mark.parametrize("rate", ["1e100000", "1e-100000", "NaN", "Infinity"])
def test_extreme_rates_refuse_before_decimal_arithmetic(rate: str) -> None:
    with pytest.raises(ValidationError):
        spec(custom_rates={"input_mtok": Decimal(rate)})


def test_rehashed_report_cannot_change_component_counts_independently(tmp_path: Path) -> None:
    path = tmp_path / "cost.json"
    report = hypothetical_report(
        CostUsage(input_tokens=100, output_tokens=10, cached_input_tokens=0, cache_write_tokens=0),
        PriceBook(spec()),
    )
    write_cost_report(path, report)
    envelope = json.loads(path.read_text())
    component = envelope["report"]["scenario"]["components"][0]
    component.update(tokens=0, amount_usd="0")
    envelope["sha256"] = content_digest(envelope["report"])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="count differs from usage"):
        load_cost_report(path)


def test_saved_report_is_self_contained_and_rejects_arithmetic_tampering(tmp_path: Path) -> None:
    report = hypothetical_report(
        CostUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=200), PriceBook(spec())
    )
    path = tmp_path / "cost.json"
    write_cost_report(path, report)
    assert load_cost_report(path) == report
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CaseExistsError, match="overwrite"):
        write_cost_report(path, report)
    envelope = json.loads(path.read_text())
    envelope["report"]["scenario"]["components"][0]["amount_usd"] = "123"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="arithmetic"):
        load_cost_report(path)
    with pytest.raises(ValidationError, match="arithmetic"):
        CostComponent(
            dimension="output_tokens", tokens=100, usd_per_million=Decimal(1), amount_usd=Decimal(5)
        )
