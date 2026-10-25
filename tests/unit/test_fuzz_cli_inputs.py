"""Private fuzz input boundaries and content-safe diagnostic rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.cli import app
from ithrl.cli.fuzz_output import FuzzCounts, FuzzOutput, render_fuzz
from ithrl.cli.output import OutputFormat
from ithrl.fuzz.configuration import FuzzConfigurationError, load_fuzz_configuration


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "hardlink", "oversize", "invalid"])
def test_private_configuration_refuses_unsafe_or_unbounded_input(
    tmp_path: Path, unsafe: str
) -> None:
    path = tmp_path / "fuzz.toml"
    path.write_text('stale_text = "PRIVATE-PAYLOAD"\n')
    path.chmod(0o600)
    if unsafe == "permissions":
        if os.name != "posix":
            pytest.skip("POSIX permission boundary")
        path.chmod(0o644)
    elif unsafe == "symlink":
        linked = tmp_path / "linked.toml"
        linked.symlink_to(path)
        path = linked
    elif unsafe == "hardlink":
        (tmp_path / "linked.toml").hardlink_to(path)
    elif unsafe == "oversize":
        path.write_text("x" * (1024 * 1024 + 1))
    before = path.read_bytes()
    with pytest.raises(FuzzConfigurationError) as caught:
        load_fuzz_configuration(path)
    assert "PRIVATE-PAYLOAD" not in str(caught.value)
    assert path.read_bytes() == before


def test_output_counts_errors_separately_from_behavior(capsys: pytest.CaptureFixture[str]) -> None:
    report = FuzzOutput(
        status="incomplete",
        confirmation_original=FuzzCounts(
            dispatched=4, matches=1, no_matches=1, invalid_observations=1, runner_errors=1
        ),
    )
    render_fuzz(report, OutputFormat.JSON)
    data = json.loads(capsys.readouterr().out)
    counts = data["confirmation_original"]
    assert counts["valid"] == 2
    assert counts["match_rate"] == 0.5
    assert counts["valid_rate"] == 0.5
    assert counts["runner_errors"] == 1
    assert data["estimand"] == "valid-only"
    assert data["effect_direction"] == "original-minus-mutant"


def test_fuzz_parser_errors_are_one_json_object() -> None:
    result = CliRunner().invoke(app, ["fuzz", "--not-an-option", "--format", "json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["command"] == "fuzz"
    assert json.loads(result.stdout)["error_code"] == "invalid-command-line"
