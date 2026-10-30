from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
RUNTIME_MODULES = frozenset(
    {
        PurePosixPath("ithrl/__init__.py"),
        PurePosixPath("ithrl/__main__.py"),
        PurePosixPath("ithrl/adapters/__init__.py"),
        PurePosixPath("ithrl/adapters/_jsonl.py"),
        PurePosixPath("ithrl/adapters/codex/__init__.py"),
        PurePosixPath("ithrl/adapters/codex/context.py"),
        PurePosixPath("ithrl/adapters/codex/decisions.py"),
        PurePosixPath("ithrl/adapters/codex/discovery.py"),
        PurePosixPath("ithrl/adapters/codex/parser.py"),
        PurePosixPath("ithrl/adapters/contracts.py"),
        PurePosixPath("ithrl/adapters/copilot/__init__.py"),
        PurePosixPath("ithrl/adapters/copilot/context.py"),
        PurePosixPath("ithrl/adapters/copilot/decisions.py"),
        PurePosixPath("ithrl/adapters/copilot/discovery.py"),
        PurePosixPath("ithrl/adapters/copilot/parser.py"),
        PurePosixPath("ithrl/adapters/errors.py"),
        PurePosixPath("ithrl/adapters/discovery.py"),
        PurePosixPath("ithrl/adapters/registry.py"),
        PurePosixPath("ithrl/behavior.py"),
        PurePosixPath("ithrl/calibration.py"),
        PurePosixPath("ithrl/case/__init__.py"),
        PurePosixPath("ithrl/case/contracts.py"),
        PurePosixPath("ithrl/case/evidence.py"),
        PurePosixPath("ithrl/case/filesystem.py"),
        PurePosixPath("ithrl/case/source.py"),
        PurePosixPath("ithrl/case/source_context.py"),
        PurePosixPath("ithrl/case/source_storage.py"),
        PurePosixPath("ithrl/case/storage.py"),
        PurePosixPath("ithrl/cli/__init__.py"),
        PurePosixPath("ithrl/cli/fuzz.py"),
        PurePosixPath("ithrl/cli/fuzz_run.py"),
        PurePosixPath("ithrl/cli/fuzz_output.py"),
        PurePosixPath("ithrl/cli/fuzz_session.py"),
        PurePosixPath("ithrl/cli/extract.py"),
        PurePosixPath("ithrl/cli/native.py"),
        PurePosixPath("ithrl/cli/output.py"),
        PurePosixPath("ithrl/cli/progress.py"),
        PurePosixPath("ithrl/cli/paths.py"),
        PurePosixPath("ithrl/cli/sampled.py"),
        PurePosixPath("ithrl/cli/sampled_output.py"),
        PurePosixPath("ithrl/cli/sampled_reduction.py"),
        PurePosixPath("ithrl/cli/sampled_session.py"),
        PurePosixPath("ithrl/cli/shrink.py"),
        PurePosixPath("ithrl/context.py"),
        PurePosixPath("ithrl/decisions.py"),
        PurePosixPath("ithrl/execution/__init__.py"),
        PurePosixPath("ithrl/execution/budget.py"),
        PurePosixPath("ithrl/execution/contracts.py"),
        PurePosixPath("ithrl/execution/preflight.py"),
        PurePosixPath("ithrl/experiment.py"),
        PurePosixPath("ithrl/extraction.py"),
        PurePosixPath("ithrl/fuzz/__init__.py"),
        PurePosixPath("ithrl/fuzz/contracts.py"),
        PurePosixPath("ithrl/fuzz/configuration.py"),
        PurePosixPath("ithrl/fuzz/campaign.py"),
        PurePosixPath("ithrl/fuzz/progress.py"),
        PurePosixPath("ithrl/fuzz/deterministic.py"),
        PurePosixPath("ithrl/fuzz/generation.py"),
        PurePosixPath("ithrl/fuzz/sampling.py"),
        PurePosixPath("ithrl/fuzz/simplify.py"),
        PurePosixPath("ithrl/fuzz/statistics.py"),
        PurePosixPath("ithrl/measurement.py"),
        PurePosixPath("ithrl/mutation.py"),
        PurePosixPath("ithrl/observations.py"),
        PurePosixPath("ithrl/path_safety.py"),
        PurePosixPath("ithrl/reduce/__init__.py"),
        PurePosixPath("ithrl/reduce/contracts.py"),
        PurePosixPath("ithrl/reduce/engine.py"),
        PurePosixPath("ithrl/reduce/search.py"),
        PurePosixPath("ithrl/results/__init__.py"),
        PurePosixPath("ithrl/results/models.py"),
        PurePosixPath("ithrl/results/storage.py"),
        PurePosixPath("ithrl/results/summary.py"),
        PurePosixPath("ithrl/results/validation.py"),
        PurePosixPath("ithrl/run_state.py"),
        PurePosixPath("ithrl/sampling.py"),
        PurePosixPath("ithrl/runner/__init__.py"),
        PurePosixPath("ithrl/runner/contracts.py"),
        PurePosixPath("ithrl/runner/codex/__init__.py"),
        PurePosixPath("ithrl/runner/codex/capability.py"),
        PurePosixPath("ithrl/runner/codex/configuration.py"),
        PurePosixPath("ithrl/runner/codex/isolation.py"),
        PurePosixPath("ithrl/runner/codex/materialize.py"),
        PurePosixPath("ithrl/runner/codex/policy.py"),
        PurePosixPath("ithrl/runner/codex/protocol.py"),
        PurePosixPath("ithrl/runner/codex/protocol_contracts.py"),
        PurePosixPath("ithrl/runner/codex/relay.py"),
        PurePosixPath("ithrl/runner/codex/runner.py"),
        PurePosixPath("ithrl/runner/codex/runtime_configuration.py"),
        PurePosixPath("ithrl/runner/codex/schema.py"),
        PurePosixPath("ithrl/runner/codex/transcript.py"),
        PurePosixPath("ithrl/runner/codex/worker.py"),
        PurePosixPath("ithrl/runner/copilot/__init__.py"),
        PurePosixPath("ithrl/runner/copilot/capability.py"),
        PurePosixPath("ithrl/runner/copilot/configuration.py"),
        PurePosixPath("ithrl/runner/copilot/isolation.py"),
        PurePosixPath("ithrl/runner/copilot/materialize.py"),
        PurePosixPath("ithrl/runner/copilot/policy.py"),
        PurePosixPath("ithrl/runner/copilot/protocol/__init__.py"),
        PurePosixPath("ithrl/runner/copilot/protocol/contracts.py"),
        PurePosixPath("ithrl/runner/copilot/protocol/decoding.py"),
        PurePosixPath("ithrl/runner/copilot/protocol/parser.py"),
        PurePosixPath("ithrl/runner/copilot/protocol/state.py"),
        PurePosixPath("ithrl/runner/copilot/runner.py"),
        PurePosixPath("ithrl/runner/copilot/worker.py"),
        PurePosixPath("ithrl/runner/evidence.py"),
        PurePosixPath("ithrl/runner/fake.py"),
        PurePosixPath("ithrl/runner/registry.py"),
        PurePosixPath("ithrl/runner/runtime/__init__.py"),
        PurePosixPath("ithrl/runner/runtime/certificates.py"),
        PurePosixPath("ithrl/runner/runtime/contracts.py"),
        PurePosixPath("ithrl/runner/runtime/output.py"),
        PurePosixPath("ithrl/runner/runtime/process.py"),
        PurePosixPath("ithrl/runner/runtime/protection.py"),
        PurePosixPath("ithrl/runner/runtime/state.py"),
        PurePosixPath("ithrl/runner/validation.py"),
        PurePosixPath("ithrl/synthetic.py"),
        PurePosixPath("ithrl/statistics.py"),
        PurePosixPath("ithrl/timestamps.py"),
        PurePosixPath("ithrl/workflow.py"),
    }
)

RUNTIME_ASSETS = frozenset({PurePosixPath("ithrl/examples/synthetic-trace.json")})
PUBLIC_DOCS = frozenset(
    PurePosixPath("docs", name)
    for name in (
        "index.md",
        "native-runs.md",
        "results.md",
        "cli.md",
        "troubleshooting.md",
        "measurements.md",
        "architecture.md",
        "third-party-notices.txt",
    )
) | frozenset(
    PurePosixPath("docs", directory, path.name)
    for directory in ("decisions", "research")
    for path in (PROJECT_ROOT / "docs" / directory).glob("*.md")
)


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    configured_output = os.environ.get("ITHRL_DIST_DIR")
    if configured_output is None:
        output = tmp_path_factory.mktemp("distributions")
        _run(
            "uv",
            "build",
            "--sdist",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(output),
            cwd=PROJECT_ROOT,
        )
    else:
        output = Path(configured_output).resolve()
    return _one_distribution(output, "*.whl"), _one_distribution(output, "*.tar.gz")


def test_wheel_contains_only_runtime_package_and_metadata(
    distributions: tuple[Path, Path],
) -> None:
    wheel, _ = distributions
    with zipfile.ZipFile(wheel) as archive:
        members = tuple(PurePosixPath(name) for name in archive.namelist())

    packaged_modules = {member for member in members if member.suffix == ".py"}
    roots = {member.parts[0] for member in members}

    assert packaged_modules == RUNTIME_MODULES
    assert {member for member in members if member.parts[0] == "ithrl"} == (
        RUNTIME_MODULES | RUNTIME_ASSETS
    )
    assert "ithrl" in roots
    assert len(roots) == 2
    assert any(root.endswith(".dist-info") for root in roots)
    assert any(member.name == "entry_points.txt" for member in members)
    assert any(member.name == "LICENSE" for member in members)
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert _wheel_version(wheel) == project["project"]["version"]
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    assert metadata["Summary"] == project["project"]["description"]
    assert metadata["Requires-Python"] == ">=3.12"
    assert metadata["License-Expression"] == "Apache-2.0"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert metadata["Author"] == "Vincent Morel"
    assert metadata["Author-email"] is None
    assert set(metadata.get_all("Classifier", [])) == set(project["project"]["classifiers"])
    assert set(metadata.get_all("Project-URL", [])) == {
        f"{label}, {url}" for label, url in project["project"]["urls"].items()
    }
    _assert_no_private_or_checkout_content(members)


def test_sdist_contains_all_inputs_needed_to_rebuild_the_wheel(
    distributions: tuple[Path, Path],
) -> None:
    _, sdist = distributions
    with tarfile.open(sdist) as archive:
        members = tuple(PurePosixPath(member.name) for member in archive.getmembers())

    relative_members = tuple(PurePosixPath(*member.parts[1:]) for member in members)
    expected_files = {PurePosixPath("src", module) for module in RUNTIME_MODULES}

    assert {
        PurePosixPath("LICENSE"),
        PurePosixPath("CHANGELOG.md"),
        PurePosixPath("README.md"),
        PurePosixPath("ROADMAP.md"),
        PurePosixPath("CONTRIBUTING.md"),
        PurePosixPath("pyproject.toml"),
    } <= set(relative_members)
    assert {member for member in relative_members if member.parts[0] == "docs"} == PUBLIC_DOCS
    packaged_sources = {
        member
        for member in relative_members
        if member.parts[:2] == ("src", "ithrl") and member.suffix == ".py"
    }
    assert packaged_sources == expected_files
    assert {member for member in relative_members if member.parts[:2] == ("src", "ithrl")} == (
        expected_files | {PurePosixPath("src", asset) for asset in RUNTIME_ASSETS}
    )
    assert {member.parts[0] for member in relative_members if member.parts} <= {
        ".gitignore",
        "LICENSE",
        "CHANGELOG.md",
        "PKG-INFO",
        "README.md",
        "ROADMAP.md",
        "CONTRIBUTING.md",
        "pyproject.toml",
        "src",
        "docs",
    }
    _assert_no_private_or_checkout_content(relative_members)


@pytest.mark.parametrize("origin", ["wheel", "sdist"])
def test_clean_install_completes_credential_free_workflow(
    distributions: tuple[Path, Path], tmp_path: Path, origin: str
) -> None:
    wheel, sdist = distributions
    expected_version = _wheel_version(wheel)
    if origin == "sdist":
        wheel = _rebuild_wheel(sdist, tmp_path)
        assert _wheel_version(wheel) == expected_version
    _assert_installed_workflow(wheel, tmp_path, expected_version)


def _rebuild_wheel(sdist: Path, tmp_path: Path) -> Path:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(source_parent, filter="data")
    extracted_roots = tuple(source_parent.iterdir())
    assert len(extracted_roots) == 1
    source_root = extracted_roots[0]

    rebuilt = tmp_path / "rebuilt"
    _run(
        "uv",
        "build",
        "--wheel",
        "--no-sources",
        "--out-dir",
        str(rebuilt),
        str(source_root),
        cwd=tmp_path,
    )
    return _one_distribution(rebuilt, "*.whl")


def _assert_installed_workflow(wheel: Path, tmp_path: Path, expected_version: str) -> None:
    environment = tmp_path / "environment"
    _run("uv", "venv", "--python", sys.executable, str(environment), cwd=tmp_path)
    executable = environment / ("Scripts/ithrl.exe" if os.name == "nt" else "bin/ithrl")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run("uv", "pip", "install", "--python", str(python), str(wheel), cwd=tmp_path)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    case = runtime / "case"
    child_environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TERM",
            "TMP",
            "TMPDIR",
            "WINDIR",
        )
        if name in os.environ
    }
    child_environment["NO_COLOR"] = "1"
    child_environment["XDG_DATA_HOME"] = str(runtime / "data")
    child_environment["HOME"] = str(runtime / "home")

    help_result = _run(str(executable), "--help", cwd=runtime, env=child_environment)
    assert "Test how agent context affects a model decision" in help_result.stdout
    version_result = _run(str(executable), "--version", cwd=runtime, env=child_environment)
    assert version_result.stdout == f"ithrl {expected_version}\n"
    extract_help = _run(str(executable), "extract", "--help", cwd=runtime, env=child_environment)
    assert "--example" in extract_help.stdout
    completion = _run(
        str(executable),
        cwd=runtime,
        env=child_environment | {"_ITHRL_COMPLETE": "source_zsh"},
    )
    assert "#compdef ithrl" in completion.stdout
    extract_result = _run(
        str(executable),
        "extract",
        "--example",
        "--output",
        str(case),
        cwd=runtime,
        env=child_environment,
    )
    assert "No network requests or provider calls" in extract_result.stdout
    repeated = _run(
        str(executable),
        "extract",
        "--example",
        "--output",
        str(case),
        "--format",
        "json",
        cwd=runtime,
        env=child_environment,
        expected_exits=(2,),
    )
    assert json.loads(repeated.stdout)["error_code"] == "case-exists"
    dry_run = _run(
        str(executable),
        "shrink",
        str(case),
        "--dry-run",
        "--format",
        "json",
        cwd=runtime,
        env=child_environment,
    )
    assert json.loads(dry_run.stdout)["status"] == "ready"
    shrink_result = _run(
        str(executable),
        "shrink",
        str(case),
        cwd=runtime,
        env=child_environment,
    )
    assert "1-minimal; all valid single removals tested" in shrink_result.stdout
    assert "No network requests or provider calls" in shrink_result.stdout
    (summary_path,) = (case / "results").glob("*/summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["confirmation_result"] == "confirmed-preserving"
    assert summary["provider_calls"] == summary["actions_executed"] == 0
    request = json.loads((summary_path.parent / "request.json").read_text(encoding="utf-8"))
    assert [item["content"] for item in request["input"]] == ["A", "C"]
    _assert_installed_resume(executable, runtime, child_environment)
    _run(
        str(python),
        "-c",
        "from importlib.util import find_spec\n"
        "assert find_spec('scipy') is None\n"
        "from ithrl.fuzz.statistics import require_fuzz_numerics\n"
        "try:\n"
        "    require_fuzz_numerics()\n"
        "except ImportError as error:\n"
        "    assert 'ithrl[fuzz]' in str(error)\n"
        "else:\n"
        "    raise AssertionError('missing optional numerical dependency was accepted')\n",
        cwd=runtime,
        env=child_environment,
    )


def test_installed_fuzz_extra_provides_exact_numerics(
    distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    wheel, _ = distributions
    environment = tmp_path / "fuzz-environment"
    _run("uv", "venv", "--python", sys.executable, str(environment), cwd=tmp_path)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run("uv", "pip", "install", "--python", str(python), f"{wheel}[fuzz]", cwd=tmp_path)
    _run(
        str(python),
        "-I",
        "-c",
        "from ithrl.fuzz.statistics import confirm_change\n"
        "from ithrl.fuzz.contracts import FuzzOutcome\n"
        "from ithrl.statistics import BinomialSample\n"
        "result = confirm_change(BinomialSample(20,20), BinomialSample(0,20), "
        "required_original=20, required_mutant=20, alpha=.05, minimum_decrease=.1)\n"
        "assert result.outcome is FuzzOutcome.CONFIRMED_CHANGE\n",
        cwd=tmp_path,
    )


def _assert_installed_resume(executable: Path, runtime: Path, environment: dict[str, str]) -> None:
    case = runtime / "resume-case"
    _run(
        str(executable), "extract", "--example", "--output", str(case), cwd=runtime, env=environment
    )
    stopped = _run(
        str(executable),
        "shrink",
        str(case),
        "--max-candidates",
        "1",
        "--format",
        "json",
        cwd=runtime,
        env=environment,
        expected_exits=(6,),
    )
    report = json.loads(stopped.stdout)
    assert report["status"] == "stopped"
    for _ in range(7):
        assert report["next_command_shell"] == "posix"
        command = shlex.split(report["next_command"])
        assert command[0] == "ithrl"
        resumed = _run(
            str(executable),
            *command[1:],
            "--format",
            "json",
            cwd=runtime,
            env=environment,
            expected_exits=(0, 6),
        )
        report = json.loads(resumed.stdout)
        if report["status"] == "reduced":
            break
        assert report["status"] == "stopped"
    assert report["status"] == "reduced"
    assert report["confirmation_result"] == "confirmed-preserving"


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_paths) == 1
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    assert metadata["Name"] == "ithrl"
    version = metadata["Version"]
    assert isinstance(version, str)
    assert version
    return version


def _assert_no_private_or_checkout_content(members: tuple[PurePosixPath, ...]) -> None:
    prohibited_parts = {
        ".git",
        ".github",
        ".ithrl",
        ".local",
        "__pycache__",
        "fixtures",
        "tests",
    }
    prohibited_names = {
        ".coverage",
        ".env",
    }
    prohibited_suffixes = {".ithrl", ".pyc", ".sqlite", ".trace"}

    for member in members:
        assert prohibited_parts.isdisjoint(member.parts), member
        assert member.name not in prohibited_names, member
        assert not member.name.startswith(".env"), member
        assert not member.name.endswith((".sqlite-shm", ".sqlite-wal")), member
        assert member.suffix not in prohibited_suffixes, member


def _one_distribution(directory: Path, pattern: str) -> Path:
    matches = tuple(directory.glob(pattern))
    assert len(matches) == 1, f"expected one {pattern} in {directory}, found {matches}"
    return matches[0]


def _run(
    *command: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    expected_exits: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - every argument is controlled by this test module.
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode not in expected_exits:
        pytest.fail(
            f"command failed with exit code {result.returncode}: {command!r}\n"
            f"{result.stdout}\n{result.stderr}",
            pytrace=False,
        )
    return result
