from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.execution import (
    CapabilityProfile,
)
from ithrl.extraction import extract_decision
from ithrl.runner.copilot import (
    CopilotConfiguration,
)
from ithrl.runner.copilot.policy import (
    ENDPOINT,
    PROTOCOL_VERSION,
    PROVIDER,
    RECONSTRUCTION_METHOD,
    CopilotTarget,
    target_policy,
)

# Deliberately replaces only the SDK worker boundary. Runner tests still execute
# the real Bubblewrap, private materialization, process monitor and parser. This
# fixture does not establish SDK/native inference compatibility or provider support.
_FAKE_COPILOT = "#!/usr/bin/python3\nraise SystemExit(0)\n"

_FAKE_COPILOT_WORKER = r"""#!/usr/bin/python3
import hashlib
import json
import os
import sys
import time

MODE = "__MODE__"


def emit(event_type, data, **extra):
    event = {
        "id": f"event-{event_type}",
        "parentId": None,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "type": event_type,
        "data": data,
    }
    event.update(extra)
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


with open(sys.argv[1], encoding="utf-8") as source:
    settings = json.load(source)
os.unlink(sys.argv[1])
with open(settings["nativeSession"], "rb") as source:
    native = source.read()
records = [json.loads(line) for line in native.splitlines() if line.strip()]
assert records[0]["type"] == "session.start"
assert any(record["type"] == "user.message" for record in records)

def receipt(kind, **fields):
    print(json.dumps({"type": "ithrl.copilot." + kind, "schema_version": 1, **fields}), flush=True)

if MODE == "startup-sleep":
    time.sleep(60)

receipt(
    "input", native_session_sha256=hashlib.sha256(native).hexdigest(),
    native_session_bytes=len(native), new_user_messages=0,
    system_message_sha256=None if settings["systemMessage"] is None else
        hashlib.sha256(settings["systemMessage"].encode()).hexdigest(),
)
request_digest = hashlib.sha256(b"credential-free fixture dispatch").hexdigest()
receipt("forward", request_sha256=request_digest, provider_calls_started=1)
if MODE == "sleep":
    time.sleep(60)
if MODE == "malformed":
    print("private-native-output", flush=True)
    raise SystemExit(0)
if MODE == "output":
    for _ in range(3000):
        print("x" * 1024, flush=True)
    raise SystemExit(0)
if MODE == "state":
    with open(os.path.join(os.environ["COPILOT_HOME"], "oversized.bin"), "wb") as output:
        output.write(b"x" * (512 * 1024))

turn_id = "turn-1"
model = settings["model"]
emit("session.skills_loaded", {"count": 0})
emit("session.mcp_servers_loaded", {"count": 0})
emit("session.tools_updated", {"tools": []})
emit("session.info", {"configuration": {"tools": "filtered"}})
emit("assistant.turn_start", {"turnId": turn_id, "interactionId": "interaction-1"})
emit("model.call_start", {"turnId": turn_id, "model": model})
emit(
    "model.call_finished",
    {"turnId": turn_id, "model": model, "outcome": "success", "dispatchDurationMs": 1},
)

shell_action = settings["target"] == "shell-action"
required_context = True
if MODE == "context-sensitive":
    required_context = "ITHRL_REQUIRED_CONTEXT" in native.decode()
if shell_action and MODE not in {"tool-free", "empty-final", "nonzero-final", "reasoning"}:
    request = {
        "toolCallId": "call-1",
        "name": "bash",
        "type": "function",
        "arguments": {"command": "pwd" if required_context else "echo no-match", "description": "Show directory"},
    }
    emit(
        "assistant.message",
        {
            "messageId": "message-1",
            "apiCallId": "api-call-1",
            "model": model,
            "content": "",
            "toolRequests": [request],
            "turnId": turn_id,
        },
    )
    emit(
        "tool.execution_start",
        {
            "toolCallId": "call-1",
            "toolName": "bash",
            "arguments": request["arguments"],
            "turnId": turn_id,
            "model": model,
        },
    )
    emit(
        "tool.execution_complete",
        {
            "toolCallId": "call-1",
            "toolName": "bash",
            "turnId": turn_id,
            "model": model,
            "success": False,
            "error": {"code": "denied", "message": "denied by fixture"},
        },
    )
    receipt("complete", provider_calls_started=1, requests_rejected=0,
            websockets_rejected=0, permissions_denied=1,
            request_sha256=request_digest, success=True)
    raise SystemExit(0)

emit(
    "assistant.message",
    {
        "messageId": "message-1",
        "apiCallId": "api-call-1",
        "model": model,
        "content": "" if MODE == "empty-final" else "Fixture complete." if required_context and MODE != "reasoning" else "Fixture does not match.",
        "toolRequests": [],
        "turnId": turn_id,
    },
)
if MODE == "reasoning":
    emit("assistant.reasoning", {"content": "Private fixture timeline.", "reasoningId": "reasoning-1", "rte": True})
emit("assistant.turn_end", {"turnId": turn_id})
emit("assistant.idle", {}, ephemeral=True)
receipt("complete", provider_calls_started=1, requests_rejected=0,
        websockets_rejected=0, permissions_denied=0,
        request_sha256=request_digest, success=True)
if MODE == "nonzero-final":
    raise SystemExit(5)
if MODE == "continued":
    emit("session.info", {"private": "continued-private-output"})
"""


def _record(ordinal: int, record_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"10000000-0000-4000-8000-{ordinal:012d}",
        "parentId": None,
        "timestamp": f"2026-01-01T00:00:{ordinal:02d}.000Z",
        "type": record_type,
        "data": data,
    }


def _source_records(
    target: CopilotTarget, *, context_sensitive: bool = False
) -> list[dict[str, Any]]:
    turn_id = "30000000-0000-4000-8000-000000000001"
    tool_requests: list[dict[str, Any]] = []
    if target == "shell-action":
        tool_requests.append(
            {
                "toolCallId": "80000000-0000-4000-8000-000000000001",
                "name": "bash",
                "arguments": {"command": "pwd", "description": "Show directory"},
            }
        )
    records = [
        _record(
            0,
            "session.start",
            {
                "sessionId": "20000000-0000-4000-8000-000000000001",
                "version": 1,
                "producer": "copilot-agent",
                "copilotVersion": "1.0.88",
                "startTime": "2026-01-01T00:00:00.000Z",
                "selectedModel": "fixture-model",
            },
        ),
        _record(1, "user.message", {"content": "Private direct trigger.", "turnId": turn_id}),
        _record(2, "assistant.turn_start", {"turnId": turn_id}),
        _record(
            3,
            "assistant.message",
            {
                "messageId": "70000000-0000-4000-8000-000000000001",
                "model": "fixture-model",
                "content": "" if tool_requests else "Fixture complete.",
                "toolRequests": tool_requests,
                "turnId": turn_id,
                **({} if tool_requests else {"phase": "final_answer", "parentToolCallId": None}),
            },
        ),
        _record(4, "assistant.turn_end", {"turnId": turn_id}),
    ]
    if context_sensitive:
        records[1:1] = [
            _record(5, "user.message", {"content": "ITHRL_REQUIRED_CONTEXT", "turnId": "prior-1"}),
            _record(
                6, "user.message", {"content": "Irrelevant removable context.", "turnId": "prior-2"}
            ),
        ]
    return records


def _case(
    tmp_path: Path,
    target: CopilotTarget,
    *,
    context_sensitive: bool = False,
) -> tuple[Path, CaseWorkspace, CaseEvidence]:
    source = tmp_path / "source" / "events.jsonl"
    source.parent.mkdir()
    source.write_text(
        "".join(
            json.dumps(record, separators=(",", ":")) + "\n"
            for record in _source_records(target, context_sensitive=context_sensitive)
        ),
        encoding="utf-8",
    )
    session = parse_session(source)
    decision = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    return source, workspace, workspace.load()


def _configuration() -> CopilotConfiguration:
    return CopilotConfiguration(
        authentication="gh-token",
        environment_names=("GH_TOKEN",),
        environment=MappingProxyType({"GH_TOKEN": "private-fixture-token"}),
    )


def _capability(
    target: CopilotTarget,
    *,
    provider: str = PROVIDER,
    endpoint: str = ENDPOINT,
) -> CapabilityProfile:
    return CapabilityProfile(
        runner_type="copilot",
        runner_version="1",
        harness="copilot",
        runtime_harness_version="fixture",
        protocol_version=PROTOCOL_VERSION,
        source_schema="copilot-events-jsonl",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=target_policy(target).context_operations,
        reconstruction_method=RECONSTRUCTION_METHOD,
        supported_targets=(target,),
        provider_transport="required",
        provider_calls_per_attempt=1,
        tool_network_access="forbidden",
        action_visibility="visible" if target == "shell-action" else "not-applicable",
        action_denial="enforced",
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        protected_state_enforcement="preventive",
        disposable_state_effect="writes",
        disposable_state_limit_enforcement="enforced",
        process_containment="process-group",
        supports_timeout=True,
        supports_cancellation=True,
        cancellation_grace_seconds=3,
        reports_usage=False,
        input_token_limit_enforcement="unavailable",
        provider=provider,
        endpoint=endpoint,
        deviations=("fixture human-readable detail",),
    )
