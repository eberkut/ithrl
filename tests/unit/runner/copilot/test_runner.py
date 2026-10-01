from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.execution import (
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.runner import (
    PreparedAttempt,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    prepare_candidate,
)
from ithrl.runner.copilot import (
    CopilotRunner,
    CopilotRuntime,
    DetectedCopilotCapability,
)
from ithrl.runner.copilot.policy import (
    ENDPOINT,
    PROVIDER,
    CopilotTarget,
)
from ithrl.runner.runtime import ProtectedPath
from native_support.copilot import _FAKE_COPILOT, _capability, _case, _configuration


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
