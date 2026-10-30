from __future__ import annotations

import asyncio
import json
import ssl
import stat
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import MappingProxyType
from typing import Any, override

import pytest

import ithrl.runner.codex.runner as codex_runner_module
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.context import content_digest
from ithrl.execution import (
    CapabilityProfile,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.mutation import TextAppend, TextMutation
from ithrl.runner import (
    PreparedAttempt,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    prepare_candidate,
)
from ithrl.runner.codex import CodexProviderConfiguration, CodexRunner, CodexRuntime
from ithrl.runner.codex.isolation import program_digest
from ithrl.runner.codex.materialize import (
    CodexMaterializationError,
    CodexMaterializationErrorCode,
    editable_codex_text,
)
from ithrl.runner.codex.protocol_contracts import MAX_INFERENCE_REQUESTS
from ithrl.runner.codex.transcript import physical_request_counts
from ithrl.runner.contracts import CandidateInvalidError
from ithrl.runner.evidence import AttemptEvidence
from ithrl.runner.runtime import ProtectedOutputStore, ProtectedPath
from native_support.codex import (
    _FAKE_APP_SERVER,
    _FAKE_CODEX_WORKER,
    FIXTURES,
    _capability,
    _case,
    _configuration,
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
    max_provider_calls_per_attempt: int = MAX_INFERENCE_REQUESTS,
) -> CodexRunner:
    fake = tmp_path / f"codex-{mode}"
    fake.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n", encoding="utf-8")
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
    runner = CodexRunner(
        capability_profile=capability
        or _capability(
            capability_configuration or configuration,
            runtime,
            max_provider_calls_per_attempt=max_provider_calls_per_attempt,
        ),
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
    worker_source = tmp_path / f"worker-{mode}.py"
    worker_source.write_text(_FAKE_CODEX_WORKER.replace("__FIXTURE_MODE__", mode), encoding="utf-8")
    runner._worker_source = worker_source
    runner._worker_digest = program_digest(worker_source)
    return runner


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


def test_runner_fingerprint_binds_the_installed_client_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _case(tmp_path)
    versions = iter(("1.2.3", "1.2.4"))

    def installed_version(name: str) -> str:
        assert name == "ithrl"
        return next(versions)

    monkeypatch.setattr(codex_runner_module, "distribution_version", installed_version)
    first = _runner(tmp_path, workspace)
    second = _runner(tmp_path, workspace)

    assert first.capability_profile.digest == second.capability_profile.digest
    assert first.fingerprint != second.fingerprint


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
        allowed_endpoint=runner.capability_profile.endpoint,
    )
    limits = ResourceLimits(
        max_provider_calls=MAX_INFERENCE_REQUESTS,
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


@contextmanager
def _https_provider(
    tmp_path: Path,
) -> Generator[tuple[str, Path, list[tuple[str, bytes]]]]:
    certificate = tmp_path / "provider.pem"
    key = tmp_path / "provider.key"
    subprocess.run(  # noqa: S603 - fixed tool and arguments create private temporary test certificates
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=ithrl-fixture",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            "-addext",
            "extendedKeyUsage=serverAuth",
        ],
        check=True,
        capture_output=True,
    )
    calls: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            calls.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            payload = b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}/responses", certificate, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("selected_limit", [2, 3])
def test_production_worker_relay_enforces_the_profile_cap_end_to_end(
    tmp_path: Path, selected_limit: int
) -> None:
    workspace, evidence = _case(tmp_path)
    fake = tmp_path / "codex-app-server"
    fake.write_text(_FAKE_APP_SERVER, encoding="utf-8")
    fake.chmod(0o700)
    original = tmp_path / "original-workspace"
    original.mkdir()
    marker = original / "marker.txt"
    marker.write_text("unchanged\n")
    runtime = CodexRuntime(
        codex=fake, bubblewrap=Path("/usr/bin/bwrap"), python=Path(sys.executable)
    )
    with _https_provider(tmp_path) as (endpoint, certificate, calls):
        template = _configuration()
        environment = {
            **template.environment,
            "SSL_CERT_FILE": str(certificate),
            "NO_PROXY": "127.0.0.1",
        }
        configuration = replace(
            template,
            endpoint=endpoint,
            rendered_config=template.rendered_config.replace(
                b"https://example.invalid", endpoint.removesuffix("/responses").encode()
            ),
            environment_names=tuple(sorted(environment)),
            environment=MappingProxyType(environment),
        )
        capability = _capability(
            configuration, runtime, max_provider_calls_per_attempt=selected_limit
        ).model_copy(update={"endpoint": endpoint})
        runner = CodexRunner(
            capability_profile=capability,
            runtime=runtime,
            configuration=configuration,
            state_parent=tmp_path / "attempts",
            output_root=tmp_path / "output",
            source_paths=(ProtectedPath("case", workspace.root),),
            workspace_paths=(ProtectedPath("original-workspace", original),),
        )
        attempt = _attempt(evidence, runner)
        if selected_limit == 2:
            with pytest.raises(RunnerError) as refused:
                asyncio.run(runner.run(attempt))
            assert refused.value.code is RunnerErrorCode.PROVIDER_CALL_LIMIT
            assert refused.value.reason_code == "provider-call-limit"
            accounting = refused.value.accounting
            native_output = refused.value.native_output
        else:
            result = asyncio.run(runner.run(attempt))
            assert result.final_text == "Fixture complete."
            accounting = result.accounting
            native_output = result.native_output

    assert len(calls) == selected_limit
    assert [path for path, _ in calls] == ["/responses"] * selected_limit
    assert [json.loads(body)["ordinal"] for _, body in calls] == list(range(1, selected_limit + 1))
    assert accounting is not None
    assert (
        accounting.provider_calls_started == accounting.provider_calls_completed == selected_limit
    )
    assert accounting.actions_executed == accounting.tool_network_requests == 0
    assert accounting.cleanup_outcome == "verified"
    assert accounting.source_store_effect == accounting.original_workspace_effect == "unchanged"
    assert native_output is not None
    output = ProtectedOutputStore.open(tmp_path / "output").read(native_output)
    assert physical_request_counts(output.stdout) == (selected_limit, selected_limit)
    assert not tuple((tmp_path / "attempts").iterdir())
    assert marker.read_text() == "unchanged\n"
    workspace.load()


@pytest.mark.parametrize("mode", ["overload", "overload-after-output"])
def test_structured_overload_requires_no_output_for_fresh_sample(tmp_path: Path, mode: str) -> None:
    workspace, case = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode=mode)
    attempt = _attempt(case, runner)
    with pytest.raises(RunnerError) as caught:
        asyncio.run(runner.run(attempt))
    error = caught.value
    assert error.code is RunnerErrorCode.PROVIDER_OVERLOADED
    evidence = AttemptEvidence.from_error(attempt, error)
    assert evidence.overload_replacement_safe is (mode == "overload")
    assert not evidence.retry_safe
    assert evidence.acceptance_unknown
    assert evidence.provider_calls_possible == MAX_INFERENCE_REQUESTS
    assert evidence.accounting is not None
    assert evidence.accounting.provider_calls_started == 1
    assert evidence.accounting.cleanup_outcome == "verified"
    assert evidence.accounting.source_store_effect == "unchanged"
    assert evidence.accounting.original_workspace_effect == "unchanged"
    assert evidence.native_output is not None
    assert evidence.native_output.complete
    assert AttemptEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize(
    "changes",
    [
        {"code": RunnerErrorCode.PROTOCOL},
        {"code": RunnerErrorCode.TIMEOUT},
        {"code": RunnerErrorCode.CANCELLED},
        {"behavior_output_observed": True},
        {"behavior_output_observed": None},
        {"effective_policy_digest": None},
        {"provider": None},
        {"endpoint": "https://other.example.invalid"},
        {"native_output": None},
    ],
)
def test_overload_proof_rejects_missing_or_inconsistent_facts(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    workspace, case = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="overload")
    attempt = _attempt(case, runner)
    with pytest.raises(RunnerError) as caught:
        asyncio.run(runner.run(attempt))
    error = caught.value
    for key, value in changes.items():
        setattr(error, key, value)
    assert not AttemptEvidence.from_error(attempt, error).overload_replacement_safe


@pytest.mark.parametrize(
    "changes",
    [
        {"cleanup_outcome": "failed"},
        {"process_outcome": "unknown"},
        {"source_store_effect": "unknown"},
        {"original_workspace_effect": "changed"},
        {"actions_requested": 1, "actions_denied": 1},
        {"actions_requested": 1, "actions_executed": 1},
        {"tool_network_requests": 1},
        {"residual_disposable_state_bytes": 1, "cleanup_outcome": "failed"},
        {"provider_calls_started": 2, "provider_calls_completed": 2},
        {"elapsed_milliseconds": 5001},
        {"disposable_state_bytes": 4 * 1024 * 1024 + 1},
        {"disposable_state_entries": 1001},
    ],
)
def test_overload_proof_rejects_unsafe_effects_and_resource_overruns(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    workspace, case = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="overload")
    attempt = _attempt(case, runner)
    with pytest.raises(RunnerError) as caught:
        asyncio.run(runner.run(attempt))
    error = caught.value
    assert error.accounting is not None
    error.accounting = error.accounting.model_copy(update=changes)
    assert not AttemptEvidence.from_error(attempt, error).overload_replacement_safe


@pytest.mark.parametrize("code", list(CodexMaterializationErrorCode))
@pytest.mark.parametrize("removed", [False, True])
def test_pure_candidate_admission_only_classifies_local_reconstruction_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: CodexMaterializationErrorCode,
    removed: bool,
) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    attempt = _attempt(evidence, runner)
    if removed:
        node = next(n for n in evidence.context.nodes if n.mutable and not n.protected)
        attempt = replace(
            attempt,
            candidate=prepare_candidate(evidence, attempt.execution_plan, (node.id,)),
            phase="search",
            arm="candidate",
        )

    def refuse(_attempt: PreparedAttempt) -> None:
        raise CodexMaterializationError(code, "private reconstruction diagnostic")

    monkeypatch.setattr(codex_runner_module, "materialize_codex_attempt", refuse)
    expected = (
        CandidateInvalidError
        if removed
        and code
        in {
            CodexMaterializationErrorCode.MISSING_TRIGGER,
            CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT,
        }
        else CodexMaterializationError
    )
    with pytest.raises(expected):
        runner.validate_candidate(attempt)
    assert not (tmp_path / "attempts").exists()
    assert list((tmp_path / "output").iterdir()) == []


def test_candidate_admission_does_not_probe_codex_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    attempt = _attempt(evidence, runner)

    def forbidden_validation(_attempt: PreparedAttempt) -> None:
        pytest.fail("pure admission must not validate runtime executables or configuration")

    monkeypatch.setattr(runner, "_validate_attempt", forbidden_validation)
    runner.validate_candidate(attempt)
    assert not (tmp_path / "attempts").exists()
    assert list((tmp_path / "output").iterdir()) == []


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
    output = ProtectedOutputStore.open(tmp_path / "output").read(result.native_output)
    records = [json.loads(line) for line in output.stdout.splitlines()]
    owned_input = next(record["value"] for record in records if record["kind"] == "client")
    assert owned_input["method"] == "fixture/request"
    assert owned_input["params"]["client_version"] == distribution_version("ithrl")
    assert not (tmp_path / "attempts" / result.attempt_id).exists()
    assert stat.S_IMODE((tmp_path / "output").stat().st_mode) == 0o700
    assert (tmp_path / "original-workspace" / "marker.txt").read_text() == "unchanged\n"


def test_relay_uses_profile_bound_when_resource_allowance_is_larger(tmp_path: Path) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace, max_provider_calls_per_attempt=4)
    attempt = _attempt(evidence, runner)
    assert attempt.resource_limits.max_provider_calls == 16
    result = asyncio.run(runner.run(attempt))
    assert result.native_output is not None
    output = ProtectedOutputStore.open(tmp_path / "output").read(result.native_output)
    records = [json.loads(line) for line in output.stdout.splitlines()]
    request = next(record["value"] for record in records if record["kind"] == "client")
    assert request["params"]["transport"]["max_requests"] == 4


def test_selected_call_limit_is_distinct_failure_with_full_conservative_charge(
    tmp_path: Path,
) -> None:
    workspace, case = _case(tmp_path)
    runner = _runner(tmp_path, workspace, mode="call-limit", max_provider_calls_per_attempt=4)
    attempt = _attempt(case, runner)
    with pytest.raises(RunnerError) as caught:
        asyncio.run(runner.run(attempt))
    error = caught.value
    assert error.code is RunnerErrorCode.PROVIDER_CALL_LIMIT
    assert error.reason_code == "provider-call-limit"
    assert error.provider_acceptance_state.value == "unknown"
    assert error.accounting is not None
    assert error.accounting.provider_calls_started == error.accounting.provider_calls_completed == 4
    evidence = AttemptEvidence.from_error(attempt, error)
    assert evidence.provider_calls_possible == 4
    assert evidence.provider_call_charge(4) == 4
    assert evidence.acceptance_unknown
    assert not evidence.retry_safe
    assert not evidence.overload_replacement_safe
    assert evidence.accounting is not None
    assert evidence.accounting.cleanup_outcome == "verified"
    assert AttemptEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize("selected_limit", [0, 17, True, 1.0])
def test_rejects_invalid_profile_call_bound(tmp_path: Path, selected_limit: object) -> None:
    workspace, _ = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    invalid = runner.capability_profile.model_copy(
        update={"provider_calls_per_attempt": selected_limit}
    )
    with pytest.raises(ValueError, match="does not satisfy"):
        _runner(tmp_path, workspace, capability=invalid)


def test_registered_runner_delivers_exact_append_without_changing_trigger_or_policy(
    tmp_path: Path,
) -> None:
    workspace, evidence = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    baseline = _attempt(evidence, runner)
    site = editable_codex_text(baseline)[0]
    appended = "\n\nSynthetic archived note."
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=site.content_index,
                original_text_digest=content_digest(site.original_text),
                text=appended,
            ),
        )
    )
    changed = replace(
        baseline,
        candidate=prepare_candidate(evidence, baseline.execution_plan, mutation=mutation),
        phase="search",
        arm="candidate",
    )
    runner.validate_candidate(changed)
    assert not (tmp_path / "attempts").exists()
    source = (FIXTURES / "codex-session" / "rollout.jsonl").read_bytes()
    requests = []
    for attempt in (baseline, changed):
        result = asyncio.run(runner.run(attempt))
        assert result.native_output is not None
        output = ProtectedOutputStore.open(tmp_path / "output").read(result.native_output)
        records = [json.loads(line) for line in output.stdout.splitlines()]
        requests.append([record["value"] for record in records if record["kind"] == "client"])
        assert result.accounting.source_store_effect == "unchanged"
        assert result.accounting.original_workspace_effect == "unchanged"
        assert result.accounting.cleanup_outcome == "verified"
        assert result.accounting.actions_executed == 0

    expected = deepcopy(requests[0])
    message = expected[0]["params"]["history"][0]
    assert message["role"] == "user"
    assert message["content"][site.content_index]["text"] == site.original_text
    message["content"][site.content_index]["text"] = site.original_text + appended
    assert requests[1] == expected
    assert (FIXTURES / "codex-session" / "rollout.jsonl").read_bytes() == source
    assert workspace.load().digest == evidence.digest
    assert (tmp_path / "original-workspace" / "marker.txt").read_text() == "unchanged\n"


def test_rejects_old_removal_only_capability(tmp_path: Path) -> None:
    workspace, _ = _case(tmp_path)
    runner = _runner(tmp_path, workspace)
    previous = runner.capability_profile.model_copy(
        update={"supported_context_operations": ("dependency-closed-removal",)}
    )
    assert previous.digest != runner.capability_profile.digest
    with pytest.raises(ValueError, match="does not satisfy"):
        _runner(tmp_path, workspace, capability=previous)
    assert not (tmp_path / "attempts").exists()


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
        stdout_path = tmp_path / "attempts" / attempt.attempt_id / "stdout.bin"
        deadline = asyncio.get_running_loop().time() + 3
        physical_counts = None
        try:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    physical_counts = physical_request_counts(stdout_path.read_bytes())
                except OSError:
                    physical_counts = None
                if physical_counts == (1, 0):
                    break
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            task.cancel()
            with pytest.raises(RunnerCancelled):
                await task
            raise
        if physical_counts != (1, 0):
            task.cancel()
            with pytest.raises(RunnerCancelled):
                await task
            pytest.fail(f"worker start receipt was not captured: {physical_counts}")
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
    assert cancelled.error.provider_acceptance_state.value == "unknown"
    evidence = AttemptEvidence.from_error(attempt, cancelled.error)
    assert evidence.provider_calls_possible == MAX_INFERENCE_REQUESTS
    assert not (tmp_path / "attempts" / attempt.attempt_id).exists()
