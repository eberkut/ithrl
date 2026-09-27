"""Resolve private GitHub Copilot authentication for an isolated attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from ithrl.case import CaseFormatError
from ithrl.case.filesystem import read_safe_bytes
from ithrl.context import content_digest

MAX_CONFIG_BYTES = 1024 * 1024
AUTH_ENVIRONMENT_NAMES = (
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
# Forward only connectivity settings required by the Node-based CLI. Inheriting the
# complete parent environment could expose unrelated credentials to the harness.
PASSTHROUGH_ENVIRONMENT_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)

type CopilotAuthentication = Literal[
    "copilot-github-token",
    "gh-token",
    "github-token",
    "managed-config",
]

_AUTHENTICATION_BY_ENVIRONMENT: dict[str, CopilotAuthentication] = {
    "COPILOT_GITHUB_TOKEN": "copilot-github-token",
    "GH_TOKEN": "gh-token",
    "GITHUB_TOKEN": "github-token",
}

_ISOLATED_SETTINGS = b'{"askUser":false,"autoUpdate":false,"remote":"off","remoteExport":false}'


class CopilotConfigurationErrorCode(StrEnum):
    """Stable refusal categories for private Copilot configuration."""

    AUTHENTICATION_UNAVAILABLE = "authentication-unavailable"
    MALFORMED_AUTHENTICATION = "malformed-authentication"


class CopilotConfigurationError(RuntimeError):
    """Copilot authentication cannot be isolated safely."""

    def __init__(self, code: CopilotConfigurationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CopilotConfiguration:
    """Private authentication and fixed settings for one Copilot runtime."""

    authentication: CopilotAuthentication
    environment_names: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False, compare=False)
    config_data: bytes | None = field(default=None, repr=False, compare=False)
    settings_data: bytes = field(
        default=_ISOLATED_SETTINGS,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        auth_names = _validate_environment(self.environment_names, self.environment)
        _validate_authentication(self.authentication, auth_names, self.config_data)
        _validate_settings(self.settings_data)
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @property
    def public_digest(self) -> str:
        """Return a stable identity that contains no authentication material."""

        return content_digest(
            {
                "authentication": self.authentication,
                "environment_names": list(self.environment_names),
                "isolated_settings": self.settings_data.decode("utf-8"),
            }
        )


def resolve_copilot_configuration(
    *,
    environment: Mapping[str, str],
    config_file: Path | None,
) -> CopilotConfiguration:
    """Select documented token precedence or one bounded managed config file."""

    for name in AUTH_ENVIRONMENT_NAMES:
        value = environment.get(name)
        if value:
            copied = _connectivity_environment(environment)
            copied[name] = value
            return CopilotConfiguration(
                authentication=_AUTHENTICATION_BY_ENVIRONMENT[name],
                environment_names=tuple(copied),
                environment=MappingProxyType(copied),
            )
    if config_file is None:
        raise CopilotConfigurationError(
            CopilotConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE,
            "Copilot authentication is unavailable",
        )
    connectivity = _connectivity_environment(environment)
    return CopilotConfiguration(
        authentication="managed-config",
        environment_names=tuple(connectivity),
        environment=MappingProxyType(connectivity),
        config_data=_read_managed_config(config_file),
    )


def _connectivity_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value for name in PASSTHROUGH_ENVIRONMENT_NAMES if (value := environment.get(name))
    }


def _validate_environment(
    names: tuple[str, ...],
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    if tuple(environment) != names:
        raise ValueError("Copilot environment does not match its declaration")
    if any(not value for value in environment.values()):
        raise ValueError("Copilot environment contains an empty value")
    allowed_names = set(PASSTHROUGH_ENVIRONMENT_NAMES) | set(AUTH_ENVIRONMENT_NAMES)
    if any(name not in allowed_names for name in names):
        raise ValueError("Copilot environment contains an unsupported name")
    auth_names = tuple(name for name in names if name in AUTH_ENVIRONMENT_NAMES)
    if len(auth_names) > 1:
        raise ValueError("Copilot authentication must use one credential source")
    return auth_names


def _validate_authentication(
    authentication: CopilotAuthentication,
    auth_names: tuple[str, ...],
    config_data: bytes | None,
) -> None:
    has_file = config_data is not None
    if has_file == bool(auth_names):
        raise ValueError("Copilot authentication must use exactly one private source")
    if has_file != (authentication == "managed-config"):
        raise ValueError("Copilot authentication method does not match its private source")
    if auth_names and _AUTHENTICATION_BY_ENVIRONMENT[auth_names[0]] != authentication:
        raise ValueError("Copilot authentication method does not match its private source")
    if config_data == b"":
        raise ValueError("Copilot managed configuration cannot be empty")


def _validate_settings(settings_data: bytes) -> None:
    if settings_data != _ISOLATED_SETTINGS:
        raise ValueError("Copilot isolated settings are fixed by the runner policy")


def _read_managed_config(path: Path) -> bytes:
    try:
        content = read_safe_bytes(
            path,
            max_bytes=MAX_CONFIG_BYTES,
            label="Copilot managed configuration",
        )
        if not content:
            raise ValueError
    except (CaseFormatError, ValueError) as error:
        raise CopilotConfigurationError(
            CopilotConfigurationErrorCode.MALFORMED_AUTHENTICATION,
            "Copilot managed authentication is invalid",
        ) from error
    return content


__all__ = [
    "AUTH_ENVIRONMENT_NAMES",
    "MAX_CONFIG_BYTES",
    "PASSTHROUGH_ENVIRONMENT_NAMES",
    "CopilotAuthentication",
    "CopilotConfiguration",
    "CopilotConfigurationError",
    "CopilotConfigurationErrorCode",
    "resolve_copilot_configuration",
]
