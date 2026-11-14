from __future__ import annotations

from importlib.metadata import PackagePath
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ithrl.runner.copilot import sdk_runtime


def _distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "fixture_sdk"
    package.mkdir()
    module = package / "__init__.py"
    module.write_text("value = 1\n")

    def locate_file(entry: str | Path) -> Path:
        return tmp_path / entry

    distribution = SimpleNamespace(
        metadata={"Name": "github-copilot-sdk"},
        requires=(),
        files=(PackagePath("fixture_sdk/__init__.py"),),
        locate_file=locate_file,
    )
    monkeypatch.setattr(
        sdk_runtime.importlib.metadata, "distribution", Mock(return_value=distribution)
    )
    return module


def test_runtime_refuses_an_unselected_python(tmp_path: Path) -> None:
    foreign = tmp_path / "other-python"
    foreign.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="runtime layout cannot be exposed safely"):
        sdk_runtime.SDKRuntime(python=foreign)


@pytest.mark.parametrize(
    ("bound", "message"),
    [("_MAX_FILES", "file inspection limit"), ("_MAX_BYTES", "byte inspection limit")],
)
def test_runtime_bounds_the_selected_dependency_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str, message: str
) -> None:
    _distribution(tmp_path, monkeypatch)
    monkeypatch.setattr(sdk_runtime, bound, 1)
    with pytest.raises(ValueError, match=message):
        sdk_runtime.SDKRuntime()


def test_runtime_refuses_distribution_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _distribution(tmp_path, monkeypatch)
    real = tmp_path / "other.py"
    module.rename(real)
    module.symlink_to(real)
    with pytest.raises(ValueError, match="distribution contains an unsafe file"):
        sdk_runtime.SDKRuntime()


def test_runtime_detects_modified_dependency_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _distribution(tmp_path, monkeypatch)
    runtime = sdk_runtime.SDKRuntime()
    runtime.assert_unchanged()
    assert str(module.parent) in runtime.mounts("/fixture-state")
    module.write_text("value = 2\n")
    with pytest.raises(ValueError, match="runtime changed after selection"):
        runtime.assert_unchanged()
