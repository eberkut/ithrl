from __future__ import annotations

import io
import json
import os
import signal
import socket
import time
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

import pytest

import ithrl.runner.copilot.capability as capability
from ithrl.runner.copilot.capability import (
    CopilotCapabilityError,
    CopilotCapabilityErrorCode,
    DetectedCopilotCapability,
    detect_copilot_capability,
)
from ithrl.runner.copilot.configuration import (
    CopilotConfiguration,
    resolve_copilot_configuration,
)
from ithrl.runner.copilot.isolation import CopilotRuntime
from ithrl.runner.copilot.policy import CopilotTarget

_REAL_ISOLATION_PROBE = capability._probe_isolation_boundary
_REAL_SDK_PROTOCOL_PROBE = capability._probe_sdk_protocol


@pytest.fixture(autouse=True)
def _stub_isolation_gate_for_structural_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def already_verified(_runtime: CopilotRuntime, _root: Path) -> None:
        return None

    monkeypatch.setattr(capability, "_probe_isolation_boundary", already_verified)

    def protocol_already_verified(*_arguments: object) -> None:
        return None

    monkeypatch.setattr(capability, "_probe_sdk_protocol", protocol_already_verified)


def _fake_copilot(
    tmp_path: Path,
    *,
    version: str = "99.42.7",
    version_trailer: str = "",
    version_notice: bool = False,
    fail_with: str | None = None,
    protected: Path | None = None,
    network_port: int | None = None,
) -> Path:
    executable = tmp_path / "copilot"
    source = f"""#!/usr/bin/python3
import json
import os
import socket
import sys
from pathlib import Path

if any(name in os.environ for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
    print("credential leaked to structural probe", file=sys.stderr)
    raise SystemExit(91)
settings = json.loads((Path(os.environ["COPILOT_HOME"]) / "settings.json").read_text())
if settings != {{"askUser": False, "autoUpdate": False, "remoteSessions": False, "remoteExport": False}}:
    raise SystemExit(92)
arguments = sys.argv[1:]
if {protected is not None!r}:
    for destination in (Path({str(protected)!r}), Path(sys.argv[0]), Path("/tmp/unexpected-discovery-write"), Path("/run/unexpected-discovery-write")):
        try:
            destination.write_bytes(b"unsafe structural write")
        except OSError:
            pass
        else:
            raise SystemExit(96)
    if list(Path("/home").iterdir()) or list(Path("/root").iterdir()):
        raise SystemExit(97)
    with socket.socket() as connection:
        connection.settimeout(1)
        try:
            connection.connect(("127.0.0.1", {network_port!r}))
        except OSError:
            pass
        else:
            raise SystemExit(98)
    for name in ("HOME", "COPILOT_HOME", "COPILOT_CACHE_HOME", "TMPDIR"):
        destination = Path(os.environ[name]) / ("proof-" + "-".join(arguments[:3]))
        destination.write_text("private writable state")
if {fail_with!r} is not None:
    print({fail_with!r}, file=sys.stderr)
    raise SystemExit(93)
if arguments == ["--version"]:
    print("GitHub Copilot CLI {version}{version_trailer}")
    if {version_notice!r}:
        print("Run 'copilot update' to check for updates.")
    raise SystemExit(0)
raise SystemExit(64)
"""
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _runtime(executable: Path, *, bubblewrap: Path = Path("/usr/bin/bwrap")) -> CopilotRuntime:
    return CopilotRuntime(copilot=executable, bubblewrap=bubblewrap)


def _configuration() -> CopilotConfiguration:
    return resolve_copilot_configuration(
        environment={"GH_TOKEN": "private-credential"},
        config_file=None,
    )


def _managed_configuration(tmp_path: Path) -> CopilotConfiguration:
    config = tmp_path / "config.json"
    config.write_bytes(b'{\n  // private managed config\n  "token": "private"\n}\n')
    config.chmod(0o600)
    return resolve_copilot_configuration(environment={}, config_file=config)


@pytest.mark.parametrize(
    ("target", "provider_calls", "visibility"),
    [
        ("final-response", 1, "not-applicable"),
        ("shell-action", 1, "visible"),
    ],
)
def test_detects_target_specific_capability_from_structural_contracts(
    tmp_path: Path,
    target: CopilotTarget,
    provider_calls: int,
    visibility: str,
) -> None:
    runtime = _runtime(_fake_copilot(tmp_path, version="123.456.789"))
    configuration = _configuration()

    detected = detect_copilot_capability(runtime, configuration, target=target)

    assert isinstance(detected, DetectedCopilotCapability)
    assert detected.runtime is runtime
    assert detected.configuration is configuration
    profile = detected.profile
    assert profile.runner_type == "copilot"
    assert profile.runtime_harness_version == "123.456.789"
    assert profile.protocol_version == "copilot-sdk-events-v1"
    assert profile.supported_targets == (target,)
    assert profile.supported_capture_types == ("logical-session",)
    assert profile.supported_materializations == ("source-backed",)
    assert profile.supported_context_operations == (
        ("dependency-closed-removal", "append-text")
        if target == "final-response"
        else ("dependency-closed-removal",)
    )
    assert profile.reconstruction_method == "copilot-native-prefix-sdk-v1"
    assert profile.provider_transport == "required"
    assert profile.provider_calls_per_attempt == provider_calls
    assert profile.provider == "github-copilot"
    assert profile.endpoint == "copilot://managed"
    assert profile.tool_network_access == "forbidden"
    assert profile.action_visibility == visibility
    assert profile.action_denial == "enforced"
    assert profile.action_execution == "denied"
    assert profile.source_store_effect == "unchanged"
    assert profile.original_workspace_effect == "unchanged"
    assert profile.protected_state_enforcement == "preventive"
    assert profile.disposable_state_effect == "writes"
    assert profile.disposable_state_limit_enforcement == "enforced"
    assert profile.process_containment == "process-group"
    assert any(
        "captured system guidance is restored exactly" in deviation
        for deviation in profile.deviations
    )


def test_version_probe_accepts_update_notice(tmp_path: Path) -> None:
    runtime = _runtime(
        _fake_copilot(tmp_path, version="1.0.88", version_trailer=".", version_notice=True)
    )

    detected = detect_copilot_capability(runtime, _configuration(), target="final-response")

    assert detected.profile.runtime_harness_version == "1.0.88"


def test_detection_invokes_isolation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[CopilotRuntime, Path]] = []

    def record(runtime: CopilotRuntime, root: Path) -> None:
        observed.append((runtime, root))

    monkeypatch.setattr(capability, "_probe_isolation_boundary", record)
    runtime = _runtime(_fake_copilot(tmp_path))

    detect_copilot_capability(runtime, _configuration(), target="final-response")

    assert len(observed) == 1
    assert observed[0][0] is runtime
    assert not observed[0][1].exists()


def test_every_structural_command_has_a_preventive_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "protected-host-marker"
    protected.write_bytes(b"unchanged")
    commands: list[tuple[str, ...]] = []
    roots: list[Path] = []
    original_run = capability._run_probe

    def record(
        argv: tuple[str, ...],
        environment: dict[str, str],
        cwd: Path,
        *,
        failure_code: CopilotCapabilityErrorCode = CopilotCapabilityErrorCode.PROBE_FAILED,
        state_root: Path | None = None,
        probe_stage: str | None = None,
    ) -> capability._CommandResult:
        commands.append(argv)
        roots.append(cwd.parent)
        assert state_root == cwd.parent
        assert "--unshare-net" in argv
        assert "--unshare-pid" in argv
        assert not {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"} & environment.keys()
        return original_run(
            argv,
            environment,
            cwd,
            failure_code=failure_code,
            state_root=state_root,
            probe_stage=probe_stage,
        )

    monkeypatch.setattr(capability, "_run_probe", record)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        executable = _fake_copilot(
            tmp_path,
            protected=protected,
            network_port=listener.getsockname()[1],
        )
        original = executable.read_bytes()

        detected = detect_copilot_capability(
            _runtime(executable), _configuration(), target="final-response"
        )

    assert protected.read_bytes() == b"unchanged"
    assert executable.read_bytes() == original
    assert len(commands) == 1
    assert not any(root.exists() for root in roots)
    assert any(
        "copilot-isolated-discovery-v1" in deviation for deviation in detected.profile.deviations
    )


def test_structural_commands_refuse_when_boundary_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(CopilotCapabilityError) as refused:
        detect_copilot_capability(
            _runtime(_fake_copilot(tmp_path), bubblewrap=Path("/bin/false")),
            _configuration(),
            target="final-response",
        )

    assert refused.value.code is CopilotCapabilityErrorCode.PROBE_FAILED


@pytest.mark.parametrize("mode", ["entries", "bytes", "symlink", "fifo", "permissions"])
def test_bounds_and_refuses_unsafe_private_discovery_state(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    if mode == "entries":
        monkeypatch.setattr(capability, "_MAX_PROBE_STATE_ENTRIES", 1)
        for name in ("one", "two"):
            (root / name).touch()
    elif mode == "bytes":
        monkeypatch.setattr(capability, "_MAX_PROBE_STATE_BYTES", 1)
        (root / "data").write_bytes(b"two")
    elif mode == "symlink":
        (root / "link").symlink_to(tmp_path)
    elif mode == "fifo":
        os.mkfifo(root / "fifo")
    else:
        root.chmod(0o755)

    with pytest.raises(CopilotCapabilityError) as refused:
        capability._require_bounded_probe_state(root)

    assert refused.value.code is CopilotCapabilityErrorCode.PROBE_FAILED


def test_growth_during_probe_is_bounded_and_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_copilot(tmp_path)
    fake.write_text(
        fake.read_text().replace(
            "arguments = sys.argv[1:]",
            "arguments = sys.argv[1:]\n"
            'Path(os.environ["COPILOT_CACHE_HOME"], "oversized").write_bytes(b"x" * 4096)\n'
            "import time\ntime.sleep(60)",
        )
    )
    monkeypatch.setattr(capability, "_MAX_PROBE_STATE_BYTES", 2048)

    with pytest.raises(CopilotCapabilityError, match="byte limit"):
        detect_copilot_capability(_runtime(fake), _configuration(), target="final-response")


def test_byte_limit_diagnostic_identifies_owned_file_without_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    directory = root / "private-directory"
    directory.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (directory / "private-file").write_bytes(b"private-file-contents")
    monkeypatch.setattr(capability, "_MAX_PROBE_STATE_BYTES", 10)

    with pytest.raises(CopilotCapabilityError) as refused:
        capability._require_bounded_probe_state(root)

    assert refused.value.state_limit_details == {
        "limit_bytes": 10,
        "accounted_bytes": 21,
        "file_path": "private-directory/private-file",
        "file_bytes": 21,
    }
    assert refused.value.state_files == (("private-directory/private-file", 21),)
    assert "private" not in str(refused.value)
    assert "private" not in repr(refused.value)
    assert str(tmp_path) not in str(refused.value.state_limit_details)


def test_interruption_stops_probe_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    executable = root / "probe"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    Path({str(root / 'child.pid')!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        f"Path({str(root / 'parent.pid')!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o700)
    original_check = capability._require_bounded_probe_state

    def interrupt(state: Path) -> None:
        if (state / "child.pid").exists():
            raise KeyboardInterrupt
        original_check(state)

    monkeypatch.setattr(capability, "_require_bounded_probe_state", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            capability._run_probe((str(executable),), {}, root, state_root=root)
        for name in ("child.pid", "parent.pid"):
            process_id = int((root / name).read_text())
            deadline = time.monotonic() + 2
            while _process_is_running(process_id) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _process_is_running(process_id)
    finally:
        for name in ("child.pid", "parent.pid"):
            if (root / name).exists():
                with suppress(ProcessLookupError):
                    os.kill(int((root / name).read_text()), signal.SIGKILL)


def _process_is_running(process_id: int) -> bool:
    try:
        fields = Path(f"/proc/{process_id}/stat").read_text().split()
    except FileNotFoundError:
        return False
    return len(fields) > 2 and fields[2] not in {"Z", "X"}


def test_real_isolation_probe_reads_host_view_and_cleans_owned_state(tmp_path: Path) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    try:
        with TemporaryDirectory(
            prefix="ithrl-copilot-test-",
            dir=capability._ISOLATION_HOST_PARENT,
        ):
            pass
    except OSError as error:
        pytest.skip(
            "real Bubblewrap host-view test requires writable /var/tmp: "
            f"{error.strerror or type(error).__name__}"
        )

    _REAL_ISOLATION_PROBE(
        _runtime(Path("/bin/true"), bubblewrap=Path("/usr/bin/bwrap")),
        root,
    )

    assert list(root.iterdir()) == []


def test_isolation_probe_refuses_ineffective_boundary_and_cleans_owned_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    ineffective = tmp_path / "not-bubblewrap"
    ineffective.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ineffective.chmod(0o700)

    with pytest.raises(CopilotCapabilityError, match="isolation runtime") as refused:
        _REAL_ISOLATION_PROBE(
            _runtime(Path("/bin/true"), bubblewrap=ineffective),
            root,
        )

    assert refused.value.code is CopilotCapabilityErrorCode.UNAVAILABLE
    assert list(root.iterdir()) == []


def test_capability_and_detection_do_not_serialize_credentials(tmp_path: Path) -> None:
    detected = detect_copilot_capability(
        _runtime(_fake_copilot(tmp_path)),
        _configuration(),
        target="final-response",
    )

    serialized = json.dumps(detected.profile.model_dump(mode="json"))
    assert "private-credential" not in serialized
    assert "private-credential" not in repr(detected)
    assert "private-credential" not in detected.profile.digest


def test_managed_config_remains_bound_to_detected_capability(tmp_path: Path) -> None:
    configuration = _managed_configuration(tmp_path)
    detected = detect_copilot_capability(
        _runtime(_fake_copilot(tmp_path)), configuration, target="final-response"
    )
    assert detected.configuration is configuration


def test_probe_failures_do_not_expose_process_output(tmp_path: Path) -> None:
    secret = "private-probe-error"

    with pytest.raises(CopilotCapabilityError) as refused:
        detect_copilot_capability(
            _runtime(_fake_copilot(tmp_path, fail_with=secret)),
            _configuration(),
            target="final-response",
        )

    assert refused.value.code is CopilotCapabilityErrorCode.PROBE_FAILED
    assert secret not in str(refused.value)
    assert secret not in repr(refused.value)
    assert refused.value.probe_stage == "version"
    assert refused.value.probe_exit_code == 93
    assert refused.value.stderr.endswith(b"private-probe-error\n")
    assert len(refused.value.stderr) <= capability._MAX_PROBE_OUTPUT_BYTES


def test_invalid_version_preserves_bounded_successful_probe_output(tmp_path: Path) -> None:
    executable = _fake_copilot(tmp_path, version="private invalid-version")
    with pytest.raises(CopilotCapabilityError) as refused:
        detect_copilot_capability(_runtime(executable), _configuration(), target="final-response")
    assert refused.value.probe_stage == "version"
    assert refused.value.probe_exit_code == 0
    assert refused.value.stdout
    assert len(refused.value.stdout) <= capability._MAX_PROBE_OUTPUT_BYTES
    assert "private invalid-version" not in str(refused.value)


def test_timeout_retains_private_partial_output_after_stopping_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capability, "_PROBE_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(CopilotCapabilityError, match="timed out") as refused:
        capability._run_probe(
            (
                "/usr/bin/python3",
                "-c",
                "import os,time; os.write(1,b'private stdout'); os.write(2,b'private stderr'); time.sleep(60)",
            ),
            {},
            tmp_path,
            probe_stage="version",
        )

    assert refused.value.probe_stage == "version"
    assert refused.value.probe_exit_code is not None
    assert refused.value.stdout == b"private stdout"
    assert refused.value.stderr == b"private stderr"
    assert "private stdout" not in str(refused.value)
    assert "private stderr" not in repr(refused.value)


def test_output_limit_retains_only_bounded_private_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capability, "_MAX_PROBE_OUTPUT_BYTES", 32)

    with pytest.raises(CopilotCapabilityError, match="output limit") as refused:
        capability._run_probe(
            ("/usr/bin/python3", "-c", "import os; os.write(1,b'x'*4096); os.write(2,b'y'*4096)"),
            {},
            tmp_path,
            probe_stage="sdk-protocol",
        )

    assert refused.value.probe_stage == "sdk-protocol"
    assert refused.value.probe_exit_code is not None
    assert refused.value.stdout == b"x" * 32
    assert len(refused.value.stderr) <= 32
    assert "x" * 32 not in repr(refused.value)


def test_unavailable_process_has_stage_without_synthetic_exit_code(tmp_path: Path) -> None:
    with pytest.raises(CopilotCapabilityError) as refused:
        capability._run_probe(
            (str(tmp_path / "missing"),), {}, tmp_path, probe_stage="sdk-protocol"
        )

    assert refused.value.code is CopilotCapabilityErrorCode.UNAVAILABLE
    assert refused.value.probe_stage == "sdk-protocol"
    assert refused.value.probe_exit_code is None
    assert refused.value.stdout == refused.value.stderr == b""


def test_diagnostic_read_failure_does_not_replace_original_refusal() -> None:
    class Unreadable(io.BytesIO):
        @override
        def seek(self, offset: int, whence: int = 0) -> int:
            raise OSError("private read error")

    error = CopilotCapabilityError(CopilotCapabilityErrorCode.PROBE_FAILED, "fixed reason")

    capability._capture_probe_error(
        error, None, Unreadable(), io.BytesIO(b"private stderr"), "isolation"
    )

    assert str(error) == "fixed reason"
    assert error.stdout == b""
    assert error.stderr == b"private stderr"
    assert error.probe_stage == "isolation"


def test_successful_probe_result_repr_does_not_expose_private_bytes() -> None:
    result = capability._CommandResult(stdout=b"private stdout", stderr=b"private stderr")

    assert "private" not in repr(result)


@pytest.mark.parametrize("target", ["final-response", "shell-action"])
def test_all_targets_require_successful_sdk_protocol_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: CopilotTarget
) -> None:
    observed: list[CopilotTarget] = []

    def record(
        runtime: CopilotRuntime, environment: dict[str, str], cwd: Path, target: CopilotTarget
    ) -> None:
        assert not {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"} & environment.keys()
        observed.append(target)

    monkeypatch.setattr(capability, "_probe_sdk_protocol", record)
    runtime = _runtime(_fake_copilot(tmp_path))
    detect_copilot_capability(runtime, _configuration(), target=target)
    assert observed == [target]

    def refuse(*arguments: object) -> None:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL, "native protocol unavailable"
        )

    monkeypatch.setattr(capability, "_probe_sdk_protocol", refuse)
    with pytest.raises(CopilotCapabilityError, match="native protocol unavailable"):
        detect_copilot_capability(runtime, _configuration(), target=target)


def test_sdk_protocol_probe_is_isolated_and_uses_public_client_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    observed: list[str] = []

    def record(
        argv: tuple[str, ...],
        environment: dict[str, str],
        cwd: Path,
        *,
        state_root: Path,
        probe_stage: str,
        failure_code: CopilotCapabilityErrorCode,
    ) -> capability._CommandResult:
        assert "--unshare-net" in argv
        assert "--unshare-pid" in argv
        assert state_root == root
        assert cwd == workspace
        assert probe_stage == "sdk-protocol"
        assert failure_code is CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL
        assert not {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"} & environment.keys()
        observed.append((root / "protocol-worker.py").read_text())
        return capability._CommandResult(stdout=b"ithrl-copilot-sdk-protocol-ok\n", stderr=b"")

    monkeypatch.setattr(capability, "_run_probe", record)
    # The autouse fixture stubs this boundary for unrelated structural tests.
    _REAL_SDK_PROTOCOL_PROBE(_runtime(_fake_copilot(tmp_path)), {}, workspace, "final-response")
    assert len(observed) == 1
    source = observed[0]
    assert "RuntimeConnection.for_stdio(path='/tmp/ithrl-state/runner/copilot', args=[])" in source
    assert "await client.start()" in source
    assert 'await client.ping("ithrl-credential-free-protocol")' in source
    assert "finally:\n        await client.stop()" in source
