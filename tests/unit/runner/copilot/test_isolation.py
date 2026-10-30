from __future__ import annotations

import json
import os
import py_compile
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ithrl.runner.copilot.configuration import (
    MAX_CONFIG_BYTES,
    CopilotConfiguration,
    resolve_copilot_configuration,
)
from ithrl.runner.copilot.isolation import (
    CopilotIsolationError,
    CopilotRuntime,
    _bubblewrap_argv,
    _discovery_argv,
    prepare_copilot_process,
    program_digest,
)
from ithrl.runner.copilot.policy import CopilotTarget, target_policy
from ithrl.runner.runtime import OwnedAttemptState

ATTEMPT_ID = "attempt-00000000000000000000000000000001"


def test_discovery_uses_same_filesystem_boundary_with_network_denied(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    argv = _discovery_argv(runtime, state=tmp_path / "state", arguments=("--help",))

    assert "--unshare-net" in argv
    assert "--unshare-pid" in argv
    assert "--new-session" in argv
    assert argv[argv.index("--ro-bind") + 1 : argv.index("--ro-bind") + 3] == ("/", "/")
    assert argv[-2:] == ("/tmp/ithrl-state/runner/copilot", "--help")  # noqa: S108
    assert argv.count(str(runtime.copilot)) == 1
    assert argv.count("--remount-ro") == 4


def _program(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def _runtime(tmp_path: Path) -> CopilotRuntime:
    return CopilotRuntime(
        copilot=_program(tmp_path / "copilot"),
        bubblewrap=_program(tmp_path / "bwrap"),
    )


def _configuration() -> CopilotConfiguration:
    return resolve_copilot_configuration(
        environment={"GH_TOKEN": "private-token"},
        config_file=None,
    )


@pytest.mark.parametrize("target", ["final-response", "shell-action"])
def test_prepares_private_target_specific_worker_state(
    tmp_path: Path,
    target: CopilotTarget,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py", "#!/usr/bin/python3\n")
    configuration = _configuration()

    spec = prepare_copilot_process(
        state,
        runtime=_runtime(tmp_path),
        configuration=configuration,
        native_session=b'{"type":"session"}\n',
        session_id="session-1",
        system_message="private guidance",
        model="fixture-model",
        target=target,
        worker_source=worker,
        worker_digest=program_digest(worker),
    )

    request = json.loads((state.root / "request.json").read_bytes())
    assert request["target"] == target
    assert request["sessionId"] == "session-1"
    assert request["systemMessage"] == "private guidance"
    assert request["model"] == "fixture-model"
    assert request["commandFlags"] == [
        *target_policy(target).command_flags,
        "--secret-env-vars=GH_TOKEN",
    ]
    assert spec.stop_after_stdout_line is None
    assert spec.argv[-1] == "/tmp/ithrl-state/worker"  # noqa: S108
    assert spec.cwd == state.root / "workspace"
    assert spec.environment["GH_TOKEN"] == "private-token"
    assert spec.environment["COPILOT_HOME"] == "/tmp/ithrl-state/copilot-home"  # noqa: S108
    assert spec.environment["COPILOT_CACHE_HOME"] == (
        "/tmp/ithrl-state/copilot-cache"  # noqa: S108
    )
    assert spec.environment["HOME"] == "/tmp/ithrl-state/home"  # noqa: S108
    assert "--tmpfs" in spec.argv
    assert "--unshare-net" not in spec.argv
    assert "--remount-ro" not in spec.argv
    assert "/home" in spec.argv
    assert "/tmp" in spec.argv  # noqa: S108
    assert "/run" in spec.argv
    assert "/tmp/ithrl-state/runner/copilot" in spec.argv  # noqa: S108
    assert "private-token" not in " ".join(spec.argv)
    assert "private trigger" not in " ".join(spec.argv)
    assert "private-token" not in repr(spec)
    assert "private trigger" not in repr(spec)
    assert spec.argv[spec.argv.index("-X") + 1] == (
        "pycache_prefix=/tmp/ithrl-state/runner/bytecode-cache"
    )
    assert (
        state.root / "copilot-home" / "session-state" / "session-1" / "events.jsonl"
    ).read_bytes() == b'{"type":"session"}\n'
    assert stat.S_IMODE((state.root / "request.json").stat().st_mode) == 0o600
    assert (
        stat.S_IMODE(
            (state.root / "copilot-home" / "session-state" / "session-1" / "events.jsonl")
            .stat()
            .st_mode
        )
        == 0o600
    )
    assert stat.S_IMODE((state.root / "worker").stat().st_mode) == 0o500
    assert "SSL_CERT_FILE" not in spec.environment
    assert "NODE_EXTRA_CA_CERTS" not in spec.environment
    assert not list(state.root.glob("*.pem"))
    assert json.loads((state.root / "copilot-home" / "settings.json").read_bytes()) == {
        "askUser": False,
        "autoUpdate": False,
        "remoteSessions": False,
        "remoteExport": False,
    }


def test_worker_ignores_unfingerprinted_installed_bytecode(tmp_path: Path) -> None:
    module = tmp_path / "sdk_import.py"
    module.write_text("value = 2\n")
    metadata = module.stat()
    py_compile.compile(str(module), doraise=True)
    module.write_text("value = 1\n")
    os.utime(module, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    argv = _bubblewrap_argv(_runtime(tmp_path), state=tmp_path, worker="worker")
    setting = argv[argv.index("-X") + 1].replace("/tmp/ithrl-state", str(tmp_path))  # noqa: S108
    command = [
        sys.executable,
        "-S",
        "-B",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import sdk_import; print(sdk_import.value)",
        str(tmp_path),
    ]
    cached = subprocess.run(  # noqa: S603 - selected interpreter and fixed synthetic command
        command, check=True, capture_output=True, text=True
    )
    protected = subprocess.run(  # noqa: S603 - selected interpreter and fixed synthetic command
        [*command[:1], "-X", setting, *command[1:]],
        check=True,
        capture_output=True,
        text=True,
    )
    assert cached.stdout.strip() == "2"
    assert protected.stdout.strip() == "1"
    assert module.read_text() == "value = 1\n"
    assert not (tmp_path / "runner" / "bytecode-cache").exists()


@pytest.mark.parametrize(
    "names",
    [("SSL_CERT_FILE",), ("NODE_EXTRA_CA_CERTS",), ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")],
)
@pytest.mark.parametrize("symlink", [False, True])
def test_configured_ca_files_are_snapshotted_in_private_attempt_state(
    tmp_path: Path,
    names: tuple[str, ...],
    symlink: bool,
) -> None:
    environment = {
        "GH_TOKEN": "private-token",
        "HTTPS_PROXY": "http://fixture-proxy.invalid:8080",
        "no_proxy": "fixture.invalid",
        "SSL_CERT_DIR": "/etc/ssl/certs",
    }
    contents = {}
    sources = {}
    for name in names:
        content = b"-----BEGIN CERTIFICATE-----\n" + name.encode() + b"\n"
        source = tmp_path / f"{name}.pem"
        source.write_bytes(content)
        source.chmod(0o644)
        selected = tmp_path / f"{name}-link.pem" if symlink else source
        if symlink:
            selected.symlink_to(source)
        environment[name] = str(selected)
        contents[name] = content
        sources[name] = source
    configuration = resolve_copilot_configuration(environment=environment, config_file=None)
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py")

    spec = prepare_copilot_process(
        state,
        runtime=_runtime(tmp_path),
        configuration=configuration,
        native_session=b'{"type":"session"}\n',
        session_id="session-1",
        system_message="private guidance",
        model="fixture-model",
        target="final-response",
        worker_source=worker,
        worker_digest=program_digest(worker),
    )

    filenames = {
        "SSL_CERT_FILE": "ssl-cert-file.pem",
        "NODE_EXTRA_CA_CERTS": "node-extra-ca-certs.pem",
    }
    for name in names:
        copied = state.root / filenames[name]
        assert copied.read_bytes() == contents[name]
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert spec.environment[name] == f"/tmp/ithrl-state/{filenames[name]}"  # noqa: S108
        assert configuration.environment[name] == environment[name]
        assert sources[name].read_bytes() == contents[name]
        assert stat.S_IMODE(sources[name].stat().st_mode) == 0o644
        assert str(sources[name]) not in " ".join(spec.argv)
        assert str(sources[name]) not in repr(spec)
    for name in ("HTTPS_PROXY", "no_proxy", "SSL_CERT_DIR"):
        assert spec.environment[name] == environment[name]
    assert stat.S_IMODE(state.root.stat().st_mode) == 0o700
    result = state.cleanup()
    assert result.residual_bytes == result.residual_entries == 0
    assert not state.root.exists()
    assert all(source.exists() for source in sources.values())


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"])
@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "oversized", "fifo", "loop"])
def test_configured_ca_file_refusals_are_bounded_and_content_safe(
    tmp_path: Path,
    name: str,
    kind: str,
) -> None:
    source = tmp_path / "private-ca-bundle"
    if kind == "directory":
        source.mkdir()
    elif kind == "empty":
        source.touch()
    elif kind == "oversized":
        with source.open("wb") as stream:
            stream.truncate(MAX_CONFIG_BYTES + 1)
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "loop":
        source.symlink_to(source)
    configuration = resolve_copilot_configuration(
        environment={"GH_TOKEN": "private-token", name: str(source)},
        config_file=None,
    )
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py")

    with pytest.raises(CopilotIsolationError, match=r"cannot snapshot.*CA bundle") as caught:
        prepare_copilot_process(
            state,
            runtime=_runtime(tmp_path),
            configuration=configuration,
            native_session=b'{"type":"session"}\n',
            session_id="session-1",
            system_message="private guidance",
            model="fixture-model",
            target="final-response",
            worker_source=worker,
            worker_digest=program_digest(worker),
        )

    assert str(source) not in str(caught.value)
    assert "private-token" not in str(caught.value)
    assert not list(state.root.glob("*.pem"))
    assert state.cleanup().residual_bytes == 0


def test_managed_config_is_written_only_inside_private_copilot_home(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_data = b'{\n// retained comment\n"oauth_token":"private-token"\n}\n'
    config_file.write_bytes(config_data)
    config_file.chmod(0o600)
    configuration = resolve_copilot_configuration(environment={}, config_file=config_file)
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py")

    spec = prepare_copilot_process(
        state,
        runtime=_runtime(tmp_path),
        configuration=configuration,
        native_session=b'{"type":"session"}\n',
        session_id="session-1",
        system_message="private guidance",
        model="fixture-model",
        target="final-response",
        worker_source=worker,
        worker_digest=program_digest(worker),
    )

    copied = state.root / "copilot-home" / "config.json"
    assert copied.read_bytes() == config_data
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert "private-token" not in repr(spec)
    assert "private-token" not in " ".join(spec.argv)


def test_runtime_identity_detects_executable_mutation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    runtime.copilot.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    runtime.copilot.chmod(0o700)

    with pytest.raises(CopilotIsolationError, match="executable changed"):
        runtime.assert_unchanged()


def test_runtime_rejects_relative_traversal_and_non_executable_paths(tmp_path: Path) -> None:
    executable = _program(tmp_path / "copilot")
    non_executable = tmp_path / "bwrap"
    non_executable.write_text("fixture", encoding="utf-8")
    non_executable.chmod(0o600)

    with pytest.raises(ValueError, match="must be absolute"):
        CopilotRuntime(copilot=Path("../copilot"), bubblewrap=executable)
    with pytest.raises(CopilotIsolationError, match="unavailable"):
        CopilotRuntime(copilot=executable, bubblewrap=non_executable)


def test_preparation_refuses_changed_worker_and_unsafe_owned_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    worker = _program(tmp_path / "worker.py")
    expected = program_digest(worker)
    worker.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    worker.chmod(0o700)
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)

    with pytest.raises(CopilotIsolationError, match="worker source changed"):
        prepare_copilot_process(
            state,
            runtime=runtime,
            configuration=_configuration(),
            native_session=b'{"type":"session"}\n',
            session_id="session-1",
            system_message=None,
            model="fixture-model",
            target="final-response",
            worker_source=worker,
            worker_digest=expected,
        )

    unsafe_state = OwnedAttemptState.create(
        tmp_path / "other-state",
        "attempt-00000000000000000000000000000002",
    )
    (unsafe_state.root / "workspace").symlink_to(tmp_path)
    current = program_digest(worker)

    with pytest.raises(CopilotIsolationError, match="cannot prepare private"):
        prepare_copilot_process(
            unsafe_state,
            runtime=runtime,
            configuration=_configuration(),
            native_session=b'{"type":"session"}\n',
            session_id="session-1",
            system_message=None,
            model="fixture-model",
            target="final-response",
            worker_source=worker,
            worker_digest=current,
        )
