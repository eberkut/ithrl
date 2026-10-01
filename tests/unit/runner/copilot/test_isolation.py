from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ithrl.runner.copilot.configuration import (
    CopilotConfiguration,
    resolve_copilot_configuration,
)
from ithrl.runner.copilot.isolation import (
    CopilotIsolationError,
    CopilotRuntime,
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
        semantic_session=b'{"type":"session"}\n',
        trigger="private trigger",
        model="fixture-model",
        target=target,
        worker_source=worker,
        worker_digest=program_digest(worker),
    )

    request = json.loads((state.root / "request.json").read_bytes())
    assert request["target"] == target
    assert request["trigger"] == "private trigger"
    assert request["model"] == "fixture-model"
    assert request["commandFlags"] == [
        *target_policy(target).command_flags,
        "--secret-env-vars=GH_TOKEN",
    ]
    assert (spec.stop_after_stdout_line is not None) is (target == "shell-action")
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
    assert (state.root / "session.jsonl").read_bytes() == b'{"type":"session"}\n'
    assert stat.S_IMODE((state.root / "request.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state.root / "session.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((state.root / "worker").stat().st_mode) == 0o500
    assert json.loads((state.root / "copilot-home" / "settings.json").read_bytes()) == {
        "askUser": False,
        "autoUpdate": False,
        "remote": "off",
        "remoteExport": False,
    }


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
        semantic_session=b'{"type":"session"}\n',
        trigger="private trigger",
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
            semantic_session=b'{"type":"session"}\n',
            trigger="trigger",
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
            semantic_session=b'{"type":"session"}\n',
            trigger="trigger",
            model="fixture-model",
            target="final-response",
            worker_source=worker,
            worker_digest=current,
        )


@pytest.mark.parametrize(
    ("semantic_session", "trigger", "model", "message"),
    [
        (b"", "trigger", "model", "session exceeds"),
        (b"{}\n", "", "model", "trigger exceeds"),
        (b"{}\n", "trigger", "", "model identity"),
        (b"{}\n", "trigger", "bad\0model", "model identity"),
    ],
)
def test_private_worker_input_is_bounded_and_validated(
    tmp_path: Path,
    semantic_session: bytes,
    trigger: str,
    model: str,
    message: str,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py")

    with pytest.raises(CopilotIsolationError, match=message):
        prepare_copilot_process(
            state,
            runtime=_runtime(tmp_path),
            configuration=_configuration(),
            semantic_session=semantic_session,
            trigger=trigger,
            model=model,
            target="final-response",
            worker_source=worker,
            worker_digest=program_digest(worker),
        )
