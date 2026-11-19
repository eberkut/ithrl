"""Offline token-price equivalents with explicit usage and rate provenance."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from importlib.metadata import version
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from ithrl.context import content_digest

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0, le=10**15)]


def _exact_rate(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("rates require decimal strings")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal rate") from None


Rate = Annotated[
    Decimal,
    BeforeValidator(_exact_rate),
    Field(ge=0, allow_inf_nan=False, max_digits=30, decimal_places=12),
]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=60, decimal_places=18)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9./:_-]{0,127}$")]
type PriceKey = Literal[
    "input_mtok",
    "output_mtok",
    "cache_read_mtok",
    "cache_write_mtok",
    "cache_write_5m_mtok",
    "cache_write_1h_mtok",
]
type TokenDimension = Literal[
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
]
type WriteMode = Literal["generic", "durations"]
_DIMENSIONS: dict[PriceKey, TokenDimension] = {
    "input_mtok": "input_tokens",
    "output_mtok": "output_tokens",
    "cache_read_mtok": "cached_input_tokens",
    "cache_write_mtok": "cache_write_tokens",
    "cache_write_5m_mtok": "cache_write_5m_tokens",
    "cache_write_1h_mtok": "cache_write_1h_tokens",
}


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class CostUsage(_Contract):
    """Inclusive input/output totals; cache and reasoning counts are subsets."""

    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cached_input_tokens: Count | None = None
    cache_write_tokens: Count | None = None
    cache_write_5m_tokens: Count | None = None
    cache_write_1h_tokens: Count | None = None
    reasoning_output_tokens: Count | None = None

    @model_validator(mode="after")
    def subsets(self) -> CostUsage:
        writes = max(
            self.cache_write_total or 0,
            (self.cache_write_5m_tokens or 0) + (self.cache_write_1h_tokens or 0),
        )
        if self.input_tokens is not None and (
            (self.cached_input_tokens or 0) + writes > self.input_tokens
        ):
            raise ValueError("cache token subsets exceed input tokens")
        if (
            self.output_tokens is not None
            and self.reasoning_output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("reasoning token subset exceeds output tokens")
        if self.cache_write_tokens is not None:
            parts = (self.cache_write_5m_tokens, self.cache_write_1h_tokens)
            if sum(x or 0 for x in parts) > self.cache_write_tokens or (
                all(x is not None for x in parts)
                and sum(x or 0 for x in parts) != self.cache_write_tokens
            ):
                raise ValueError("cache write durations disagree with their total")
        return self

    @property
    def cache_write_total(self) -> int | None:
        if self.cache_write_tokens is not None:
            return self.cache_write_tokens
        if self.cache_write_5m_tokens is not None and self.cache_write_1h_tokens is not None:
            return self.cache_write_5m_tokens + self.cache_write_1h_tokens
        return None

    def billable_counts(self, mode: WriteMode) -> dict[TokenDimension, int | None]:
        """One count projection shared by calculation and saved-report validation."""
        cached, writes = self.cached_input_tokens, self.cache_write_total
        if self.input_tokens == 0:
            cached, writes = 0, 0
        counts: dict[TokenDimension, int | None] = {
            "input_tokens": self.input_tokens - cached - writes
            if self.input_tokens is not None and cached is not None and writes is not None
            else None,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": cached,
        }
        if mode == "generic":
            counts["cache_write_tokens"] = writes
        else:
            counts["cache_write_5m_tokens"] = 0 if writes == 0 else self.cache_write_5m_tokens
            counts["cache_write_1h_tokens"] = 0 if writes == 0 else self.cache_write_1h_tokens
        return counts


class RateTier(_Contract):
    start: Count
    price: Rate


class TieredRate(_Contract):
    base: Rate
    tiers: Annotated[tuple[RateTier, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def ordered(self) -> TieredRate:
        starts = [tier.start for tier in self.tiers]
        if starts != sorted(set(starts)):
            raise ValueError("price thresholds must be unique and increasing")
        return self


class RecordedSource(_Contract):
    """Explicit private deployment mapping; reports disclose only its digest."""

    provider: Annotated[str, Field(min_length=1, max_length=256, repr=False)]
    model: Annotated[str, Field(min_length=1, max_length=256, repr=False)]
    endpoint: Annotated[str, Field(min_length=1, max_length=2048, repr=False)]

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump())


class PricingSpec(_Contract):
    """Operator-selected reference pricing, independent of native execution policy."""

    provider: Identifier
    model: Identifier
    as_of: datetime
    context: Annotated[dict[Identifier, Identifier], Field(max_length=8)] = Field(
        default_factory=dict
    )
    recorded_source: RecordedSource | None = Field(default=None, repr=False)
    custom_rates: dict[PriceKey, Rate | TieredRate] | None = None

    @field_validator("as_of")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("pricing reference time requires a UTC offset")
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("pricing reference time is outside the supported UTC range") from None

    @model_validator(mode="after")
    def no_empty_prices(self) -> PricingSpec:
        if self.custom_rates is not None and not self.custom_rates:
            raise ValueError("custom prices cannot be empty")
        if self.context.get("service_tier") == "auto":
            raise ValueError("auto is not a priced service tier")
        return self


class PricingProvenance(_Contract):
    provider: Identifier
    model: Identifier
    as_of: datetime
    context: Annotated[dict[Identifier, Identifier], Field(max_length=8)]
    currency: Literal["USD"] = "USD"
    semantics: Literal[
        "api-list-price-equivalent", "published-reference-rate", "custom-rate-equivalent"
    ]
    source: Literal["genai-prices-bundled", "operator-supplied"]
    package_version: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9.+-]+$")
    ]
    data_digest: Digest
    recorded_source_digest: Digest | None
    timestamp_basis: Literal["operator-selected-reference-time"] = (
        "operator-selected-reference-time"
    )


class CostComponent(_Contract):
    dimension: TokenDimension
    tokens: Count | None
    usd_per_million: Rate | None
    amount_usd: Money | None
    reason: (
        Literal[
            "usage-unknown",
            "rate-unavailable",
            "request-context-unknown",
            "cache-retention-unknown",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def arithmetic(self) -> CostComponent:
        if self.amount_usd is None:
            if self.reason is None:
                raise ValueError("unpriced component requires a reason")
        else:
            with localcontext() as context:
                context.prec = 60
                expected = (
                    Decimal(self.tokens) * self.usd_per_million / 1_000_000
                    if self.tokens is not None and self.usd_per_million is not None
                    else Decimal(0)
                    if self.tokens == 0
                    else None
                )
            if self.reason is not None or self.amount_usd != expected:
                raise ValueError("cost component arithmetic differs")
        return self


class TokenCharges(_Contract):
    write_mode: WriteMode
    components: Annotated[tuple[CostComponent, ...], Field(max_length=6)]

    def validate_usage(self, usage: CostUsage) -> None:
        expected = usage.billable_counts(self.write_mode)
        if (
            len(self.components) != len(expected)
            or {c.dimension for c in self.components} != expected.keys()
        ):
            raise ValueError("token charges require the complete unique count projection")
        for component in self.components:
            if component.tokens != expected[component.dimension]:
                raise ValueError("priced component count differs from usage")
            if component.reason == "usage-unknown" and component.tokens is not None:
                raise ValueError("known usage cannot be marked unknown")
            if component.tokens is None and component.reason != "usage-unknown":
                raise ValueError("missing usage requires an unknown component")
            if component.reason == "request-context-unknown" and usage.input_tokens is not None:
                raise ValueError("request-context refusal requires an unknown prompt size")

    @property
    def subtotal_usd(self) -> Decimal | None:
        values = [x.amount_usd for x in self.components if x.amount_usd is not None]
        return sum_money(values) if values else None

    @property
    def complete(self) -> bool:
        return bool(self.components) and all(x.amount_usd is not None for x in self.components)


class TokenEstimate(TokenCharges):
    usage: CostUsage

    @model_validator(mode="after")
    def matching_counts(self) -> TokenEstimate:
        self.validate_usage(self.usage)
        return self


def sum_money(values: list[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        return sum(values, Decimal(0))


class PriceBook:
    """Resolve one exact model from bundled data without updater or global snapshot state."""

    def __init__(self, spec: PricingSpec) -> None:
        from genai_prices.types import ModelPrice, Tier, TieredPrices

        self.spec = spec
        context = spec.context
        if spec.custom_rates is not None:
            rates = {
                key: TieredPrices(value.base, [Tier(x.start, x.price) for x in value.tiers])
                if isinstance(value, TieredRate)
                else value
                for key, value in spec.custom_rates.items()
            }
            self.prices = ModelPrice(**rates)
            digest = content_digest(spec.model_dump(mode="json")["custom_rates"])
        else:
            from genai_prices.data import providers

            provider = next((p for p in providers if p.id == spec.provider), None)
            model = (
                next((m for m in provider.models if m.id == spec.model), None) if provider else None
            )
            if model is None:
                raise ValueError("exact pricing provider/model is unavailable")
            if isinstance(model.prices, list) and not any(
                p.constraint is None or p.constraint.active(spec.as_of) for p in model.prices
            ):
                raise ValueError("pricing reference date is unsupported")
            variants = [
                v
                for v in model.price_variants or ()
                if v.constraint is None or v.constraint.active(spec.as_of)
            ]
            if not context and any(v.when == {"service_tier": "default"} for v in variants):
                context = {"service_tier": "default"}
            if context and not any(v.when == context for v in variants):
                raise ValueError("pricing context has no applicable exact variant")
            self.prices = model.get_prices(spec.as_of, context)
            if spec.provider == "anthropic" and self.prices.cache_write_mtok is not None:
                # Anthropic publishes its default cache-write rate for a five-minute TTL.
                self.prices = ModelPrice(
                    **{
                        **self.prices.__dict__,
                        "cache_write_5m_mtok": self.prices.cache_write_mtok,
                    }
                )
            from genai_prices.data_units import unit_data

            token_keys = {
                unit["price_key"]
                for unit in unit_data.values()
                if unit["dimensions"].get("family") == "tokens"
            }
            if any(
                value is not None and key in token_keys - _DIMENSIONS.keys()
                for key, value in self.prices.__dict__.items()
            ):
                raise ValueError("model uses unsupported token-price dimensions")
            if not any(getattr(self.prices, key) is not None for key in _DIMENSIONS):
                raise ValueError("model has no supported token prices")
            package = files("genai_prices")
            digest = content_digest(
                {
                    name: hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()
                    for name in ("data.py", "data_units.py")
                }
            )
        # OpenAI write rates depend on retention, which a generic count does not identify.
        # https://developers.openai.com/api/docs/guides/prompt-caching
        self._write_retention_unknown = (
            spec.custom_rates is None
            and spec.provider == "openai"
            and self.prices.cache_write_mtok is not None
        )
        self.provenance = PricingProvenance(
            provider=spec.provider,
            model=spec.model,
            as_of=spec.as_of,
            context=context,
            semantics="custom-rate-equivalent"
            if spec.custom_rates is not None
            else "published-reference-rate"
            if spec.provider == "github-copilot"
            else "api-list-price-equivalent",
            source="operator-supplied" if spec.custom_rates is not None else "genai-prices-bundled",
            package_version=version("genai-prices"),
            data_digest=digest,
            recorded_source_digest=spec.recorded_source.digest if spec.recorded_source else None,
        )

    def estimate(self, usage: CostUsage) -> TokenEstimate:
        from genai_prices.types import TieredPrices, calc_unit_price

        has_durations = (
            any(
                getattr(self.prices, key) is not None
                for key in ("cache_write_5m_mtok", "cache_write_1h_mtok")
            )
            or usage.cache_write_5m_tokens is not None
            or usage.cache_write_1h_tokens is not None
        )
        mode: WriteMode = "durations" if has_durations else "generic"
        counts = usage.billable_counts(mode)
        components = []
        for key, dimension in _DIMENSIONS.items():
            if dimension not in counts:
                continue
            price, count = getattr(self.prices, key), counts[dimension]
            retention_unknown = key == "cache_write_mtok" and self._write_retention_unknown
            if retention_unknown:
                price = None
            reason = None
            if count is None:
                reason = "usage-unknown"
            elif price is None and count != 0:
                reason = "cache-retention-unknown" if retention_unknown else "rate-unavailable"
            elif isinstance(price, TieredPrices) and usage.input_tokens is None and count != 0:
                reason = "request-context-unknown"
            with localcontext() as context:
                context.prec = 60
                rate = (
                    calc_unit_price(price, 1_000_000, usage.input_tokens or 0, 1_000_000)
                    if reason is None
                    and price is not None
                    and not (isinstance(price, TieredPrices) and usage.input_tokens is None)
                    else None
                )
                amount = (
                    calc_unit_price(price, count, usage.input_tokens or 0, 1_000_000)
                    if reason is None
                    else None
                )
            components.append(
                CostComponent(
                    dimension=dimension,
                    tokens=count,
                    usd_per_million=rate,
                    amount_usd=amount,
                    reason=reason,
                )
            )
        return TokenEstimate(usage=usage, write_mode=mode, components=tuple(components))
