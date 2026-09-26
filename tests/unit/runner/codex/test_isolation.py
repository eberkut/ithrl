from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from ithrl.runner.codex.configuration import CodexProviderConfiguration
from ithrl.runner.codex.isolation import (
    CodexRuntime,
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
