from __future__ import annotations

import os
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

import pytest

import ithrl.runner.codex.isolation as isolation
from ithrl.runner.codex.configuration import CodexProviderConfiguration
from ithrl.runner.codex.isolation import (
    CodexIsolationError,
    CodexRuntime,
    _bubblewrap_boundary,
    prepare_codex_process,
    program_digest,
)
from ithrl.runner.runtime import OwnedAttemptState

ATTEMPT_ID = "attempt-00000000000000000000000000000001"


def _configuration(
    *,
    auth_data: bytes | None = None,
    environment: dict[str, str] | None = None,
) -> CodexProviderConfiguration:
    selected = environment or {}
    return CodexProviderConfiguration(
        source_provider_id="fixture-provider",
        provider_id="ithrl",
        endpoint="https://example.invalid/v1/responses",
        authentication="custom-environment",
        environment_names=tuple(sorted(selected)),
        deviations=("provider-request-retries-disabled",),
        rendered_config=b'model_provider = "ithrl"\n',
        environment=MappingProxyType(selected),
        auth_data=auth_data,
    )


def _program(path: Path, text: str = "pass\n") -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return path


def test_prepares_private_worker_state_and_minimal_environment(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "state", ATTEMPT_ID)
    worker = _program(tmp_path / "worker.py")
    protocol = _program(tmp_path / "protocol.py")
    contracts = _program(tmp_path / "protocol_contracts.py")
    executable = Path("/bin/true")

    spec = prepare_codex_process(
        state,
        runtime=CodexRuntime(
            codex=executable,
            bubblewrap=executable,
            python=executable,
        ),
        configuration=_configuration(
            auth_data=b'{"token":"private-token"}\n',
            environment={
                "ITHRL_PROVIDER_SECRET_0": "private-key",
                "HTTPS_PROXY": "http://private-proxy.invalid",
            },
        ),
        request={"private_prompt": "do not expose"},
        worker_source=worker,
        worker_digest=program_digest(worker),
        support_programs=(
            (protocol, program_digest(protocol)),
            (contracts, program_digest(contracts)),
        ),
    )

    assert spec.cwd == state.root / "workspace"
    assert spec.environment["HOME"] == "/tmp/ithrl-state/home"  # noqa: S108
    assert spec.environment["CODEX_HOME"] == "/tmp/ithrl-state/codex-home"  # noqa: S108
    assert spec.environment["ITHRL_PROVIDER_SECRET_0"] == "private-key"
    assert "private-key" not in " ".join(spec.argv)
    assert "private-token" not in " ".join(spec.argv)
    assert "do not expose" not in " ".join(spec.argv)
    assert "--tmpfs" in spec.argv
    assert "/home" in spec.argv
    assert "/tmp/ithrl-state/runner/codex" in spec.argv  # noqa: S108
    assert stat.S_IMODE((state.root / "request.json").stat().st_mode) == 0o600
    assert (state.root / "codex-home" / "config.toml").read_bytes() == (
        b'model_provider = "ithrl"\n'
    )
    assert not (state.root / "codex-home" / "auth.json").exists()
    assert len(spec.inherited_fds) == 1
    assert "--ro-bind-data" in spec.argv
    assert stat.S_IMODE((state.root / "worker.py").stat().st_mode) == 0o500
    assert stat.S_IMODE((state.root / "protocol.py").stat().st_mode) == 0o500
    assert stat.S_IMODE((state.root / "protocol_contracts.py").stat().st_mode) == 0o500
    assert "private-token" not in repr(spec)
    assert "private-key" not in repr(spec)
    for descriptor in spec.inherited_fds:
        os.close(descriptor)


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="requires Linux memfd")
def test_auth_memory_file_avoids_standard_stream_descriptors() -> None:
    script = """
import os
from ithrl.runner.codex.isolation import _memory_file

os.close(0)
descriptor = _memory_file(b"fixture-auth")
assert descriptor is not None and descriptor >= 3
assert os.read(descriptor, 12) == b"fixture-auth"
os.close(descriptor)
"""

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and test source
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_configuration_rejects_undeclared_environment_fields() -> None:
    with pytest.raises(ValueError, match="environment does not match"):
        CodexProviderConfiguration(
            source_provider_id="fixture-provider",
            provider_id="ithrl",
            endpoint="https://example.invalid/v1/responses",
            authentication="custom-environment",
            environment_names=(),
            deviations=(),
            rendered_config=b'model_provider = "ithrl"\n',
            environment=MappingProxyType({"UNRELATED_SECRET": "private"}),
        )


def test_configuration_rejects_caller_selected_child_environment_name() -> None:
    with pytest.raises(ValueError, match="unsafe name"):
        _configuration(environment={"LD_PRELOAD": "private"})


def test_current_managed_python_is_exposed_read_only_without_host_home(
    tmp_path: Path,
) -> None:
    runtime = CodexRuntime(
        codex=Path("/bin/true"),
        bubblewrap=Path("/usr/bin/bwrap"),
        python=Path(sys.executable),
    )
    root = tmp_path / "state"
    root.mkdir()
    (root / "runner").mkdir()
    hidden = tmp_path / "hidden-home-marker"
    hidden.write_text("private", encoding="utf-8")
    script = (
        "import json,encodings,sys; from pathlib import Path; "
        f"assert not Path({str(hidden)!r}).exists(); "
        "assert Path(sys.executable).is_file(); "
        "Path('/tmp/ithrl-state/proof').write_text('ok')"
    )
    completed = subprocess.run(  # noqa: S603 - fixed test interpreter and synthetic code
        (*_bubblewrap_boundary(runtime, state=root), str(runtime.python), "-c", script),
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert (root / "proof").read_text() == "ok"
    assert hidden.read_text() == "private"
    if runtime._python_prefix is not None:
        assert str(runtime._python_prefix) in completed.args
        assert sysconfig.get_path("stdlib").startswith(str(runtime._python_prefix))


def test_refuses_unproven_python_installation_under_a_hidden_home(tmp_path: Path) -> None:
    pretend_python = _program(tmp_path / "python")
    with pytest.raises(CodexIsolationError, match="cannot be safely exposed"):
        CodexRuntime(
            codex=Path("/bin/true"),
            bubblewrap=Path("/usr/bin/bwrap"),
            python=pretend_python,
        )


def test_refuses_interpreter_prefix_overlapping_disposable_state(tmp_path: Path) -> None:
    runtime = CodexRuntime(
        codex=Path("/bin/true"),
        bubblewrap=Path("/usr/bin/bwrap"),
        python=Path(sys.executable),
    )
    if runtime._python_prefix is None:
        pytest.skip("current interpreter is outside masked directories")
    with pytest.raises(CodexIsolationError, match="overlaps disposable state"):
        _bubblewrap_boundary(runtime, state=runtime._python_prefix / "state")


def test_system_python_needs_no_hidden_runtime_mount() -> None:
    runtime = CodexRuntime(
        codex=Path("/bin/true"),
        bubblewrap=Path("/usr/bin/bwrap"),
        python=Path("/usr/bin/python3"),
    )
    assert runtime._python_prefix is None


@pytest.mark.parametrize("parent_kind", ["temporary", "checkout"])
@pytest.mark.parametrize("venv_symlink", [False, True])
def test_proves_current_runtime_layout_independent_of_installed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_kind: str,
    venv_symlink: bool,
) -> None:
    parent = tmp_path if parent_kind == "temporary" else Path.cwd()
    with TemporaryDirectory(prefix=".ithrl-test-runtime-", dir=parent) as temporary:
        prefix = Path(temporary)
        if not prefix.is_relative_to("/tmp") and not prefix.is_relative_to("/home"):  # noqa: S108
            pytest.skip("checkout is outside the masked runtime roots")
        (prefix / "bin").mkdir()
        python = _program(prefix / "bin" / "python")
        stdlib = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        (stdlib / "encodings").mkdir(parents=True)
        (stdlib / "encodings" / "__init__.py").write_text("", encoding="utf-8")
        selected = python
        if venv_symlink:
            (prefix / "venv" / "bin").mkdir(parents=True)
            selected = prefix / "venv" / "bin" / "python"
            selected.symlink_to(python)
        monkeypatch.setattr(isolation.sys, "executable", str(selected))
        monkeypatch.setattr(isolation.sys, "base_prefix", str(prefix))

        def stdlib_path(_name: str) -> str:
            return str(stdlib)

        monkeypatch.setattr(isolation.sysconfig, "get_path", stdlib_path)

        runtime = CodexRuntime(
            codex=Path("/bin/true"),
            bubblewrap=Path("/usr/bin/bwrap"),
            python=selected,
        )
        assert runtime.python == python
        assert runtime._python_prefix == prefix
        runtime.assert_unchanged()


@pytest.mark.parametrize("prefix", [Path("/tmp"), Path("/home/fixture-account"), Path.home()])  # noqa: S108
def test_refuses_broad_runtime_prefixes(
    monkeypatch: pytest.MonkeyPatch,
    prefix: Path,
) -> None:
    python = prefix / "bin" / "python"
    stdlib = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    monkeypatch.setattr(isolation.sys, "executable", str(python))
    monkeypatch.setattr(isolation.sys, "base_prefix", str(prefix))

    def stdlib_path(_name: str) -> str:
        return str(stdlib)

    def resolved(self: Path, strict: bool = False) -> Path:
        del strict
        return self

    def existing_file(_self: Path) -> bool:
        return True

    monkeypatch.setattr(isolation.sysconfig, "get_path", stdlib_path)
    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(Path, "is_file", existing_file)

    with pytest.raises(CodexIsolationError, match="cannot be safely exposed"):
        isolation._python_runtime_prefix(python)


def test_refuses_stdlib_encodings_symlink_escaping_exact_runtime_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "runtime"
    (prefix / "bin").mkdir(parents=True)
    python = _program(prefix / "bin" / "python")
    stdlib = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    (stdlib / "encodings").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    (stdlib / "encodings" / "__init__.py").symlink_to(outside)
    monkeypatch.setattr(isolation.sys, "executable", str(python))
    monkeypatch.setattr(isolation.sys, "base_prefix", str(prefix))

    def stdlib_path(_name: str) -> str:
        return str(stdlib)

    monkeypatch.setattr(isolation.sysconfig, "get_path", stdlib_path)

    with pytest.raises(CodexIsolationError, match="cannot be safely exposed"):
        isolation._python_runtime_prefix(python)
