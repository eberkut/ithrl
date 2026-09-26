"""Resolve a minimal Codex provider configuration for one disposable attempt."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Never, cast

from ithrl.case import CaseError
from ithrl.case.filesystem import read_safe_bytes
from ithrl.context import content_digest
from ithrl.runner.codex.runtime_configuration import (
    PROXY_ENVIRONMENT_NAMES,
    RUNTIME_PROVIDER_ID,
    is_safe_runtime_environment_name,
    normalize_base_url,
    render_runner_configuration,
    responses_endpoint,
)

_MAX_CONFIG_BYTES = 1024 * 1024
MAX_AUTH_BYTES = 1024 * 1024
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
_CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_RUNTIME_SECRET_PREFIX = "ITHRL_PROVIDER_SECRET_"
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")
_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")
_PROTECTED_ENVIRONMENT_NAMES = (
    frozenset(
        {
            "CODEX_HOME",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONHASHSEED",
            "PYTHONDONTWRITEBYTECODE",
        }
    )
    | PROXY_ENVIRONMENT_NAMES
)
_CUSTOM_PROVIDER_FIELDS = frozenset(
    {
        "base_url",
        "env_http_headers",
        "env_key",
        "env_key_instructions",
        "name",
        "request_max_retries",
        "stream_idle_timeout_ms",
        "stream_max_retries",
        "supports_standalone_web_search",
        "supports_websockets",
        "wire_api",
    }
)
_UNSAFE_CUSTOM_PROVIDER_FIELDS = frozenset(
    {
        "auth",
        "experimental_bearer_token",
        "http_headers",
    }
)
type CodexAuthentication = Literal[
    "openai-api-key-environment",
    "openai-api-key-file",
    "openai-chatgpt-file",
    "custom-environment",
]


class CodexConfigurationErrorCode(StrEnum):
    """Stable failure categories for Codex provider resolution."""

    MALFORMED_CONFIG = "malformed-config"
    UNSUPPORTED_PROVIDER = "unsupported-provider"
    UNSAFE_PROVIDER = "unsafe-provider"
    AUTHENTICATION_UNAVAILABLE = "authentication-unavailable"
    MALFORMED_AUTHENTICATION = "malformed-authentication"
    AMBIGUOUS_AUTHENTICATION = "ambiguous-authentication"
    AMBIGUOUS_DESTINATION = "ambiguous-destination"
    MISSING_ENVIRONMENT = "missing-environment"


class CodexConfigurationError(RuntimeError):
    """A local Codex provider cannot be reproduced safely."""

    def __init__(self, code: CodexConfigurationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodexProviderConfiguration:
    """Resolved provider inputs copied into one disposable Codex home."""

    source_provider_id: str = field(repr=False)
    provider_id: str
    endpoint: str = field(repr=False)
    authentication: CodexAuthentication
    environment_names: tuple[str, ...] = field(repr=False)
    deviations: tuple[str, ...]
    rendered_config: bytes = field(repr=False, compare=False)
    environment: Mapping[str, str] = field(repr=False, compare=False)
    auth_data: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.provider_id != RUNTIME_PROVIDER_ID:
            raise ValueError("Codex runtime provider id is invalid")
        if tuple(sorted(set(self.environment_names))) != self.environment_names:
            raise ValueError("Codex provider environment names must be sorted and unique")
        if set(self.environment) != set(self.environment_names):
            raise ValueError("Codex provider environment does not match its declared names")
        if any(not isinstance(value, str) or not value for value in self.environment.values()):
            raise ValueError("Codex provider environment contains an invalid value")
        if any(not is_safe_runtime_environment_name(name) for name in self.environment_names):
            raise ValueError("Codex provider environment contains an unsafe name")
        if self.auth_data is not None and not self.auth_data:
            raise ValueError("Codex authentication data cannot be empty")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @property
    def public_digest(self) -> str:
        """Return the stable identity of provider settings that contain no credentials."""

        return content_digest(
            {
                "source_provider_id": self.source_provider_id,
                "runtime_provider_id": self.provider_id,
                "endpoint": self.endpoint,
                "authentication": self.authentication,
                "environment_names": list(self.environment_names),
                "deviations": list(self.deviations),
                "rendered_config": self.rendered_config.decode("utf-8"),
            }
        )


def resolve_codex_configuration(
    *,
    provider_id: str,
    config_file: Path | None,
    auth_file: Path | None,
    environment: Mapping[str, str],
) -> CodexProviderConfiguration:
    """Resolve one captured provider without retaining unrelated user configuration."""

    document = _read_config(config_file)
    if provider_id == "openai":
        return _resolve_openai(document, auth_file=auth_file, environment=environment)
    return _resolve_custom(
        document,
        provider_id=provider_id,
        environment=environment,
    )


def _read_config(config_file: Path | None) -> dict[str, object]:
    if config_file is None:
        return {}
    try:
        content = read_safe_bytes(
            config_file,
            max_bytes=_MAX_CONFIG_BYTES,
            label="Codex configuration file",
        )
        document = tomllib.loads(content.decode("utf-8"))
    except (CaseError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_CONFIG,
            "Codex configuration cannot be read safely",
        ) from error
    return cast(dict[str, object], document)


def _resolve_openai(
    document: Mapping[str, object],
    *,
    auth_file: Path | None,
    environment: Mapping[str, str],
) -> CodexProviderConfiguration:
    base_url = _optional_base_url(document.get("openai_base_url"))
    api_key = environment.get("OPENAI_API_KEY", "")
    if not isinstance(api_key, str):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE,
            "Codex OpenAI authentication is unavailable",
        )
    if auth_file is not None and api_key:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.AMBIGUOUS_AUTHENTICATION,
            "Codex has more than one available OpenAI authentication source",
        )

    if auth_file is not None:
        auth_mode, auth_data = _read_openai_authentication(auth_file)
        authentication: CodexAuthentication
        if auth_mode == "apikey":
            authentication = "openai-api-key-file"
            default_base_url = _OPENAI_API_BASE_URL
        else:
            authentication = "openai-chatgpt-file"
            default_base_url = _CHATGPT_CODEX_BASE_URL
        selected_environment: dict[str, str] = {}
        requires_openai_auth = True
        env_key = None
        selected_auth_data = auth_data
    elif api_key:
        authentication = "openai-api-key-environment"
        default_base_url = _OPENAI_API_BASE_URL
        runtime_name = _runtime_secret_name(0)
        selected_environment = {runtime_name: api_key}
        requires_openai_auth = False
        env_key = runtime_name
        selected_auth_data = None
    else:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE,
            "Codex OpenAI authentication is unavailable",
        )

    normalized_base_url = base_url or default_base_url
    endpoint = responses_endpoint(normalized_base_url)
    rendered = render_runner_configuration(
        base_url=normalized_base_url,
        env_key=env_key,
        env_http_headers={},
        requires_openai_auth=requires_openai_auth,
        stream_idle_timeout_ms=None,
    )
    selected_environment.update(_proxy_environment(environment))
    names = tuple(sorted(selected_environment))
    return CodexProviderConfiguration(
        source_provider_id="openai",
        provider_id=RUNTIME_PROVIDER_ID,
        endpoint=endpoint,
        authentication=authentication,
        environment_names=names,
        deviations=_provider_deviations(),
        rendered_config=rendered,
        environment=MappingProxyType(selected_environment),
        auth_data=selected_auth_data,
    )


def _resolve_custom(
    document: Mapping[str, object],
    *,
    provider_id: str,
    environment: Mapping[str, str],
) -> CodexProviderConfiguration:
    providers = document.get("model_providers")
    if providers is None:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
            "The selected Codex provider is not defined in the user configuration",
        )
    if not isinstance(providers, dict):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_CONFIG,
            "Codex provider configuration has an invalid format",
        )
    provider = providers.get(provider_id)
    if provider is None:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
            "The selected Codex provider is not defined in the user configuration",
        )
    if not isinstance(provider, dict):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_CONFIG,
            "The selected Codex provider has an invalid format",
        )
    provider_document = cast(dict[str, object], provider)
    unsupported = set(provider_document) - _CUSTOM_PROVIDER_FIELDS
    if unsupported & _UNSAFE_CUSTOM_PROVIDER_FIELDS:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSAFE_PROVIDER,
            "The selected Codex provider contains inline or command-backed authentication",
        )
    if unsupported:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
            "The selected Codex provider contains unsupported configuration fields",
        )
    if provider_document.get("wire_api", "responses") != "responses":
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
            "The selected Codex provider does not use the Responses API",
        )

    base_url = _required_base_url(provider_document.get("base_url"))
    env_key = _optional_environment_name(provider_document.get("env_key"))
    env_headers = _environment_headers(provider_document.get("env_http_headers"))
    _validate_optional_provider_fields(provider_document)
    referenced_names = set(env_headers.values())
    if env_key is not None:
        referenced_names.add(env_key)
    if not referenced_names:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE,
            "The selected Codex provider has no environment-backed authentication",
        )
    source_environment = _select_environment(referenced_names, environment)
    remapped_names = {
        name: _runtime_secret_name(index) for index, name in enumerate(sorted(referenced_names))
    }
    selected_environment = {
        remapped_names[name]: value for name, value in source_environment.items()
    }
    runtime_env_key = remapped_names[env_key] if env_key is not None else None
    runtime_env_headers = {header: remapped_names[name] for header, name in env_headers.items()}
    stream_idle_timeout_ms = _optional_positive_integer(
        provider_document.get("stream_idle_timeout_ms"),
        field_name="stream idle timeout",
    )
    endpoint = responses_endpoint(base_url)
    rendered = render_runner_configuration(
        base_url=base_url,
        env_key=runtime_env_key,
        env_http_headers=runtime_env_headers,
        requires_openai_auth=False,
        stream_idle_timeout_ms=stream_idle_timeout_ms,
    )
    selected_environment.update(_proxy_environment(environment))
    names = tuple(sorted(selected_environment))
    return CodexProviderConfiguration(
        source_provider_id=provider_id,
        provider_id=RUNTIME_PROVIDER_ID,
        endpoint=endpoint,
        authentication="custom-environment",
        environment_names=names,
        deviations=_provider_deviations(),
        rendered_config=rendered,
        environment=MappingProxyType(selected_environment),
    )


def _read_openai_authentication(auth_file: Path) -> tuple[Literal["apikey", "chatgpt"], bytes]:
    try:
        content = read_safe_bytes(
            auth_file,
            max_bytes=MAX_AUTH_BYTES,
            label="Codex authentication file",
        )
        document = json.loads(content)
    except (CaseError, UnicodeError, json.JSONDecodeError) as error:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION,
            "Codex authentication cannot be read safely",
        ) from error
    if not isinstance(document, dict):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION,
            "Codex authentication has an invalid format",
        )

    return _resolve_openai_auth_document(cast(dict[str, object], document)), content


def _resolve_openai_auth_document(
    document: Mapping[str, object],
) -> Literal["apikey", "chatgpt"]:
    mode = document.get("auth_mode")
    available = _available_openai_auth_modes(document)
    if mode is None:
        if len(available) != 1:
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.AMBIGUOUS_AUTHENTICATION,
                "Codex authentication mode cannot be determined",
            )
        return next(iter(available))
    if mode not in {"apikey", "chatgpt"}:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
            "The Codex OpenAI authentication mode is unsupported",
        )
    if mode not in available:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION,
            "Codex authentication is missing data required by its mode",
        )
    return cast(Literal["apikey", "chatgpt"], mode)


def _available_openai_auth_modes(
    document: Mapping[str, object],
) -> set[Literal["apikey", "chatgpt"]]:
    available: set[Literal["apikey", "chatgpt"]] = set()
    if isinstance(document.get("OPENAI_API_KEY"), str) and bool(document["OPENAI_API_KEY"]):
        available.add("apikey")
    if isinstance(document.get("tokens"), dict) and bool(document["tokens"]):
        available.add("chatgpt")
    return available


def _optional_base_url(value: object) -> str | None:
    if value is None:
        return None
    return _required_base_url(value)


def _required_base_url(value: object) -> str:
    if not isinstance(value, str):
        _raise_invalid_base_url()
    try:
        return normalize_base_url(value)
    except ValueError:
        _raise_invalid_base_url()


def _raise_invalid_base_url() -> Never:
    raise CodexConfigurationError(
        CodexConfigurationErrorCode.AMBIGUOUS_DESTINATION,
        "The selected Codex provider has an invalid base URL",
    )


def _optional_environment_name(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or _ENVIRONMENT_NAME.fullmatch(value) is None
        or value in _PROTECTED_ENVIRONMENT_NAMES
    ):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.UNSAFE_PROVIDER,
            "The selected Codex provider has an unsafe environment reference",
        )
    return value


def _runtime_secret_name(index: int) -> str:
    return f"{_RUNTIME_SECRET_PREFIX}{index}"


def _environment_headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_CONFIG,
            "The selected Codex provider has invalid environment-backed headers",
        )
    result: dict[str, str] = {}
    for header, environment_name in value.items():
        if not isinstance(header, str) or _HTTP_HEADER_NAME.fullmatch(header) is None:
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.MALFORMED_CONFIG,
                "The selected Codex provider has invalid environment-backed headers",
            )
        selected_name = _optional_environment_name(environment_name)
        if selected_name is None:
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.MALFORMED_CONFIG,
                "The selected Codex provider has invalid environment-backed headers",
            )
        result[header] = selected_name
    return result


def _select_environment(
    names: set[str],
    environment: Mapping[str, str],
) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name in sorted(names):
        value = environment.get(name)
        if not isinstance(value, str) or not value:
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.MISSING_ENVIRONMENT,
                "A required Codex provider environment variable is unavailable",
            )
        selected[name] = value
    return selected


def _proxy_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name in sorted(PROXY_ENVIRONMENT_NAMES)
        if isinstance((value := environment.get(name)), str) and value
    }


def _validate_optional_provider_fields(provider: Mapping[str, object]) -> None:
    for field_name in ("request_max_retries", "stream_max_retries"):
        value = provider.get(field_name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.MALFORMED_CONFIG,
                "The selected Codex provider has invalid retry configuration",
            )
    for field_name in ("supports_standalone_web_search", "supports_websockets"):
        value = provider.get(field_name)
        if value is not None and not isinstance(value, bool):
            raise CodexConfigurationError(
                CodexConfigurationErrorCode.MALFORMED_CONFIG,
                "The selected Codex provider has invalid capability configuration",
            )


def _optional_positive_integer(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CodexConfigurationError(
            CodexConfigurationErrorCode.MALFORMED_CONFIG,
            f"The selected Codex provider has an invalid {field_name}",
        )
    return value


def _provider_deviations() -> tuple[str, ...]:
    return (
        "provider-request-retries-disabled",
        "provider-stream-retries-disabled",
        "provider-websockets-disabled",
        "standalone-web-search-disabled",
    )
