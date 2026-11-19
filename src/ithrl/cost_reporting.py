"""Private post-run cost reports, independent of native execution and sampling."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ithrl.case.filesystem import atomic_write, read_safe_text
from ithrl.context import content_digest
from ithrl.costs import (
    CostUsage,
    Count,
    Digest,
    PriceBook,
    PricingProvenance,
    TokenCharges,
    TokenEstimate,
    _Contract,
    sum_money,
)
from ithrl.observations import Observation, ObservationStore
from ithrl.path_safety import validate_directory_chain
from ithrl.results.validation import loads_json
from ithrl.runner import ProviderAcceptanceState

MAX_COST_REPORT_BYTES = 64 * 1024 * 1024


class RecordedCost(_Contract):
    observation_digest: Digest
    attempt_digest: Digest
    attribution_digest: Digest
    service_tier_digest: Digest | None
    recorded_at: datetime
    outcome: Literal["match", "no_match", "invalid_observation", "runner_error"]
    usage_provenance: Literal["runner-reported-attempt"] = "runner-reported-attempt"
    usage: CostUsage | None
    provider_acceptance: ProviderAcceptanceState
    provider_calls_started: Count | None
    provider_calls_completed: Count | None
    provider_calls_possible: Count
    provider_calls_started_exact: bool
    additional_call_exposure_unknown: bool
    charges: TokenCharges | None
    reason: (
        Literal[
            "no-provider-calls",
            "usage-unavailable",
            "attribution-unmatched",
            "service-tier-unmatched",
            "multiple-provider-requests",
            "incomplete-provider-request",
        ]
        | None
    )


class CostReport(_Contract):
    schema_version: Literal[1] = 1
    kind: Literal["token-cost-estimate"] = "token-cost-estimate"
    mode: Literal["recorded-usage", "hypothetical"]
    pricing: PricingProvenance
    case_digest: Digest | None = None
    experiment_digest: Digest | None = None
    observations: Annotated[tuple[RecordedCost, ...], Field(max_length=10_000)] = ()
    scenario: TokenEstimate | None = None

    @model_validator(mode="after")
    def scope(self) -> CostReport:
        if self.mode == "hypothetical":
            if (
                self.scenario is None
                or self.observations
                or self.case_digest
                or self.experiment_digest
            ):
                raise ValueError("hypothetical report requires only scenario inputs")
        elif (
            self.scenario is not None or self.case_digest is None or self.experiment_digest is None
        ):
            raise ValueError("recorded report requires case and experiment scope")
        digests = [row.attempt_digest for row in self.observations]
        if len(digests) != len(set(digests)):
            raise ValueError("cost report repeats an attempt")
        for row in self.observations:
            if (
                row.provider_acceptance is ProviderAcceptanceState.UNKNOWN
                and row.provider_calls_started_exact
            ):
                raise ValueError("unknown acceptance cannot establish an exact started-call count")
            if row.charges is not None and row.reason is not None:
                raise ValueError("a refused attempt cannot have priced components")
            if row.charges is None and row.reason is None:
                raise ValueError("unpriced attempt requires a reason")
            if row.charges is not None and (
                row.usage is None
                or row.provider_acceptance is not ProviderAcceptanceState.ACCEPTED
                or not row.provider_calls_started_exact
                or row.provider_calls_started != 1
                or row.provider_calls_completed != 1
                or row.attribution_digest != self.pricing.recorded_source_digest
                or _tier_conflicts(row.service_tier_digest, self.pricing)
            ):
                raise ValueError("priced usage requires matching single-call attribution")
            if row.charges is not None and row.usage is not None:
                row.charges.validate_usage(row.usage)
            if row.reason == "no-provider-calls" and (
                row.provider_acceptance is not ProviderAcceptanceState.NOT_STARTED
                or row.provider_calls_started != 0
                or row.provider_calls_completed != 0
                or row.provider_calls_possible != 0
                or row.additional_call_exposure_unknown
            ):
                raise ValueError("zero-provider cost requires complete zero-call evidence")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

    def summary(self) -> dict[str, object]:
        if self.scenario is not None:
            return {
                "scope": "hypothetical-token-counts",
                "subtotal_usd": _amount(self.scenario.subtotal_usd),
                "token_dimensions_complete": self.scenario.complete,
            }
        amounts = [
            Decimal(0)
            if row.reason == "no-provider-calls"
            else row.charges.subtotal_usd
            if row.charges is not None
            else None
            for row in self.observations
        ]
        known = [value for value in amounts if value is not None]
        reasons: Counter[str] = Counter(
            row.reason for row in self.observations if row.reason not in (None, "no-provider-calls")
        )
        reasons.update(
            component.reason
            for row in self.observations
            if row.charges is not None
            for component in row.charges.components
            if component.reason is not None
        )
        return {
            "scope": "recorded-observations-for-case-and-experiment",
            "observations": len(self.observations),
            "priced_observations": sum(
                row.charges is not None and row.charges.complete for row in self.observations
            ),
            "partially_priced_observations": sum(
                row.charges is not None
                and not row.charges.complete
                and row.charges.subtotal_usd is not None
                for row in self.observations
            ),
            "unpriced_observations": sum(value is None for value in amounts),
            "known_zero_provider_observations": sum(
                row.reason == "no-provider-calls" for row in self.observations
            ),
            "subtotal_usd": _amount(sum_money(known)) if known else None,
            "provider_calls_started_known": sum(
                row.provider_calls_started or 0 for row in self.observations
            ),
            "provider_calls_completed_known": sum(
                row.provider_calls_completed or 0 for row in self.observations
            ),
            "provider_calls_possible_recorded": sum(
                row.provider_calls_possible for row in self.observations
            ),
            "attempts_with_unknown_additional_calls": sum(
                row.additional_call_exposure_unknown for row in self.observations
            ),
            "pending_provider_calls": None,
            "whole_investigation_coverage": "unavailable",
            "unpriced_reasons": dict(sorted(reasons.items())),
        }


def _amount(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _tier_conflicts(digest: str | None, pricing: PricingProvenance) -> bool:
    selected = pricing.context.get("service_tier")
    return digest is not None and selected is not None and digest != content_digest(selected)


def _record(observation: Observation, book: PriceBook) -> RecordedCost:
    evidence = observation.attempt_evidence
    accounting = evidence.accounting
    source = {
        "provider": evidence.provider,
        "model": evidence.model_returned,
        "endpoint": evidence.endpoint,
    }
    attribution = content_digest(source)
    service_tier = (
        content_digest(evidence.service_tier) if evidence.service_tier is not None else None
    )
    acceptance = evidence.provider_acceptance_state
    if accounting is not None:
        started, completed = accounting.provider_calls_started, accounting.provider_calls_completed
    elif acceptance is ProviderAcceptanceState.NOT_STARTED:
        started, completed = 0, 0
    elif acceptance is ProviderAcceptanceState.ACCEPTED:
        started, completed = 1, None
    else:
        started = completed = None
    possible = evidence.provider_calls_possible
    additional_unknown = acceptance is not ProviderAcceptanceState.NOT_STARTED and (
        accounting is None
        or (
            evidence.provider_acceptance_state is ProviderAcceptanceState.ACCEPTED
            and evidence.termination_reason != "completed"
        )
    )
    if started == completed == possible == 0 and acceptance is ProviderAcceptanceState.NOT_STARTED:
        reason = "no-provider-calls"
    elif evidence.usage is None:
        reason = "usage-unavailable"
    elif attribution != book.provenance.recorded_source_digest:
        reason = "attribution-unmatched"
    elif _tier_conflicts(service_tier, book.provenance):
        reason = "service-tier-unmatched"
    elif started is not None and started > 1:
        reason = "multiple-provider-requests"
    elif acceptance is not ProviderAcceptanceState.ACCEPTED or started != 1 or completed != 1:
        reason = "incomplete-provider-request"
    else:
        reason = None
    usage = (
        CostUsage.model_validate(evidence.usage.model_dump())
        if evidence.usage is not None
        else None
    )
    charges = None
    if reason is None and usage is not None:
        estimate = book.estimate(usage)
        charges = TokenCharges(write_mode=estimate.write_mode, components=estimate.components)
    return RecordedCost(
        observation_digest=content_digest(observation.model_dump(mode="json")),
        attempt_digest=evidence.attempt_digest,
        attribution_digest=attribution,
        service_tier_digest=service_tier,
        recorded_at=observation.recorded_at,
        outcome=observation.outcome.value,
        usage=usage,
        provider_acceptance=acceptance,
        provider_calls_started=started,
        provider_calls_completed=completed,
        provider_calls_possible=possible,
        provider_calls_started_exact=acceptance is ProviderAcceptanceState.NOT_STARTED
        or (accounting is not None and acceptance is ProviderAcceptanceState.ACCEPTED),
        additional_call_exposure_unknown=additional_unknown,
        charges=charges,
        reason=reason,
    )


def recorded_report(
    store: ObservationStore,
    *,
    case_digest: str,
    experiment_digest: str,
    book: PriceBook,
    limit: int = 10_000,
) -> CostReport:
    if book.spec.recorded_source is None:
        raise ValueError("recorded usage requires an explicit source-to-pricing mapping")
    # A physical attempt can be referenced repeatedly after resume; count it once.
    observations = store.read_snapshot(
        case_digest=case_digest, experiment_digest=experiment_digest, limit=limit
    )
    unique: dict[str, Observation] = {}
    for observation in observations:
        key = observation.attempt_evidence.attempt_id
        if previous := unique.get(key):
            if (
                previous.attempt_evidence != observation.attempt_evidence
                or previous.candidate_digest != observation.candidate_digest
                or previous.outcome != observation.outcome
                or previous.error_category != observation.error_category
            ):
                raise ValueError("duplicate attempt has conflicting evidence")
        else:
            unique[key] = observation
    return CostReport(
        mode="recorded-usage",
        pricing=book.provenance,
        case_digest=case_digest,
        experiment_digest=experiment_digest,
        observations=tuple(_record(row, book) for row in unique.values()),
    )


def hypothetical_report(usage: CostUsage, book: PriceBook) -> CostReport:
    return CostReport(mode="hypothetical", pricing=book.provenance, scenario=book.estimate(usage))


def write_cost_report(path: Path, report: CostReport) -> None:
    validate_directory_chain(path.parent)
    data = report.model_dump(mode="json")
    envelope = {"report": data, "sha256": content_digest(data)}
    text = json.dumps(envelope, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2) + "\n"
    if len(text.encode()) > MAX_COST_REPORT_BYTES:
        raise ValueError("cost report exceeds its byte limit")
    atomic_write(path, text)


def load_cost_report(path: Path) -> CostReport:
    envelope = loads_json(
        read_safe_text(path, max_bytes=MAX_COST_REPORT_BYTES, label="cost report"),
        label="cost report",
    )
    if not isinstance(envelope, dict) or set(envelope) != {"report", "sha256"}:
        raise ValueError("invalid cost report envelope")
    report = CostReport.model_validate_json(json.dumps(envelope["report"]))
    if report.digest != envelope["sha256"]:
        raise ValueError("cost report digest differs")
    return report
