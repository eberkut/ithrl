"""Verify the Codex app-server schema used by the final-response runner."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import cast

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from ithrl.context import JsonObject
from ithrl.runner.codex.protocol import (
    ACTION_ITEM_TYPES,
    ACTION_REQUEST_METHODS,
    PASSIVE_ITEM_TYPES,
)

_MAX_SCHEMA_BYTES = 2 * 1024 * 1024
_SCHEMA_FILES = (
    "ClientRequest.json",
    "CommandExecutionRequestApprovalResponse.json",
    "DynamicToolCallResponse.json",
    "FileChangeRequestApprovalResponse.json",
    "McpServerElicitationRequestResponse.json",
    "ServerNotification.json",
    "ServerRequest.json",
    "v1/InitializeResponse.json",
    "v2/ThreadStartResponse.json",
    "v2/TurnStartResponse.json",
)
_CLIENT_METHODS = frozenset({"initialize", "thread/inject_items", "thread/start", "turn/start"})
_NOTIFICATION_METHODS = frozenset(
    {
        "account/rateLimits/updated",
        "item/agentMessage/delta",
        "item/completed",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "model/rerouted",
        "model/verification",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/started",
    }
)
_KNOWN_THREAD_ITEMS = ACTION_ITEM_TYPES | PASSIVE_ITEM_TYPES


class CodexSchemaError(RuntimeError):
    """The installed app-server protocol does not satisfy the runner contract."""


def verify_schema_bundle(root: Path) -> str:
    """Return a digest when the generated app-server schema is supported."""

    documents, digest = _read_bundle(root)
    client = documents["ClientRequest.json"]
    notifications = documents["ServerNotification.json"]
    requests = documents["ServerRequest.json"]

    client_methods = _require_methods(client, _CLIENT_METHODS)
    notification_methods = _require_methods(notifications, _NOTIFICATION_METHODS)
    if not set(_method_definitions(requests)) >= ACTION_REQUEST_METHODS:
        raise CodexSchemaError("Codex app-server action requests are not supported")

    _verify_client_contract(client, client_methods)
    _verify_notification_contract(notifications, notification_methods)
    _verify_response_contract(documents)
    return digest


def _read_bundle(root: Path) -> tuple[dict[str, dict[str, object]], str]:
    documents: dict[str, dict[str, object]] = {}
    digest = hashlib.sha256()
    for relative in _SCHEMA_FILES:
        try:
            raw = _read_schema(root / relative)
        except OSError as error:
            raise CodexSchemaError("Codex app-server schema bundle is incomplete") from error
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CodexSchemaError(
                "Codex app-server schema bundle contains invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise CodexSchemaError("Codex app-server schema bundle has an invalid document")
        documents[relative] = cast(dict[str, object], value)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return documents, digest.hexdigest()


def _read_schema(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("unsafe schema file")
    if metadata.st_size < 1 or metadata.st_size > _MAX_SCHEMA_BYTES:
        raise OSError("schema file exceeds its size bound")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError("schema file changed while opening")
        raw = stream.read(_MAX_SCHEMA_BYTES + 1)
    if len(raw) != metadata.st_size:
        raise OSError("schema file changed while reading")
    return raw


def _verify_client_contract(
    schema: dict[str, object],
    methods: dict[str, str],
) -> None:
    required = {
        "initialize": {"capabilities", "clientInfo"},
        "thread/inject_items": {"items", "threadId"},
        "thread/start": {
            "approvalPolicy",
            "approvalsReviewer",
            "baseInstructions",
            "cwd",
            "developerInstructions",
            "ephemeral",
            "model",
            "modelProvider",
            "sandbox",
        },
        "turn/start": {
            "approvalPolicy",
            "approvalsReviewer",
            "cwd",
            "effort",
            "input",
            "model",
            "personality",
            "sandboxPolicy",
            "threadId",
            "toolOutput",
        },
    }
    for method, properties in required.items():
        _require_properties(_definition(schema, methods[method]), properties, method)
    thread_start = _definition(schema, methods["thread/start"])
    turn_start = _definition(schema, methods["turn/start"])
    for params in (thread_start, turn_start):
        _require_nullable_reference(params, "approvalPolicy", "AskForApproval")
        _require_nullable_reference(params, "approvalsReviewer", "ApprovalsReviewer")
    _require_nullable_reference(thread_start, "sandbox", "SandboxMode")
    _require_nullable_reference(turn_start, "sandboxPolicy", "SandboxPolicy")
    if not _supports_string_enum_value(_definition(schema, "AskForApproval"), "never"):
        raise CodexSchemaError("Codex app-server cannot enforce no-approval execution")
    if not _supports_string_enum_value(_definition(schema, "ApprovalsReviewer"), "user"):
        raise CodexSchemaError("Codex app-server has no supported approval reviewer")
    if not _supports_string_enum_value(_definition(schema, "SandboxMode"), "read-only"):
        raise CodexSchemaError("Codex app-server has no read-only thread sandbox")
    sandbox = _definition(schema, "SandboxPolicy")
    read_only = _tagged_variant(sandbox, "readOnly")
    if read_only is None or _property(read_only, "networkAccess").get("type") != "boolean":
        raise CodexSchemaError("Codex app-server has no supported turn sandbox policy")
    try:
        validator_class = validator_for(schema, default=Draft7Validator)
        validator_class.check_schema(schema)
        validator = validator_class(schema)
    except SchemaError as error:
        raise CodexSchemaError("Codex app-server client request schema is invalid") from error
    for method, params in _client_request_samples():
        request: JsonObject = {"id": 1, "method": method, "params": params}
        if not validator.is_valid(request):
            raise CodexSchemaError(f"Codex app-server {method} request shape is unsupported")


def _client_request_samples() -> list[tuple[str, JsonObject]]:
    """Return every materially distinct request form emitted by the worker."""

    samples: list[tuple[str, JsonObject]] = [
        (
            "initialize",
            {
                "capabilities": {"experimentalApi": False},
                "clientInfo": {"name": "ithrl", "title": "ithrl", "version": "0.0.0"},
            },
        ),
        (
            "thread/inject_items",
            {"threadId": "thread", "items": [{"type": "message"}]},
        ),
    ]

    thread_start: JsonObject = {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": "/tmp/ithrl-state/workspace",  # noqa: S108 - schema sample
        "ephemeral": True,
        "model": "model",
        "modelProvider": "ithrl",
        "sandbox": "read-only",
    }
    instruction_variants: tuple[JsonObject, ...] = (
        {},
        {"baseInstructions": "instructions"},
        {"developerInstructions": "instructions"},
        {
            "baseInstructions": "instructions",
            "developerInstructions": "instructions",
        },
    )
    samples.extend(
        ("thread/start", {**thread_start, **instructions}) for instructions in instruction_variants
    )

    turn_start: JsonObject = {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": "/tmp/ithrl-state/workspace",  # noqa: S108 - schema sample
        "model": "model",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "threadId": "thread",
    }
    setting_variants: tuple[JsonObject, ...] = (
        {},
        {"effort": "low"},
        {"personality": "none"},
        {"effort": "low", "personality": "none"},
    )
    trigger_variants: tuple[JsonObject, ...] = (
        {"input": [{"type": "text", "text": "input"}]},
        {
            "input": [],
            "toolOutput": {"name": "tool", "namespace": None, "output": "output"},
        },
        {
            "input": [],
            "toolOutput": {
                "name": "tool",
                "namespace": None,
                "output": [{"type": "input_text", "text": "output"}],
            },
        },
    )
    samples.extend(
        ("turn/start", {**turn_start, **settings, **trigger})
        for settings in setting_variants
        for trigger in trigger_variants
    )
    return samples


def _verify_notification_contract(
    schema: dict[str, object],
    methods: dict[str, str],
) -> None:
    required = {
        "account/rateLimits/updated": {"rateLimits"},
        "item/agentMessage/delta": {"delta", "itemId", "threadId", "turnId"},
        "item/completed": {"item", "threadId", "turnId"},
        "item/started": {"item", "threadId", "turnId"},
        "model/rerouted": {
            "fromModel",
            "reason",
            "threadId",
            "toModel",
            "turnId",
        },
        "model/verification": {"threadId", "turnId", "verifications"},
        "item/plan/delta": {"delta", "itemId", "threadId", "turnId"},
        "item/reasoning/summaryPartAdded": {
            "itemId",
            "summaryIndex",
            "threadId",
            "turnId",
        },
        "item/reasoning/summaryTextDelta": {
            "delta",
            "itemId",
            "summaryIndex",
            "threadId",
            "turnId",
        },
        "item/reasoning/textDelta": {
            "contentIndex",
            "delta",
            "itemId",
            "threadId",
            "turnId",
        },
        "thread/tokenUsage/updated": {"threadId", "tokenUsage", "turnId"},
        "thread/started": {"thread"},
        "thread/status/changed": {"status", "threadId"},
        "turn/completed": {"threadId", "turn"},
        "turn/started": {"threadId", "turn"},
    }
    for method, properties in required.items():
        _require_properties(_definition(schema, methods[method]), properties, method)

    completed = _definition(schema, methods["item/completed"])
    thread_item = _referenced_definition(schema, completed, "item")
    variants = _object_list(thread_item.get("oneOf"), "ThreadItem")
    item_types = {_variant_tag(variant, "ThreadItem") for variant in variants}
    if item_types != _KNOWN_THREAD_ITEMS:
        raise CodexSchemaError("Codex app-server exposes unknown turn item types")
    agent = next(
        variant for variant in variants if _variant_tag(variant, "ThreadItem") == "agentMessage"
    )
    _require_properties(agent, {"id", "phase", "text", "type"}, "agentMessage")
    message_phase = _referenced_definition(schema, agent, "phase")
    if not _supports_string_enum_value(message_phase, "final_answer"):
        raise CodexSchemaError("Codex app-server has no final-answer message phase")

    usage_notification = _definition(schema, methods["thread/tokenUsage/updated"])
    usage = _referenced_definition(schema, usage_notification, "tokenUsage")
    _require_properties(usage, {"last", "total"}, "thread token usage")
    usage_counts = _referenced_definition(schema, usage, "last")
    _require_properties(
        usage_counts,
        {"cachedInputTokens", "inputTokens", "outputTokens"},
        "token usage breakdown",
    )
    turn = _referenced_definition(schema, _definition(schema, methods["turn/completed"]), "turn")
    _require_properties(turn, {"id", "items", "status"}, "completed turn")
    thread = _referenced_definition(
        schema, _definition(schema, methods["thread/started"]), "thread"
    )
    _require_properties(thread, {"id"}, "started thread")


def _verify_response_contract(documents: dict[str, dict[str, object]]) -> None:
    _require_properties(
        documents["v2/ThreadStartResponse.json"],
        {"model", "serviceTier", "thread"},
        "thread/start",
    )
    thread_start = documents["v2/ThreadStartResponse.json"]
    _require_properties(
        _referenced_definition(thread_start, thread_start, "thread"),
        {"id", "model"},
        "thread/start thread",
    )
    _require_properties(documents["v2/TurnStartResponse.json"], {"turn"}, "turn/start")
    turn_start = documents["v2/TurnStartResponse.json"]
    _require_properties(
        _referenced_definition(turn_start, turn_start, "turn"),
        {"id"},
        "turn/start turn",
    )
    if not _property_supports_string_value(
        documents["CommandExecutionRequestApprovalResponse.json"], "decision", "decline"
    ):
        raise CodexSchemaError("Codex command execution cannot be declined")
    if not _property_supports_string_value(
        documents["FileChangeRequestApprovalResponse.json"], "decision", "decline"
    ):
        raise CodexSchemaError("Codex file changes cannot be declined")
    _require_properties(
        documents["DynamicToolCallResponse.json"],
        {"contentItems", "success"},
        "dynamic tool response",
    )
    elicitation = documents["McpServerElicitationRequestResponse.json"]
    _require_properties(elicitation, {"_meta", "action", "content"}, "MCP elicitation response")
    if not _property_supports_string_value(elicitation, "action", "decline"):
        raise CodexSchemaError("Codex MCP elicitation cannot be declined")


def _require_methods(schema: dict[str, object], expected: frozenset[str]) -> dict[str, str]:
    methods = _method_definitions(schema)
    if any(not methods.get(method) for method in expected):
        raise CodexSchemaError("Codex app-server is missing a required protocol method")
    return {method: methods[method] for method in expected}


def _method_definitions(schema: dict[str, object]) -> dict[str, str]:
    variants = _object_list(schema.get("oneOf"), "request union")
    methods: dict[str, str] = {}
    for variant in variants:
        properties = _object(variant.get("properties"), "request properties")
        method_schema = _object(properties.get("method"), "request method")
        values = method_schema.get("enum")
        params = _object(properties.get("params"), "request parameters")
        reference = params.get("$ref")
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
            raise CodexSchemaError("Codex app-server request union has an invalid variant")
        methods[values[0]] = (
            reference.removeprefix("#/definitions/")
            if isinstance(reference, str) and reference.startswith("#/definitions/")
            else ""
        )
    return methods


def _variant_tag(variant: dict[str, object], label: str) -> str:
    properties = _object(variant.get("properties"), label)
    type_schema = _object(properties.get("type"), label)
    values = type_schema.get("enum")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise CodexSchemaError(f"Codex app-server {label} has an invalid variant")
    return values[0]


def _definition(schema: dict[str, object], name: str) -> dict[str, object]:
    definitions = _object(schema.get("definitions"), "schema definitions")
    return _object(definitions.get(name), name)


def _require_properties(schema: dict[str, object], required: set[str], label: str) -> None:
    properties = _object(schema.get("properties"), label)
    if not required <= set(properties):
        raise CodexSchemaError(f"Codex app-server {label} shape is unsupported")


def _property_supports_string_value(
    schema: dict[str, object],
    property_name: str,
    value: str,
) -> bool:
    try:
        return _supports_string_enum_value(
            _referenced_definition(schema, schema, property_name), value
        )
    except CodexSchemaError:
        return False


def _property(schema: dict[str, object], name: str) -> dict[str, object]:
    properties = _object(schema.get("properties"), name)
    return _object(properties.get(name), name)


def _referenced_definition(
    schema: dict[str, object],
    owner: dict[str, object],
    property_name: str,
) -> dict[str, object]:
    property_schema = _property(owner, property_name)
    references = [property_schema.get("$ref")]
    variants = property_schema.get("anyOf")
    if isinstance(variants, list):
        references.extend(variant.get("$ref") for variant in variants if isinstance(variant, dict))
    prefix = "#/definitions/"
    names = {
        reference.removeprefix(prefix)
        for reference in references
        if isinstance(reference, str) and reference.startswith(prefix)
    }
    if len(names) != 1:
        raise CodexSchemaError(f"Codex app-server {property_name} reference is unsupported")
    return _definition(schema, names.pop())


def _require_nullable_reference(
    schema: dict[str, object],
    property_name: str,
    definition: str,
) -> None:
    variants = _object_list(_property(schema, property_name).get("anyOf"), property_name)
    reference = f"#/definitions/{definition}"
    if not any(variant.get("$ref") == reference for variant in variants) or not any(
        variant.get("type") == "null" for variant in variants
    ):
        raise CodexSchemaError(f"Codex app-server {property_name} policy shape is unsupported")


def _enum_values(schema: dict[str, object], label: str) -> set[str]:
    values = schema.get("enum")
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) for value in values)
    ):
        raise CodexSchemaError(f"Codex app-server {label} has an invalid enum")
    return set(cast(list[str], values))


def _supports_string_enum_value(schema: dict[str, object], value: str) -> bool:
    values = schema.get("enum")
    if schema.get("type") == "string" and isinstance(values, list) and value in values:
        return True
    variants = schema.get("oneOf")
    return isinstance(variants, list) and any(
        isinstance(variant, dict)
        and variant.get("type") == "string"
        and isinstance(variant.get("enum"), list)
        and value in variant["enum"]
        for variant in variants
    )


def _tagged_variant(
    schema: dict[str, object],
    tag: str,
) -> dict[str, object] | None:
    variants = _object_list(schema.get("oneOf"), "tagged union")
    for variant in variants:
        type_schema = _property(variant, "type")
        if type_schema.get("type") == "string" and tag in _enum_values(
            type_schema, "tagged union type"
        ):
            return variant
    return None


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodexSchemaError(f"Codex app-server {label} is invalid")
    return cast(dict[str, object], value)


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise CodexSchemaError(f"Codex app-server {label} is invalid")
    return [_object(item, label) for item in value]
