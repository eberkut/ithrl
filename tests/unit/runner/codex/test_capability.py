from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
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
cli_auth_credentials_store = "file"

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
    schema_files = {
        str(path.relative_to(schema)): path.read_bytes()
        for path in schema.rglob("*")
        if path.is_file()
    }
    source = f"""#!/usr/bin/python3
import os
import shutil
import signal
import sys
import time
from pathlib import Path

def _write_schema(destination):
    for name, content in {schema_files!r}.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("codex-cli {version}")
    raise SystemExit(0)
if arguments == ["features", "list"]:
    print({feature_lines!r})
    raise SystemExit(0)
if arguments[:2] == ["app-server", "generate-json-schema"]:
    destination = Path(arguments[arguments.index("--out") + 1])
    _write_schema(destination)
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
    assert capability.supported_context_operations == ("dependency-closed-removal", "append-text")
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


def test_isolation_probe_reports_unavailable_host_temporary_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "probe-root"
    root.mkdir()
    monkeypatch.setattr(capability, "_ISOLATION_HOST_PARENT", tmp_path / "missing")

    with pytest.raises(
        CodexCapabilityError,
        match="requires writable /var/tmp temporary storage",
    ) as refused:
        _REAL_ISOLATION_PROBE(_runtime(Path("/bin/true")), root)

    assert refused.value.code is CodexCapabilityErrorCode.UNAVAILABLE
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


def test_every_structural_probe_denies_host_writes_home_reads_and_network(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "fixture-schema"
    shutil.copytree(SCHEMA_FIXTURE, schema)
    executable = _fake_codex(tmp_path, schema)
    hidden = tmp_path / "private-host-home"
    hidden.write_text("private-host-content", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        adversarial = f"""
import socket
assert os.environ['ITHRL_PROVIDER_SECRET_0'] == 'ithrl-capability-probe'
assert not Path({str(hidden)!r}).exists()
for forbidden in (
    Path(sys.argv[0]), Path('/etc/ithrl-unexpected-write'),
    Path('/tmp/unbounded-scratch'), Path('/run/unbounded-scratch'),
    Path('/home/unbounded-scratch'), Path('/root/unbounded-scratch'),
):
    try:
        forbidden.write_bytes(b'unsafe')
    except OSError:
        pass
    else:
        raise SystemExit(71)
with socket.socket() as connection:
    connection.settimeout(0.2)
    try:
        connection.connect(('127.0.0.1', {port}))
    except OSError:
        pass
    else:
        raise SystemExit(72)
Path(os.environ['HOME']).joinpath('private-state-ok').write_bytes(b'ok')
Path(os.environ['TMPDIR']).joinpath('private-scratch-ok').write_bytes(b'ok')
"""
        executable.write_text(
            executable.read_text().replace(
                "arguments = sys.argv[1:]", adversarial + "\narguments = sys.argv[1:]"
            ),
            encoding="utf-8",
        )
        before = executable.read_bytes()
        profile = detect_codex_capability(_runtime(executable), _configuration())
    assert executable.read_bytes() == before
    assert hidden.read_text() == "private-host-content"
    assert "structural discovery is host-write isolated and network denied" in profile.deviations


def test_structural_discovery_refuses_unavailable_bubblewrap(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    runtime = CodexRuntime(
        codex=executable,
        bubblewrap=Path("/bin/false"),
        python=Path("/usr/bin/python3"),
    )
    with pytest.raises(CodexCapabilityError, match="did not pass") as refused:
        detect_codex_capability(runtime, _configuration())
    assert refused.value.code is CodexCapabilityErrorCode.UNAVAILABLE


@pytest.mark.parametrize("kind", ["oversized-home", "fifo"])
def test_bounds_and_validates_all_private_structural_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    actions = {
        "oversized-home": "private.write_bytes(b'x' * 200_000)",
        "fifo": "os.mkfifo(private)",
    }
    executable.write_text(
        executable.read_text().replace(
            'print("codex-cli 99.42.7")',
            "private = Path(os.environ['HOME']) / 'probe-state'\n    "
            + actions[kind]
            + '\n    print("codex-cli 99.42.7")',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(capability, "_MAX_SCHEMA_TOTAL_BYTES", 100_000)
    with pytest.raises(CodexCapabilityError) as refused:
        detect_codex_capability(_runtime(executable), _configuration())
    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert "private" not in str(refused.value)


def test_structural_discovery_accepts_native_startup_symlinks(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    startup = """alias_root = Path(os.environ['CODEX_HOME']) / 'tmp/arg0/codex-arg0aB12cD'
alias_root.mkdir(parents=True, exist_ok=True)
for name in ('apply_patch', 'applypatch', 'codex-linux-sandbox', 'codex-execve-wrapper'):
    alias = alias_root / name
    if not alias.is_symlink():
        alias.symlink_to('/tmp/ithrl-state/runner/codex')
"""
    executable.write_text(
        executable.read_text().replace(
            "arguments = sys.argv[1:]", startup + "\narguments = sys.argv[1:]"
        ),
        encoding="utf-8",
    )
    profile = detect_codex_capability(_runtime(executable), _configuration())
    assert profile.runtime_harness_version == "99.42.7"


@pytest.mark.parametrize(
    ("relative_path", "target"),
    [
        ("home/arbitrary-directory/unknown-helper", "/etc/passwd"),
        ("codex-home/cache/new-helper", "../../../runner/codex"),
        ("tmp/dangling-link", "missing-target"),
    ],
)
def test_scratch_symlinks_are_opaque_but_schema_symlinks_are_refused(
    tmp_path: Path, relative_path: str, target: str
) -> None:
    alias = tmp_path / relative_path
    alias.parent.mkdir(parents=True)
    alias.symlink_to(target)
    capability._require_bounded_discovery_state(tmp_path)
    with pytest.raises(CodexCapabilityError, match="symbolic link") as refused:
        capability._require_bounded_schema(tmp_path)
    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert relative_path not in str(refused.value)
    assert target not in str(refused.value)


@pytest.mark.parametrize("limit", ["bytes", "entries"])
def test_symlinks_count_toward_discovery_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    alias = tmp_path / "link"
    target = "unread-dangling-target"
    alias.symlink_to(target)
    if limit == "bytes":
        monkeypatch.setattr(capability, "_MAX_SCHEMA_TOTAL_BYTES", len(target.encode()) - 1)
    else:
        monkeypatch.setattr(capability, "_MAX_SCHEMA_ENTRIES", 0)
    with pytest.raises(CodexCapabilityError, match="exceeds"):
        capability._require_bounded_discovery_state(tmp_path)


def test_scratch_directory_symlinks_are_neither_read_nor_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "too-large-if-followed").write_bytes(b"x" * 1000)
    root = tmp_path / "state"
    root.mkdir()
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(capability, "_MAX_SCHEMA_TOTAL_BYTES", 500)
    monkeypatch.setattr(capability, "_MAX_SCHEMA_ENTRIES", 1)

    def denied_readlink(path: Path) -> str:
        raise AssertionError("scratch links must remain opaque")

    monkeypatch.setattr(capability.os, "readlink", denied_readlink)
    capability._require_bounded_discovery_state(root)
    with pytest.raises(CodexCapabilityError, match="symbolic link"):
        capability._require_bounded_schema(root)


@pytest.mark.parametrize("tree_kind", ["schema", "discovery"])
@pytest.mark.parametrize("root_kind", ["file", "directory-link", "file-link"])
def test_validation_root_must_be_a_real_directory(
    tmp_path: Path, tree_kind: str, root_kind: str
) -> None:
    root = tmp_path / "root"
    if root_kind == "file":
        root.write_bytes(b"private")
    else:
        target = tmp_path / "target"
        if root_kind == "directory-link":
            target.mkdir()
        else:
            target.write_bytes(b"private")
        root.symlink_to(target, target_is_directory=root_kind == "directory-link")
    validate = (
        capability._require_bounded_schema
        if tree_kind == "schema"
        else capability._require_bounded_discovery_state
    )
    with pytest.raises(CodexCapabilityError, match="root is not a directory"):
        validate(root)


def test_native_schema_root_symlink_is_refused_before_schema_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    executable.write_text(
        executable.read_text().replace(
            "_write_schema(destination)\n    raise SystemExit(0)",
            "elsewhere = Path(os.environ['HOME']) / 'schema-copy'\n"
            "    _write_schema(elsewhere)\n"
            "    destination.rmdir()\n"
            "    destination.symlink_to(elsewhere, target_is_directory=True)\n"
            "    raise SystemExit(0)",
        ),
        encoding="utf-8",
    )

    def must_not_read_schema(root: Path) -> str:
        raise AssertionError("a redirected schema root must not be read")

    monkeypatch.setattr(capability, "verify_schema_bundle", must_not_read_schema)
    with pytest.raises(CodexCapabilityError, match="root is not a directory"):
        detect_codex_capability(_runtime(executable), _configuration())


@pytest.mark.parametrize("tree_kind", ["schema", "discovery"])
def test_private_state_walk_errors_refuse_without_exposing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tree_kind: str,
) -> None:
    def inaccessible_walk(
        root: Path,
        *,
        top_down: bool,
        on_error: Callable[[OSError], object] | None,
        follow_symlinks: bool,
    ) -> Iterator[tuple[Path, list[str], list[str]]]:
        assert top_down
        assert not follow_symlinks
        yield root, [], []
        assert on_error is not None
        on_error(PermissionError("private inaccessible state"))

    monkeypatch.setattr(Path, "walk", inaccessible_walk)
    validate = (
        capability._require_bounded_schema
        if tree_kind == "schema"
        else capability._require_bounded_discovery_state
    )
    with pytest.raises(CodexCapabilityError, match="could not be inspected") as refused:
        validate(tmp_path)
    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert isinstance(refused.value.__cause__, PermissionError)
    assert "private inaccessible" not in str(refused.value)


def test_unreadable_native_state_refuses_and_cleans_disposable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission_check = tmp_path / "permission-check"
    permission_check.mkdir(mode=0o700)
    permission_check.chmod(0)
    try:
        try:
            with os.scandir(permission_check):
                pass
        except PermissionError:
            pass
        else:
            pytest.skip("process privileges bypass directory read permissions")
    finally:
        permission_check.chmod(0o700)
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    executable.write_text(
        executable.read_text().replace(
            'print("codex-cli 99.42.7")',
            "private = Path(os.environ['HOME']) / 'unreadable-state'\n"
            "    private.mkdir()\n"
            "    (private / 'native-output').write_bytes(b'private')\n"
            "    private.chmod(0)\n"
            '    print("codex-cli 99.42.7")',
        ),
        encoding="utf-8",
    )
    roots: list[Path] = []
    original_run_probe = capability._run_probe

    def track_probe(
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        cwd: Path,
        *,
        failure_code: CodexCapabilityErrorCode = CodexCapabilityErrorCode.UNAVAILABLE,
        state_root: Path | None = None,
        inherited_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        assert state_root is not None
        roots.append(state_root)
        return original_run_probe(
            argv,
            environment,
            cwd,
            failure_code=failure_code,
            state_root=state_root,
            inherited_fds=inherited_fds,
        )

    monkeypatch.setattr(capability, "_run_probe", track_probe)
    with pytest.raises(CodexCapabilityError, match="could not be inspected") as refused:
        detect_codex_capability(_runtime(executable), _configuration())
    assert refused.value.code is CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL
    assert roots
    assert all(not root.exists() for root in roots)


def test_structural_timeout_stops_namespace_and_cleans_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_codex(tmp_path, SCHEMA_FIXTURE)
    executable.write_text(
        executable.read_text().replace('print("codex-cli 99.42.7")', "time.sleep(60)"),
        encoding="utf-8",
    )
    monkeypatch.setattr(capability, "_PROBE_TIMEOUT_SECONDS", 0.1)
    processes: list[subprocess.Popen[bytes]] = []
    roots: list[Path] = []
    original_popen = subprocess.Popen

    def capture_process(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        roots.append(Path(str(kwargs["cwd"])).parent)
        return process

    monkeypatch.setattr(capability.subprocess, "Popen", capture_process)
    with pytest.raises(CodexCapabilityError, match="did not pass"):
        detect_codex_capability(_runtime(executable), _configuration())
    assert processes
    assert all(process.poll() is not None for process in processes)
    assert all(not root.exists() for root in roots)


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
        "    _write_schema(destination)",
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
            capability._run_probe((str(executable), "--version"), {}, tmp_path)
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
