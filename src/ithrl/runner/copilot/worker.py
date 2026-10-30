"""Disposable SDK host for one native Copilot turn without a new user message."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import sys
import threading
from pathlib import Path
from typing import Any, cast, override
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_NATIVE_SESSION_BYTES = 64 * 1024 * 1024
_SANDBOX_ROOT = "/tmp/ithrl-state"  # noqa: S108 - private process namespace
_MAX_MODEL_BYTES = 1024
_MAX_SHELL_ACTIONS = 32  # BehaviorObservation limit; worker runs without project imports.
_MAX_TOOL_ID_LENGTH = 512
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")

# Explicitly interpreted event classes. Session/model telemetry and sandbox decisions
# remain private observations; unknown essential event namespaces terminate the
# attempt before any further inference forwarding.
_ALLOWED_EVENTS = frozenset(
    {
        "session.resume",
        "sandbox.decision",
        "session.tools_updated",
        "session.managed_settings_resolved",
        "session.skills_loaded",
        "session.indexed_search",
        "session.info",
        "session.warning",
        "session.mcp_servers_loaded",
        "system.message",
        "pending_messages.modified",
        "session.usage_info",
        "assistant.turn_start",
        "model.call_start",
        "model.call_finished",
        "model.call_final_result",
        "assistant.usage",
        "assistant.reasoning",
        "assistant.message",
        "assistant.turn_end",
        "assistant.idle",
        "session.idle",
        "session.usage_checkpoint",
        "session.background_tasks_changed",
        "tool.execution_start",
        "tool.execution_complete",
        "session.error",
    }
)


class WorkerError(RuntimeError):
    """A content-safe Copilot worker refusal."""


def _emit(document: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_private_file(path: Path, *, maximum: int, unlink: bool = False) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 0 < metadata.st_size <= maximum
        ):
            raise WorkerError("Copilot worker input is unsafe")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            finished = os.fstat(stream.fileno())

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

        if len(raw) != metadata.st_size or any(
            identity(value) != identity(metadata) for value in (opened, finished, path.lstat())
        ):
            raise WorkerError("Copilot worker input changed while reading")
        if unlink:
            path.unlink()
        return raw
    except OSError as error:
        raise WorkerError("Copilot worker input is unavailable") from error


def _parse_request(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_constant
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise WorkerError("Copilot worker request contains invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "commandFlags",
        "model",
        "nativeSession",
        "sessionId",
        "systemMessage",
        "target",
        "workspace",
    }:
        raise WorkerError("Copilot worker request has an unsupported shape")
    flags = value["commandFlags"]
    if (
        not isinstance(flags, list)
        or len(flags) > 64
        or any(
            not isinstance(flag, str)
            or not flag.startswith("--")
            or "\0" in flag
            or len(flag) > 4096
            for flag in flags
        )
        or not _bounded_text(value["model"], _MAX_MODEL_BYTES)
        or not isinstance(value["sessionId"], str)
        or not _SESSION_ID.fullmatch(value["sessionId"])
        or value["target"] not in {"final-response", "shell-action"}
        or not all(_absolute_text_path(value[name]) for name in ("nativeSession", "workspace"))
        or (
            value["systemMessage"] is not None
            and not _bounded_text(value["systemMessage"], MAX_REQUEST_BYTES)
        )
    ):
        raise WorkerError("Copilot worker request has invalid fields")
    return cast(dict[str, Any], value)


def _managed_request(
    request: Any,
    context: Any,
    *,
    session_id: str,
) -> bytes:
    """Refuse inherited custom routing before the physical forwarding seam."""
    url = urlsplit(str(request.url))
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in {None, 443}
        or not url.hostname
        or not (
            url.hostname == "api.githubcopilot.com" or url.hostname.endswith(".githubcopilot.com")
        )
        or request.method != "POST"
        or context.session_id != session_id
        or context.parent_agent_id is not None
        or context.transport != "http"
    ):
        raise WorkerError("Copilot inference routing is outside the managed profile")
    # Provider payload encoding is SDK-owned. Verify model and system guidance
    # through native session events, rather than assuming a Chat Completions body.
    content = request.content
    if len(content) > MAX_NATIVE_SESSION_BYTES:
        raise WorkerError("Copilot inference request exceeds its byte limit")
    return content


def _is_model_catalog_request(request: Any, context: Any) -> bool:
    """Recognize the SDK's session-independent managed model discovery request."""

    url = urlsplit(str(request.url))
    return (
        request.method == "GET"
        and not request.content
        and context.session_id is None
        and context.parent_agent_id is None
        and context.transport == "http"
        and url.scheme == "https"
        and url.username is None
        and url.password is None
        and url.port in {None, 443}
        and url.hostname is not None
        and (url.hostname == "api.githubcopilot.com" or url.hostname.endswith(".githubcopilot.com"))
        and url.path == "/models"
        and not url.query
        and not url.fragment
    )


class ForwardGate:
    """Atomically reserve at most one physical HTTP inference forward."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.forwarded = 0
        self.rejected = 0
        self.websockets_rejected = 0

    def reserve(self) -> bool:
        with self._lock:
            if self.forwarded:
                self.rejected += 1
                return False
            self.forwarded = 1
            return True


def _resume_options(
    request: dict[str, Any], *, on_event: Any, on_permission_request: Any
) -> dict[str, Any]:
    # Empty mode already disables hooks, host Git, skills, instruction discovery,
    # session telemetry and session storage. Empty extension collections are omitted
    # by the SDK; only effective overrides and callbacks belong here.
    options = {
        "model": request["model"],
        "working_directory": request["workspace"],
        "config_directory": f"{_SANDBOX_ROOT}/copilot-home",
        "available_tools": ["builtin:bash"] if request["target"] == "shell-action" else [],
        "enable_config_discovery": False,
        "enable_managed_settings": False,
        "request_extensions": False,
        "request_canvas_renderer": False,
        "plugin_directories": [],
        "skill_directories": [],
        "instruction_directories": [],
        "hook_directories": [],
        "agent_directories": [],
        "additional_directories": [],
        "allow_transcript_recovery": False,
        "continue_pending_work": False,
        "autopilot": {"continuation_enabled": False, "max_continues": 0},
        "streaming": False,
        "capi": {"enable_web_socket_responses": False},
        "on_permission_request": on_permission_request,
        "on_event": on_event,
    }
    if request["systemMessage"] is not None:
        options["system_message"] = {"mode": "replace", "content": request["systemMessage"]}
    return options


async def _run_session(executable: Path, request: dict[str, Any]) -> int:
    # -S disables site initialization and all ambient .pth/import hooks. Only the
    # explicitly mounted installed SDK requirements enter the import search path.
    sys.path.insert(0, f"{_SANDBOX_ROOT}/runner/sdk-packages")
    from copilot import (
        CopilotClient,
        CopilotRequestHandler,
        ManagedSettings,
        ManagedSettingsPermissions,
        RuntimeConnection,
    )
    from copilot.generated.rpc import SendMessagesRequest

    logging.disable(logging.CRITICAL)
    native = _read_private_file(Path(request["nativeSession"]), maximum=MAX_NATIVE_SESSION_BYTES)
    gate = ForwardGate()
    catalog_gate = ForwardGate()
    done = asyncio.Event()
    policy_proof = asyncio.Event()
    context_proof = asyncio.Event()
    failed = False
    denied_target = False
    permission_denials = 0
    effective_policy_verified = False
    system_message_verified = request["systemMessage"] is None
    model_verified = False
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_code: str | None = None
    stage = "startup"
    dispatch_digest: str | None = None
    receipt: dict[str, object] = {
        "type": "ithrl.copilot.input",
        "schema_version": 1,
        "native_session_sha256": _digest(native),
        "native_session_bytes": len(native),
        "system_message_sha256": None
        if request["systemMessage"] is None
        else _digest(request["systemMessage"].encode()),
        "new_user_messages": 0,
    }
    _emit(receipt)

    requested_tools: dict[str, str] = {}
    started_tools: set[str] = set()
    completed_tools: set[str] = set()
    settings = request

    class OneRequest(CopilotRequestHandler):
        @override
        async def send_request(self, request: Any, ctx: Any) -> Any:
            nonlocal failed, dispatch_digest, failure_stage, failure_reason, failure_code
            if denied_target:
                # The selected shell request is already visible and denied. A
                # native follow-up is outside this observation and never forwarded.
                gate.reserve()
                raise WorkerError("Copilot selected action has already been observed")
            if settings["target"] == "shell-action" and gate.forwarded:
                # A follow-up inference is rejected independently of native event
                # delivery; ordered action events still determine the observation.
                gate.reserve()
                raise WorkerError("Copilot attempted more than one physical inference request")
            try:
                await policy_proof.wait()
                if failed or not effective_policy_verified:
                    raise WorkerError("Copilot effective managed permission policy is unverified")
                if _is_model_catalog_request(request, ctx):
                    if not catalog_gate.reserve():
                        raise WorkerError("Copilot repeated its model catalogue request")
                    return await super().send_request(request, ctx)
                await context_proof.wait()
                if failed or not system_message_verified or not model_verified:
                    raise WorkerError("Copilot native inference context is unverified")
                content = _managed_request(
                    request,
                    ctx,
                    session_id=settings["sessionId"],
                )
                if not gate.reserve():
                    raise WorkerError("Copilot attempted more than one physical inference request")
                dispatch_digest = _digest(content)
                _emit(
                    {
                        "type": "ithrl.copilot.forward",
                        "schema_version": 1,
                        "request_sha256": dispatch_digest,
                        "provider_calls_started": 1,
                    }
                )
                return await super().send_request(request, ctx)
            except Exception as error:
                failed = True
                failure_stage = "inference-request"
                failure_reason = _failure_reason(error)
                failure_code = (
                    "managed-policy-unverified"
                    if not effective_policy_verified
                    else "native-context-unverified"
                    if not system_message_verified or not model_verified
                    else "inference-request-refused"
                )
                done.set()
                raise WorkerError("Copilot inference request failed or was refused") from None

        @override
        async def open_websocket(self, ctx: Any) -> Any:
            nonlocal failed
            gate.websockets_rejected += 1
            failed = True
            done.set()
            raise WorkerError("Copilot WebSocket inference is forbidden")

    def deny_permission(*_arguments: Any, **_keywords: Any) -> dict[str, str]:
        nonlocal permission_denials
        permission_denials += 1
        return {"kind": "reject"}

    def on_event(event: Any) -> None:
        nonlocal failed, effective_policy_verified, denied_target
        nonlocal system_message_verified, model_verified
        nonlocal failure_stage, failure_reason, failure_code
        document = event.to_dict()
        if denied_target and document.get("type") not in {
            "assistant.message",
            "tool.execution_start",
            "tool.execution_complete",
        }:
            return
        kind = document.get("type")
        if not isinstance(kind, str) or kind.startswith("ithrl."):
            failed = True
            done.set()
            policy_proof.set()
            context_proof.set()
            return
        _emit(document)
        if kind not in _ALLOWED_EVENTS and not kind.startswith(("model.", "session.")):
            failed = True
            done.set()
            policy_proof.set()
            context_proof.set()
            return
        data = document.get("data", {})
        if kind == "system.message" and request["systemMessage"] is not None:
            system_message_verified = (
                isinstance(data, dict) and data.get("content") == request["systemMessage"]
            )
            if not system_message_verified:
                failed = True
                failure_stage = "native-context"
                failure_reason = "Copilot native inference context is unverified"
                failure_code = "native-context-unverified"
                done.set()
        if kind == "model.call_start":
            model_verified = isinstance(data, dict) and data.get("model") == request["model"]
            if not model_verified:
                failed = True
                failure_stage = "native-context"
                failure_reason = "Copilot native inference context is unverified"
                failure_code = "native-context-unverified"
                done.set()
        if system_message_verified and model_verified:
            context_proof.set()
        if kind == "session.managed_settings_resolved":
            context = data.get("permissionsContext", {}) if isinstance(data, dict) else {}
            permissions = context.get("permissions", {}) if isinstance(context, dict) else {}
            effective_policy_verified = (
                isinstance(data, dict)
                and data.get("clientManaged") is True
                and data.get("bypassPermissionsDisabled") is True
                and data.get("failClosed") is False
                and isinstance(permissions, dict)
                and permissions.get("disableBypassPermissionsMode") == "disable"
                and permissions.get("disableAssistedPermissionsMode") is True
                and isinstance(permissions.get("deny"), list)
                and "Shell(*)" in permissions["deny"]
            )
            if not effective_policy_verified:
                failed = True
                failure_stage = "managed-policy"
                failure_reason = "Copilot effective managed permission policy is unverified"
                failure_code = "managed-policy-unverified"
                done.set()
            policy_proof.set()
        if kind in {"user.message", "session.handoff"} or document.get("agentId") is not None:
            failed = True
            done.set()
        if kind == "assistant.message":
            tools: object = data.get("toolRequests", []) if isinstance(data, dict) else None
            if tools is None and isinstance(data, dict):
                tools = list[object]()
            if not isinstance(tools, list) or (tools and request["target"] != "shell-action"):
                failed = True
            elif tools:
                for tool in tools:
                    identity = tool.get("toolCallId") if isinstance(tool, dict) else None
                    name = tool.get("name") if isinstance(tool, dict) else None
                    if (
                        not isinstance(tool, dict)
                        or denied_target
                        or not isinstance(identity, str)
                        or not 0 < len(identity) <= _MAX_TOOL_ID_LENGTH
                        or identity in requested_tools
                        or len(requested_tools) >= _MAX_SHELL_ACTIONS
                        or not isinstance(name, str)
                        or name not in {"bash", "shell"}
                        or tool.get("type", "function") != "function"
                    ):
                        failed = True
                        break
                    requested_tools[identity] = name
            if failed:
                done.set()
        if kind in {"tool.execution_start", "tool.execution_complete"}:
            identity = data.get("toolCallId") if isinstance(data, dict) else None
            if (
                request["target"] != "shell-action"
                or not isinstance(identity, str)
                or identity not in requested_tools
                or data.get("toolName", requested_tools.get(identity))
                != requested_tools.get(identity)
            ):
                failed = True
            elif kind == "tool.execution_start":
                if identity in started_tools:
                    failed = True
                else:
                    started_tools.add(identity)
            elif (
                identity not in started_tools
                or identity in completed_tools
                or data.get("success") is not False
                or not isinstance(data.get("error"), dict)
                or data["error"].get("code") != "denied"
            ):
                failed = True
            else:
                completed_tools.add(identity)
                if gate.forwarded == 1 and completed_tools == requested_tools.keys() and not failed:
                    denied_target = True
                    done.set()
            if failed:
                done.set()
        if kind in {"assistant.idle", "session.error"}:
            if kind == "session.error" or (
                request["target"] == "shell-action"
                and requested_tools
                and completed_tools != requested_tools.keys()
            ):
                failed = True
            done.set()
            policy_proof.set()
            context_proof.set()
        if failed:
            policy_proof.set()
            context_proof.set()

    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(
            path=os.fspath(executable), args=request["commandFlags"]
        ),
        mode="empty",
        base_directory=f"{_SANDBOX_ROOT}/copilot-home",
        working_directory=request["workspace"],
        request_handler=OneRequest(),
        log_level="error",
    )
    try:
        await client.start()
        stage = "resume-session"
        options = _resume_options(request, on_event=on_event, on_permission_request=deny_permission)
        # Native history can carry prior grants. Host managed policy composes
        # restrictively with them and applies before any tool permission callback.
        options["managed_settings"] = ManagedSettings(
            permissions=ManagedSettingsPermissions(
                disable_bypass_permissions_mode="disable",
                disable_assisted_permissions_mode=True,
                deny=["Shell(*)"],
                allow=[],
                limit_to=[],
            )
        )
        session = await client.resume_session(request["sessionId"], **options)
        stage = "send-messages"
        sent = await session.rpc.send_messages(SendMessagesRequest(messages=[]))
        if sent.message_ids:
            raise WorkerError("Copilot continuation unexpectedly added a message")
        await done.wait()
    except Exception as error:
        if not denied_target:
            failed = True
            if failure_stage is None:
                failure_stage = stage
                failure_reason = _failure_reason(error)
    finally:
        try:
            await asyncio.wait_for(client.stop(), timeout=5)
        except Exception:
            failed = True
        _emit(
            {
                "type": "ithrl.copilot.complete",
                "schema_version": 1,
                "provider_calls_started": gate.forwarded,
                "catalog_requests_started": catalog_gate.forwarded,
                "requests_rejected": gate.rejected,
                "websockets_rejected": gate.websockets_rejected,
                "permissions_denied": permission_denials,
                "request_sha256": dispatch_digest,
                "success": not failed,
                "failure_stage": failure_stage,
                "failure_code": failure_code,
                "failure_reason": failure_reason,
            }
        )
    return 70 if failed else 0


def _failure_reason(error: Exception) -> str:
    """Project native failures onto fixed, content-safe diagnostic messages."""

    if isinstance(error, WorkerError):
        return str(error)  # Only runner-owned fixed messages use this exception.
    message = str(error).lower()
    if "certificate" in message:
        return "Copilot TLS certificate validation failed"
    if any(word in message for word in ("auth", "credential", "logged in")):
        return "Copilot authentication was unavailable to the SDK session"
    if "model" in message:
        return "Copilot could not initialize the requested model"
    if any(word in message for word in ("network", "connect", "fetch", "certificate")):
        return "Copilot could not establish the required connection"
    return "Copilot rejected the SDK operation"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        return 64
    try:
        request = _parse_request(
            _read_private_file(
                Path(_SANDBOX_ROOT) / "request.json", maximum=MAX_REQUEST_BYTES, unlink=True
            )
        )
        executable = Path(_SANDBOX_ROOT) / "runner" / "copilot"
        if not executable.is_file() or executable.stat().st_mode & 0o111 == 0:
            raise WorkerError("Copilot executable is unavailable")
        return asyncio.run(_run_session(executable, request))
    except (WorkerError, OSError, ValueError):
        sys.stderr.write("Copilot native SDK execution failed\n")
        return 70


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\0" not in value
        and len(value.encode("utf-8")) <= maximum
    )


def _absolute_text_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\0" not in value
        and Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError


if __name__ == "__main__":
    raise SystemExit(main())
