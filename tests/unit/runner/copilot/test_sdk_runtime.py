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


def test_runtime_refuses_a_dependency_replaced_with_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _distribution(tmp_path, monkeypatch)
    runtime = sdk_runtime.SDKRuntime()
    module.unlink()
    module.mkdir()
    with pytest.raises(ValueError, match="runtime contains an unsafe file"):
        runtime.assert_unchanged()


@pytest.mark.parametrize("changed_at", [2, 3])
def test_runtime_refuses_dependency_changes_during_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_at: int
) -> None:
    module = _distribution(tmp_path, monkeypatch)
    runtime = sdk_runtime.SDKRuntime()
    signature = sdk_runtime._signature
    calls = 0

    def changed_signature(path: Path) -> tuple[int, int, int, int, int]:
        nonlocal calls
        before = signature(path)
        if path == module:
            calls += 1
            if calls >= changed_at:
                return (*before[:4], before[4] + 1)
        return before

    monkeypatch.setattr(sdk_runtime, "_signature", changed_signature)
    with pytest.raises(ValueError, match="runtime changed during inspection"):
        runtime.assert_unchanged()
    assert calls == changed_at


def test_runtime_mounts_a_python_without_a_shared_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _distribution(tmp_path, monkeypatch)
    config_var = sdk_runtime.sysconfig.get_config_var

    def static_library(name: str) -> object:
        return {"LIBDIR": str(tmp_path), "LDLIBRARY": "missing-library.so"}.get(
            name, config_var(name)
        )

    monkeypatch.setattr(sdk_runtime.sysconfig, "get_config_var", static_library)
    runtime = sdk_runtime.SDKRuntime()
    assert runtime.shared_library is None
    mounts = runtime.mounts("/fixture-state")
    assert str(runtime.python) in mounts
    assert str(runtime.stdlib) in mounts
    assert str(module.parent) in mounts
