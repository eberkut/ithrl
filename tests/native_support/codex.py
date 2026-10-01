from __future__ import annotations

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

FIXTURES = Path(__file__).parents[1] / "fixtures"
_FAKE_APP_SERVER = r"""#!/usr/bin/python3
import json
import sys
import time

mode = "__FIXTURE_MODE__"

if mode == "sleep":
    time.sleep(60)
if mode == "malformed":
    sys.stdout.write("not-json\n")
    sys.stdout.flush()
    raise SystemExit(0)

def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()

message = receive()
send({"id": message["id"], "result": {"serverInfo": {"name": "fixture", "version": "1"}}})
receive()
message = receive()
model = message["params"]["model"]
returned_model = "private-model-" * 30 if mode == "action-long-metadata" else model
if message["params"]["modelProvider"] != "ithrl":
    raise SystemExit(4)
send({"id": message["id"], "result": {"thread": {"id": "thread-1", "model": returned_model, "serviceTier": "default"}}})
send({"method": "thread/started", "params": {"thread": {"id": "thread-1"}}})
message = receive()
if message["method"] == "thread/inject_items":
    send({"id": message["id"], "result": {}})
    message = receive()
send({"id": message["id"], "result": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}})
send({"method": "item/started", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "userMessage", "id": "user-message-1", "content": []}}})
if mode == "after-dispatch-malformed":
    sys.stdout.write("not-json\n")
    sys.stdout.flush()
    raise SystemExit(0)
if mode == "output":
    for _ in range(3_000):
        send({"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "message-1", "delta": "x" * 1024}})
if mode in {"action", "action-long-metadata"}:
    send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-1", "turnId": "turn-1", "command": "private-command"}})
    denial = receive()
    if denial.get("result") != {"decision": "decline"}:
        raise SystemExit(3)
    raise SystemExit(0)
send({"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "message-1", "delta": "Fixture complete."}})
send({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {"last": {"inputTokens": 101, "outputTokens": 7, "cachedInputTokens": 50, "reasoningOutputTokens": 2, "totalTokens": 108}, "total": {"inputTokens": 101, "outputTokens": 7, "cachedInputTokens": 50, "reasoningOutputTokens": 2, "totalTokens": 108}, "modelContextWindow": 128000}}})
final_text = "private-response-" * 5_000 if mode == "long-final" else "Fixture complete."
item = {"type": "agentMessage", "id": "message-1", "text": final_text, "phase": "final_answer"}
send({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": item}})
send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "items": [item], "status": "completed"}}})
for _ in sys.stdin:
    pass
"""


def _case(tmp_path: Path) -> tuple[CaseWorkspace, CaseEvidence]:
    session = parse_session(FIXTURES / "codex-session" / "rollout.jsonl")
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
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="codex-app-server-inject-items-v1",
        supported_targets=("final-response",),
        provider_transport="required",
        provider_calls_per_attempt=1,
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
