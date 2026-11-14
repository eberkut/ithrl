"""The module entry point exposes the installed CLI without provider access."""

import runpy
import sys
from importlib.metadata import version

import pytest


def test_importing_the_entrypoint_does_not_start_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_cli() -> None:
        pytest.fail("importing the entrypoint started the CLI")

    monkeypatch.setattr("ithrl.cli.app", unexpected_cli)
    namespace = runpy.run_module("ithrl", run_name="ithrl.__main__")
    assert namespace["app"] is unexpected_cli


def test_module_entrypoint_reports_the_distribution_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["ithrl", "--version"])

    with pytest.raises(SystemExit) as exited:
        runpy.run_module("ithrl", run_name="__main__")

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"ithrl {version('ithrl')}\n"
