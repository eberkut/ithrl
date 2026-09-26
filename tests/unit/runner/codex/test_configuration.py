from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import cast

import pytest

from ithrl.runner.codex.configuration import (
    CodexConfigurationError,
    CodexConfigurationErrorCode,
    CodexProviderConfiguration,
    resolve_codex_configuration,
)


def _private_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _resolve(
    tmp_path: Path,
    *,
    provider_id: str = "openai",
    config: str = "",
    auth: str | None = None,
    environment: dict[str, str] | None = None,
) -> CodexProviderConfiguration:
    config_file = _private_file(tmp_path / "config.toml", config) if config else None
    auth_file = _private_file(tmp_path / "auth.json", auth) if auth is not None else None
    return resolve_codex_configuration(
        provider_id=provider_id,
        config_file=config_file,
        auth_file=auth_file,
        environment=environment or {},
    )


def _rendered(configuration: CodexProviderConfiguration) -> dict[str, object]:
    return tomllib.loads(configuration.rendered_config.decode("utf-8"))


def _rendered_providers(configuration: CodexProviderConfiguration) -> dict[str, object]:
    providers = _rendered(configuration)["model_providers"]
    assert isinstance(providers, dict)
    return cast(dict[str, object], providers)


def _rendered_provider(configuration: CodexProviderConfiguration) -> dict[str, object]:
    provider = _rendered_providers(configuration)["ithrl"]
    assert isinstance(provider, dict)
    return cast(dict[str, object], provider)


def _rendered_features(configuration: CodexProviderConfiguration) -> dict[str, object]:
    features = _rendered(configuration)["features"]
    assert isinstance(features, dict)
    return cast(dict[str, object], features)


def test_resolves_openai_environment_key_to_api_route(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        environment={
            "OPENAI_API_KEY": "private-api-key",
            "HTTPS_PROXY": "http://private-proxy.invalid",
            "UNRELATED": "private",
        },
    )

    assert configuration.source_provider_id == "openai"
    assert configuration.provider_id == "ithrl"
    assert configuration.endpoint == "https://api.openai.com/v1/responses"
    assert configuration.authentication == "openai-api-key-environment"
    assert configuration.environment_names == ("HTTPS_PROXY", "ITHRL_PROVIDER_SECRET_0")
    assert dict(configuration.environment) == {
        "HTTPS_PROXY": "http://private-proxy.invalid",
        "ITHRL_PROVIDER_SECRET_0": "private-api-key",
    }
    assert configuration.auth_data is None
    provider = _rendered_provider(configuration)
    assert provider["env_key"] == "ITHRL_PROVIDER_SECRET_0"
    assert provider["requires_openai_auth"] is False
    assert provider["request_max_retries"] == 0
    assert provider["stream_max_retries"] == 0
    assert provider["supports_websockets"] is False
    assert "private-api-key" not in configuration.rendered_config.decode()
    assert "private-api-key" not in repr(configuration)
    assert all(enabled is False for enabled in _rendered_features(configuration).values())


def test_resolves_chatgpt_auth_file_to_codex_route(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        auth='{"auth_mode":"chatgpt","tokens":{"access_token":"private-token"}}',
    )

    assert configuration.endpoint == "https://chatgpt.com/backend-api/codex/responses"
    assert configuration.authentication == "openai-chatgpt-file"
    assert configuration.environment_names == ()
    assert configuration.auth_data == (
        b'{"auth_mode":"chatgpt","tokens":{"access_token":"private-token"}}'
    )
    provider = _rendered_provider(configuration)
    assert provider["requires_openai_auth"] is True
    assert "env_key" not in provider
    assert "private-token" not in repr(configuration)
    assert "private-token" not in configuration.rendered_config.decode()

    (tmp_path / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{}}', encoding="utf-8")
    assert configuration.auth_data == (
        b'{"auth_mode":"chatgpt","tokens":{"access_token":"private-token"}}'
    )


def test_resolves_api_key_auth_file_to_api_route(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        auth='{"auth_mode":"apikey","OPENAI_API_KEY":"private-key"}',
    )

    assert configuration.endpoint == "https://api.openai.com/v1/responses"
    assert configuration.authentication == "openai-api-key-file"
    assert configuration.auth_data == b'{"auth_mode":"apikey","OPENAI_API_KEY":"private-key"}'


@pytest.mark.parametrize(
    ("auth", "authentication"),
    [
        ('{"OPENAI_API_KEY":"private-key"}', "openai-api-key-file"),
        ('{"tokens":{"access_token":"private-token"}}', "openai-chatgpt-file"),
    ],
)
def test_infers_unambiguous_legacy_auth_mode(
    tmp_path: Path,
    auth: str,
    authentication: str,
) -> None:
    configuration = _resolve(tmp_path, auth=auth)

    assert configuration.authentication == authentication


def test_openai_base_url_overrides_auth_route_and_is_normalized(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        config='openai_base_url = "https://gateway.example.invalid/openai/v1/"\n',
        auth='{"auth_mode":"chatgpt","tokens":{"access_token":"private-token"}}',
    )

    assert configuration.endpoint == "https://gateway.example.invalid/openai/v1/responses"
    assert _rendered_provider(configuration)["base_url"] == (
        "https://gateway.example.invalid/openai/v1"
    )


def test_resolves_only_selected_custom_provider_and_environment(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        provider_id="gateway",
        config="""
[model_providers.gateway]
name = "Private gateway"
base_url = "https://gateway.example.invalid/openai/v1"
env_key = "GATEWAY_TOKEN"
env_http_headers = { "X-Tenant" = "GATEWAY_TENANT" }
wire_api = "responses"
request_max_retries = 9
stream_max_retries = 8
stream_idle_timeout_ms = 12345
supports_websockets = true
supports_standalone_web_search = true

[model_providers.unused]
base_url = "https://unused.example.invalid/v1"
env_key = "UNUSED_TOKEN"
""",
        environment={
            "GATEWAY_TOKEN": "private-token",
            "GATEWAY_TENANT": "private-tenant",
            "UNUSED_TOKEN": "unrelated-private-token",
        },
    )

    assert configuration.source_provider_id == "gateway"
    assert configuration.endpoint == "https://gateway.example.invalid/openai/v1/responses"
    assert configuration.authentication == "custom-environment"
    assert configuration.environment_names == (
        "ITHRL_PROVIDER_SECRET_0",
        "ITHRL_PROVIDER_SECRET_1",
    )
    assert dict(configuration.environment) == {
        "ITHRL_PROVIDER_SECRET_0": "private-tenant",
        "ITHRL_PROVIDER_SECRET_1": "private-token",
    }
    assert set(_rendered_providers(configuration)) == {"ithrl"}
    provider = _rendered_provider(configuration)
    assert provider["request_max_retries"] == 0
    assert provider["stream_max_retries"] == 0
    assert provider["stream_idle_timeout_ms"] == 12345
    assert provider["supports_websockets"] is False
    assert provider["supports_standalone_web_search"] is False
    assert provider["env_key"] == "ITHRL_PROVIDER_SECRET_1"
    assert provider["env_http_headers"] == {"X-Tenant": "ITHRL_PROVIDER_SECRET_0"}
    assert "unused" not in configuration.rendered_config.decode()
    assert "private-token" not in repr(configuration)
    assert "private-tenant" not in repr(configuration)
    assert "GATEWAY_TOKEN" not in repr(configuration)
    assert "GATEWAY_TENANT" not in repr(configuration)


def test_supports_custom_provider_with_environment_backed_headers_only(
    tmp_path: Path,
) -> None:
    configuration = _resolve(
        tmp_path,
        provider_id="gateway",
        config="""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_http_headers = { Authorization = "GATEWAY_AUTHORIZATION" }
""",
        environment={"GATEWAY_AUTHORIZATION": "Bearer private-token"},
    )

    provider = _rendered_provider(configuration)
    assert "env_key" not in provider
    assert provider["env_http_headers"] == {"Authorization": "ITHRL_PROVIDER_SECRET_0"}


def test_remaps_caller_selected_environment_names_before_process_start(tmp_path: Path) -> None:
    configuration = _resolve(
        tmp_path,
        provider_id="gateway",
        config="""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "LD_PRELOAD"
""",
        environment={"LD_PRELOAD": "private-token"},
    )

    assert dict(configuration.environment) == {"ITHRL_PROVIDER_SECRET_0": "private-token"}
    assert _rendered_provider(configuration)["env_key"] == "ITHRL_PROVIDER_SECRET_0"
    assert "LD_PRELOAD" not in configuration.rendered_config.decode()


def test_public_digest_is_stable_and_contains_no_credentials(tmp_path: Path) -> None:
    first = _resolve(tmp_path / "first", environment={"OPENAI_API_KEY": "first-secret"})
    second = _resolve(tmp_path / "second", environment={"OPENAI_API_KEY": "second-secret"})

    assert first.public_digest == second.public_digest
    assert "first-secret" not in first.public_digest


@pytest.mark.parametrize(
    "field",
    [
        'auth = { command = "credential-helper" }',
        'experimental_bearer_token = "private-token"',
        'http_headers = { Authorization = "private-token" }',
    ],
)
def test_rejects_inline_or_command_backed_custom_authentication(
    tmp_path: Path,
    field: str,
) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config=f"""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "GATEWAY_TOKEN"
{field}
""",
            environment={"GATEWAY_TOKEN": "private-environment-token"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.UNSAFE_PROVIDER
    assert "private-token" not in str(refused.value)
    assert "private-environment-token" not in str(refused.value)


@pytest.mark.parametrize(
    "field",
    [
        'query_params = { "api-version" = "private" }',
        "requires_openai_auth = true",
        'unknown_future_field = "private-value"',
    ],
)
def test_rejects_unsupported_custom_provider_fields(tmp_path: Path, field: str) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config=f"""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "GATEWAY_TOKEN"
{field}
""",
            environment={"GATEWAY_TOKEN": "private-token"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER
    assert "private-value" not in str(refused.value)


def test_rejects_non_responses_provider(tmp_path: Path) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config="""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "GATEWAY_TOKEN"
wire_api = "chat"
""",
            environment={"GATEWAY_TOKEN": "private-token"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER
    assert "Responses API" in str(refused.value)


@pytest.mark.parametrize(
    "config",
    [
        'model_providers = "invalid"\n',
        '[model_providers]\ngateway = "invalid"\n',
    ],
)
def test_rejects_malformed_provider_tables(tmp_path: Path, config: str) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(tmp_path, provider_id="gateway", config=config)

    assert refused.value.code is CodexConfigurationErrorCode.MALFORMED_CONFIG


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.example.invalid/v1",
        "gateway.example.invalid/v1",
        "https://user:private@gateway.example.invalid/v1",
        "https://gateway.example.invalid/v1?private=value",
        "https://gateway.example.invalid/v1#private",
        "https://gateway.example.invalid/v1/responses",
        "https://gateway.example.invalid/v1/../responses-api",
        "https://gateway.example.invalid\\private/v1",
        "https://gateway.example.invalid/private\npath",
        " https://gateway.example.invalid/v1",
    ],
)
def test_rejects_ambiguous_provider_destination(tmp_path: Path, base_url: str) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config=f"""
[model_providers.gateway]
base_url = {base_url!r}
env_key = "GATEWAY_TOKEN"
""",
            environment={"GATEWAY_TOKEN": "private-token"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.AMBIGUOUS_DESTINATION
    assert "private" not in str(refused.value)


def test_rejects_missing_referenced_environment_without_naming_it(tmp_path: Path) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config="""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "PRIVATE_GATEWAY_TOKEN"
""",
        )

    assert refused.value.code is CodexConfigurationErrorCode.MISSING_ENVIRONMENT
    assert "PRIVATE_GATEWAY_TOKEN" not in str(refused.value)


@pytest.mark.parametrize("environment_name", ["HOME", "CODEX_HOME", "not-valid"])
def test_rejects_unsafe_environment_references(
    tmp_path: Path,
    environment_name: str,
) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            provider_id="gateway",
            config=f"""
[model_providers.gateway]
base_url = "https://gateway.example.invalid/v1"
env_key = "{environment_name}"
""",
            environment={environment_name: "private-value"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.UNSAFE_PROVIDER
    assert environment_name not in str(refused.value)


def test_rejects_ambiguous_openai_authentication(tmp_path: Path) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(
            tmp_path,
            auth=('{"auth_mode":"chatgpt","tokens":{"access_token":"private-token"}}'),
            environment={"OPENAI_API_KEY": "private-api-key"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.AMBIGUOUS_AUTHENTICATION
    assert "private-token" not in str(refused.value)
    assert "private-api-key" not in str(refused.value)


@pytest.mark.parametrize(
    ("auth", "code"),
    [
        ("not-json", CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION),
        ("[]", CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION),
        ('{"auth_mode":"apikey"}', CodexConfigurationErrorCode.MALFORMED_AUTHENTICATION),
        (
            '{"OPENAI_API_KEY":"private-key","tokens":{"access_token":"private"}}',
            CodexConfigurationErrorCode.AMBIGUOUS_AUTHENTICATION,
        ),
        (
            '{"auth_mode":"agentIdentity","agent_identity":"private"}',
            CodexConfigurationErrorCode.UNSUPPORTED_PROVIDER,
        ),
    ],
)
def test_rejects_malformed_or_unsupported_authentication(
    tmp_path: Path,
    auth: str,
    code: CodexConfigurationErrorCode,
) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(tmp_path, auth=auth)

    assert refused.value.code is code
    assert "private" not in str(refused.value)


def test_rejects_missing_openai_authentication(tmp_path: Path) -> None:
    with pytest.raises(CodexConfigurationError) as refused:
        _resolve(tmp_path)

    assert refused.value.code is CodexConfigurationErrorCode.AUTHENTICATION_UNAVAILABLE


def test_rejects_malformed_bounded_config_without_disclosing_path(tmp_path: Path) -> None:
    config_file = _private_file(tmp_path / "private-config.toml", "not = [valid")

    with pytest.raises(CodexConfigurationError) as refused:
        resolve_codex_configuration(
            provider_id="openai",
            config_file=config_file,
            auth_file=None,
            environment={"OPENAI_API_KEY": "private-key"},
        )

    assert refused.value.code is CodexConfigurationErrorCode.MALFORMED_CONFIG
    assert os.fspath(config_file) not in str(refused.value)
