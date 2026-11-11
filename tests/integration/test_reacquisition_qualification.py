"""Sanitized source-to-CLI controls for passive qualification, without live execution."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from integration.test_reacquisition_cli import _forbidden
from ithrl.adapters.copilot.parser import PARSER
from ithrl.adapters.errors import AdapterError
from ithrl.cli import ExitCode, app
from ithrl.context import JsonObject, JsonValue
from unit.test_codex_reacquisition import _call, _compacted, _meta, _output, _record
from unit.test_copilot_reacquisition import _document, _records

InspectSource = Callable[[Sequence[JsonObject]], dict[str, Any]]


@pytest.fixture
def inspect_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InspectSource:
    """Every control uses the normal CLI with only its immutable source readable."""
    import builtins
    import os
    import socket
    import subprocess

    import ithrl.cli.native as native

    def inspect(records: Sequence[JsonObject]) -> dict[str, Any]:
        source = tmp_path / "private-session.jsonl"
        source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        before = source.read_bytes(), source.stat()
        original_open = os.open

        def source_only(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            assert Path(path) == source
            assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as passive:
            for target, attribute in (
                (subprocess, "Popen"),
                (socket, "socket"),
                (native, "native_selection"),
                (native, "detect_codex_capability"),
                (native, "detect_copilot_capability"),
                (builtins, "open"),
                (Path, "open"),
            ):
                passive.setattr(target, attribute, _forbidden)
            passive.setattr(os, "open", source_only)
            args = ["inspect", "reacquire", str(source), "--format", "json"]
            result = CliRunner().invoke(app, args)
            report = json.loads(result.stdout)
            if result.exit_code == ExitCode.SUCCESS and report["boundaries"]:
                result = CliRunner().invoke(
                    app, [*args, "--boundary", report["boundaries"][0]["id"]]
                )
                assert result.exit_code == ExitCode.SUCCESS, result.output
                report = json.loads(result.stdout)
        assert source.read_bytes() == before[0]
        after = source.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before[1].st_ino,
            before[1].st_size,
            before[1].st_mtime_ns,
        )
        for private in (str(source), "fixture.txt", "/fixture/", "return 1", "fixture text"):
            assert private not in result.stdout
        if result.exit_code == ExitCode.SUCCESS:
            assert report["provider_calls"] == report["actions_executed"] == 0
            assert report["usage"] == {"status": "unknown", "savings": "not-estimated"}
            assert report["text_bytes"] == 0
        else:
            assert result.exit_code == ExitCode.USAGE, result.output
        return report

    return inspect


def test_changed_file_capture_keeps_operation_and_result_relations_separate(
    inspect_source: InspectSource,
) -> None:
    records = _records()
    records[8]["data"]["result"] = {"content": "return 2", "detailedContent": "1: return 2"}
    report = inspect_source(records)
    (group,) = report["groups"]
    assert group["operation_relation"] == "exact-recorded-operation"
    assert {r["relation"] for r in group["result_relations"]} == {"different"}
    assert group["resource_state"] == "unknown"
    assert "captured-results-do-not-establish-historical-resource-state" in report["limitations"]


@pytest.mark.parametrize("scope", ["content", "detailedContent"])
@pytest.mark.parametrize("variant", ["missing", "empty", "truncated"])
def test_result_scopes_remain_independent(
    inspect_source: InspectSource, scope: str, variant: str
) -> None:
    records = _records()
    result = records[8]["data"]["result"]
    if variant == "missing":
        del result[scope]
    elif variant == "empty":
        result[scope] = ""
    else:
        result[scope] = {"text": "partial", "truncated": True}
    report = inspect_source(records)
    (group,) = report["groups"]
    relations = {item["scope"]: item["relation"] for item in group["result_relations"]}
    assert relations[scope] == "unavailable"
    assert relations["detailedContent" if scope == "content" else "content"] == "equal"


@pytest.mark.parametrize("marker", ["Output:", "Final output:"])
@pytest.mark.parametrize("chunk", [False, True])
def test_status_only_native_exec_is_not_substantive_capture(
    inspect_source: InspectSource, marker: str, chunk: bool
) -> None:
    value = ("Chunk ID: fixture\n" if chunk else "") + (
        f"Wall time: 0.1 seconds\nProcess exited with code 0\n{marker}\n"
    )
    report = inspect_source(
        [
            _meta(),
            _call("before", name="exec_command"),
            _output("before", value),
            _compacted(),
            _call("after", name="exec_command"),
            _output("after", value),
        ]
    )
    assert report["groups"] == []
    assert report["coverage"]["ineligible_operations"] == 2


@pytest.mark.parametrize("variant", ["failed", "partial"])
def test_unapplied_compaction_cannot_create_selectable_boundary(
    inspect_source: InspectSource, variant: str
) -> None:
    records = _records()
    if variant == "failed":
        records[5]["data"]["success"] = False
    else:
        del records[5]
    report = inspect_source(records)
    assert report["boundaries"] == report["groups"] == []


def test_call_straddling_boundary_is_excluded(inspect_source: InspectSource) -> None:
    report = inspect_source(
        [
            _meta(),
            _call("before"),
            _compacted(),
            _output("before"),
            _call("after"),
            _output("after"),
        ]
    )
    assert report["groups"] == []
    assert report["coverage"]["outside_adjacent_epochs_or_straddling"] == 1


def test_nested_history_and_unknown_records_cannot_inject_operations(
    inspect_source: InspectSource,
) -> None:
    copied: list[JsonValue] = [_call("copied"), _output("copied")]
    report = inspect_source(
        [
            _meta(),
            _compacted(replacement_history=deepcopy(copied)),
            _record("future.event", {"history": copied}),
            _call("after"),
            _output("after"),
        ]
    )
    assert report["groups"] == []
    assert report["coverage"]["projected_operations"] == 1
    assert report["coverage"]["unknown_records"] == 1


@pytest.mark.parametrize("variant", ["duplicate-id", "malformed-id", "schema", "producer"])
def test_passive_profile_still_refuses_malformed_native_identity(
    inspect_source: InspectSource, variant: str
) -> None:
    records = _records()
    if variant == "duplicate-id":
        records[1]["id"] = records[0]["id"]
    elif variant == "malformed-id":
        records[2]["data"]["toolCallId"] = []
    else:
        records[0]["data"]["version" if variant == "schema" else "producer"] = "unsupported"
    report = inspect_source(records)
    assert "error_code" in report
    assert "groups" not in report


def test_missing_turn_ids_are_passive_only_through_normal_cli(
    inspect_source: InspectSource,
) -> None:
    records = _records()
    report = inspect_source(records)
    assert len(report["groups"]) == 1
    assert "missing-turn-ids-native-call-identities-used" in report["limitations"]
    with pytest.raises(AdapterError, match="turn ID"):
        PARSER.parse(_document(records))


def test_copilot_snapshot_and_future_event_remain_opaque(inspect_source: InspectSource) -> None:
    records = _records()
    copied = deepcopy(records[1:4])
    for index, kind in enumerate(("model.messages_snapshot", "future.event")):
        records.append(
            {
                "id": f"opaque-{index}",
                "parentId": records[-1]["id"],
                "timestamp": "2026-01-01T00:00:00Z",
                "type": kind,
                "data": {"messages": copied},
            }
        )
    report = inspect_source(records)
    assert len(report["groups"]) == 1
    assert report["coverage"]["projected_operations"] == 2
    assert report["coverage"]["unknown_records"] == 1
