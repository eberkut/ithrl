from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import MappingProxyType

import pytest

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.execution import (
    CapabilityProfile,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.extraction import extract_decision
from ithrl.runner import (
    PreparedAttempt,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    prepare_candidate,
)
from ithrl.runner.codex import CodexProviderConfiguration, CodexRunner, CodexRuntime
from ithrl.runner.codex.policy import (
    DISABLED_FEATURES,
    configuration_deviation,
    runtime_deviation,
)
from ithrl.runner.evidence import AttemptEvidence
from ithrl.runner.runtime import ProtectedPath

FIXTURES = Path(__file__).parents[3] / "fixtures"
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
cli_auth_credentials_store = "ephemeral"

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


def _runner(
    tmp_path: Path,
    workspace: CaseWorkspace,
    *,
    mode: str = "complete",
    configuration: CodexProviderConfiguration | None = None,
    capability: CapabilityProfile | None = None,
    capability_configuration: CodexProviderConfiguration | None = None,
    state_parent: Path | None = None,
    output_root: Path | None = None,
) -> CodexRunner:
    fake = tmp_path / f"codex-{mode}"
    fake.write_text(_FAKE_APP_SERVER.replace("__FIXTURE_MODE__", mode), encoding="utf-8")
    fake.chmod(0o700)
    original_workspace = tmp_path / "original-workspace"
    original_workspace.mkdir(exist_ok=True)
    marker = original_workspace / "marker.txt"
    if not marker.exists():
        marker.write_text("unchanged\n", encoding="utf-8")
    source = FIXTURES / "codex-session" / "rollout.jsonl"
    configuration = configuration or _configuration()
    runtime = CodexRuntime(
        codex=fake,
        bubblewrap=Path("/usr/bin/bwrap"),
        python=Path("/usr/bin/python3"),
    )
    return CodexRunner(
        capability_profile=capability
        or _capability(capability_configuration or configuration, runtime),
        runtime=runtime,
        configuration=configuration,
        state_parent=state_parent or tmp_path / "attempts",
        output_root=output_root or tmp_path / "output",
        source_paths=(
            ProtectedPath("native-source", source),
            ProtectedPath("case", workspace.root),
        ),
        workspace_paths=(ProtectedPath("original-workspace", original_workspace),),
    )


@pytest.mark.parametrize("storage", ["state", "output"])
def test_rejects_storage_inside_protected_workspace(tmp_path: Path, storage: str) -> None:
    workspace, _ = _case(tmp_path)
    protected = tmp_path / "original-workspace"
    protected.mkdir()
    marker = protected / "marker.txt"
    marker.write_bytes(b"protected fixture\n")
    before = tuple((path.relative_to(protected), path.read_bytes()) for path in protected.iterdir())
    state_parent = protected / "attempts" if storage == "state" else None
    output_root = protected / "output" if storage == "output" else None

    with pytest.raises(ValueError, match="storage overlaps protected state"):
        _runner(
            tmp_path,
            workspace,
            state_parent=state_parent,
            output_root=output_root,
        )
    if storage == "output":
        assert output_root is not None
        assert not output_root.exists()
    after = tuple((path.relative_to(protected), path.read_bytes()) for path in protected.iterdir())
    assert after == before


def test_rejects_overlapping_attempt_and_output_storage(tmp_path: Path) -> None:
    workspace, _ = _case(tmp_path)
    output_root = tmp_path / "output"

    with pytest.raises(ValueError, match="storage overlaps protected state"):
        _runner(
            tmp_path,
            workspace,
            state_parent=output_root / "attempts",
            output_root=output_root,
        )


def _attempt(
    evidence: CaseEvidence,
    runner: CodexRunner,
    *,
    max_wall_time_seconds: int = 5,
    max_native_output_bytes: int = 1024 * 1024,
) -> PreparedAttempt:
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="fixture-provider",
        allowed_endpoint="https://example.invalid/responses",
    )
    limits = ResourceLimits(
        max_provider_calls=1,
        max_wall_time_seconds=max_wall_time_seconds,
        max_native_output_bytes=max_native_output_bytes,
        max_disposable_state_bytes=4 * 1024 * 1024,
        max_disposable_state_entries=1000,
    )
    decision = evidence.decision
    assert decision is not None
    plan = ResolvedExecutionPlan(
        case_digest=evidence.digest,
        capture_type="logical-session",
        candidate_materialization="source-backed",
        capability=runner.capability_profile,
        harness="codex",
        source_harness_version=evidence.manifest.harness_version,
        source_schema="codex-rollout-jsonl",
        model_requested=evidence.manifest.model_requested,
        decision_digest=decision.selector.digest,
        runner_fingerprint=runner.fingerprint,
        safety_policy=policy,
        resource_limits=limits,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type="final-response",
    )
    return PreparedAttempt(
        evidence=evidence,
        candidate=prepare_candidate(evidence, plan),
        target=experiment.target,
        execution_plan=plan,
        safety_policy=policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )


def test_runs_one_final_response_inside_shared_isolation(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace)

    result = asyncio.run(runner.run(_attempt(evidence, runner)))

    assert result.final_text == "Fixture complete."
    assert result.model_returned == "fixture-model"
    assert result.service_tier == "default"
    assert result.usage is not None
    assert result.usage.input_tokens == 101
    assert result.usage.cached_input_tokens == 50
    assert result.accounting.provider_calls_started == 1
    assert result.accounting.provider_calls_completed == 1
    assert result.accounting.actions_executed == 0
    assert result.accounting.tool_network_requests == 0
    assert result.accounting.cleanup_outcome == "verified"
    assert result.accounting.source_store_effect == "unchanged"
    assert result.accounting.original_workspace_effect == "unchanged"
    assert result.native_output is not None
    assert not (tmp_path / "attempts" / result.attempt_id).exists()
    assert stat.S_IMODE((tmp_path / "output").stat().st_mode) == 0o700
    assert (tmp_path / "original-workspace" / "marker.txt").read_text() == "unchanged\n"


def test_rejects_action_request_before_any_action_loop(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="action")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner)))

    assert refused.value.code is RunnerErrorCode.SAFETY
    assert refused.value.accounting is not None
    assert refused.value.accounting.actions_requested == 1
    assert refused.value.accounting.actions_denied == 1
    assert refused.value.accounting.actions_executed == 0
    assert "private-command" not in str(refused.value)


def test_failed_outcome_omits_oversized_identity_metadata(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="action-long-metadata")
    attempt = _attempt(evidence, runner)

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(attempt))

    assert refused.value.code is RunnerErrorCode.PROTOCOL
    assert refused.value.model_returned is None
    assert "private-model" not in str(refused.value)
    persisted = AttemptEvidence.from_error(attempt, refused.value)
    assert persisted.model_returned is None


def test_rejects_provider_configuration_mismatch_before_execution(tmp_path: Path) -> None:
    workspace, _ = _case(tmp_path)
    configuration = _configuration()
    mismatched = CodexProviderConfiguration(
        source_provider_id="other-provider",
        provider_id="ithrl",
        endpoint=configuration.endpoint,
        authentication=configuration.authentication,
        environment_names=configuration.environment_names,
        deviations=configuration.deviations,
        rendered_config=configuration.rendered_config,
        environment=configuration.environment,
    )

    with pytest.raises(ValueError, match="does not match"):
        _runner(
            tmp_path,
            workspace,
            configuration=mismatched,
            capability_configuration=configuration,
        )

    assert not (tmp_path / "attempts").exists()


def test_rejects_runtime_replacement_before_attempt_execution(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    attempt = _attempt(evidence, runner)
    (tmp_path / "codex-complete").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(attempt))

    assert refused.value.code is RunnerErrorCode.PREPARATION
    assert not (tmp_path / "attempts").exists()


def test_maps_protocol_failure_without_exposing_native_output(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="malformed")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner)))

    assert refused.value.code is RunnerErrorCode.PROTOCOL
    assert "not-json" not in str(refused.value)
    assert refused.value.accounting is not None
    assert refused.value.accounting.cleanup_outcome == "verified"


def test_maps_timeout_after_verified_cleanup(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="sleep")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner, max_wall_time_seconds=1)))

    assert refused.value.code is RunnerErrorCode.TIMEOUT
    assert refused.value.accounting is not None
    assert refused.value.accounting.process_outcome == "terminated"
    assert refused.value.accounting.cleanup_outcome == "verified"
    assert refused.value.accounting.provider_calls_started == 1
    assert refused.value.accounting.provider_calls_completed == 0


def test_maps_output_limit_and_preserves_bounded_native_evidence(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="output")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner, max_native_output_bytes=4096)))

    assert refused.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert refused.value.native_output is not None
    assert refused.value.native_output.size_bytes == 4096
    assert refused.value.native_output.complete is False
    assert refused.value.accounting is not None
    assert refused.value.accounting.provider_calls_started == 1
    assert refused.value.accounting.provider_calls_completed == 0


def test_maps_oversized_final_response_without_exposing_content(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="long-final")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner)))

    assert refused.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert "private-response" not in str(refused.value)
    assert refused.value.accounting is not None
    assert refused.value.accounting.provider_calls_completed == 1


def test_cancellation_waits_for_runner_cleanup(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="sleep")
    attempt = _attempt(evidence, runner)

    async def cancel() -> RunnerCancelled:
        task = asyncio.create_task(runner.run(attempt))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(RunnerCancelled) as refused:
            await task
        return refused.value

    cancelled = asyncio.run(cancel())

    assert cancelled.error.accounting is not None
    assert cancelled.error.accounting.process_outcome == "terminated"
    assert cancelled.error.accounting.cleanup_outcome == "verified"
    assert cancelled.error.accounting.provider_calls_started == 1
    assert cancelled.error.accounting.provider_calls_completed == 0
    assert not (tmp_path / "attempts" / attempt.attempt_id).exists()
