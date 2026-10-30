from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from ithrl.runner.copilot.configuration import (
    MAX_CONFIG_BYTES,
    PASSTHROUGH_ENVIRONMENT_NAMES,
    CopilotConfiguration,
    CopilotConfigurationError,
    CopilotConfigurationErrorCode,
    resolve_copilot_configuration,
)


def _private_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_authentication_environment_uses_documented_precedence() -> None:
    configuration = resolve_copilot_configuration(
        environment={
            "COPILOT_GITHUB_TOKEN": "first-private-token",
            "GH_TOKEN": "second-private-token",
            "GITHUB_TOKEN": "third-private-token",
            "UNRELATED": "unrelated-private-value",
        },
        config_file=None,
    )

    assert configuration.authentication == "copilot-github-token"
    assert configuration.environment_names == ("COPILOT_GITHUB_TOKEN",)
    assert dict(configuration.environment) == {"COPILOT_GITHUB_TOKEN": "first-private-token"}
    assert configuration.config_data is None
    rendered = repr(configuration)
    assert "first-private-token" not in rendered
    assert "second-private-token" not in rendered
    assert "third-private-token" not in rendered


@pytest.mark.parametrize(
    ("environment", "authentication", "name"),
    [
        ({"GH_TOKEN": "private"}, "gh-token", "GH_TOKEN"),
        ({"GITHUB_TOKEN": "private"}, "github-token", "GITHUB_TOKEN"),
    ],
)
def test_authentication_environment_supports_each_fallback(
    environment: dict[str, str],
    authentication: str,
    name: str,
) -> None:
    configuration = resolve_copilot_configuration(
        environment=environment,
        config_file=None,
    )

    assert configuration.authentication == authentication
    assert configuration.environment_names == (name,)


def test_forwards_only_allowlisted_connectivity_environment() -> None:
    connectivity = {
        name: f"private-{index}" for index, name in enumerate(PASSTHROUGH_ENVIRONMENT_NAMES)
    }
    configuration = resolve_copilot_configuration(
        environment={
            **connectivity,
            "GH_TOKEN": "private-token",
            "AWS_SECRET_ACCESS_KEY": "must-not-be-forwarded",
            "UNRELATED": "must-not-be-forwarded",
        },
        config_file=None,
    )

    assert dict(configuration.environment) == {**connectivity, "GH_TOKEN": "private-token"}
    assert configuration.environment_names == (
        *PASSTHROUGH_ENVIRONMENT_NAMES,
        "GH_TOKEN",
    )
    rendered = repr(configuration)
    assert "private-token" not in rendered
    assert "must-not-be-forwarded" not in rendered
    assert all(value not in rendered for value in connectivity.values())
    assert all(value not in configuration.public_digest for value in connectivity.values())


def test_managed_config_is_bounded_snapshotted_and_secret_safe(tmp_path: Path) -> None:
    config_file = _private_file(
        tmp_path / "config.json",
        b'{"oauth_token":"first-private-token","nested":{"enabled":true}}',
    )

    configuration = resolve_copilot_configuration(
        environment={},
        config_file=config_file,
    )
    config_file.write_bytes(b'{"oauth_token":"replacement-private-token"}')

    assert configuration.authentication == "managed-config"
    assert configuration.environment_names == ()
    assert configuration.config_data == (
        b'{"oauth_token":"first-private-token","nested":{"enabled":true}}'
    )
    assert "first-private-token" not in repr(configuration)
    assert "first-private-token" not in configuration.public_digest


def test_public_digest_identifies_method_not_credential_value(tmp_path: Path) -> None:
    first = resolve_copilot_configuration(
        environment={"GH_TOKEN": "first-private-token"},
        config_file=None,
    )
    second = resolve_copilot_configuration(
        environment={"GH_TOKEN": "second-private-token"},
        config_file=None,
    )
    managed_first = resolve_copilot_configuration(
        environment={},
        config_file=_private_file(tmp_path / "first.json", b'{"token":"first"}'),
    )
    managed_second = resolve_copilot_configuration(
        environment={},
        config_file=_private_file(tmp_path / "second.json", b'{"token":"second"}'),
    )

    assert first.public_digest == second.public_digest
    assert managed_first.public_digest == managed_second.public_digest
    assert first.public_digest != managed_first.public_digest


def test_isolated_settings_disable_remote_and_interactive_features() -> None:
    configuration = resolve_copilot_configuration(
        environment={"GH_TOKEN": "private"},
        config_file=None,
    )

    assert json.loads(configuration.settings_data) == {
        "askUser": False,
        "autoUpdate": False,
        "remoteSessions": False,
        "remoteExport": False,
    }


def test_missing_authentication_is_classified_without_exposing_path(tmp_path: Path) -> None:
    with pytest.raises(CopilotConfigurationError) as refused:
        resolve_copilot_configuration(environment={}, config_file=None)

    assert refused.value.code is CopilotConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE
    assert str(tmp_path) not in str(refused.value)


@pytest.mark.parametrize(
    "content",
    [
        b"not-json-private-value",
        b'{\n  // Copilot accepts comments\n  "token": "private-value"\n}\n',
        b"\xffprivate-opaque-value\n",
    ],
)
def test_managed_config_is_bounded_opaque_data(tmp_path: Path, content: bytes) -> None:
    config_file = _private_file(tmp_path / "private-config.json", content)

    configuration = resolve_copilot_configuration(environment={}, config_file=config_file)

    assert configuration.config_data == content
    assert "private-value" not in repr(configuration)
    assert "private-opaque-value" not in repr(configuration)
    assert "private-value" not in configuration.public_digest


def test_oversized_and_unsafe_managed_config_are_rejected(tmp_path: Path) -> None:
    empty = _private_file(tmp_path / "empty.json", b"")
    oversized = _private_file(tmp_path / "oversized.json", b" " * (MAX_CONFIG_BYTES + 1))
    target = _private_file(tmp_path / "target.json", b"{}")
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(target)

    for config_file in (empty, oversized, symlink):
        with pytest.raises(CopilotConfigurationError) as refused:
            resolve_copilot_configuration(environment={}, config_file=config_file)

        assert refused.value.code is CopilotConfigurationErrorCode.MALFORMED_AUTHENTICATION
        assert str(config_file) not in str(refused.value)


def test_configuration_rejects_inconsistent_private_sources() -> None:
    with pytest.raises(ValueError, match="exactly one private source"):
        CopilotConfiguration(
            authentication="gh-token",
            environment_names=("GH_TOKEN",),
            environment=MappingProxyType({"GH_TOKEN": "private"}),
            config_data=b"{}",
        )
