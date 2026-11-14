"""Saved-source passive inspection needs no native runtime or referenced resources."""

from __future__ import annotations

import builtins
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import ithrl.cli.native as native
from ithrl.adapters.contracts import ParsingLimits
from ithrl.adapters.errors import AdapterError
from ithrl.cli import ExitCode, app
from ithrl.reacquisition import inspect_reacquisition
from unit.test_codex_reacquisition import _call, _compacted, _meta, _output
from unit.test_copilot_reacquisition import _records as copilot_records


def _source(tmp_path: Path, harness: str) -> Path:
    records = (
        [
            _meta(),
            _call("before"),
            _output("before"),
            _compacted(),
            _call("after"),
            _output("after"),
        ]
        if harness == "codex"
        else copilot_records()
    )
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail(
        "passive inspection attempted native, network, authentication or referenced-file access"
    )


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_boundary_listing_then_inspection_are_private_provider_free_and_preserve_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    source = _source(tmp_path, harness)
    before = source.read_bytes(), source.stat()
    original_open = os.open
    opened: list[Path] = []

    def source_only(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert Path(path) == source
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        opened.append(Path(path))
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as passive:
        passive.setattr(subprocess, "Popen", _forbidden)
        passive.setattr(socket, "socket", _forbidden)
        passive.setattr(native, "native_selection", _forbidden)
        passive.setattr(native, "detect_codex_capability", _forbidden)
        passive.setattr(native, "detect_copilot_capability", _forbidden)
        passive.setattr(builtins, "open", _forbidden)
        passive.setattr(Path, "open", _forbidden)
        passive.setattr(os, "open", source_only)
        listing = CliRunner().invoke(app, ["inspect", "reacquire", str(source), "--format", "json"])
        assert listing.exit_code == ExitCode.SUCCESS, listing.output
        listed = json.loads(listing.stdout)
        assert listed["groups"] == []
        assert len(listed["boundaries"]) == 1
        selected = CliRunner().invoke(
            app,
            [
                "inspect",
                "reacquire",
                str(source),
                "--boundary",
                listed["boundaries"][0]["id"],
                "--format",
                "json",
            ],
        )
        assert selected.exit_code == ExitCode.SUCCESS, selected.output
    report = json.loads(selected.stdout)
    assert len(report["groups"]) == 1
    assert report["groups"][0]["operation_relation"] == "exact-recorded-operation"
    assert {item["relation"] for item in report["groups"][0]["result_relations"]} == {"equal"}
    assert report["provider_calls"] == report["actions_executed"] == 0
    assert source.read_bytes() == before[0]
    after = source.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before[1].st_ino,
        before[1].st_size,
        before[1].st_mtime_ns,
    )
    assert opened == [source, source]
    assert str(source) not in selected.stdout
    assert "/fixture/" not in selected.stdout
    assert "fixture.txt" not in selected.stdout
    assert "fixture text" not in selected.stdout
    assert "return 1" not in selected.stdout


def test_human_report_and_opt_in_text_keep_control_characters_escaped(tmp_path: Path) -> None:
    source = _source(tmp_path, "codex")
    unselected = CliRunner().invoke(app, ["inspect", "reacquire", str(source)])
    assert unselected.exit_code == ExitCode.SUCCESS, unselected.output
    assert "Passive compaction inspection" in unselected.stdout
    assert "lines 4-4" in unselected.stdout
    assert "Select a boundary with --boundary" in unselected.stdout
    listing = json.loads(
        CliRunner().invoke(app, ["inspect", "reacquire", str(source), "--format", "json"]).stdout
    )
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "reacquire",
            str(source),
            "--boundary",
            listing["boundaries"][0]["id"],
            "--show-text",
            "--max-text-bytes",
            "23",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Passive compaction inspection" in result.stdout
    assert "text bytes: 23; truncated: True" in result.stdout


def test_human_report_explains_absence_of_matching_groups(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    records = [_meta(), _call("before"), _output("before"), _compacted()]
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    runner = CliRunner()
    listing = runner.invoke(app, ["inspect", "reacquire", str(source), "--format", "json"])
    assert listing.exit_code == ExitCode.SUCCESS, listing.output
    boundary = json.loads(listing.stdout)["boundaries"][0]["id"]
    result = runner.invoke(app, ["inspect", "reacquire", str(source), "--boundary", boundary])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "No eligible matching groups in the selected adjacent epochs." in result.stdout
    assert "savings: not estimated" in result.stdout


@pytest.mark.parametrize("variant", ["malformed", "unsupported", "symlink", "missing-boundary"])
def test_invalid_source_and_boundary_fail_without_echoing_private_content(
    tmp_path: Path, variant: str
) -> None:
    source = _source(tmp_path, "copilot")
    arguments = ["inspect", "reacquire", str(source), "--format", "json"]
    expected = "invalid-jsonl"
    if variant == "malformed":
        source.write_text("private-input-must-not-appear\n")
    elif variant == "unsupported":
        source.write_text('{"private-input-must-not-appear": 1}\n')
        expected = "unsupported-format"
    elif variant == "symlink":
        link = tmp_path / "linked.jsonl"
        link.symlink_to(source)
        arguments[2] = str(link)
        expected = "unsafe-source"
    else:
        arguments += ["--boundary", "private-input-must-not-appear"]
        expected = "reacquisition-invalid"
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["error_code"] == expected
    assert "private-input-must-not-appear" not in result.stdout
    assert str(source) not in result.stdout


def test_source_size_and_record_limits_are_enforced_before_projection(tmp_path: Path) -> None:
    source = _source(tmp_path, "codex")
    for limits, expected in (
        (ParsingLimits(max_bytes=32, max_line_bytes=32), "source-too-large"),
        (ParsingLimits(max_records=2), "too-many-records"),
    ):
        with pytest.raises(AdapterError) as refused:
            inspect_reacquisition(source, limits=limits)
        assert refused.value.code.value == expected


@pytest.mark.parametrize(
    "options",
    [
        ["--max-groups", "0"],
        ["--max-groups", "1001"],
        ["--max-text-bytes", "0"],
        ["--max-text-bytes", "65537"],
    ],
)
def test_invalid_report_options_refuse(tmp_path: Path, options: list[str]) -> None:
    source = _source(tmp_path, "codex")
    result = CliRunner().invoke(
        app, ["inspect", "reacquire", str(source), *options, "--format", "json"]
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["error_code"] == "invalid-command-line"
