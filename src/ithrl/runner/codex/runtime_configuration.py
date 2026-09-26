"""Build and verify the minimal configuration used by the Codex child process."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from typing import Never
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ithrl.runner.codex.policy import DISABLED_FEATURES

RUNTIME_PROVIDER_ID = "ithrl"
PROXY_ENVIRONMENT_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

_RUNTIME_SECRET_NAME = re.compile(r"^ITHRL_PROVIDER_SECRET_[0-9]+$")
_RENDERED_ROOT_FIELDS = frozenset(
    {
        "approval_policy",
        "cli_auth_credentials_store",
        "features",
        "model_provider",
        "model_providers",
        "sandbox_mode",
        "web_search",
    }
)
_RENDERED_PROVIDER_FIELDS = frozenset(
    {
        "base_url",
        "env_http_headers",
        "env_key",
        "name",
        "request_max_retries",
        "requires_openai_auth",
        "stream_idle_timeout_ms",
        "stream_max_retries",
        "supports_standalone_web_search",
        "supports_websockets",
        "wire_api",
    }
)


def is_safe_runtime_environment_name(name: str) -> bool:
    """Return whether a name can enter the isolated Codex process."""

    return name in PROXY_ENVIRONMENT_NAMES or _RUNTIME_SECRET_NAME.fullmatch(name) is not None


def normalize_base_url(value: str) -> str:
    """Validate and normalize a provider base URL without retaining credentials."""

    if _has_unsafe_url_text(value):
        raise ValueError("provider base URL is unsafe")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("provider base URL is unsafe") from error
    if not _is_supported_url_shape(parsed):
        raise ValueError("provider base URL is unsafe")
    host = parsed.hostname
    if host is None:  # Proved by _is_supported_url_shape; keeps narrowing explicit.
        raise ValueError("provider base URL is unsafe")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def responses_endpoint(base_url: str) -> str:
    """Return the Responses endpoint for a normalized provider base URL."""

    return f"{base_url}/responses"


def render_runner_configuration(
    *,
    base_url: str,
    env_key: str | None,
    env_http_headers: Mapping[str, str],
    requires_openai_auth: bool,
    stream_idle_timeout_ms: int | None,
) -> bytes:
    """Render the complete configuration accepted by the isolated runner."""

    lines = [
        f'model_provider = "{RUNTIME_PROVIDER_ID}"',
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        'cli_auth_credentials_store = "ephemeral"',
        "",
        f"[model_providers.{RUNTIME_PROVIDER_ID}]",
        'name = "ithrl isolated provider"',
        f"base_url = {_toml_string(base_url)}",
        'wire_api = "responses"',
        f"requires_openai_auth = {str(requires_openai_auth).lower()}",
        "request_max_retries = 0",
        "stream_max_retries = 0",
        "supports_websockets = false",
        "supports_standalone_web_search = false",
    ]
    if env_key is not None:
        lines.append(f"env_key = {_toml_string(env_key)}")
    if stream_idle_timeout_ms is not None:
        lines.append(f"stream_idle_timeout_ms = {stream_idle_timeout_ms}")
    if env_http_headers:
        headers = ", ".join(
            f"{_toml_string(header)} = {_toml_string(environment_name)}"
            for header, environment_name in sorted(env_http_headers.items())
        )
        lines.append(f"env_http_headers = {{ {headers} }}")
    lines.extend(("", "[features]"))
    lines.extend(f"{feature} = false" for feature in sorted(DISABLED_FEATURES))
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def validate_runner_configuration(
    rendered_config: bytes,
    *,
    endpoint: str,
    environment_names: tuple[str, ...],
) -> None:
    """Require the exact destination and safety controls used by the runner."""

    try:
        document = tomllib.loads(rendered_config.decode("utf-8", errors="strict"))
        providers = document["model_providers"]
        provider = providers[RUNTIME_PROVIDER_ID]
        features = document["features"]
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        _raise_unsafe_runner_configuration(error)
    if (
        set(document) != _RENDERED_ROOT_FIELDS
        or not isinstance(providers, dict)
        or set(providers) != {RUNTIME_PROVIDER_ID}
        or not isinstance(provider, dict)
        or not _valid_rendered_provider(provider, endpoint, environment_names)
    ):
        _raise_unsafe_runner_configuration()
    expected_root = {
        "model_provider": RUNTIME_PROVIDER_ID,
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "web_search": "disabled",
        "cli_auth_credentials_store": "ephemeral",
    }
    if any(
        document[name] != value for name, value in expected_root.items()
    ) or features != dict.fromkeys(DISABLED_FEATURES, False):
        _raise_unsafe_runner_configuration()


def _valid_rendered_provider(
    provider: dict[str, object],
    endpoint: str,
    environment_names: tuple[str, ...],
) -> bool:
    if (
        not {
            "base_url",
            "name",
            "request_max_retries",
            "requires_openai_auth",
            "stream_max_retries",
            "supports_standalone_web_search",
            "supports_websockets",
            "wire_api",
        }
        <= set(provider)
        <= _RENDERED_PROVIDER_FIELDS
    ):
        return False
    if not _valid_rendered_destination(provider, endpoint) or not _valid_rendered_policy(provider):
        return False
    idle_timeout = provider.get("stream_idle_timeout_ms")
    if idle_timeout is not None and (type(idle_timeout) is not int or idle_timeout <= 0):
        return False
    return _valid_rendered_environment(provider, environment_names)


def _valid_rendered_destination(provider: dict[str, object], endpoint: str) -> bool:
    base_url = provider["base_url"]
    return bool(
        isinstance(base_url, str)
        and _is_supported_url_shape(urlsplit(base_url))
        and responses_endpoint(base_url) == endpoint
    )


def _valid_rendered_policy(provider: dict[str, object]) -> bool:
    return bool(
        isinstance(provider["name"], str)
        and provider["name"]
        and provider["wire_api"] == "responses"
        and type(provider["requires_openai_auth"]) is bool
        and type(provider["request_max_retries"]) is int
        and provider["request_max_retries"] == 0
        and type(provider["stream_max_retries"]) is int
        and provider["stream_max_retries"] == 0
        and provider["supports_websockets"] is False
        and provider["supports_standalone_web_search"] is False
    )


def _valid_rendered_environment(
    provider: dict[str, object],
    environment_names: tuple[str, ...],
) -> bool:
    references: set[object] = {provider.get("env_key")}
    headers = provider.get("env_http_headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(name, str) or not isinstance(reference, str)
        for name, reference in headers.items()
    ):
        return False
    references.update(headers.values())
    references.discard(None)
    secret_names = {name for name in environment_names if name not in PROXY_ENVIRONMENT_NAMES}
    return references == secret_names


def _has_unsafe_url_text(value: str) -> bool:
    return (
        not value
        or value != value.strip()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
    )


def _is_supported_url_shape(parsed: SplitResult) -> bool:
    path_segments = parsed.path.split("/")
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and not {".", ".."}.intersection(path_segments)
        and not parsed.path.rstrip("/").endswith("/responses")
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _raise_unsafe_runner_configuration(error: Exception | None = None) -> Never:
    raise ValueError("Codex runner configuration does not satisfy the safety policy") from error
