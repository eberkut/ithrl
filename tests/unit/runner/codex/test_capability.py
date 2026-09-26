from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

import pytest

import ithrl.runner.codex.capability as capability
from ithrl.runner.codex.capability import (
    CodexCapabilityError,
    CodexCapabilityErrorCode,
    detect_codex_capability,
)
from ithrl.runner.codex.configuration import CodexProviderConfiguration
from ithrl.runner.codex.isolation import CodexRuntime
from ithrl.runner.codex.policy import DISABLED_FEATURES

SCHEMA_FIXTURE = Path(__file__).parents[3] / "fixtures" / "codex-app-server" / "schema"

_FEATURES = {
    "apps",
    "browser_use",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "plugins",
    "remote_plugin",
    "request_permissions_tool",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_suggest",
    "unbounded_connection_retries",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
}
_REAL_ISOLATION_PROBE = capability._probe_isolation_boundary


@pytest.fixture(autouse=True)
def _stub_isolation_gate_for_unrelated_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def already_verified(_runtime: CodexRuntime, _root: Path) -> None:
        return None

    monkeypatch.setattr(capability, "_probe_isolation_boundary", already_verified)


def _configuration() -> CodexProviderConfiguration:
    rendered = """model_provider = "ithrl"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
cli_auth_credentials_store = "ephemeral"

[model_providers.ithrl]
name = "fixture"
base_url = "https://provider.invalid/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
supports_websockets = false
supports_standalone_web_search = false
env_key = "ITHRL_PROVIDER_SECRET_0"

[features]
""" + "".join(f"{feature} = false\n" for feature in sorted(DISABLED_FEATURES))
    return CodexProviderConfiguration(
        source_provider_id="fixture-provider",
        provider_id="ithrl",
        endpoint="https://provider.invalid/v1/responses",
        authentication="custom-environment",
        environment_names=("ITHRL_PROVIDER_SECRET_0",),
        deviations=("provider-request-retries-disabled",),
        rendered_config=rendered.encode(),
        environment=MappingProxyType({"ITHRL_PROVIDER_SECRET_0": "private-value"}),
    )


def _fake_codex(
    tmp_path: Path,
    schema: Path,
    *,
    version: str = "99.42.7",
    disabled_features: set[str] = _FEATURES,
) -> Path:
    executable = tmp_path / "codex"
    feature_lines = "\n".join(f"{name} stable false" for name in sorted(disabled_features))
    source = f"""#!/usr/bin/python3
import os
import shutil
import signal
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("codex-cli {version}")
    raise SystemExit(0)
if arguments == ["features", "list"]:
    print({feature_lines!r})
    raise SystemExit(0)
if arguments[:2] == ["app-server", "generate-json-schema"]:
    destination = Path(arguments[arguments.index("--out") + 1])
    shutil.copytree({str(schema)!r}, destination, dirs_exist_ok=True)
    raise SystemExit(0)
raise SystemExit(64)
"""
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _runtime(executable: Path) -> CodexRuntime:
    return CodexRuntime(
        codex=executable,
        bubblewrap=Path("/usr/bin/bwrap"),
        python=Path("/usr/bin/python3"),
    )


def _capture_memory_descriptors(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    descriptors: list[int] = []
    original_memory_file = capability._memory_file

    def capture_descriptor(content: bytes | None) -> int | None:
        descriptor = original_memory_file(content)
        if descriptor is not None:
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(capability, "_memory_file", capture_descriptor)
    return descriptors


def _assert_closed(descriptor: int) -> None:
    with pytest.raises(OSError, match=re.escape(os.strerror(errno.EBADF))) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


def test_detects_support_from_structure_without_version_allowlist(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(
        tmp_path,
        schema,
        version="123.456.789",
        disabled_features=_FEATURES - {"unified_exec"},
    )

    capability = detect_codex_capability(_runtime(executable), _configuration())

    assert capability.runtime_harness_version == "123.456.789"
    assert capability.protocol_version is not None
    assert capability.protocol_version.startswith("app-server-schema-")
    assert capability.provider == "fixture-provider"
    assert capability.endpoint == "https://provider.invalid/v1/responses"
    assert capability.supported_targets == ("final-response",)
    assert capability.action_execution == "denied"


def test_isolation_probe_reads_host_view_and_cleans_owned_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    try:
        with TemporaryDirectory(
            prefix="ithrl-codex-test-",
            dir=capability._ISOLATION_HOST_PARENT,
        ):
            pass
    except OSError as error:
        pytest.skip(
            "real Bubblewrap host-view test requires writable /var/tmp: "
            f"{error.strerror or type(error).__name__}"
        )

    _REAL_ISOLATION_PROBE(_runtime(Path("/bin/true")), root)

    assert list(root.iterdir()) == []


def test_isolation_probe_closes_authentication_descriptor_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    host_parent = tmp_path / "host-parent"
    host_parent.mkdir()
    monkeypatch.setattr(capability, "_ISOLATION_HOST_PARENT", host_parent)
    descriptors = _capture_memory_descriptors(monkeypatch)

    def complete_probe(
        argv: tuple[str, ...],
        _environment: Mapping[str, str],
        cwd: Path,
        *,
        failure_code: CodexCapabilityErrorCode = CodexCapabilityErrorCode.UNAVAILABLE,
        state_root: Path | None = None,
        inherited_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        del failure_code, state_root
        assert "--ro-bind-data" in argv
        assert inherited_fds == tuple(descriptors)
        (cwd / "isolation-probe.txt").write_bytes(capability._ISOLATION_PROBE_RESULT)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(capability, "_run_probe", complete_probe)

    _REAL_ISOLATION_PROBE(_runtime(Path("/bin/true")), root)

    assert list(root.iterdir()) == []
    assert len(descriptors) == 1
    _assert_closed(descriptors[0])


def test_refuses_executable_that_cannot_provide_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    host_parent = tmp_path / "host-parent"
    host_parent.mkdir()
    monkeypatch.setattr(capability, "_ISOLATION_HOST_PARENT", host_parent)
    ineffective = tmp_path / "not-bubblewrap"
    ineffective.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ineffective.chmod(stat.S_IRWXU)
    runtime = CodexRuntime(
        codex=Path("/bin/true"),
        bubblewrap=ineffective,
        python=Path("/usr/bin/python3"),
    )
    descriptors = _capture_memory_descriptors(monkeypatch)

    with pytest.raises(
        CodexCapabilityError,
        match="isolation runtime is unavailable or incompatible",
    ) as refused:
        _REAL_ISOLATION_PROBE(runtime, root)

    assert refused.value.code is CodexCapabilityErrorCode.UNAVAILABLE
    assert list(root.iterdir()) == []
    assert len(descriptors) == 1
    _assert_closed(descriptors[0])


def test_refuses_unverified_tool_policy(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema, disabled_features=_FEATURES - {"shell_tool"})

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNSAFE_POLICY
    assert "shell_tool" not in str(refused.value)


def test_refuses_unbounded_connection_retries(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(
        tmp_path,
        schema,
        disabled_features=_FEATURES - {"unbounded_connection_retries"},
    )

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNSAFE_POLICY


@pytest.mark.parametrize(
    ("current", "replacement"),
    [
        ("request_max_retries = 0\n", ""),
        ("request_max_retries = 0", "request_max_retries = 1"),
        ("stream_max_retries = 0", 'stream_max_retries = "0"'),
    ],
)
def test_refuses_unverified_retry_configuration(
    tmp_path: Path,
    current: str,
    replacement: str,
) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    configuration = _configuration()
    rendered = configuration.rendered_config.decode().replace(current, replacement)
    unsafe = CodexProviderConfiguration(
        source_provider_id=configuration.source_provider_id,
        provider_id=configuration.provider_id,
        endpoint=configuration.endpoint,
        authentication=configuration.authentication,
        environment_names=configuration.environment_names,
        deviations=configuration.deviations,
        rendered_config=rendered.encode(),
        environment=configuration.environment,
    )

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), unsafe)

    assert refused.value.code is CodexCapabilityErrorCode.UNSAFE_POLICY


def test_refuses_unreferenced_provider_secret(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    configuration = _configuration()
    unsafe = CodexProviderConfiguration(
        source_provider_id=configuration.source_provider_id,
        provider_id=configuration.provider_id,
        endpoint=configuration.endpoint,
        authentication=configuration.authentication,
        environment_names=("ITHRL_PROVIDER_SECRET_0", "ITHRL_PROVIDER_SECRET_1"),
        deviations=configuration.deviations,
        rendered_config=configuration.rendered_config,
        environment=MappingProxyType(
            {
                "ITHRL_PROVIDER_SECRET_0": "referenced-private-value",
                "ITHRL_PROVIDER_SECRET_1": "unreferenced-private-value",
            }
        ),
    )

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), unsafe)

    assert refused.value.code is CodexCapabilityErrorCode.UNSAFE_POLICY
    assert "private-value" not in str(refused.value)


def test_refuses_unsupported_generated_protocol(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    (schema / "ServerRequest.json").unlink()
    executable = _fake_codex(tmp_path, schema)

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert "incomplete" in str(refused.value)


def test_refuses_invalid_version_output_without_exposing_probe_output(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    content = executable.read_text(encoding="utf-8").replace(
        'print("codex-cli 99.42.7")',
        'print("private invalid version output")',
    )
    executable.write_text(content, encoding="utf-8")
    executable.chmod(stat.S_IRWXU)

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNAVAILABLE
    assert "private invalid" not in str(refused.value)


def test_capability_probe_never_serializes_credential_values(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    configuration = _configuration()

    capability = detect_codex_capability(_runtime(executable), configuration)

    serialized = json.dumps(capability.model_dump(mode="json"))
    assert "private-value" not in serialized
    assert "private-value" not in repr(configuration)


def test_stops_probe_when_output_exceeds_limit(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    content = executable.read_text(encoding="utf-8").replace(
        'print("codex-cli 99.42.7")',
        'sys.stdout.write("private-output" * 100_000); sys.stdout.flush(); '
        "import time; time.sleep(60)",
    )
    executable.write_text(content, encoding="utf-8")
    executable.chmod(stat.S_IRWXU)

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNAVAILABLE
    assert "private-output" not in str(refused.value)


def test_stops_schema_probe_when_file_limit_is_exceeded(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    content = executable.read_text(encoding="utf-8").replace(
        f"    shutil.copytree({str(schema)!r}, destination, dirs_exist_ok=True)",
        "    destination.mkdir(parents=True, exist_ok=True)\n"
        "    for index in range(2_100):\n"
        '        (destination / f"{index}.json").write_text("{}")\n'
        "    import time\n"
        "    time.sleep(60)",
    )
    executable.write_text(content, encoding="utf-8")
    executable.chmod(stat.S_IRWXU)

    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())

    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert "file limit" in str(refused.value)


def test_refuses_and_stops_probe_that_leaves_a_descendant(tmp_path: Path) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    child_pid_path = tmp_path / "child.pid"
    content = executable.read_text(encoding="utf-8").replace(
        '    print("codex-cli 99.42.7")',
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"        Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        "        time.sleep(60)\n"
        "        raise SystemExit(0)\n"
        f"    while not Path({str(child_pid_path)!r}).exists():\n"
        "        time.sleep(0.01)\n"
        '    print("codex-cli 99.42.7")',
    )
    executable.write_text(content, encoding="utf-8")
    executable.chmod(stat.S_IRWXU)

    child_pid: int | None = None
    try:
        with pytest.raises(CodexCapabilityError, match="descendant process"):
            detect_codex_capability(_runtime(executable), _configuration())
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while _process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _process_is_running(child_pid)
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_keyboard_interrupt_stops_probe_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "interruptible-probe"
    parent_pid_path = tmp_path / "parent.pid"
    child_pid_path = tmp_path / "child.pid"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os\n"
        "import signal\n"
        "import time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        f"Path({str(parent_pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(stat.S_IRWXU)
    original_check = capability._require_bounded_probe_output

    def interrupt_after_child_started(
        stdout_bytes: int,
        stderr_bytes: int,
        failure_code: CodexCapabilityErrorCode,
    ) -> None:
        if child_pid_path.exists():
            raise KeyboardInterrupt
        original_check(stdout_bytes, stderr_bytes, failure_code)

    monkeypatch.setattr(capability, "_require_bounded_probe_output", interrupt_after_child_started)
    try:
        with pytest.raises(KeyboardInterrupt):
            capability._run_probe(
                (str(executable),),
                {"PATH": "/usr/bin:/bin"},
                tmp_path,
            )
        process_ids = [
            int(path.read_text(encoding="utf-8")) for path in (parent_pid_path, child_pid_path)
        ]
        assert not any(_process_is_running(process_id) for process_id in process_ids)
    finally:
        for path in (parent_pid_path, child_pid_path):
            if path.exists():
                process_id = int(path.read_text(encoding="utf-8"))
                with suppress(ProcessLookupError):
                    os.kill(process_id, signal.SIGKILL)


def _process_is_running(process_id: int) -> bool:
    try:
        fields = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return False
    return fields[2] != "Z"
