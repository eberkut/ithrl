"""Research preparation refuses oversized inputs and avoids environment startup hooks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2] / "research/benchmarks"


@pytest.fixture
def replay() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_replay", ROOT / "reproduce.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_is_bounded_before_reading(replay: ModuleType, tmp_path: Path) -> None:
    archive = tmp_path / "iniconfig-source.tar.gz"
    with archive.open("wb") as stream:
        stream.truncate(replay.MAX_ARCHIVE_BYTES + 1)
    with pytest.raises(ValueError, match="compressed-size limit"):
        replay.archive_bytes(tmp_path, "iniconfig")


def test_environment_probe_ignores_pth_startup_code(replay: ModuleType, tmp_path: Path) -> None:

    python = Path("/usr/bin/python3.12")
    if not python.exists():
        pytest.skip("pinned system Python is required")
    import subprocess

    expected_version = (
        subprocess.run(  # noqa: S603 - fixed system interpreter, isolated version probe
            [str(python), "-I", "-S", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .removeprefix("Python ")
    )
    environment = tmp_path / "env"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").symlink_to(python)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
    packages = environment / "lib/python3.12/site-packages"
    metadata = packages / "fixture-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: fixture\nVersion: 1.0\n")
    marker = tmp_path / "executed"
    (packages / "startup.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    )
    _base, actual = replay.environment(
        environment / "bin/python", {"python": expected_version, "packages": {"fixture": "1.0"}}
    )
    assert actual["packages"] == {"fixture": "1.0"}
    assert not marker.exists()
