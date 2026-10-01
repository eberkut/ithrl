from __future__ import annotations

import io
import json
from collections.abc import Iterable
from typing import Any

import pytest

import ithrl.runner.codex.protocol as protocol
import ithrl.runner.codex.transcript as transcript
from ithrl.runner.codex.protocol import (
    ACTION_ITEM_TYPES,
    PASSIVE_ITEM_TYPES,
    CodexProtocolError,
    ProtocolOutcome,
    ProtocolUsage,
    run_session,
)
from ithrl.runner.codex.transcript import parse_transcript

THREAD_ID = "thread-fixture-001"
TURN_ID = "turn-fixture-001"
MODEL = "fixture-model"
PROVIDER = "fixture-provider"
FINAL_TEXT = "Final response from the sanitized fixture."
type Message = dict[str, Any]


def _request(
    *,
    history: list[Message] | None = None,
    input_items: list[Message] | None = None,
    tool_output: Message | None = None,
) -> Message:
    return {
        "schema_version": 1,
        "model": MODEL,
        "provider": PROVIDER,
        "effort": "low",
        "personality": "none",
        "baseInstructions": "Fixture instructions.",
        "developerInstructions": None,
        "history": history or [],
        "input": input_items if input_items is not None else [{"type": "text", "text": "Prompt."}],
        "toolOutput": tool_output,
    }


def _prefix(*, history: bool = False) -> list[Message]:
    messages: list[Message] = [
        {"id": 1, "result": {"capabilities": {}}},
        {
            "id": 2,
            "result": {
                "model": MODEL,
                "serviceTier": "standard",
                "thread": {
                    "id": THREAD_ID,
                },
            },
        },
        {"method": "thread/started", "params": {"thread": {"id": THREAD_ID}}},
    ]
    if history:
        messages.append({"id": 3, "result": {}})
        turn_request_id = 4
    else:
        turn_request_id = 3
    messages.append({"id": turn_request_id, "result": {"turn": {"id": TURN_ID}}})
    return messages


def _usage() -> Message:
    counts = {
        "inputTokens": 100,
        "outputTokens": 25,
        "cachedInputTokens": 10,
    }
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "tokenUsage": {
                "last": counts.copy(),
                "total": counts.copy(),
            },
        },
    }


def _final_item(text: str = FINAL_TEXT) -> Message:
    return {
        "method": "item/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {
                "id": "message-fixture-001",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": text,
            },
        },
    }


def _turn_completed(*, items: list[Message] | None = None, status: str = "completed") -> Message:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "turn": {"id": TURN_ID, "status": status, "items": items or []},
        },
    }


def _server_bytes(messages: Iterable[object]) -> bytes:
    return b"".join(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for message in messages
    )


def _run(
    messages: Iterable[object],
    *,
    request: Message | None = None,
) -> tuple[ProtocolOutcome, list[Message], list[Message]]:
    writer = io.BytesIO()
    audit = io.StringIO()
    outcome = run_session(
        io.BytesIO(_server_bytes(messages)),
        writer,
        audit,
        request or _request(),
    )
    client_messages = [json.loads(line) for line in writer.getvalue().splitlines()]
    audit_records = [json.loads(line) for line in audit.getvalue().splitlines()]
    return outcome, client_messages, audit_records


def _successful_exchange(*, text: str = FINAL_TEXT, history: bool = False) -> list[Message]:
    return [*_prefix(history=history), _usage(), _final_item(text), _turn_completed()]


def test_completed_exchange_captures_final_response_usage_and_identity() -> None:
    outcome, client, audit = _run(_successful_exchange())

    assert outcome == ProtocolOutcome(
        status="completed",
        error_code=None,
        final_text=FINAL_TEXT,
        model_returned=MODEL,
        service_tier="standard",
        usage=ProtocolUsage(input_tokens=100, output_tokens=25, cached_input_tokens=10),
        provider_acceptance="accepted",
        provider_calls_started=1,
        provider_calls_completed=1,
        actions_requested=0,
        actions_denied=0,
    )
    assert [message["method"] for message in client if "method" in message] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    thread_start = next(message for message in client if message.get("method") == "thread/start")
    assert thread_start["params"]["ephemeral"] is True
    assert thread_start["params"]["model"] == MODEL
    turn_start = next(message for message in client if message.get("method") == "turn/start")
    assert turn_start["params"]["threadId"] == THREAD_ID
    assert turn_start["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn_start["params"]["effort"] == "low"
    assert turn_start["params"]["personality"] == "none"
    assert audit[-1]["kind"] == "summary"


def test_history_is_injected_before_turn_and_tool_output_is_a_distinct_trigger() -> None:
    history = [{"type": "message", "id": "retained-001", "text": "Retained context."}]
    tool_output = {
        "name": "exec",
        "namespace": None,
        "output": [{"type": "input_text", "text": "Synthetic result."}],
    }
    request = _request(history=history, input_items=[], tool_output=tool_output)
    outcome, client, _ = _run(_successful_exchange(history=True), request=request)

    assert outcome.status == "completed"
    methods = [message.get("method") for message in client if "method" in message]
    assert methods == [
        "initialize",
        "initialized",
        "thread/start",
        "thread/inject_items",
        "turn/start",
    ]
    injected = next(message for message in client if message.get("method") == "thread/inject_items")
    assert injected["params"] == {"threadId": THREAD_ID, "items": history}
    turn_start = next(message for message in client if message.get("method") == "turn/start")
    assert turn_start["params"]["input"] == []
    assert turn_start["params"]["toolOutput"] == tool_output


@pytest.mark.parametrize(
    ("method", "expected_result"),
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        (
            "item/tool/call",
            {
                "contentItems": [{"type": "inputText", "text": "Tool execution is disabled."}],
                "success": False,
            },
        ),
        ("mcpServer/elicitation/request", {"action": "decline", "content": None, "_meta": None}),
    ],
)
def test_action_request_is_declined_and_final_response_loop_stops(
    method: str,
    expected_result: Message,
) -> None:
    action = {"id": "server-action-001", "method": method, "params": {"private": "fixture"}}
    trailing = _successful_exchange()
    outcome, client, audit = _run([*_prefix(), action, *trailing[3:]])

    assert outcome.status == "error"
    assert outcome.error_code == "action-requested"
    assert outcome.actions_requested == 1
    assert outcome.actions_denied == 1
    assert outcome.provider_calls_completed == 0
    assert outcome.final_text is None
    response = next(message for message in client if message.get("id") == "server-action-001")
    assert response == {"id": "server-action-001", "result": expected_result}
    assert not any(message.get("method") == "turn/completed" for message in client)
    assert audit[-1]["kind"] == "summary"


@pytest.mark.parametrize("method", ["item/permissions/requestApproval", "unknown/request"])
def test_unsafe_and_unknown_server_requests_are_rejected(method: str) -> None:
    request_message: Message = {"id": "server-request-001", "method": method, "params": {}}
    outcome, client, _ = _run([*_prefix(), request_message])

    assert outcome.status == "error"
    assert outcome.error_code == "unsupported-server-request"
    assert outcome.actions_requested == 0
    response = next(message for message in client if message.get("id") == "server-request-001")
    assert response["error"] == {"code": -32601, "message": "Request disabled by ithrl."}


@pytest.mark.parametrize(
    "item_type",
    sorted(ACTION_ITEM_TYPES),
)
def test_action_item_notification_fails_closed_without_answering_or_continuing(
    item_type: str,
) -> None:
    action_item = {
        "method": "item/started",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {"id": "action-001", "type": item_type},
        },
    }
    outcome, client, _ = _run([*_prefix(), action_item, *_successful_exchange()[3:]])

    assert outcome.status == "error"
    assert outcome.error_code == "unintercepted-action"
    assert not any(message.get("id") == "action-001" for message in client)


@pytest.mark.parametrize(
    "item_type",
    sorted(PASSIVE_ITEM_TYPES - {"agentMessage", "plan", "reasoning"}),
)
def test_passive_item_notification_preserves_the_exchange(item_type: str) -> None:
    passive_item = {
        "method": "item/started",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {"id": "passive-001", "type": item_type},
        },
    }
    exchange = _successful_exchange()
    turn_started = len(_prefix())

    outcome, _, _ = _run([*exchange[:turn_started], passive_item, *exchange[turn_started:]])

    assert outcome.status == "completed"


def test_unknown_item_notification_fails_closed() -> None:
    item = {
        "method": "item/started",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {"id": "unknown-001", "type": "futureAction"},
        },
    }
    outcome, _, _ = _run([*_prefix(), item])

    assert outcome.status == "error"
    assert outcome.error_code == "unintercepted-action"


def test_unknown_notification_fails_closed() -> None:
    notification = {
        "method": "future/notification",
        "params": {"threadId": THREAD_ID, "turnId": TURN_ID},
    }
    outcome, _, _ = _run([*_prefix(), notification])

    assert outcome.status == "error"
    assert outcome.error_code == "unsupported-notification"


def test_thread_warning_is_informational_and_preserved_only_in_the_audit() -> None:
    message = "Sanitized fixture warning."
    warning = {"method": "warning", "params": {"threadId": THREAD_ID, "message": message}}
    outcome, _, audit = _run([*_prefix(), warning, _usage(), _final_item(), _turn_completed()])

    assert outcome.status == "completed"
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert {"kind": "server", "value": warning} in audit
    assert message not in repr(outcome)
    assert parse_transcript(_server_bytes(audit)) == outcome


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"threadId": "other-thread", "message": "Fixture warning."}, "identity-mismatch"),
        ({"message": "Fixture warning."}, "identity-mismatch"),
        ({"threadId": THREAD_ID}, "invalid-message"),
        ({"threadId": THREAD_ID, "message": ""}, "invalid-message"),
        ({"threadId": THREAD_ID, "message": True}, "invalid-message"),
    ],
)
def test_warning_requires_the_selected_thread_and_nonempty_text(params: Message, code: str) -> None:
    outcome, _, _ = _run([*_prefix(), {"method": "warning", "params": params}])

    assert outcome.error_code == code
    assert outcome.provider_calls_completed == 0


@pytest.mark.parametrize(
    "params",
    [{"authMode": None}, {"authMode": "chatgpt"}, {"authMode": "chatgpt", "planType": "plus"}],
)
def test_account_updates_are_informational(params: Message) -> None:
    exchange = _successful_exchange()
    outcome, _, audit = _run(
        [exchange[0], {"method": "account/updated", "params": params}, *exchange[1:]]
    )

    assert outcome.status == "completed"
    assert outcome.provider_acceptance == "accepted"
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert parse_transcript(_server_bytes(audit)) == outcome


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"authMode": ""},
        {"authMode": False},
        {"authMode": None, "planType": ""},
        {"authMode": "chatgpt", "planType": 1},
    ],
)
def test_account_updates_require_well_formed_optional_identity_fields(params: Message) -> None:
    outcome, _, _ = _run([_prefix()[0], {"method": "account/updated", "params": params}])

    assert outcome.error_code == "invalid-message"
    assert outcome.provider_acceptance == "not-started"
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 0


@pytest.mark.parametrize("position", [1, len(_prefix())])
def test_disabled_remote_control_notification_preserves_the_exchange(position: int) -> None:
    exchange = _successful_exchange()
    notification = {"method": "remoteControl/status/changed", "params": {"status": "disabled"}}
    outcome, _, audit = _run([*exchange[:position], notification, *exchange[position:]])

    assert outcome.status == "completed"
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert outcome.actions_requested == outcome.actions_denied == 0
    assert parse_transcript(_server_bytes(audit)) == outcome


@pytest.mark.parametrize("status", [None, True, "enabled", "connected", "future-status"])
@pytest.mark.parametrize("position", [1, len(_prefix())])
def test_non_disabled_remote_control_stops_without_continuing(
    status: object, position: int
) -> None:
    exchange = _successful_exchange()
    notification = {"method": "remoteControl/status/changed", "params": {"status": status}}
    outcome, client, _ = _run([*exchange[:position], notification, *exchange[position:]])

    assert outcome.status == "error"
    assert outcome.error_code == "remote-control-active"
    assert outcome.provider_calls_completed == 0
    assert outcome.provider_calls_started == int(position != 1)
    assert client[-1]["method"] == ("thread/start" if position == 1 else "turn/start")


@pytest.mark.parametrize(
    ("method", "fields"),
    [
        ("item/agentMessage/delta", {"delta": "part"}),
        ("item/plan/delta", {"delta": "part"}),
        ("item/reasoning/summaryPartAdded", {"summaryIndex": 0}),
        (
            "item/reasoning/summaryTextDelta",
            {"delta": "part", "summaryIndex": 0},
        ),
        ("item/reasoning/textDelta", {"contentIndex": 0, "delta": "part"}),
    ],
)
def test_verified_passive_notifications_preserve_the_exchange(
    method: str,
    fields: Message,
) -> None:
    notification = {
        "method": method,
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "itemId": "item-001",
            **fields,
        },
    }
    exchange = _successful_exchange()
    turn_started = len(_prefix())
    outcome, _, _ = _run([*exchange[:turn_started], notification, *exchange[turn_started:]])

    assert outcome.status == "completed"


def test_turn_scoped_notification_before_turn_identity_is_rejected() -> None:
    foreign = _final_item("FOREIGN")
    outcome, _, _ = _run([*_prefix()[:-1], foreign])

    assert outcome.status == "error"
    assert outcome.error_code == "identity-mismatch"


def test_cumulative_usage_refuses_more_than_one_provider_call() -> None:
    usage = _usage()
    usage["params"]["tokenUsage"]["total"]["inputTokens"] = 101
    exchange = _successful_exchange()
    usage_index = len(_prefix())
    outcome, _, _ = _run([*exchange[:usage_index], usage, *exchange[usage_index + 1 :]])

    assert outcome.status == "error"
    assert outcome.error_code == "multiple-provider-calls"


def test_thread_lifecycle_and_global_notifications_preserve_the_exchange() -> None:
    exchange = _successful_exchange()
    turn_started = len(_prefix())
    notifications: list[Message] = [
        {
            "method": "thread/status/changed",
            "params": {"threadId": THREAD_ID, "status": {"type": "active"}},
        },
        {
            "method": "model/verification",
            "params": {"threadId": THREAD_ID, "turnId": TURN_ID, "verifications": []},
        },
        {
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {}},
        },
    ]
    outcome, _, _ = _run([*exchange[:turn_started], *notifications, *exchange[turn_started:]])

    assert outcome.status == "completed"


def test_model_reroute_updates_returned_model() -> None:
    exchange = _successful_exchange()
    turn_started = len(_prefix())
    reroute = {
        "method": "model/rerouted",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "fromModel": MODEL,
            "toModel": "fixture-model-rerouted",
            "reason": "capacity",
        },
    }
    outcome, _, _ = _run([*exchange[:turn_started], reroute, *exchange[turn_started:]])

    assert outcome.status == "completed"
    assert outcome.model_returned == "fixture-model-rerouted"


def test_mismatched_response_id_is_not_treated_as_a_request_result() -> None:
    outcome, _, _ = _run([{"id": 99, "result": {}}])

    assert outcome.status == "error"
    assert outcome.error_code == "unexpected-response"
    assert outcome.provider_calls_started == 0


@pytest.mark.parametrize(
    "bad_params",
    [
        {"threadId": "other-thread", "turnId": TURN_ID},
        {"threadId": THREAD_ID, "turnId": "other-turn"},
    ],
)
def test_mismatched_thread_or_turn_identity_is_rejected(bad_params: dict[str, str]) -> None:
    message = _usage()
    message["params"] = bad_params | {
        "tokenUsage": {
            "last": {"inputTokens": 1, "outputTokens": 1, "cachedInputTokens": 0},
            "total": {"inputTokens": 1, "outputTokens": 1, "cachedInputTokens": 0},
        }
    }
    outcome, _, _ = _run([*_prefix(), message])

    assert outcome.status == "error"
    assert outcome.error_code == "identity-mismatch"


def test_malformed_and_non_object_json_are_content_safe() -> None:
    for raw in (b"not-json\n", b"[]\n"):
        writer = io.BytesIO()
        audit = io.StringIO()
        outcome = run_session(io.BytesIO(raw), writer, audit, _request())
        expected = "invalid-json" if raw.startswith(b"not") else "invalid-message"
        assert outcome.error_code == expected
        assert "Prompt." not in repr(outcome)
        assert "Prompt." not in str(outcome)


def test_non_finite_json_number_is_rejected() -> None:
    outcome = run_session(
        io.BytesIO(b'{"id":1,"result":{"value":NaN}}\n'),
        io.BytesIO(),
        io.StringIO(),
        _request(),
    )

    assert outcome.error_code == "invalid-json"


def test_oversized_protocol_line_is_rejected() -> None:
    oversized = b"{" + b"x" * protocol.MAX_PROTOCOL_LINE_BYTES + b"\n"
    outcome = run_session(io.BytesIO(oversized), io.BytesIO(), io.StringIO(), _request())

    assert outcome.status == "error"
    assert outcome.error_code == "line-limit"


def test_message_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_PROTOCOL_MESSAGES", 0)

    outcome, _, _ = _run([])

    assert outcome.status == "error"
    assert outcome.error_code == "message-limit"


def test_missing_final_response_has_actionable_error() -> None:
    outcome, _, _ = _run([*_prefix(), _usage(), _turn_completed()])

    assert outcome.status == "error"
    assert outcome.error_code == "missing-final-response"
    assert outcome.final_text is None


def test_multiple_final_responses_are_ambiguous() -> None:
    first = _final_item("first")
    second = _final_item("second")
    second["params"]["item"]["id"] = "message-fixture-002"
    outcome, _, _ = _run([*_prefix(), _usage(), first, second, _turn_completed()])

    assert outcome.status == "error"
    assert outcome.error_code == "ambiguous-completion"
    assert outcome.final_text is None


def test_missing_usage_is_not_reported_as_a_success() -> None:
    outcome, _, _ = _run([*_prefix(), _final_item(), _turn_completed()])

    assert outcome.status == "error"
    assert outcome.error_code == "missing-usage"
    assert outcome.provider_calls_completed == 1


def test_failed_turn_is_not_reported_as_a_success() -> None:
    outcome, _, _ = _run([*_prefix(), _usage(), _turn_completed(status="failed")])

    assert outcome.status == "error"
    assert outcome.error_code == "turn-failed"
    assert outcome.provider_calls_completed == 0


def test_transcript_round_trip_duplicate_summary_and_tamper_rejection() -> None:
    outcome, _, audit = _run(_successful_exchange())
    transcript = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in audit
    )

    assert parse_transcript(transcript.encode("utf-8")) == outcome

    duplicate = transcript + json.dumps(audit[-1], separators=(",", ":")) + "\n"
    with pytest.raises(CodexProtocolError, match="unique summary"):
        parse_transcript(duplicate.encode("utf-8"))

    tampered_records = list(audit)
    tampered_records[-1] = {
        **tampered_records[-1],
        "value": {**tampered_records[-1]["value"], "extra": "tampered"},
    }
    with pytest.raises(CodexProtocolError, match="summary"):
        parse_transcript(_server_bytes(tampered_records))


def test_transcript_rejects_malformed_and_oversized_records() -> None:
    with pytest.raises(CodexProtocolError, match="invalid JSON"):
        parse_transcript(b'{"kind":"summary"\n')

    oversized = b"{" + b"x" * protocol.MAX_PROTOCOL_LINE_BYTES + b"}\n"
    with pytest.raises(CodexProtocolError, match="line limit"):
        parse_transcript(oversized)


def test_transcript_limit_includes_client_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, _, audit = _run(_successful_exchange())
    monkeypatch.setattr(transcript, "MAX_PROTOCOL_MESSAGES", 2)
    monkeypatch.setattr(transcript, "MAX_CLIENT_PROTOCOL_MESSAGES", 3)
    records = [
        *({"kind": "server", "value": {"id": 1}} for _ in range(2)),
        *({"kind": "client", "value": {"id": 1}} for _ in range(3)),
        audit[-1],
    ]

    assert parse_transcript(_server_bytes(records)) == outcome
    with pytest.raises(CodexProtocolError, match="invalid shape"):
        parse_transcript(_server_bytes([{"kind": "server", "value": {"id": 1}}, *records]))


def test_captured_final_text_is_hidden_from_outcome_repr_but_round_trips() -> None:
    outcome, _, audit = _run(_successful_exchange(text="private fixture response"))

    assert "private fixture response" not in repr(outcome)
    assert parse_transcript(_server_bytes(audit)).final_text == "private fixture response"


def test_protocol_value_objects_reject_inconsistent_outcomes() -> None:
    with pytest.raises(ValueError, match="completed Codex outcome"):
        ProtocolOutcome(
            status="completed",
            error_code="bad",
            final_text=None,
            model_returned=None,
            service_tier=None,
            usage=None,
            provider_acceptance="unknown",
            provider_calls_started=0,
            provider_calls_completed=0,
            actions_requested=0,
            actions_denied=0,
        )
