"""Validate protected output from one Codex app-server worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ithrl.execution import MAX_NATIVE_OUTPUT_BYTES
from ithrl.runner.codex.protocol import (
    MAX_CLIENT_PROTOCOL_MESSAGES,
    MAX_PROTOCOL_LINE_BYTES,
    MAX_PROTOCOL_MESSAGES,
    CodexProtocolError,
    ProtocolOutcome,
    ProtocolStatus,
    ProtocolUsage,
    ProviderAcceptance,
    validate_protocol_json,
)
from ithrl.runner.codex.protocol_contracts import MAX_INFERENCE_REQUESTS


@dataclass(slots=True)
class _ProviderReceipts:
    reserved: int = 0
    started: int = 0
    completed: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    active: str | None = None
    unsafe: bool = False
    closed: bool = False

    def add(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "ordinal",
            "count",
            "status",
            "request_bytes",
            "response_bytes",
        }:
            raise ValueError("invalid provider receipt")
        if self.closed:
            raise ValueError("provider receipt follows closure")
        kind = value["kind"]
        ordinal, count, status, request_bytes, response_bytes = (
            _count(value[key])
            for key in ("ordinal", "count", "status", "request_bytes", "response_bytes")
        )
        if request_bytes < self.request_bytes or response_bytes < self.response_bytes:
            raise ValueError("provider byte counters decreased")
        if kind == "closed":
            if (
                ordinal != self.reserved
                or count != self.started
                or status != 0
                or request_bytes != self.request_bytes
            ):
                raise ValueError("invalid provider closure")
            self.closed = True
        elif kind == "reserved":
            if (
                self.active is not None
                or self.unsafe
                or ordinal != self.reserved + 1
                or ordinal > MAX_INFERENCE_REQUESTS
                or count != self.started
                or status != 0
                or response_bytes != self.response_bytes
            ):
                raise ValueError("invalid provider reservation")
            self.reserved = ordinal
            self.active = "reserved"
        elif kind == "started":
            if (
                self.active != "reserved"
                or ordinal != self.reserved
                or count != self.started + 1
                or status != 0
                or request_bytes != self.request_bytes
                or response_bytes != self.response_bytes
            ):
                raise ValueError("invalid provider dispatch")
            self.started = count
            self.active = "started"
        elif kind == "completed":
            if (
                self.active != "started"
                or ordinal != self.reserved
                or count != self.started
                or not 100 <= status <= 599
                or request_bytes != self.request_bytes
            ):
                raise ValueError("invalid provider completion")
            self.completed += 1
            self.active = None
        elif kind in {"failed", "rejected"}:
            if count != self.started or request_bytes != self.request_bytes:
                raise ValueError("invalid provider failure")
            if kind == "rejected" and (
                ordinal != 0
                or not 400 <= status <= 599
                or self.active is not None
                or response_bytes != self.response_bytes
            ):
                raise ValueError("invalid provider rejection")
            if kind == "failed" and (ordinal not in {0, self.reserved} or status != 0):
                raise ValueError("invalid provider failure")
            self.unsafe = True
            self.active = None
        else:
            raise ValueError("unknown provider receipt")
        self.request_bytes = request_bytes
        self.response_bytes = response_bytes


def _records(data: bytes, *, partial: bool = False) -> list[dict[str, object]]:
    if len(data) > MAX_NATIVE_OUTPUT_BYTES:
        raise CodexProtocolError("transcript-limit", "Codex transcript exceeds the byte limit")
    lines = data.splitlines()
    if partial and not data.endswith(b"\n"):
        lines = lines[:-1]
    max_records = (
        MAX_PROTOCOL_MESSAGES + MAX_CLIENT_PROTOCOL_MESSAGES + MAX_INFERENCE_REQUESTS * 4 + 1
    )
    if not lines or len(lines) > max_records:
        raise CodexProtocolError("transcript-format", "Codex transcript has an invalid shape")
    records: list[dict[str, object]] = []
    native_counts = {"client": 0, "server": 0}
    for line in lines:
        if len(line) > MAX_PROTOCOL_LINE_BYTES:
            raise CodexProtocolError("transcript-limit", "Codex transcript exceeds the line limit")
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise CodexProtocolError(
                "transcript-json", "Codex transcript contains invalid JSON"
            ) from None
        if (
            not isinstance(record, dict)
            or set(record) != {"kind", "value"}
            or not isinstance(record.get("kind"), str)
            or record.get("kind") not in {"client", "server", "provider", "summary"}
        ):
            raise CodexProtocolError("transcript-format", "Codex transcript has an invalid record")
        validate_protocol_json(record)
        kind = record["kind"]
        if kind in native_counts:
            native_counts[kind] += 1
        records.append(record)
    if (
        native_counts["client"] > MAX_CLIENT_PROTOCOL_MESSAGES
        or native_counts["server"] > MAX_PROTOCOL_MESSAGES
    ):
        raise CodexProtocolError("transcript-format", "Codex transcript has an invalid shape")
    return records


def parse_transcript(data: bytes) -> ProtocolOutcome:
    """Require a terminal summary corroborated by ordered physical receipts."""
    records = _records(data)
    summaries = [record["value"] for record in records if record["kind"] == "summary"]
    if len(summaries) != 1 or not isinstance(summaries[0], dict):
        raise CodexProtocolError("transcript-summary", "Codex transcript has no unique summary")
    if not data.endswith(b"\n") or records[-1]["kind"] != "summary":
        raise CodexProtocolError("transcript-summary", "Codex transcript summary is not terminal")
    try:
        outcome = _outcome_from_document(cast(dict[str, object], summaries[0]))
        receipts = _ProviderReceipts()
        for record in records:
            if record["kind"] == "provider":
                receipts.add(record["value"])
            elif receipts.closed and record["kind"] != "summary":
                raise ValueError("native activity follows transport closure")
        if not receipts.closed or (
            outcome.provider_calls_started,
            outcome.provider_calls_completed,
        ) != (receipts.started, receipts.completed):
            raise ValueError("provider counts lack physical receipts")
        if outcome.status == "completed" and (
            receipts.unsafe
            or receipts.active is not None
            or receipts.started == 0
            or receipts.completed != receipts.started
            or outcome.provider_acceptance != "accepted"
        ):
            raise ValueError("provider transport did not complete safely")
        return outcome
    except (KeyError, TypeError, ValueError):
        raise CodexProtocolError(
            "transcript-summary", "Codex transcript summary or provider receipts are invalid"
        ) from None


def physical_request_counts(data: bytes) -> tuple[int, int] | None:
    """Recover receipt-backed counts from a failed worker's complete prefix.

    Without a closure receipt these are lower bounds. A lost/truncated
    tail can hide further dispatches and must still be conservatively charged
    by the runtime. Missing or invalid receipts never establish zero requests.
    """
    try:
        receipts = _ProviderReceipts()
        for record in _records(data, partial=True):
            if record["kind"] == "provider":
                receipts.add(record["value"])
        if receipts.started:
            return receipts.started, receipts.completed
        if receipts.closed:
            return receipts.started, receipts.completed
    except (CodexProtocolError, KeyError, TypeError, ValueError):
        return None
    return None


def _outcome_from_document(document: dict[str, object]) -> ProtocolOutcome:
    expected = {
        "status",
        "error_code",
        "final_text",
        "model_returned",
        "service_tier",
        "usage",
        "provider_acceptance",
        "provider_calls_started",
        "provider_calls_completed",
        "actions_requested",
        "actions_denied",
    }
    if set(document) != expected | {"behavior_output_observed"}:
        raise ValueError("unexpected summary fields")
    output_observed = document["behavior_output_observed"]
    if type(output_observed) is not bool:
        raise ValueError("output visibility must be a boolean")
    status = document["status"]
    acceptance = document["provider_acceptance"]
    if status not in {"completed", "error"} or acceptance not in {
        "not-started",
        "accepted",
        "unknown",
    }:
        raise ValueError("invalid outcome enum")
    return ProtocolOutcome(
        status=cast(ProtocolStatus, status),
        error_code=_nullable_string(document["error_code"]),
        final_text=_nullable_string(document["final_text"]),
        model_returned=_nullable_string(document["model_returned"]),
        service_tier=_nullable_string(document["service_tier"]),
        usage=_usage(document["usage"]),
        provider_acceptance=cast(ProviderAcceptance, acceptance),
        provider_calls_started=_count(document["provider_calls_started"]),
        provider_calls_completed=_count(document["provider_calls_completed"]),
        actions_requested=_count(document["actions_requested"]),
        actions_denied=_count(document["actions_denied"]),
        behavior_output_observed=output_observed,
    )


def _usage(value: object) -> ProtocolUsage | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
    }:
        raise ValueError("invalid usage")
    return ProtocolUsage(
        input_tokens=_count(value["input_tokens"]),
        output_tokens=_count(value["output_tokens"]),
        cached_input_tokens=_count(value["cached_input_tokens"]),
    )


def _nullable_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("invalid optional string")
    return value


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    return value
