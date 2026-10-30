from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.execution import (
    CapabilityProfile,
)
from ithrl.extraction import extract_decision
from ithrl.runner.codex import CodexProviderConfiguration, CodexRuntime
from ithrl.runner.codex.policy import (
    DISABLED_FEATURES,
    configuration_deviation,
    runtime_deviation,
)
from ithrl.runner.codex.protocol_contracts import MAX_INFERENCE_REQUESTS

FIXTURES = Path(__file__).parents[1] / "fixtures"

_FAKE_APP_SERVER = r"""#!/usr/bin/python3
import json
import os
import stat
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

assert sys.argv[1:] == ["app-server"]
config_path = Path(os.environ["CODEX_HOME"]) / "config.toml"
assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
config = tomllib.loads(config_path.read_text())
provider = config["model_providers"][config["model_provider"]]
base_url = provider["base_url"]
assert base_url.startswith("http://127.0.0.1:")

def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

def send(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)

message = receive()
assert message["method"] == "initialize"
send({"id": message["id"], "result": {}})
assert receive()["method"] == "initialized"
message = receive()
assert message["method"] == "thread/start"
model = message["params"]["model"]
send({"id": message["id"], "result": {"thread": {"id": "thread-1", "model": model, "serviceTier": "default"}}})
message = receive()
if message["method"] == "thread/inject_items":
    send({"id": message["id"], "result": {}})
    message = receive()
assert message["method"] == "turn/start"
send({"id": message["id"], "result": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}})

for ordinal in range(1, 4):
    request = urllib.request.Request(
        base_url + "/responses",
        data=json.dumps({"ordinal": ordinal}).encode(),
        headers={"Authorization": "Bearer " + os.environ[provider["env_key"]]},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        assert response.status in (200, 429)
        response.read()

item = {"type": "agentMessage", "id": "message-1", "text": "Fixture complete.", "phase": "final_answer"}
send({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": item}})
send({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {"last": {"inputTokens": 101, "outputTokens": 7, "cachedInputTokens": 50}, "total": {"inputTokens": 101, "outputTokens": 7, "cachedInputTokens": 50}}}})
send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "items": [item], "status": "completed"}}})
for _ in sys.stdin:
    pass
"""

# Runner fixtures replace only the ithrl worker boundary. They exercise real
# Bubblewrap, private materialization, monitoring and transcript normalization;
# their synthetic receipts do not establish native/provider compatibility.
_FAKE_CODEX_WORKER = r"""#!/usr/bin/python3
import json
import os
import sys
import time

mode = "__FIXTURE_MODE__"
with open(sys.argv[1], encoding="utf-8") as source:
    request = json.load(source)
os.unlink(sys.argv[1])

def emit(kind, value):
    print(json.dumps({"kind": kind, "value": value}, separators=(",", ":")), flush=True)

def receipt(kind, *, count=1, status=0, ordinal=1, request_bytes=100):
    emit("provider", {
        "kind": kind, "ordinal": ordinal, "count": count, "status": status,
        "request_bytes": request_bytes, "response_bytes": 0,
    })

if mode == "malformed":
    print("not-json", flush=True)
    raise SystemExit(0)

receipt("reserved", count=0)
receipt("started")
if mode == "sleep":
    time.sleep(60)
if mode == "after-dispatch-malformed":
    print("not-json", flush=True)
    raise SystemExit(0)
if mode == "output":
    for _ in range(3000):
        emit("server", {"fixture_output": "x" * 1024})
    raise SystemExit(0)
if mode == "state":
    with open(os.path.join(os.environ["CODEX_HOME"], "oversized.bin"), "wb") as output:
        output.write(b"x" * (512 * 1024))

# Persist the owned input directly, without pretending to exercise native RPC.
emit("client", {"method": "fixture/request", "params": request})
overloaded = mode in {"overload", "overload-after-output"}
limited = mode == "call-limit"
action = mode in {"action", "action-long-metadata"}
receipt("completed", status=503 if overloaded else 200)
request_count = request["transport"]["max_requests"] if limited else 1
if limited:
    for ordinal in range(2, request_count + 1):
        receipt("reserved", count=ordinal - 1, ordinal=ordinal, request_bytes=100 * ordinal)
        receipt("started", count=ordinal, ordinal=ordinal, request_bytes=100 * ordinal)
        receipt("completed", count=ordinal, ordinal=ordinal, request_bytes=100 * ordinal, status=200)
    receipt("rejected", count=request_count, ordinal=0, request_bytes=100 * request_count, status=429)
receipt("closed", count=request_count, ordinal=request_count, request_bytes=100 * request_count)
final_text = "Fixture complete."
if mode == "long-final":
    final_text = "private-response-" * 5000
if mode == "context-sensitive" and "ITHRL_REQUIRED_CONTEXT" not in json.dumps(request["history"]):
    final_text = "Fixture does not match."
emit("summary", {
    "status": "error" if overloaded or limited or action else "completed",
    "error_code": "provider-call-limit" if limited else "provider-overloaded" if overloaded else "action-requested" if action else None,
    "final_text": None if overloaded or limited or action else final_text,
    "model_returned": "private-model-" * 30 if mode == "action-long-metadata" else request["model"],
    "service_tier": "default",
    "usage": None if overloaded else {"input_tokens": 101, "output_tokens": 7, "cached_input_tokens": 50},
    "provider_acceptance": "unknown" if overloaded or limited else "accepted",
    "provider_calls_started": request_count,
    "provider_calls_completed": request_count,
    "actions_requested": int(action),
    "actions_denied": int(action),
    "behavior_output_observed": not overloaded or mode == "overload-after-output",
})
"""


def _case(tmp_path: Path, *, context_sensitive: bool = False) -> tuple[CaseWorkspace, CaseEvidence]:
    source = FIXTURES / "codex-session" / "rollout.jsonl"
    if context_sensitive:
        records = [json.loads(line) for line in source.read_text().splitlines()]
        for record in records:
            payload = record["payload"]
            if payload.get("id") == "message-fixture-user-001":
                payload["content"][0]["text"] = "ITHRL_REQUIRED_CONTEXT"
        records.insert(
            3,
            {
                "ordinal": 13,
                "timestamp": "2026-01-01T00:00:02.500Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-fixture-irrelevant",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Irrelevant removable context."}],
                    "phase": None,
                },
            },
        )
        source = tmp_path / "source" / "rollout.jsonl"
        source.parent.mkdir()
        source.write_text("".join(json.dumps(record) + "\n" for record in records))
    session = parse_session(source)
    decision = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    return workspace, workspace.load()


def _configuration() -> CodexProviderConfiguration:
    features = "".join(f"{feature} = false\n" for feature in sorted(DISABLED_FEATURES))
    return CodexProviderConfiguration(
        source_provider_id="fixture-provider",
        provider_id="ithrl",
        endpoint="https://example.invalid/responses",
        authentication="custom-environment",
        environment_names=("ITHRL_PROVIDER_SECRET_0",),
        deviations=("provider-request-retries-disabled",),
        rendered_config=(
            b"""model_provider = "ithrl"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
cli_auth_credentials_store = "file"

[model_providers.ithrl]
name = "fixture"
base_url = "https://example.invalid"
wire_api = "responses"
requires_openai_auth = false
env_key = "ITHRL_PROVIDER_SECRET_0"
request_max_retries = 0
stream_max_retries = 0
supports_websockets = false
supports_standalone_web_search = false

[features]
"""
            + features.encode()
        ),
        environment=MappingProxyType({"ITHRL_PROVIDER_SECRET_0": "fixture-auth-value"}),
    )


def _capability(
    configuration: CodexProviderConfiguration,
    runtime: CodexRuntime,
    *,
    max_provider_calls_per_attempt: int = MAX_INFERENCE_REQUESTS,
) -> CapabilityProfile:
    return CapabilityProfile(
        runner_type="codex",
        runner_version="1",
        harness="codex",
        runtime_harness_version="fixture",
        protocol_version="fixture-schema",
        source_schema="codex-rollout-jsonl",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=("dependency-closed-removal", "append-text"),
        reconstruction_method="codex-app-server-inject-items-v1",
        supported_targets=("final-response",),
        provider_transport="required",
        provider_calls_per_attempt=max_provider_calls_per_attempt,
        tool_network_access="forbidden",
        action_visibility="not-applicable",
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
        reports_usage=True,
        provider="fixture-provider",
        endpoint="https://example.invalid/responses",
        deviations=(
            configuration_deviation(configuration.public_digest),
            runtime_deviation(runtime.identity),
        ),
    )
