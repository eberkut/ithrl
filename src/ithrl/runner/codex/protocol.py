"""Bounded Codex app-server JSON-RPC exchange and protected transcript parsing."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, BinaryIO, TextIO, cast

if TYPE_CHECKING or __package__:
    from ithrl.runner.codex.protocol_contracts import (
        ACTION_ITEM_TYPES,
        ACTION_REQUEST_METHODS,
        ACTION_RESPONSES,
        MAX_CLIENT_PROTOCOL_MESSAGES,
        MAX_PROTOCOL_LINE_BYTES,
        MAX_PROTOCOL_MESSAGES,
        MAX_WORKER_REQUEST_BYTES,
        PASSIVE_ITEM_TYPES,
        PASSIVE_NOTIFICATIONS,
        SANDBOX_ROOT,
        SANDBOX_WORKSPACE,
        SUPPORTED_NOTIFICATION_METHODS,
        THREAD_PASSIVE_NOTIFICATIONS,
        CodexProtocolError,
        ProtocolOutcome,
        ProtocolStatus,
        ProtocolUsage,
        ProviderAcceptance,
        validate_protocol_json,
    )
else:
    from protocol_contracts import (
        ACTION_ITEM_TYPES,
        ACTION_REQUEST_METHODS,
        ACTION_RESPONSES,
        MAX_CLIENT_PROTOCOL_MESSAGES,
        MAX_PROTOCOL_LINE_BYTES,
        MAX_PROTOCOL_MESSAGES,
        MAX_WORKER_REQUEST_BYTES,
        PASSIVE_ITEM_TYPES,
        PASSIVE_NOTIFICATIONS,
        SANDBOX_ROOT,
        SANDBOX_WORKSPACE,
        SUPPORTED_NOTIFICATION_METHODS,
        THREAD_PASSIVE_NOTIFICATIONS,
        CodexProtocolError,
        ProtocolOutcome,
        ProtocolStatus,
        ProtocolUsage,
        ProviderAcceptance,
        validate_protocol_json,
    )

__all__ = (
    "ACTION_ITEM_TYPES",
    "ACTION_REQUEST_METHODS",
    "MAX_CLIENT_PROTOCOL_MESSAGES",
    "MAX_PROTOCOL_LINE_BYTES",
    "MAX_PROTOCOL_MESSAGES",
    "MAX_WORKER_REQUEST_BYTES",
    "PASSIVE_ITEM_TYPES",
    "SANDBOX_ROOT",
    "SANDBOX_WORKSPACE",
    "SUPPORTED_NOTIFICATION_METHODS",
    "CodexProtocolError",
    "ProtocolOutcome",
    "ProtocolStatus",
    "ProtocolUsage",
    "ProviderAcceptance",
    "run_session",
    "validate_protocol_json",
)


@dataclass(slots=True)
class _SessionState:
    thread_id: str | None = None
    turn_id: str | None = None
    model_returned: str | None = None
    service_tier: str | None = None
    usage: ProtocolUsage | None = None
    provider_acceptance: ProviderAcceptance = "not-started"
    turn_completed: bool = False
    actions_requested: int = 0
    actions_denied: int = 0
    final_messages: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    behavior_output_observed: bool = False


class _Transport:
    def __init__(self, reader: BinaryIO, writer: BinaryIO, audit: TextIO) -> None:
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self.message_count = 0
        self.sent_count = 0
        self.next_id = 1

    def request(self, method: str, params: dict[str, object], state: _SessionState) -> object:
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise CodexProtocolError("server-error", "Codex app-server rejected a request")
                if "result" not in message:
                    raise CodexProtocolError(
                        "invalid-response", "Codex app-server returned an invalid response"
                    )
                return message["result"]
            _handle_server_message(self, message, state)

    def notification(self, method: str, params: dict[str, object]) -> None:
        self.send({"method": method, "params": params})

    def send(self, message: dict[str, object]) -> None:
        self.sent_count += 1
        if self.sent_count > MAX_CLIENT_PROTOCOL_MESSAGES:
            raise CodexProtocolError("message-limit", "Codex app-server exceeded the message limit")
        validate_protocol_json(message)
        encoded = _encode_line(message)
        self.writer.write(encoded)
        self.writer.flush()
        self._audit("client", message)

    def receive(self) -> dict[str, object]:
        self.message_count += 1
        if self.message_count > MAX_PROTOCOL_MESSAGES:
            raise CodexProtocolError("message-limit", "Codex app-server exceeded the message limit")
        line = self.reader.readline(MAX_PROTOCOL_LINE_BYTES + 1)
        if not line:
            raise CodexProtocolError("unexpected-eof", "Codex app-server closed unexpectedly")
        if len(line) > MAX_PROTOCOL_LINE_BYTES or not line.endswith(b"\n"):
            raise CodexProtocolError(
                "line-limit", "Codex app-server output exceeded the line limit"
            )
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CodexProtocolError(
                "invalid-json", "Codex app-server returned invalid JSON"
            ) from None
        if not isinstance(value, dict):
            raise CodexProtocolError(
                "invalid-message", "Codex app-server returned an invalid message"
            )
        validate_protocol_json(value)
        message = cast(dict[str, object], value)
        self._audit("server", message)
        return message

    def _audit(self, kind: str, value: object) -> None:
        audit_record(self.audit, kind, value)


_AUDIT_LOCK = threading.Lock()


def audit_record(audit: TextIO, kind: str, value: object) -> None:
    """Serialize one protected record across native and relay callback threads."""
    line = (
        json.dumps(
            {"kind": kind, "value": value},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    with _AUDIT_LOCK:
        audit.write(line)
        audit.flush()


def write_summary(audit: TextIO, outcome: ProtocolOutcome) -> None:
    """Write the worker's terminal outcome after transport and native cleanup."""
    audit_record(audit, "summary", _outcome_document(outcome))


def run_session(
    reader: BinaryIO,
    writer: BinaryIO,
    audit: TextIO,
    request: dict[str, object],
) -> ProtocolOutcome:
    """Return native facts and audit RPC; the worker attaches physical counts.

    The returned counts are zero because native events cannot establish HTTP
    requests. Only the worker may attach relay counters and publish a summary.
    """

    state = _SessionState()
    transport = _Transport(reader, writer, audit)
    try:
        _run_session(transport, request, state)
        outcome = _completed_outcome(state)
    except CodexProtocolError as error:
        outcome = _failed_outcome(state, error.code)
    return outcome


def _run_session(
    transport: _Transport,
    request: dict[str, object],
    state: _SessionState,
) -> None:
    _validate_worker_request(request)
    initialize = transport.request(
        "initialize",
        {
            "clientInfo": {"name": "ithrl", "title": "ithrl", "version": request["client_version"]},
            "capabilities": {"experimentalApi": False},
        },
        state,
    )
    _require_object(initialize, "initialize response")
    transport.notification("initialized", {})

    thread_params: dict[str, object] = {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": SANDBOX_WORKSPACE,
        "ephemeral": True,
        "model": request["model"],
        "modelProvider": request["provider"],
        "sandbox": "read-only",
    }
    for field_name in ("baseInstructions", "developerInstructions"):
        if request.get(field_name) is not None:
            thread_params[field_name] = request[field_name]
    thread_result = _require_object(
        transport.request("thread/start", thread_params, state),
        "thread start response",
    )
    thread = _require_object(thread_result.get("thread"), "thread start response")
    state.thread_id = _require_string(thread.get("id"), "thread start response")
    state.model_returned = _optional_string(thread_result.get("model")) or _optional_string(
        thread.get("model")
    )
    state.service_tier = _optional_string(thread_result.get("serviceTier")) or _optional_string(
        thread.get("serviceTier")
    )

    history = request["history"]
    if not isinstance(history, list):
        raise CodexProtocolError("invalid-worker-request", "Codex worker request is invalid")
    if history:
        transport.request(
            "thread/inject_items",
            {"threadId": state.thread_id, "items": history},
            state,
        )

    turn_params: dict[str, object] = {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": SANDBOX_WORKSPACE,
        "input": request["input"],
        "model": request["model"],
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "threadId": state.thread_id,
    }
    for field_name in ("effort", "personality"):
        if request.get(field_name) is not None:
            turn_params[field_name] = request[field_name]
    if request.get("toolOutput") is not None:
        turn_params["toolOutput"] = request["toolOutput"]
    state.provider_acceptance = "unknown"
    turn_result = _require_object(
        transport.request("turn/start", turn_params, state),
        "turn start response",
    )
    turn = _require_object(turn_result.get("turn"), "turn start response")
    turn_id = _require_string(turn.get("id"), "turn start response")
    if state.turn_id is not None and state.turn_id != turn_id:
        raise CodexProtocolError("identity-mismatch", "Codex turn identity changed")
    state.turn_id = turn_id
    _record_turn_output(turn, state)

    while not state.turn_completed:
        _handle_server_message(transport, transport.receive(), state)


def _handle_server_message(
    transport: _Transport,
    message: dict[str, object],
    state: _SessionState,
) -> None:
    method = message.get("method")
    if not isinstance(method, str):
        raise CodexProtocolError(
            "unexpected-response", "Codex app-server returned an unexpected response"
        )
    if "id" in message:
        _handle_server_request(transport, message, method, state)
        return
    params = _require_object(message.get("params"), "notification")
    _validate_notification(method, params, state)


def _handle_server_request(
    transport: _Transport,
    message: dict[str, object],
    method: str,
    state: _SessionState,
) -> None:
    request_id = message.get("id")
    if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
        raise CodexProtocolError("invalid-request", "Codex app-server sent an invalid request")
    response = ACTION_RESPONSES.get(method)
    if response is not None:
        state.actions_requested += 1
        transport.send({"id": request_id, "result": response})
        state.actions_denied += 1
        raise CodexProtocolError(
            "action-requested",
            "Codex requested an action during final-response execution",
        )
    transport.send(
        {
            "id": request_id,
            "error": {"code": -32601, "message": "Request disabled by ithrl."},
        }
    )
    raise CodexProtocolError(
        "unsupported-server-request",
        "Codex requested an unsupported client operation",
    )


def _validate_notification(method: str, params: dict[str, object], state: _SessionState) -> None:
    if method == "remoteControl/status/changed":
        if params.get("status") != "disabled":
            raise CodexProtocolError(
                "remote-control-active", "Codex remote control is not disabled"
            )
        return
    if method == "thread/started":
        _validate_thread_started(params, state)
        return
    if method == "error":
        _require_identity(params, state, require_turn=True)
        error = _require_object(params.get("error"), "error notification")
        _require_string(error.get("message"), "error notification")
        if not isinstance(params.get("willRetry"), bool):
            raise CodexProtocolError("invalid-message", "Codex error notification is invalid")
        if error.get("codexErrorInfo") == "serverOverloaded" and params["willRetry"] is False:
            raise CodexProtocolError("provider-overloaded", "Codex provider reported overload")
        raise CodexProtocolError("native-error", "Codex app-server reported a native error")
    if method == "warning":
        _require_identity(params, state, require_turn=False)
        _require_string(params.get("message"), "warning notification")
        return
    if method == "account/updated":
        if "authMode" not in params:
            raise CodexProtocolError("invalid-message", "Codex account notification is invalid")
        for field_name in ("authMode", "planType"):
            if params.get(field_name) is not None:
                _require_string(params[field_name], "account notification")
        return
    if method == "account/rateLimits/updated":
        if "rateLimits" not in params:
            raise CodexProtocolError("invalid-message", "Codex rate-limit notification is invalid")
        return
    if method == "model/rerouted":
        _record_model_reroute(params, state)
        return
    if method in THREAD_PASSIVE_NOTIFICATIONS:
        _require_identity(params, state, require_turn=method == "model/verification")
        if THREAD_PASSIVE_NOTIFICATIONS[method] not in params:
            raise CodexProtocolError("invalid-message", "Codex passive notification is invalid")
        return
    if method in PASSIVE_NOTIFICATIONS:
        _validate_passive_notification(method, params, state)
        state.behavior_output_observed = True
        return
    if method == "thread/tokenUsage/updated":
        _record_token_usage(params, state)
        return
    if method in {"item/started", "item/completed"}:
        _record_item(params, state, completed=method == "item/completed")
        return
    if method == "turn/started":
        _record_turn_started(params, state)
        return
    if method == "turn/completed":
        _record_turn_completed(params, state)
        return
    raise CodexProtocolError(
        "unsupported-notification",
        "Codex app-server sent an unsupported notification",
    )


def _record_token_usage(params: dict[str, object], state: _SessionState) -> None:
    _require_identity(params, state, require_turn=True)
    token_usage = _require_object(params.get("tokenUsage"), "token usage notification")
    last = _usage_counts(token_usage.get("last"))
    total = _usage_counts(token_usage.get("total"))
    previous = state.usage
    if any(
        current < increment
        for current, increment in zip(
            (total.input_tokens, total.output_tokens, total.cached_input_tokens),
            (last.input_tokens, last.output_tokens, last.cached_input_tokens),
            strict=True,
        )
    ) or (
        previous is not None
        and any(
            current < prior
            for current, prior in zip(
                (total.input_tokens, total.output_tokens, total.cached_input_tokens),
                (previous.input_tokens, previous.output_tokens, previous.cached_input_tokens),
                strict=True,
            )
        )
    ):
        raise CodexProtocolError("invalid-usage", "Codex cumulative token usage decreased")
    state.usage = total
    if total.output_tokens:
        state.behavior_output_observed = True


def _usage_counts(value: object) -> ProtocolUsage:
    counts = _require_object(value, "token usage notification")
    usage = ProtocolUsage(
        input_tokens=_require_count(counts.get("inputTokens"), "input token count"),
        output_tokens=_require_count(counts.get("outputTokens"), "output token count"),
        cached_input_tokens=_require_count(
            counts.get("cachedInputTokens"), "cached input token count"
        ),
    )

    if usage.cached_input_tokens > usage.input_tokens:
        raise CodexProtocolError("invalid-usage", "Codex cached token usage exceeds input usage")
    return usage


def _record_item(
    params: dict[str, object],
    state: _SessionState,
    *,
    completed: bool,
) -> None:
    _require_identity(params, state, require_turn=True)
    item = _require_object(params.get("item"), "item notification")
    item_type = _require_string(item.get("type"), "item notification")
    _require_passive_item_type(item_type)
    if item_type != "userMessage":
        state.behavior_output_observed = True
    if completed and item_type == "agentMessage":
        _record_agent_message(item, state)


def _record_turn_started(params: dict[str, object], state: _SessionState) -> None:
    _require_identity(params, state, require_turn=False)
    turn = _require_object(params.get("turn"), "turn started notification")
    turn_id = _require_string(turn.get("id"), "turn started notification")
    if state.turn_id is not None and state.turn_id != turn_id:
        raise CodexProtocolError("identity-mismatch", "Codex turn identity changed")
    state.turn_id = turn_id
    _record_turn_output(turn, state)


def _record_turn_output(turn: dict[str, object], state: _SessionState) -> None:
    if "items" not in turn:
        return
    items = turn["items"]
    if not isinstance(items, list):
        raise CodexProtocolError("invalid-message", "Codex turn items are invalid")
    if any(not isinstance(item, dict) or item.get("type") != "userMessage" for item in items):
        state.behavior_output_observed = True


def _record_turn_completed(params: dict[str, object], state: _SessionState) -> None:
    _require_identity(params, state, require_turn=False)
    turn = _require_object(params.get("turn"), "turn completed notification")
    turn_id = _require_string(turn.get("id"), "turn completed notification")
    if state.turn_id != turn_id:
        raise CodexProtocolError("identity-mismatch", "Codex turn identity changed")
    if turn.get("status") != "completed":
        raise CodexProtocolError("turn-failed", "Codex turn did not complete")
    items = turn.get("items")
    if not isinstance(items, list):
        raise CodexProtocolError("invalid-completion", "Codex completion has no item list")
    for item in items:
        if not isinstance(item, dict):
            raise CodexProtocolError(
                "invalid-completion", "Codex completion contains an invalid item"
            )
        item_type = _require_string(item.get("type"), "completed turn item")
        _require_passive_item_type(item_type)
        if item_type == "agentMessage":
            _record_agent_message(item, state)
    state.turn_completed = True
    state.provider_acceptance = "accepted"
    state.behavior_output_observed = True


def _record_agent_message(item: dict[str, object], state: _SessionState) -> None:
    item_id = _require_string(item.get("id"), "agent message")
    state.final_messages[item_id] = (
        _optional_string(item.get("phase")),
        _require_string(item.get("text"), "agent message"),
    )


def _validate_passive_notification(
    method: str,
    params: dict[str, object],
    state: _SessionState,
) -> None:
    _require_identity(params, state, require_turn=True)
    _require_string(params.get("itemId"), "item progress notification")
    for field_name in PASSIVE_NOTIFICATIONS[method]:
        value = params.get(field_name)
        if field_name == "delta":
            if not isinstance(value, str):
                raise CodexProtocolError(
                    "invalid-message", "Codex item progress notification is invalid"
                )
        else:
            _require_count(value, "item progress index")


def _validate_thread_started(params: dict[str, object], state: _SessionState) -> None:
    thread = _require_object(params.get("thread"), "thread started notification")
    thread_id = _require_string(thread.get("id"), "thread started notification")
    if state.thread_id is None or thread_id != state.thread_id:
        raise CodexProtocolError("identity-mismatch", "Codex thread identity changed")


def _record_model_reroute(params: dict[str, object], state: _SessionState) -> None:
    _require_identity(params, state, require_turn=True)
    from_model = _require_string(params.get("fromModel"), "model reroute notification")
    to_model = _require_string(params.get("toModel"), "model reroute notification")
    if state.model_returned is not None and state.model_returned != from_model:
        raise CodexProtocolError("identity-mismatch", "Codex model identity changed")
    state.model_returned = to_model


def _require_passive_item_type(item_type: str) -> None:
    if item_type not in PASSIVE_ITEM_TYPES:
        raise CodexProtocolError(
            "unintercepted-action",
            "Codex exposed an item outside the verified protocol boundary",
        )


def _completed_outcome(state: _SessionState) -> ProtocolOutcome:
    if not state.turn_completed:
        raise CodexProtocolError("missing-completion", "Codex turn has no terminal completion")
    final = [text for phase, text in state.final_messages.values() if phase == "final_answer"]
    if not final:
        final = [text for phase, text in state.final_messages.values() if phase is None]
    if not final:
        raise CodexProtocolError("missing-final-response", "Codex produced no final response")
    if len(final) > 1:
        raise CodexProtocolError("ambiguous-completion", "Codex final response is ambiguous")
    if state.usage is None:
        raise CodexProtocolError("missing-usage", "Codex did not report token usage")
    return ProtocolOutcome(
        status="completed",
        error_code=None,
        final_text=final[0],
        model_returned=state.model_returned,
        service_tier=state.service_tier,
        usage=state.usage,
        provider_acceptance=state.provider_acceptance,
        provider_calls_started=0,
        provider_calls_completed=0,
        actions_requested=state.actions_requested,
        actions_denied=state.actions_denied,
        behavior_output_observed=state.behavior_output_observed,
    )


def _failed_outcome(state: _SessionState, code: str) -> ProtocolOutcome:
    return ProtocolOutcome(
        status="error",
        error_code=code,
        final_text=None,
        model_returned=state.model_returned,
        service_tier=state.service_tier,
        usage=state.usage,
        provider_acceptance=state.provider_acceptance,
        provider_calls_started=0,
        provider_calls_completed=0,
        actions_requested=state.actions_requested,
        actions_denied=state.actions_denied,
        behavior_output_observed=state.behavior_output_observed,
    )


def _outcome_document(outcome: ProtocolOutcome) -> dict[str, object]:
    document = asdict(outcome)
    if outcome.usage is not None:
        document["usage"] = asdict(outcome.usage)
    return cast(dict[str, object], document)


def _validate_worker_request(request: dict[str, object]) -> None:
    expected = {
        "schema_version",
        "client_version",
        "model",
        "provider",
        "effort",
        "personality",
        "baseInstructions",
        "developerInstructions",
        "history",
        "input",
        "toolOutput",
    }
    if set(request) != expected or request.get("schema_version") != 1:
        raise CodexProtocolError("invalid-worker-request", "Codex worker request is invalid")
    _require_string(request.get("model"), "worker request")
    _require_string(request.get("client_version"), "worker request")
    _require_string(request.get("provider"), "worker request")
    for key in ("effort", "personality", "baseInstructions", "developerInstructions"):
        if request[key] is not None:
            _require_string(request[key], "worker request")
    if not isinstance(request["history"], list) or not isinstance(request["input"], list):
        raise CodexProtocolError("invalid-worker-request", "Codex worker request is invalid")
    if bool(request["input"]) == (request["toolOutput"] is not None):
        raise CodexProtocolError("invalid-worker-request", "Codex worker request is invalid")
    validate_protocol_json(request)


def _require_identity(
    params: dict[str, object],
    state: _SessionState,
    *,
    require_turn: bool,
) -> None:
    if state.thread_id is None or params.get("threadId") != state.thread_id:
        raise CodexProtocolError("identity-mismatch", "Codex thread identity changed")
    if require_turn and (state.turn_id is None or params.get("turnId") != state.turn_id):
        raise CodexProtocolError("identity-mismatch", "Codex turn identity changed")


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodexProtocolError("invalid-message", f"Codex {label} is invalid")
    return cast(dict[str, object], value)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CodexProtocolError("invalid-message", f"Codex {label} is invalid")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _require_count(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise CodexProtocolError("invalid-message", f"Codex {label} is invalid")
    return value


def _encode_line(value: object) -> bytes:
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
        raise CodexProtocolError("line-limit", "Codex request exceeds the line limit")
    return encoded
