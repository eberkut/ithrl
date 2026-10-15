"""Validate protected output from one Codex app-server worker."""

from __future__ import annotations

import json
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


def parse_transcript(data: bytes) -> ProtocolOutcome:
    """Read the single terminal summary from protected worker output."""

    if len(data) > MAX_NATIVE_OUTPUT_BYTES:
        raise CodexProtocolError("transcript-limit", "Codex transcript exceeds the byte limit")
    summaries: list[object] = []
    lines = data.splitlines()
    max_records = MAX_PROTOCOL_MESSAGES + MAX_CLIENT_PROTOCOL_MESSAGES + 1
    if not lines or len(lines) > max_records:
        raise CodexProtocolError("transcript-format", "Codex transcript has an invalid shape")
    for line in lines:
        if len(line) > MAX_PROTOCOL_LINE_BYTES:
            raise CodexProtocolError("transcript-limit", "Codex transcript exceeds the line limit")
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CodexProtocolError(
                "transcript-json", "Codex transcript contains invalid JSON"
            ) from None
        if not isinstance(record, dict) or record.get("kind") not in {
            "client",
            "server",
            "summary",
        }:
            raise CodexProtocolError("transcript-format", "Codex transcript has an invalid record")
        validate_protocol_json(record)
        if record["kind"] == "summary":
            summaries.append(record.get("value"))
    if len(summaries) != 1 or not isinstance(summaries[0], dict):
        raise CodexProtocolError("transcript-summary", "Codex transcript has no unique summary")
    try:
        return _outcome_from_document(cast(dict[str, object], summaries[0]))
    except (KeyError, TypeError, ValueError):
        raise CodexProtocolError(
            "transcript-summary", "Codex transcript summary is invalid"
        ) from None


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
    if set(document) not in (expected, expected | {"behavior_output_observed"}):
        raise ValueError("unexpected summary fields")
    output_observed = document.get("behavior_output_observed", True)
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
