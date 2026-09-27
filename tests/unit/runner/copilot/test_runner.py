from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Any

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
from ithrl.runner.copilot import (
    CopilotConfiguration,
    CopilotRunner,
    CopilotRuntime,
    DetectedCopilotCapability,
)
from ithrl.runner.copilot.policy import (
    ENDPOINT,
    PROTOCOL_VERSION,
    PROVIDER,
    CopilotTarget,
)
from ithrl.runner.runtime import ProtectedPath

_FAKE_COPILOT = r"""#!/usr/bin/python3
import json
import os
import sys
import time

MODE = "__MODE__"
MARKER = __MARKER__


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


arguments = sys.argv[1:]
if "sessions" in arguments and "import" in arguments:
    session_path = arguments[-1]
    with open(session_path, "rb") as source:
        records = [json.loads(line) for line in source if line.strip()]
    if not records or records[0].get("type") != "session":
        raise SystemExit(3)
    print(json.dumps({"ok": True, "code": "session_imported", "sessionId": "session-1"}))
    raise SystemExit(0)

trigger = sys.stdin.readline().rstrip("\n")
if not trigger:
    raise SystemExit(4)
if MODE == "sleep":
    time.sleep(60)
if MODE == "malformed":
    print("private-native-output")
    raise SystemExit(0)
if MODE == "output":
    for _ in range(3000):
        print("x" * 1024)
    raise SystemExit(0)
if MODE == "state":
    with open(os.path.join(os.environ["COPILOT_HOME"], "oversized.bin"), "wb") as output:
        output.write(b"x" * (512 * 1024))

turn_id = "turn-1"
model = arguments[arguments.index("--model") + 1]
emit("session.skills_loaded", {"count": 0})
emit("session.mcp_servers_loaded", {"count": 0})
emit("session.tools_updated", {"tools": []})
emit("session.info", {"configuration": {"tools": "filtered"}})
emit("user.message", {"turnId": turn_id, "content": trigger})
emit("assistant.turn_start", {"turnId": turn_id, "interactionId": "interaction-1"})
emit("model.call_start", {"turnId": turn_id, "model": model})
emit(
    "model.call_finished",
    {"turnId": turn_id, "model": model, "outcome": "success", "dispatchDurationMs": 1},
)

shell_action = "--available-tools=bash" in arguments
if shell_action:
    request = {
        "toolCallId": "call-1",
        "name": "bash",
        "type": "function",
        "arguments": {"command": "pwd", "description": "Show directory"},
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
    time.sleep(2)
    with open(MARKER, "w", encoding="utf-8") as marker:
        marker.write("action executed\n")
    raise SystemExit(5)

emit(
    "assistant.message",
    {
        "messageId": "message-1",
        "apiCallId": "api-call-1",
        "model": model,
        "content": "Fixture complete.",
        "toolRequests": [],
        "turnId": turn_id,
    },
)
emit("assistant.turn_end", {"turnId": turn_id})
emit("assistant.idle", {}, ephemeral=True)
result = {
    "type": "result",
    "timestamp": "2026-01-01T00:00:01.000Z",
    "sessionId": "session-1",
    "exitCode": 0,
    "usage": {
        "premiumRequests": 0,
        "totalApiDurationMs": 1,
        "sessionDurationMs": 2,
        "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": 0},
    },
}
print(json.dumps(result, separators=(",", ":")), flush=True)
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


def _source_records(target: CopilotTarget) -> list[dict[str, Any]]:
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
    return [
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


def _case(
    tmp_path: Path,
    target: CopilotTarget,
) -> tuple[Path, CaseWorkspace, CaseEvidence]:
    source = tmp_path / "source" / "events.jsonl"
    source.parent.mkdir()
    source.write_text(
        "".join(
            json.dumps(record, separators=(",", ":")) + "\n" for record in _source_records(target)
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
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="copilot-semantic-session-import-v1",
        supported_targets=(target,),
        provider_transport="required",
        provider_calls_per_attempt=2 if target == "shell-action" else 1,
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


def _runner(
    tmp_path: Path,
    source: Path,
    case_workspace: CaseWorkspace,
    *,
    target: CopilotTarget,
    mode: str = "complete",
    state_parent: Path | None = None,
    output_root: Path | None = None,
    provider: str = PROVIDER,
    endpoint: str = ENDPOINT,
) -> tuple[CopilotRunner, Path]:
    marker = tmp_path / "action-marker.txt"
    executable = tmp_path / f"copilot-{mode}"
    executable.write_text(
        _FAKE_COPILOT.replace("__MODE__", mode).replace("__MARKER__", json.dumps(str(marker))),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    original_workspace = tmp_path / "original-workspace"
    original_workspace.mkdir(exist_ok=True)
    protected_marker = original_workspace / "protected.txt"
    if not protected_marker.exists():
        protected_marker.write_text("unchanged\n", encoding="utf-8")
    configuration = _configuration()
    runtime = CopilotRuntime(copilot=executable, bubblewrap=Path("/usr/bin/bwrap"))
    runner = CopilotRunner(
        detected_capability=DetectedCopilotCapability(
            profile=_capability(
                target,
                provider=provider,
                endpoint=endpoint,
            ),
            runtime=runtime,
            configuration=configuration,
        ),
        state_parent=state_parent or tmp_path / "attempts",
        output_root=output_root or tmp_path / "output",
        source_paths=(
            ProtectedPath("native-source", source),
            ProtectedPath("case", case_workspace.root),
        ),
        workspace_paths=(ProtectedPath("original-workspace", original_workspace),),
    )
    return runner, marker


def _attempt(
    evidence: CaseEvidence,
    runner: CopilotRunner,
    *,
    max_wall_time_seconds: int = 5,
    max_native_output_bytes: int = 1024 * 1024,
    max_disposable_state_bytes: int = 4 * 1024 * 1024,
) -> PreparedAttempt:
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    calls = runner.capability_profile.provider_calls_per_attempt
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider=PROVIDER,
        allowed_endpoint=ENDPOINT,
    )
    limits = ResourceLimits(
        max_provider_calls=calls,
        max_wall_time_seconds=max_wall_time_seconds,
        max_native_output_bytes=max_native_output_bytes,
        max_disposable_state_bytes=max_disposable_state_bytes,
        max_disposable_state_entries=1000,
    )
    decision = evidence.decision
    assert decision is not None
    plan = ResolvedExecutionPlan(
        case_digest=evidence.digest,
        capture_type="logical-session",
        candidate_materialization="source-backed",
        capability=runner.capability_profile,
        harness="copilot",
        source_harness_version=evidence.manifest.harness_version,
        source_schema="copilot-events-jsonl",
        model_requested=evidence.manifest.model_requested,
        decision_digest=decision.selector.digest,
        runner_fingerprint=runner.fingerprint,
        safety_policy=policy,
        resource_limits=limits,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type=experiment.target.type,
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


def test_runs_final_response_without_changing_protected_state(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, marker = _runner(tmp_path, source, workspace, target="final-response")
    source_before = source.read_bytes()
    case_before = tuple(
        (path.relative_to(workspace.root), path.read_bytes())
        for path in sorted(workspace.root.rglob("*"))
        if path.is_file()
    )

    result = asyncio.run(runner.run(_attempt(evidence, runner)))

    assert result.final_text == "Fixture complete."
    assert result.tool_calls == ()
    assert result.model_returned == "fixture-model"
    assert result.usage is None
    assert result.accounting.provider_calls_started == 1
    assert result.accounting.provider_calls_completed == 1
    assert result.accounting.actions_executed == 0
    assert result.accounting.cleanup_outcome == "verified"
    assert result.accounting.source_store_effect == "unchanged"
    assert result.accounting.original_workspace_effect == "unchanged"
    assert result.native_output is not None
    assert not (tmp_path / "attempts" / result.attempt_id).exists()
    assert stat.S_IMODE((tmp_path / "output").stat().st_mode) == 0o700
    assert source.read_bytes() == source_before
    assert case_before == tuple(
        (path.relative_to(workspace.root), path.read_bytes())
        for path in sorted(workspace.root.rglob("*"))
        if path.is_file()
    )
    assert (tmp_path / "original-workspace" / "protected.txt").read_text() == "unchanged\n"
    assert not marker.exists()


def test_stops_after_visible_denied_shell_action(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "shell-action")
    runner, marker = _runner(tmp_path, source, workspace, target="shell-action")

    result = asyncio.run(runner.run(_attempt(evidence, runner)))

    assert result.final_text is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments["command"] == "pwd"
    assert result.accounting.provider_calls_started == 1
    assert result.accounting.provider_calls_completed == 1
    assert result.accounting.actions_requested == 1
    assert result.accounting.actions_denied == 1
    assert result.accounting.actions_executed == 0
    assert result.accounting.process_outcome == "terminated"
    assert result.accounting.cleanup_outcome == "verified"
    assert not marker.exists()
    assert not (tmp_path / "attempts" / result.attempt_id).exists()


@pytest.mark.parametrize("storage", ["state", "output"])
def test_rejects_storage_inside_protected_workspace(tmp_path: Path, storage: str) -> None:
    source, workspace, _ = _case(tmp_path, "final-response")
    protected = tmp_path / "original-workspace"
    protected.mkdir()
    protected_marker = protected / "protected.txt"
    protected_marker.write_text("unchanged\n", encoding="utf-8")

    with pytest.raises(ValueError, match="storage overlaps protected state"):
        _runner(
            tmp_path,
            source,
            workspace,
            target="final-response",
            state_parent=protected / "attempts" if storage == "state" else None,
            output_root=protected / "output" if storage == "output" else None,
        )

    assert protected_marker.read_text() == "unchanged\n"
    rejected_path = protected / ("attempts" if storage == "state" else "output")
    assert not rejected_path.exists()


def test_rejects_overlapping_attempt_and_output_storage(tmp_path: Path) -> None:
    source, workspace, _ = _case(tmp_path, "final-response")
    output_root = tmp_path / "output"

    with pytest.raises(ValueError, match="storage overlaps protected state"):
        _runner(
            tmp_path,
            source,
            workspace,
            target="final-response",
            state_parent=output_root / "attempts",
            output_root=output_root,
        )


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    [
        ("arbitrary-provider", ENDPOINT),
        (PROVIDER, "https://arbitrary.example.invalid/copilot"),
    ],
)
def test_rejects_arbitrary_provider_destination_profiles(
    tmp_path: Path,
    provider: str,
    endpoint: str,
) -> None:
    source, workspace, _ = _case(tmp_path, "final-response")

    with pytest.raises(ValueError, match="does not satisfy the runner contract"):
        _runner(
            tmp_path,
            source,
            workspace,
            target="final-response",
            provider=provider,
            endpoint=endpoint,
        )

    assert not (tmp_path / "attempts").exists()
    assert not (tmp_path / "output").exists()


def test_rejects_runtime_replacement_before_execution(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(tmp_path, source, workspace, target="final-response")
    attempt = _attempt(evidence, runner)
    (tmp_path / "copilot-complete").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(attempt))

    assert refused.value.code is RunnerErrorCode.PREPARATION
    assert not (tmp_path / "attempts").exists()


@pytest.mark.parametrize("mode", ["malformed", "continued"])
def test_maps_invalid_native_output_without_exposing_content(
    tmp_path: Path,
    mode: str,
) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(
        tmp_path,
        source,
        workspace,
        target="final-response",
        mode=mode,
    )

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner)))

    assert refused.value.code is RunnerErrorCode.PROTOCOL
    assert "private-native-output" not in str(refused.value)
    assert "continued-private-output" not in str(refused.value)
    assert "private-fixture-token" not in str(refused.value)
    assert refused.value.accounting is not None
    assert refused.value.accounting.cleanup_outcome == "verified"


def test_maps_timeout_after_verified_cleanup(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(
        tmp_path,
        source,
        workspace,
        target="final-response",
        mode="sleep",
    )

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner, max_wall_time_seconds=1)))

    assert refused.value.code is RunnerErrorCode.TIMEOUT
    assert refused.value.accounting is not None
    assert refused.value.accounting.process_outcome == "terminated"
    assert refused.value.accounting.cleanup_outcome == "verified"
    assert refused.value.accounting.provider_calls_started == 1
    assert refused.value.accounting.provider_calls_completed == 0


def test_maps_output_limit_and_preserves_bounded_native_evidence(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(
        tmp_path,
        source,
        workspace,
        target="final-response",
        mode="output",
    )

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner, max_native_output_bytes=4096)))

    assert refused.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert refused.value.native_output is not None
    assert refused.value.native_output.size_bytes == 4096
    assert refused.value.native_output.complete is False
    assert refused.value.accounting is not None
    assert refused.value.accounting.cleanup_outcome == "verified"


def test_maps_disposable_state_limit_after_cleanup(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(
        tmp_path,
        source,
        workspace,
        target="final-response",
        mode="state",
    )

    with pytest.raises(RunnerError) as refused:
        asyncio.run(runner.run(_attempt(evidence, runner, max_disposable_state_bytes=256 * 1024)))

    assert refused.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert refused.value.accounting is not None
    assert refused.value.accounting.disposable_state_bytes > 256 * 1024
    assert refused.value.accounting.cleanup_outcome == "verified"


def test_cancellation_waits_for_runner_cleanup(tmp_path: Path) -> None:
    source, workspace, evidence = _case(tmp_path, "final-response")
    runner, _ = _runner(
        tmp_path,
        source,
        workspace,
        target="final-response",
        mode="sleep",
    )
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
