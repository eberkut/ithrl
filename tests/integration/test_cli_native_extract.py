from __future__ import annotations

import json
import shutil
import socket
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.adapters import DecisionSummary, enumerate_decisions, parse_session
from ithrl.case import CaseWorkspace
from ithrl.cli import ExitCode, app
from ithrl.runner import FakeRunner

FIXTURES = Path(__file__).parents[1] / "fixtures"
PRIVATE_MARKERS = ("Fixture complete.", "fixture-command", "fixture-value")


@dataclass(frozen=True, slots=True)
class _FixtureSelection:
    harness: str
    source: Path
    session_id: str
    decision: DecisionSummary


def _always_interactive(_format: object) -> bool:
    return True


def _fixture_selection(harness: str) -> _FixtureSelection:
    source = {
        "codex": FIXTURES / "codex-session" / "rollout.jsonl",
        "copilot": FIXTURES / "copilot-session" / "events.jsonl",
    }[harness]
    session = parse_session(source)
    decisions = enumerate_decisions(session)
    decision = next(
        item
        for item in decisions
        if any(target.type == "final-response" for target in item.targets)
    )
    return _FixtureSelection(
        harness=harness,
        source=source,
        session_id=session.metadata.session_id,
        decision=decision,
    )


def _native_arguments(
    selection: _FixtureSelection,
    case: Path,
    *,
    target: str | None = None,
) -> list[str]:
    selected_target = selection.decision.targets[0]
    return [
        "extract",
        str(selection.source),
        "--harness",
        selection.harness,
        "--decision",
        selection.decision.decision_id,
        "--target",
        target or selected_target.target_id,
        "--output",
        str(case),
    ]


def _install_default_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: _FixtureSelection,
) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    if selection.harness == "codex":
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        destination = (
            codex_home / f"sessions/2026/01/01/rollout-fixture-{selection.session_id}.jsonl"
        )
    else:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
        destination = home / f".copilot/session-state/{selection.session_id}/events.jsonl"
    destination.parent.mkdir(parents=True)
    shutil.copyfile(selection.source, destination)
    return destination


def _copilot_shell_selection(tmp_path: Path) -> _FixtureSelection:
    source = tmp_path / "copilot-shell.jsonl"
    records = [
        json.loads(line)
        for line in (FIXTURES / "copilot-session/events.jsonl").read_text().splitlines()
    ]
    requests = records[11]["data"]["toolRequests"]
    requests[0].update({"name": "bash", "arguments": {"command": "fixture-command-one"}})
    requests.append(
        {
            "toolCallId": "80000000-0000-4000-8000-000000000002",
            "name": "shell",
            "arguments": {"command": "fixture-command-two"},
        }
    )
    records[12]["data"].update(
        {"toolName": "bash", "arguments": {"command": "fixture-command-one"}}
    )
    source.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    )
    session = parse_session(source)
    decision = next(
        item
        for item in enumerate_decisions(session)
        if any(target.type == "shell-action" for target in item.targets)
    )
    return _FixtureSelection(
        harness="copilot",
        source=source,
        session_id=session.metadata.session_id,
        decision=decision,
    )


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_explicit_native_source_creates_the_selected_case(
    tmp_path: Path,
    harness: str,
) -> None:
    selection = _fixture_selection(harness)
    case = tmp_path / f"{harness}-case"

    result = CliRunner().invoke(
        app,
        [*_native_arguments(selection, case), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "case-created"
    assert report["case_path"] == str(case)
    assert report["harness"] == harness
    assert report["session_id"] == selection.session_id
    assert report["decision_id"] == selection.decision.decision_id
    assert report["target_id"] == selection.decision.targets[0].target_id
    assert report["target_type"] == "final-response"
    assert report["capture_type"] == "logical-session"
    assert report["capture_completeness"] == "complete"
    assert report["replay_state"] == "harness-local"
    assert report["replay_environment"] == "same-harness-account"
    assert report["evidence_support"] == "supported"
    assert report["experimental_support"] == "unvalidated"
    assert "experimental-support-unvalidated" in report["limitations"]
    assert report["provider_calls"] == 0
    assert report["network_calls"] == 0
    assert report["actions_executed"] == 0
    evidence = CaseWorkspace.open(case).load()
    assert evidence.decision is not None
    assert evidence.decision.decision_id == selection.decision.decision_id
    assert evidence.decision.selected_target_id == selection.decision.targets[0].target_id


def test_explicit_native_detection_does_not_depend_on_the_file_suffix(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")
    source = tmp_path / "saved-session.json"
    shutil.copyfile(selection.source, source)

    result = CliRunner().invoke(
        app,
        ["extract", str(source), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "selection-required"
    assert selection.decision.decision_id in report["message"]


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_default_store_resolves_a_stable_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    selection = _fixture_selection(harness)
    _install_default_store(tmp_path, monkeypatch, selection)
    case = tmp_path / "case"

    result = CliRunner().invoke(
        app,
        [
            "extract",
            "--harness",
            harness,
            "--session",
            selection.session_id,
            "--decision",
            selection.decision.decision_id,
            "--target",
            selection.decision.targets[0].target_id,
            "--output",
            str(case),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert report["harness"] == harness
    assert report["session_id"] == selection.session_id
    assert report["decision_id"] == selection.decision.decision_id
    assert CaseWorkspace.open(case).load().manifest.harness == harness


def test_unique_target_type_is_accepted_as_shorthand(tmp_path: Path) -> None:
    selection = _fixture_selection("copilot")

    result = CliRunner().invoke(
        app,
        [
            *_native_arguments(selection, tmp_path / "case", target="final-response"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert report["target_type"] == "final-response"
    assert report["target_id"] == selection.decision.targets[0].target_id


def test_human_and_json_output_report_the_same_content_free_selection(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")
    runner = CliRunner()

    human = runner.invoke(
        app,
        _native_arguments(selection, tmp_path / "human-case"),
        color=False,
    )
    machine = runner.invoke(
        app,
        [
            *_native_arguments(selection, tmp_path / "json-case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert human.exit_code == ExitCode.SUCCESS, human.output
    assert machine.exit_code == ExitCode.SUCCESS, machine.output
    report = json.loads(machine.stdout)
    for value in (
        report["harness"],
        report["harness_version"],
        report["session_id"],
        report["decision_id"],
        report["target_id"],
        report["target_type"],
        report["capture_type"],
        report["capture_completeness"],
        report["replay_state"],
        report["replay_environment"],
        report["evidence_support"],
        report["experimental_support"],
    ):
        assert value in human.stdout
    assert "controlled experiment not validated" in human.stdout
    assert "opaque context kept" in human.stdout
    assert report["case_digest"][:12] in human.stdout
    for marker in PRIVATE_MARKERS:
        assert marker not in human.output
        assert marker not in machine.output
    assert "No network requests or provider calls. No actions executed." in human.stdout


def test_native_extract_does_not_use_a_runner_network_or_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _fixture_selection("copilot")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native extraction attempted an external operation")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(FakeRunner, "run", forbidden)

    result = CliRunner().invoke(
        app,
        [
            *_native_arguments(selection, tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert (report["network_calls"], report["provider_calls"], report["actions_executed"]) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_default_store_is_not_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    selection = _fixture_selection(harness)
    stored_source = _install_default_store(tmp_path, monkeypatch, selection)
    original_bytes = stored_source.read_bytes()
    original_metadata = stored_source.stat()

    result = CliRunner().invoke(
        app,
        [
            "extract",
            "--harness",
            harness,
            "--session",
            selection.session_id,
            "--decision",
            selection.decision.decision_id,
            "--target",
            selection.decision.targets[0].target_id,
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    current_metadata = stored_source.stat()
    assert stored_source.read_bytes() == original_bytes
    assert current_metadata.st_mtime_ns == original_metadata.st_mtime_ns
    assert current_metadata.st_mode == original_metadata.st_mode
    assert tuple(
        path.relative_to(stored_source.parent) for path in stored_source.parent.iterdir()
    ) == (stored_source.relative_to(stored_source.parent),)


def test_noninteractive_native_extract_requires_a_decision(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")

    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(selection.source),
            "--target",
            "final-response",
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "selection-required"
    assert "--decision is required in non-interactive mode" in report["message"]
    assert selection.decision.decision_id in report["message"]
    for marker in PRIVATE_MARKERS:
        assert marker not in result.output


def test_noninteractive_discovery_requires_a_stable_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _fixture_selection("codex")
    _install_default_store(tmp_path, monkeypatch, selection)

    result = CliRunner().invoke(
        app,
        ["extract", "--harness", "codex", "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "selection-required"
    assert "--harness and --session are required in non-interactive mode" in report["message"]
    assert f"codex:{selection.session_id}" in report["message"]
    for marker in PRIVATE_MARKERS:
        assert marker not in result.output


def test_noninteractive_native_extract_requires_a_target(tmp_path: Path) -> None:
    selection = _fixture_selection("copilot")

    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(selection.source),
            "--decision",
            selection.decision.decision_id,
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "selection-required"
    assert "--target is required in non-interactive mode" in report["message"]
    assert selection.decision.targets[0].target_id in report["message"]
    for marker in PRIVATE_MARKERS:
        assert marker not in result.output


def test_explicit_source_refuses_a_harness_mismatch(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")

    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(selection.source),
            "--harness",
            "copilot",
            "--decision",
            selection.decision.decision_id,
            "--target",
            selection.decision.targets[0].target_id,
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.UNSUPPORTED
    report = json.loads(result.stdout)
    assert report["error_code"] == "unsupported-format"
    assert report["message"] == "session source does not match --harness"
    for marker in PRIVATE_MARKERS:
        assert marker not in result.output


def test_explicit_source_preserves_a_recognized_native_error(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [
        json.loads(line)
        for line in (FIXTURES / "codex-session/rollout.jsonl").read_text().splitlines()
    ]
    del records[0]["payload"]["id"]
    source.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = CliRunner().invoke(
        app,
        ["extract", str(source), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "invalid-session"
    assert "source session identifier" in report["message"]


def test_missing_default_store_reports_no_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))

    result = CliRunner().invoke(
        app,
        ["extract", "--harness", "codex", "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.UNSUPPORTED
    assert json.loads(result.stdout) == {
        "actions_executed": 0,
        "command": "extract",
        "error_code": "no-sessions",
        "exit_code": ExitCode.UNSUPPORTED,
        "kind": "deterministic",
        "message": "no supported completed sessions were found in the selected local stores",
        "network_calls": 0,
        "provider_call_accounting": "not-started",
        "provider_calls": 0,
        "schema_version": 1,
        "status": "error",
    }


def test_synthetic_and_native_options_cannot_be_combined(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(FIXTURES / "synthetic-trace.json"),
            "--inference",
            "synthetic-ac-001",
            "--harness",
            "codex",
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["error_code"] == "conflicting-options"
    assert report["message"] == "--inference cannot be combined with native session options"
    assert not (tmp_path / "case").exists()


def test_native_selection_options_report_precise_usage_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    selection = _fixture_selection("codex")

    unsupported = runner.invoke(
        app,
        ["extract", "--harness", "unknown", "--format", "json"],
        color=False,
    )
    missing_harness = runner.invoke(
        app,
        ["extract", "--session", selection.session_id, "--format", "json"],
        color=False,
    )
    mismatched_session = runner.invoke(
        app,
        [
            *_native_arguments(selection, tmp_path / "case"),
            "--session",
            "another-session",
            "--format",
            "json",
        ],
        color=False,
    )

    assert json.loads(unsupported.stdout)["error_code"] == "unsupported-harness"
    assert json.loads(missing_harness.stdout)["error_code"] == "selection-required"
    assert json.loads(mismatched_session.stdout)["error_code"] == "session-not-found"
    assert not (tmp_path / "case").exists()


def test_synthetic_inference_requires_a_source_path() -> None:
    result = CliRunner().invoke(
        app,
        ["extract", "--inference", "inference-1", "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error_code"] == "conflicting-options"


def test_existing_output_path_has_a_specific_refusal(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")
    case = tmp_path / "case"
    case.mkdir()

    result = CliRunner().invoke(
        app,
        [*_native_arguments(selection, case), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error_code"] == "case-exists"


def test_rejected_output_traversal_does_not_create_a_parent(tmp_path: Path) -> None:
    selection = _fixture_selection("codex")
    parent = tmp_path / "not-created"

    result = CliRunner().invoke(
        app,
        [
            *_native_arguments(selection, parent / ".." / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error_code"] == "unsafe-case-path"
    assert not parent.exists()


def test_guided_selection_uses_content_free_session_and_decision_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _fixture_selection("codex")
    _install_default_store(tmp_path, monkeypatch, selection)
    extract_module = import_module("ithrl.cli.extract")

    def interactive(_format: object) -> bool:
        return True

    monkeypatch.setattr(extract_module, "_is_interactive", interactive)
    case = tmp_path / "case"

    result = CliRunner().invoke(
        app,
        ["extract", "--harness", "codex", "--output", str(case)],
        input="1\n2\n",
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Recent sessions" in result.stdout
    assert selection.session_id in result.stdout
    assert selection.decision.decision_id in result.stdout
    for marker in PRIVATE_MARKERS:
        assert marker not in result.output
    assert CaseWorkspace.open(case).load().manifest.harness == "codex"


def test_guided_selection_cancellation_is_not_an_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _fixture_selection("codex")
    _install_default_store(tmp_path, monkeypatch, selection)
    extract_module = import_module("ithrl.cli.extract")
    monkeypatch.setattr(extract_module, "_is_interactive", _always_interactive)

    result = CliRunner().invoke(
        app,
        ["extract", "--harness", "codex", "--output", str(tmp_path / "case")],
        input="",
        color=False,
    )

    assert result.exit_code == ExitCode.CANCELLED
    assert "Selection cancelled." in result.output
    assert "internal error" not in result.output
    assert not (tmp_path / "case").exists()


def test_guided_selection_handles_multiple_decisions_and_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _copilot_shell_selection(tmp_path)
    _install_default_store(tmp_path, monkeypatch, selection)
    extract_module = import_module("ithrl.cli.extract")
    monkeypatch.setattr(extract_module, "_is_interactive", _always_interactive)
    case = tmp_path / "case"

    result = CliRunner().invoke(
        app,
        ["extract", "--harness", "copilot", "--output", str(case)],
        input="1\n1\n2\n",
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Completed decisions" in result.stdout
    assert "Behavior targets" in result.stdout
    assert selection.decision.targets[1].target_id in result.stdout
    evidence = CaseWorkspace.open(case).load()
    assert evidence.decision is not None
    assert evidence.decision.selected_target_id == selection.decision.targets[1].target_id
