"""Compose private native receipts, offline preparation and fresh CLI inspection."""

from __future__ import annotations

import json
import os
import shlex
import socket
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import ithrl.cli.native as native
import ithrl.runner.runtime.process as process_runtime
from integration.test_native_calibration import Harness, Scenario, _scenario
from ithrl.cli import ExitCode, app
from ithrl.cli.paths import native_storage_paths, observation_storage_paths
from ithrl.cli.preparation import preparation_destination
from ithrl.experiment import SampledExperimentDefinition, parse_experiment_toml
from ithrl.observations import ObservationStore
from native_support.smoke import protected_tree

_SETTINGS = """schema_version = 1
kind = "reduction-preparation"

[calibration]
required_valid = 3
minimum_matches = 3
max_attempts = 3

[reduction]
margin = 0.20
freshness_seconds = 7200
eligibility_policy = "independent-safe-preaccept-only"

[reduction.search.original]
required_valid = 1
max_attempts = 1
[reduction.search.candidate]
required_valid = 1
max_attempts = 1

[reduction.confirmation]
one_sided_confidence = 0.95
[reduction.confirmation.original]
required_valid = 11
max_attempts = 11
[reduction.confirmation.candidate]
required_valid = 11
max_attempts = 11
"""


def _receipt(scenario: Scenario, destination: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "shrink",
            str(scenario.case.root),
            "--dry-run",
            "--save-preflight",
            str(destination),
            "--original-workspace",
            str(scenario.original),
            "--required-valid",
            "3",
            "--minimum-matches",
            "3",
            "--max-attempts",
            "3",
            "--attempt-timeout",
            "120",
            "--max-provider-calls-per-attempt",
            str(scenario.calls_per_attempt),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert json.loads(result.stdout)["provider_calls"] == 0
    assert destination.is_file()


def _arguments(scenario: Scenario, receipt: Path, settings: Path, output: Path) -> list[str]:
    return [
        "prepare",
        str(scenario.case.root),
        "--preflight",
        str(receipt),
        "--settings",
        str(settings),
        "--output",
        str(output),
        "--max-candidates",
        "2",
        "--max-provider-calls",
        "1000",
        "--max-wall-time",
        "7200",
        "--format",
        "json",
    ]


def _forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("offline preparation accessed native execution or the observation store")


@pytest.mark.parametrize("harness", ["codex", "copilot"])
@pytest.mark.parametrize("with_sections", [False, True])
def test_prepare_round_trip_and_inspection_command_are_provider_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, with_sections: bool
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, harness, "final-response", mode="context-sensitive")
    monkeypatch.setattr(process_runtime, "_start_process", _forbidden)
    before = (protected_tree(scenario.case.root), protected_tree(scenario.original))
    receipt = tmp_path / "private" / "preflight.json"
    _receipt(scenario, receipt)
    settings = tmp_path / "settings.toml"
    policy = _SETTINGS
    field = None
    if with_sections:
        inspected = CliRunner().invoke(
            app, ["inspect", "sections", str(scenario.case.root), "--format", "json"]
        )
        assert inspected.exit_code == ExitCode.SUCCESS, inspected.output
        fields = json.loads(inspected.stdout)["fields"]
        field = next(value for value in fields if len(value["sections"]) >= 2)
        policy += (
            "\n[sections]\nmax_coarse_comparisons = 1\nmax_fine_comparisons = 1\n"
            "minimum_removed_utf8_bytes = 1\nrequire_criterion_for_confirmation = true\n"
            "stop_when_no_retained_fields = true\n[[sections.fields]]\n"
            f"context_id = {json.dumps(field['context_id'])}\n"
            f"content_index = {field['content_index']}\n"
            f"section_ids = [{json.dumps(field['sections'][0]['id'])}]\n"
        )
        assert "original_text" not in inspected.stdout.replace("original_text_digest", "")
    settings.write_text(policy)
    settings.chmod(0o600)
    output = tmp_path / "private" / "reduction.toml"
    observation_store = tmp_path / "unopened" / "observations.sqlite"
    arguments = [
        *_arguments(scenario, receipt, settings, output),
        "--observation-store",
        str(observation_store),
    ]
    with monkeypatch.context() as offline:
        offline.setattr(native, "native_selection", _forbidden)
        offline.setattr(native, "detect_codex_capability", _forbidden)
        offline.setattr(native, "detect_copilot_capability", _forbidden)
        offline.setattr(subprocess, "Popen", _forbidden)
        offline.setattr(socket, "socket", _forbidden)
        offline.setattr(ObservationStore, "__init__", _forbidden)
        prepared = CliRunner().invoke(app, arguments)
    assert prepared.exit_code == ExitCode.SUCCESS, prepared.output
    report = json.loads(prepared.stdout)
    assert report["status"] == "prepared"
    assert report["provider_calls"] == report["actions_executed"] == 0
    assert not observation_store.parent.exists()
    experiment = parse_experiment_toml(output.read_text())
    assert isinstance(experiment, SampledExperimentDefinition)
    assert experiment == parse_experiment_toml(experiment.to_toml())
    original_experiment = parse_experiment_toml(scenario.case.load().default_experiment)
    assert experiment.target == original_experiment.target
    assert experiment.oracle == original_experiment.oracle
    if field is not None:
        assert experiment.reduction is not None
        assert experiment.reduction.section_search is not None
        (selection,) = experiment.reduction.section_search.fields
        assert selection.original_text_digest == field["original_text_digest"]
        assert selection.policy_digest == field["policy_digest"]
        assert selection.section_ids == (field["sections"][0]["id"],)
    command = shlex.split(report["next_command"])
    assert command.pop(0) == "ithrl"
    assert "--dry-run" in command
    assert "--acknowledge-model-execution" not in command
    inspected = CliRunner().invoke(app, [*command, "--format", "json"])
    assert inspected.exit_code == ExitCode.SUCCESS, inspected.output
    inspected_report = json.loads(inspected.stdout)
    for key, value in report["budget"].items():
        if key != "refusal_code":
            assert inspected_report[key] == value
    assert not observation_store.exists()
    assert not scenario.database.exists()
    assert before == (protected_tree(scenario.case.root), protected_tree(scenario.original))
    assert stat.S_IMODE(receipt.stat().st_mode) == stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize("options", [[], ["--resume"], ["--calibrate-only"]])
def test_receipt_requires_new_explicit_native_dry_run_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, "copilot", "final-response")
    select = Mock(side_effect=_forbidden)
    monkeypatch.setattr(native, "native_selection", select)
    receipt = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        app,
        [
            "shrink",
            str(scenario.case.root),
            *options,
            "--save-preflight",
            str(receipt),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["error_code"] == "invalid-options"
    select.assert_not_called()
    assert not receipt.exists()


def test_receipt_rejects_deterministic_case_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = tmp_path / "case"
    extracted = CliRunner().invoke(app, ["extract", "--example", "--output", str(case)])
    assert extracted.exit_code == ExitCode.SUCCESS, extracted.output
    monkeypatch.setattr(native, "native_selection", _forbidden)
    receipt = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        app,
        ["shrink", str(case), "--dry-run", "--save-preflight", str(receipt), "--format", "json"],
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert not receipt.exists()


@pytest.mark.parametrize("destination", ["existing", "case", "workspace", "receipt", "settings"])
def test_prepare_refuses_overwrite_and_protected_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, "copilot", "final-response")
    receipt, settings = tmp_path / "receipt.json", tmp_path / "settings.toml"
    _receipt(scenario, receipt)
    settings.write_text(_SETTINGS)
    settings.chmod(0o600)
    existing = tmp_path / "existing.toml"
    existing.write_text("do not overwrite")
    output = {
        "existing": existing,
        "case": scenario.case.root / "new.toml",
        "workspace": scenario.original / "new.toml",
        "receipt": receipt,
        "settings": settings,
    }[destination]
    before = {path: path.read_bytes() for path in (receipt, settings, existing)}
    source_before = (protected_tree(scenario.case.root), protected_tree(scenario.original))
    monkeypatch.setattr(native, "native_selection", _forbidden)
    result = CliRunner().invoke(app, _arguments(scenario, receipt, settings, output))
    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["error_code"] == "preparation-invalid"
    assert before == {path: path.read_bytes() for path in before}
    assert source_before == (protected_tree(scenario.case.root), protected_tree(scenario.original))


@pytest.mark.parametrize("corrupt", ["receipt", "settings"])
def test_preparation_errors_do_not_echo_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: str
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, "copilot", "final-response")
    receipt, settings, output = (
        tmp_path / name for name in ("receipt.json", "settings.toml", "out.toml")
    )
    _receipt(scenario, receipt)
    settings.write_text(_SETTINGS)
    settings.chmod(0o600)
    (receipt if corrupt == "receipt" else settings).write_text("private-input-should-not-appear")
    result = CliRunner().invoke(app, _arguments(scenario, receipt, settings, output))
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "private-input-should-not-appear" not in result.output
    assert not output.exists()


@pytest.mark.parametrize("mode", ["receipt", "prepare"])
@pytest.mark.parametrize(
    "location",
    ["native-attempts", "native-output", "native-attempts-child", "native-output-child"]
    + [f"{store}-{index}" for store in ("default", "explicit") for index in range(7)],
)
def test_preparation_outputs_cannot_occupy_native_or_observation_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, location: str
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, "copilot", "final-response")
    explicit_store = tmp_path / "explicit" / "observations.sqlite"
    if location.startswith("native"):
        root = native_storage_paths()[int(location.startswith("native-output"))]
        output = root / "child" / "output" if location.endswith("child") else root
    else:
        store, index = location.split("-")
        output = observation_storage_paths(explicit_store if store == "explicit" else None)[
            int(index)
        ]
    before = (protected_tree(scenario.case.root), protected_tree(scenario.original))
    if mode == "receipt":
        arguments = [
            "shrink",
            str(scenario.case.root),
            "--dry-run",
            "--save-preflight",
            str(output),
            "--original-workspace",
            str(scenario.original),
            "--observation-store",
            str(explicit_store),
            "--format",
            "json",
        ]
        error_code = "preflight-output-invalid"
    else:
        receipt, settings = tmp_path / "receipt.json", tmp_path / "settings.toml"
        _receipt(scenario, receipt)
        settings.write_text(_SETTINGS)
        settings.chmod(0o600)
        arguments = [
            *_arguments(scenario, receipt, settings, output),
            "--observation-store",
            str(explicit_store),
        ]
        error_code = "preparation-invalid"
    select = Mock(side_effect=_forbidden)
    monkeypatch.setattr(native, "native_selection", select)
    monkeypatch.setattr(ObservationStore, "__init__", _forbidden)
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["error_code"] == error_code
    select.assert_not_called()
    assert not output.is_file()
    assert not explicit_store.parent.exists()
    assert not any(path.exists() for path in observation_storage_paths())
    assert before == (protected_tree(scenario.case.root), protected_tree(scenario.original))


@pytest.mark.skipif(os.name != "posix", reason="POSIX double-root path spelling")
@pytest.mark.parametrize("protected_name", ["case", "original"])
@pytest.mark.parametrize("alias_output", [False, True])
def test_double_root_spelling_cannot_bypass_protected_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected_name: str, alias_output: bool
) -> None:
    protected = tmp_path / protected_name
    protected.mkdir()
    source = protected / "source.txt"
    source.write_text("unchanged")
    output = protected / "new.toml"
    alias = Path("//" + str(protected).lstrip("/"))
    monkeypatch.setattr(Path, "resolve", _forbidden)
    with pytest.raises(ValueError, match="overlaps protected state"):
        preparation_destination(
            alias / "new.toml" if alias_output else output,
            protected=(protected if alias_output else alias,),
        )
    assert source.read_text() == "unchanged"
    assert not output.exists()


def test_invalid_prepare_options_report_prepare_command_in_json() -> None:
    result = CliRunner().invoke(app, ["prepare", "--not-a-real-option", "--format", "json"])
    assert result.exit_code == ExitCode.USAGE, result.output
    report = json.loads(result.stdout)
    assert report["command"] == "prepare"
    assert report["error_code"] == "invalid-command-line"
