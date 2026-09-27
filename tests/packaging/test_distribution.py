from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
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
        PurePosixPath("ithrl/case/__init__.py"),
        PurePosixPath("ithrl/case/contracts.py"),
        PurePosixPath("ithrl/case/evidence.py"),
        PurePosixPath("ithrl/case/filesystem.py"),
        PurePosixPath("ithrl/case/source.py"),
        PurePosixPath("ithrl/case/source_context.py"),
        PurePosixPath("ithrl/case/source_storage.py"),
        PurePosixPath("ithrl/case/storage.py"),
        PurePosixPath("ithrl/cli/__init__.py"),
        PurePosixPath("ithrl/cli/extract.py"),
        PurePosixPath("ithrl/cli/output.py"),
        PurePosixPath("ithrl/cli/paths.py"),
        PurePosixPath("ithrl/cli/shrink.py"),
        PurePosixPath("ithrl/context.py"),
        PurePosixPath("ithrl/decisions.py"),
        PurePosixPath("ithrl/execution/__init__.py"),
        PurePosixPath("ithrl/execution/contracts.py"),
        PurePosixPath("ithrl/execution/preflight.py"),
        PurePosixPath("ithrl/experiment.py"),
        PurePosixPath("ithrl/extraction.py"),
        PurePosixPath("ithrl/measurement.py"),
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
        PurePosixPath("ithrl/runner/runtime/contracts.py"),
        PurePosixPath("ithrl/runner/runtime/output.py"),
        PurePosixPath("ithrl/runner/runtime/process.py"),
        PurePosixPath("ithrl/runner/runtime/protection.py"),
        PurePosixPath("ithrl/runner/runtime/state.py"),
        PurePosixPath("ithrl/runner/validation.py"),
        PurePosixPath("ithrl/synthetic.py"),
        PurePosixPath("ithrl/timestamps.py"),
        PurePosixPath("ithrl/workflow.py"),
    }
)

SYNTHETIC_TRACE = {
    "schema_version": 1,
    "trace_schema": "ithrl.synthetic.v1",
    "inferences": [
        {
            "inference_call_id": "packaging-smoke-001",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "provider": "fake",
            "endpoint": "local",
            "request": {
                "model": "fake-deterministic-v1",
                "input": [
                    {"role": "user", "content": "A"},
                    {"role": "user", "content": "B"},
                    {"role": "user", "content": "C"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Return a command decision without executing it.",
                        "parameters": {
                            "type": "object",
                            "properties": {"cmd": {"type": "string"}},
                            "required": ["cmd"],
                            "additionalProperties": False,
                        },
                    }
                ],
                "store": False,
            },
            "response": {
                "model_returned": "fake-deterministic-v1",
                "tool_calls": [{"name": "exec_command", "arguments": {"cmd": "true"}}],
            },
            "fake_behavior": {
                "tool_name": "exec_command",
                "required_markers": ["A", "C"],
            },
        }
    ],
}


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
    assert "ithrl" in roots
    assert len(roots) == 2
    assert any(root.endswith(".dist-info") for root in roots)
    assert any(member.name == "entry_points.txt" for member in members)
    assert any(member.name == "LICENSE" for member in members)
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
        PurePosixPath("README.md"),
        PurePosixPath("pyproject.toml"),
    } <= set(relative_members)
    packaged_sources = {
        member
        for member in relative_members
        if member.parts[:2] == ("src", "ithrl") and member.suffix == ".py"
    }
    assert packaged_sources == expected_files
    assert {member.parts[0] for member in relative_members if member.parts} <= {
        ".gitignore",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "src",
    }
    _assert_no_private_or_checkout_content(relative_members)


def test_clean_wheel_from_sdist_completes_credential_free_workflow(
    distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    _, sdist = distributions
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
    wheel = _one_distribution(rebuilt, "*.whl")

    environment = tmp_path / "environment"
    _run("uv", "venv", "--python", sys.executable, str(environment), cwd=tmp_path)
    executable = environment / ("Scripts/ithrl.exe" if os.name == "nt" else "bin/ithrl")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run("uv", "pip", "install", "--python", str(python), str(wheel), cwd=tmp_path)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    trace = runtime / "trace.json"
    trace.write_text(json.dumps(SYNTHETIC_TRACE), encoding="utf-8")
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

    help_result = _run(str(executable), "--help", cwd=runtime, env=child_environment)
    assert "Test how agent context affects a model decision" in help_result.stdout
    version_result = _run(str(executable), "--version", cwd=runtime, env=child_environment)
    assert version_result.stdout == "ithrl 0.0.0\n"
    extract_result = _run(
        str(executable),
        "extract",
        str(trace),
        "--inference",
        "packaging-smoke-001",
        "--output",
        str(case),
        cwd=runtime,
        env=child_environment,
    )
    assert "No network requests or provider calls" in extract_result.stdout
    shrink_result = _run(
        str(executable),
        "shrink",
        str(case),
        cwd=runtime,
        env=child_environment,
    )
    assert "1-minimal; all valid single removals tested" in shrink_result.stdout
    assert "No network requests or provider calls" in shrink_result.stdout
    assert list((case / "results").glob("*/summary.json"))


def _assert_no_private_or_checkout_content(members: tuple[PurePosixPath, ...]) -> None:
    prohibited_parts = {
        ".git",
        ".github",
        ".ithrl",
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
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - every argument is controlled by this test module.
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        pytest.fail(
            f"command failed with exit code {result.returncode}: {command!r}\n{result.stdout}",
            pytrace=False,
        )
    return result
