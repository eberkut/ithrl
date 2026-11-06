"""Offline inspection exercises the public CLI without native or store access."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.cli import app
from ithrl.observations import ObservationStore


@pytest.fixture
def reduced_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    case = tmp_path / "case"
    runner = CliRunner()
    extracted = runner.invoke(app, ["extract", "--example", "--output", str(case)])
    assert extracted.exit_code == 0, extracted.output
    reduced = runner.invoke(app, ["shrink", str(case), "--format", "json"])
    assert reduced.exit_code == 0, reduced.output
    (result,) = (case / "results").iterdir()
    return case, result


def test_inspect_is_offline_and_leaves_all_inputs_unchanged(
    reduced_example: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, result = reduced_example
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inspection attempted a process, network or observation store")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(ObservationStore, "__init__", forbidden)
    args = ["inspect", "retained", str(case), "--result", str(result)]
    inspected = CliRunner().invoke(app, [*args, "--format", "json"])
    assert inspected.exit_code == 0, inspected.output
    report = json.loads(inspected.stdout)
    assert report["command"] == "inspect"
    assert report["mode"] == "deterministic"
    assert report["failed_candidate_observation_count"] == 0
    assert report["confirmation"]["status"] == "confirmed-preserving"
    assert report["nodes"]
    assert all("text" not in n for n in report["nodes"])
    fields = [field for node in report["nodes"] for field in node["text_section_fields"]]
    assert fields
    assert all(field["status"] == "available-untested" for field in fields)
    assert all(field["catalog"]["sections"] for field in fields)
    assert report["limitations"]
    human = CliRunner().invoke(app, args)
    assert human.exit_code == 0, human.output
    assert "Retained context inspection" in human.stdout
    assert "Interpretation limits" in human.stdout
    assert "available-untested" in human.stdout
    assert "original UTF-8 bytes" in human.stdout
    assert before == {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}


def test_inspect_text_requires_opt_in_and_obeys_global_limit(
    reduced_example: tuple[Path, Path],
) -> None:
    case, result = reduced_example
    inspected = CliRunner().invoke(
        app,
        [
            "inspect",
            "retained",
            str(case),
            "--result",
            str(result),
            "--show-text",
            "--max-text-bytes",
            "20",
            "--format",
            "json",
        ],
    )
    assert inspected.exit_code == 0, inspected.output
    report = json.loads(inspected.stdout)
    content = [n.get("text", "") for n in report["nodes"]]
    assert sum(len(t.encode("utf-8")) for t in content) <= 20
    assert report["text_requested"]
    assert report["text_truncated"]


def test_invalid_snapshot_errors_do_not_echo_private_payloads(
    reduced_example: tuple[Path, Path],
) -> None:
    case, result = reduced_example
    path = result / "summary.json"
    data = json.loads(path.read_text())
    data["candidate_digest"] = "PRIVATE_PAYLOAD_FROM_MALFORMED_EVIDENCE"
    path.write_text(json.dumps(data))
    inspected = CliRunner().invoke(
        app, ["inspect", "retained", str(case), "--result", str(result), "--format", "json"]
    )
    assert inspected.exit_code == 2
    error = json.loads(inspected.stdout)
    assert error["command"] == "inspect"
    assert error["error_code"] == "inspection-invalid"
    assert "PRIVATE_PAYLOAD" not in inspected.output


def test_inspect_can_show_a_later_retained_unit_without_displaying_earlier_text(
    reduced_example: tuple[Path, Path],
) -> None:
    case, result = reduced_example
    args = ["inspect", "retained", str(case), "--result", str(result), "--format", "json"]
    runner = CliRunner()
    metadata = runner.invoke(app, args)
    assert metadata.exit_code == 0, metadata.output
    node_id = json.loads(metadata.stdout)["nodes"][-1]["id"]
    selected = runner.invoke(
        app, [*args, "--show-text", "--text-node", node_id, "--max-text-bytes", "20"]
    )
    assert selected.exit_code == 0, selected.output
    report = json.loads(selected.stdout)
    assert report["text_node_id"] == node_id
    assert report["nodes"][-1]["text"]
    assert all(n["text"] is None for n in report["nodes"][:-1])
    assert 0 < report["text_bytes"] <= 20


def test_text_node_requires_explicit_text_display() -> None:
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "retained",
            "case",
            "--result",
            "result",
            "--text-node",
            "unit",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error_code"] == "invalid-inspection-options"


def test_inspect_parser_errors_use_the_json_envelope() -> None:
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "retained",
            "case",
            "--result",
            "result",
            "--max-text-bytes",
            "65537",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    error = json.loads(result.stdout)
    assert error["command"] == "inspect"
    assert error["error_code"] == "invalid-command-line"


@pytest.mark.parametrize("command_name", ["extract", "shrink", "fuzz", "inspect"])
@pytest.mark.parametrize("argument", ["case", "result"])
def test_inspect_parser_error_command_ignores_command_names_in_paths(
    command_name: str, argument: str
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "retained",
            command_name if argument == "case" else "case",
            "--result",
            command_name if argument == "result" else "result",
            "--max-text-bytes",
            "65537",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 2
    error = json.loads(result.stdout)
    assert error["command"] == "inspect"
    assert error["error_code"] == "invalid-command-line"
