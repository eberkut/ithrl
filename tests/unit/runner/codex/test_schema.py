from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from ithrl.runner.codex.protocol import SUPPORTED_NOTIFICATION_METHODS
from ithrl.runner.codex.schema import (
    _NOTIFICATION_METHODS,
    CodexSchemaError,
    verify_schema_bundle,
)

SCHEMA_FIXTURE = Path(__file__).parents[3] / "fixtures" / "codex-app-server" / "schema"


def _bundle(tmp_path: Path) -> Path:
    destination = tmp_path / "schema"
    shutil.copytree(SCHEMA_FIXTURE, destination)
    return destination


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")


def test_schema_and_protocol_notification_contracts_match() -> None:
    assert frozenset(_NOTIFICATION_METHODS) == SUPPORTED_NOTIFICATION_METHODS


def test_accepts_supported_structural_contract_and_added_optional_fields(
    tmp_path: Path,
) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"]["ThreadStartParams"]["properties"]["futureOptional"] = {"type": "string"}
    client["oneOf"].append(
        {
            "properties": {
                "method": {"enum": ["future/read"]},
                "params": {"type": "null"},
            }
        }
    )
    _write(client_path, client)

    digest = verify_schema_bundle(schema)

    assert len(digest) == 64


def test_rejects_missing_required_method(tmp_path: Path) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["oneOf"] = [
        variant
        for variant in client["oneOf"]
        if variant["properties"]["method"]["enum"] != ["turn/start"]
    ]
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="required protocol method"):
        verify_schema_bundle(schema)


def test_rejects_unknown_turn_item_before_provider_dispatch(tmp_path: Path) -> None:
    schema = _bundle(tmp_path)
    notification_path = schema / "ServerNotification.json"
    notifications = _load(notification_path)
    notifications["definitions"]["ThreadItem"]["oneOf"].append(
        {"properties": {"type": {"enum": ["futureAction"]}}}
    )
    _write(notification_path, notifications)

    with pytest.raises(CodexSchemaError, match="unknown turn item"):
        verify_schema_bundle(schema)


def test_rejects_changed_action_denial_shape(tmp_path: Path) -> None:
    schema = _bundle(tmp_path)
    _write(schema / "CommandExecutionRequestApprovalResponse.json", {"enum": ["accept"]})

    with pytest.raises(CodexSchemaError, match="cannot be declined"):
        verify_schema_bundle(schema)


@pytest.mark.parametrize(
    ("definition", "property_name"),
    [
        ("ThreadStartParams", "approvalPolicy"),
        ("ThreadStartParams", "approvalsReviewer"),
        ("ThreadStartParams", "sandbox"),
        ("TurnStartParams", "approvalPolicy"),
        ("TurnStartParams", "approvalsReviewer"),
        ("TurnStartParams", "sandboxPolicy"),
    ],
)
def test_rejects_changed_policy_parameter_shape(
    tmp_path: Path,
    definition: str,
    property_name: str,
) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"][definition]["properties"][property_name] = {"type": "number"}
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match=property_name):
        verify_schema_bundle(schema)


def test_rejects_non_boolean_read_only_network_policy(tmp_path: Path) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    read_only = client["definitions"]["SandboxPolicy"]["oneOf"][0]
    read_only["properties"]["networkAccess"] = {"type": "string"}
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="turn sandbox policy"):
        verify_schema_bundle(schema)


@pytest.mark.parametrize(
    ("definition", "property_name", "replacement"),
    [
        ("InitializeParams", "clientInfo", {"type": "string"}),
        ("ThreadInjectItemsParams", "items", {"type": "string"}),
        ("ThreadInjectItemsParams", "threadId", {"type": "number"}),
        ("ThreadStartParams", "baseInstructions", {"type": "number"}),
        ("ThreadStartParams", "ephemeral", {"type": "string"}),
        ("ThreadStartParams", "model", {"type": "number"}),
        ("ThreadStartParams", "model", {"pattern": "^other$"}),
        (
            "ThreadStartParams",
            "model",
            {"oneOf": [{"type": "string"}, {"const": "model"}]},
        ),
        ("TurnStartParams", "effort", {"type": "number"}),
        ("TurnStartParams", "input", {"type": "string"}),
        ("TurnStartParams", "personality", {"type": "number"}),
        ("TurnStartParams", "threadId", {"type": "number"}),
        ("TurnStartParams", "toolOutput", {"type": "string"}),
    ],
)
def test_rejects_request_value_shape_changes(
    tmp_path: Path,
    definition: str,
    property_name: str,
    replacement: dict[str, Any],
) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"][definition]["properties"][property_name] = replacement
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="request shape"):
        verify_schema_bundle(schema)


def test_rejects_nonempty_turn_input_requirement(tmp_path: Path) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"]["TurnStartParams"]["properties"]["input"] = {
        "type": "array",
        "minItems": 1,
    }
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="turn/start request shape"):
        verify_schema_bundle(schema)


@pytest.mark.parametrize(
    ("definition", "property_name"),
    [
        ("ThreadStartParams", "baseInstructions"),
        ("ThreadStartParams", "developerInstructions"),
        ("TurnStartParams", "effort"),
        ("TurnStartParams", "personality"),
        ("TurnStartParams", "toolOutput"),
    ],
)
def test_rejects_new_required_optional_request_field(
    tmp_path: Path,
    definition: str,
    property_name: str,
) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"][definition]["required"] = [property_name]
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="request shape"):
        verify_schema_bundle(schema)


@pytest.mark.parametrize("output_type", ["array", "string"])
def test_rejects_tool_output_that_only_accepts_one_content_shape(
    tmp_path: Path,
    output_type: str,
) -> None:
    schema = _bundle(tmp_path)
    client_path = schema / "ClientRequest.json"
    client = _load(client_path)
    client["definitions"]["TurnStartParams"]["properties"]["toolOutput"] = {
        "type": "object",
        "required": ["output"],
        "properties": {"output": {"type": output_type}},
    }
    _write(client_path, client)

    with pytest.raises(CodexSchemaError, match="turn/start request shape"):
        verify_schema_bundle(schema)


def test_rejects_missing_or_linked_schema_file(tmp_path: Path) -> None:
    missing = _bundle(tmp_path / "missing")
    (missing / "ServerRequest.json").unlink()
    with pytest.raises(CodexSchemaError, match="incomplete"):
        verify_schema_bundle(missing)

    linked = _bundle(tmp_path / "linked")
    target = linked / "ClientRequest.json"
    target.unlink()
    target.symlink_to(linked / "ServerRequest.json")
    with pytest.raises(CodexSchemaError, match="incomplete"):
        verify_schema_bundle(linked)
