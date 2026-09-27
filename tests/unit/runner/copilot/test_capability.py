from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

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
from ithrl.runner.copilot.policy import CopilotTarget, target_policy

_IMPORT_OPTIONS = frozenset({"--dry-run", "--output", "--working-directory"})
_REAL_ISOLATION_PROBE = capability._probe_isolation_boundary


@pytest.fixture(autouse=True)
def _stub_isolation_gate_for_structural_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def already_verified(_runtime: CopilotRuntime, _root: Path) -> None:
        return None

    monkeypatch.setattr(capability, "_probe_isolation_boundary", already_verified)


def _required_root_options(
    target: CopilotTarget,
    configuration: CopilotConfiguration,
) -> frozenset[str]:
    required = {argument.partition("=")[0] for argument in target_policy(target).command_flags}
    required.update({"--model", "--resume"})
    if configuration.environment_names:
        required.add("--secret-env-vars")
    return frozenset(required)


def _all_root_options() -> frozenset[str]:
    options = {"--model", "--resume", "--secret-env-vars"}
    for target in ("final-response", "shell-action"):
        options.update(
            argument.partition("=")[0] for argument in target_policy(target).command_flags
        )
    return frozenset(options)


def _fake_copilot(
    tmp_path: Path,
    *,
    version: str = "99.42.7",
    version_trailer: str = "",
    version_notice: bool = False,
    root_options: frozenset[str] | None = None,
    import_options: frozenset[str] = _IMPORT_OPTIONS,
    permission_contract: bool = True,
    import_ok: bool = True,
    fail_with: str | None = None,
) -> Path:
    executable = tmp_path / "copilot"
    permission_help = (
        "--available-tools disables all other tools. Denial rules always take precedence."
        if permission_contract
        else "private unsupported permission behavior"
    )
    advertised_root_options = root_options or _all_root_options()
    source = f"""#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path

if any(name in os.environ for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
    print("credential leaked to structural probe", file=sys.stderr)
    raise SystemExit(91)
settings = json.loads((Path(os.environ["COPILOT_HOME"]) / "settings.json").read_text())
if settings != {{"askUser": False, "autoUpdate": False, "remote": "off", "remoteExport": False}}:
    raise SystemExit(92)
arguments = sys.argv[1:]
if {fail_with!r} is not None:
    print({fail_with!r}, file=sys.stderr)
    raise SystemExit(93)
if arguments == ["--version"]:
    print("GitHub Copilot CLI {version}{version_trailer}")
    if {version_notice!r}:
        print("Run 'copilot update' to check for updates.")
    raise SystemExit(0)
if arguments == ["--help"]:
    print({" ".join(sorted(advertised_root_options))!r})
    raise SystemExit(0)
if arguments == ["help", "permissions"]:
    print({permission_help!r})
    raise SystemExit(0)
if arguments == ["sessions", "import", "--help"]:
    print({" ".join(sorted(import_options))!r})
    raise SystemExit(0)
if arguments[:2] == ["sessions", "import"]:
    required = {{"--dry-run", "--output", "--working-directory"}}
    if not required <= set(arguments):
        raise SystemExit(94)
    transcript = Path(arguments[-1]).read_text()
    if '"type":"session"' not in transcript:
        raise SystemExit(95)
    print(json.dumps({{"ok": {import_ok!r}}}))
    raise SystemExit(0)
raise SystemExit(64)
"""
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _runtime(executable: Path, *, bubblewrap: Path = Path("/bin/true")) -> CopilotRuntime:
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
        ("shell-action", 2, "visible"),
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
    assert profile.protocol_version == "copilot-jsonl-v1"
    assert profile.supported_targets == (target,)
    assert profile.supported_capture_types == ("logical-session",)
    assert profile.supported_materializations == ("source-backed",)
    assert profile.supported_context_operations == ("dependency-closed-removal",)
    assert profile.reconstruction_method == "copilot-semantic-session-import-v1"
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
        "captured assignment context is not imported" in deviation
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


def test_managed_config_does_not_require_secret_environment_flag(tmp_path: Path) -> None:
    configuration = _managed_configuration(tmp_path)
    root_options = _required_root_options("final-response", configuration)
    assert "--secret-env-vars" not in root_options

    detected = detect_copilot_capability(
        _runtime(_fake_copilot(tmp_path, root_options=root_options)),
        configuration,
        target="final-response",
    )

    assert detected.configuration is configuration


@pytest.mark.parametrize(
    ("target", "missing"),
    [
        ("final-response", "--model"),
        ("final-response", "--resume"),
        ("final-response", "--secret-env-vars"),
        ("final-response", "--no-remote"),
        ("shell-action", "--deny-tool"),
    ],
)
def test_refuses_missing_target_worker_or_authentication_option(
    tmp_path: Path,
    target: CopilotTarget,
    missing: str,
) -> None:
    configuration = _configuration()
    required = _required_root_options(target, configuration)
    assert missing in required

    with pytest.raises(CopilotCapabilityError) as refused:
        detect_copilot_capability(
            _runtime(_fake_copilot(tmp_path, root_options=required - {missing})),
            configuration,
            target=target,
        )

    assert refused.value.code is CopilotCapabilityErrorCode.UNSAFE_POLICY


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing-import-option", CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL),
        ("missing-permission-contract", CopilotCapabilityErrorCode.UNSAFE_POLICY),
        ("failed-import", CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL),
    ],
)
def test_refuses_missing_noninteractive_contracts(
    tmp_path: Path,
    case: str,
    expected_code: CopilotCapabilityErrorCode,
) -> None:
    if case == "missing-import-option":
        fake = _fake_copilot(tmp_path, import_options=_IMPORT_OPTIONS - {"--dry-run"})
    elif case == "missing-permission-contract":
        fake = _fake_copilot(tmp_path, permission_contract=False)
    else:
        fake = _fake_copilot(tmp_path, import_ok=False)

    with pytest.raises(CopilotCapabilityError) as refused:
        detect_copilot_capability(
            _runtime(fake),
            _configuration(),
            target="final-response",
        )

    assert refused.value.code is expected_code
    assert "private unsupported" not in str(refused.value)


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
