from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from datetime import UTC, datetime
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
        PurePosixPath("ithrl/adapters/codex/reacquisition.py"),
        PurePosixPath("ithrl/adapters/contracts.py"),
        PurePosixPath("ithrl/adapters/copilot/__init__.py"),
        PurePosixPath("ithrl/adapters/copilot/context.py"),
        PurePosixPath("ithrl/adapters/copilot/decisions.py"),
        PurePosixPath("ithrl/adapters/copilot/discovery.py"),
        PurePosixPath("ithrl/adapters/copilot/parser.py"),
        PurePosixPath("ithrl/adapters/copilot/reacquisition.py"),
        PurePosixPath("ithrl/adapters/errors.py"),
        PurePosixPath("ithrl/adapters/discovery.py"),
        PurePosixPath("ithrl/adapters/registry.py"),
        PurePosixPath("ithrl/adapters/reacquisition.py"),
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
        PurePosixPath("ithrl/cli/compare.py"),
        PurePosixPath("ithrl/cli/comparison_output.py"),
        PurePosixPath("ithrl/cli/comparison_session.py"),
        PurePosixPath("ithrl/cli/fuzz.py"),
        PurePosixPath("ithrl/cli/fuzz_run.py"),
        PurePosixPath("ithrl/cli/fuzz_output.py"),
        PurePosixPath("ithrl/cli/fuzz_session.py"),
        PurePosixPath("ithrl/cli/extract.py"),
        PurePosixPath("ithrl/cli/inspect.py"),
        PurePosixPath("ithrl/cli/cost.py"),
        PurePosixPath("ithrl/cli/native.py"),
        PurePosixPath("ithrl/cli/output.py"),
        PurePosixPath("ithrl/cli/progress.py"),
        PurePosixPath("ithrl/cli/paths.py"),
        PurePosixPath("ithrl/cli/preparation.py"),
        PurePosixPath("ithrl/cli/sampled.py"),
        PurePosixPath("ithrl/cli/sampled_output.py"),
        PurePosixPath("ithrl/cli/sampled_reduction.py"),
        PurePosixPath("ithrl/cli/sampled_session.py"),
        PurePosixPath("ithrl/cli/shrink.py"),
        PurePosixPath("ithrl/context.py"),
        PurePosixPath("ithrl/comparison/__init__.py"),
        PurePosixPath("ithrl/comparison/contracts.py"),
        PurePosixPath("ithrl/comparison/engine.py"),
        PurePosixPath("ithrl/comparison/results.py"),
        PurePosixPath("ithrl/costs.py"),
        PurePosixPath("ithrl/cost_reporting.py"),
        PurePosixPath("ithrl/decisions.py"),
        PurePosixPath("ithrl/execution/__init__.py"),
        PurePosixPath("ithrl/execution/budget.py"),
        PurePosixPath("ithrl/execution/contracts.py"),
        PurePosixPath("ithrl/execution/preflight.py"),
        PurePosixPath("ithrl/experiment.py"),
        PurePosixPath("ithrl/extraction.py"),
        PurePosixPath("ithrl/grading/__init__.py"),
        PurePosixPath("ithrl/grading/bundle.py"),
        PurePosixPath("ithrl/grading/contracts.py"),
        PurePosixPath("ithrl/grading/driver.py"),
        PurePosixPath("ithrl/grading/patch_driver.py"),
        PurePosixPath("ithrl/grading/runtime.py"),
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
        PurePosixPath("ithrl/inspection.py"),
        PurePosixPath("ithrl/mutation.py"),
        PurePosixPath("ithrl/observations.py"),
        PurePosixPath("ithrl/path_safety.py"),
        PurePosixPath("ithrl/preparation.py"),
        PurePosixPath("ithrl/reacquisition.py"),
        PurePosixPath("ithrl/reduce/__init__.py"),
        PurePosixPath("ithrl/reduce/contracts.py"),
        PurePosixPath("ithrl/reduce/engine.py"),
        PurePosixPath("ithrl/reduce/search.py"),
        PurePosixPath("ithrl/results/__init__.py"),
        PurePosixPath("ithrl/results/models.py"),
        PurePosixPath("ithrl/results/inspection.py"),
        PurePosixPath("ithrl/results/storage.py"),
        PurePosixPath("ithrl/results/summary.py"),
        PurePosixPath("ithrl/results/validation.py"),
        PurePosixPath("ithrl/run_state.py"),
        PurePosixPath("ithrl/sampled_run_metadata.py"),
        PurePosixPath("ithrl/sampling.py"),
        PurePosixPath("ithrl/text_sections.py"),
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
        PurePosixPath("ithrl/runner/copilot/sdk_runtime.py"),
        PurePosixPath("ithrl/runner/copilot/worker.py"),
        PurePosixPath("ithrl/runner/evidence.py"),
        PurePosixPath("ithrl/runner/fake.py"),
        PurePosixPath("ithrl/runner/registry.py"),
        PurePosixPath("ithrl/runner/runtime/__init__.py"),
        PurePosixPath("ithrl/runner/runtime/certificates.py"),
        PurePosixPath("ithrl/runner/runtime/contracts.py"),
        PurePosixPath("ithrl/runner/runtime/output.py"),
        PurePosixPath("ithrl/runner/runtime/probes.py"),
        PurePosixPath("ithrl/runner/runtime/process.py"),
        PurePosixPath("ithrl/runner/runtime/programs.py"),
        PurePosixPath("ithrl/runner/runtime/protection.py"),
        PurePosixPath("ithrl/runner/runtime/state.py"),
        PurePosixPath("ithrl/runner/validation.py"),
        PurePosixPath("ithrl/synthetic.py"),
        PurePosixPath("ithrl/statistics.py"),
        PurePosixPath("ithrl/timestamps.py"),
        PurePosixPath("ithrl/workflow.py"),
    }
)

SKILL_FILES = ("SKILL.md", "references/workflows.md")
RUNTIME_ASSETS = frozenset({PurePosixPath("ithrl/examples/synthetic-trace.json")})
SKILL_ASSETS = frozenset(PurePosixPath("ithrl/.agents/skills/ithrl", name) for name in SKILL_FILES)
PUBLIC_EXAMPLES = frozenset(
    PurePosixPath("examples/fuzz", name)
    for name in ("codex.jsonl", "copilot.jsonl", "campaign.toml")
)
PUBLIC_DOCS = frozenset(
    PurePosixPath("docs", name)
    for name in (
        "index.md",
        "agent-skill.md",
        "native-runs.md",
        "reduction.md",
        "fuzzing.md",
        "comparison.md",
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
PUBLIC_DOCS |= frozenset(
    PurePosixPath("docs/branding", name)
    for name in (
        "branding.css",
        "ithrl-icon.svg",
        "ithrl-favicon.svg",
        "ithrl-logo.png",
        "ithrl-social.png",
        "ithrl-logo-light.svg",
        "ithrl-logo-dark.svg",
    )
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
        RUNTIME_MODULES | RUNTIME_ASSETS | SKILL_ASSETS
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
        for name in SKILL_FILES:
            assert (
                archive.read(f"ithrl/.agents/skills/ithrl/{name}")
                == (PROJECT_ROOT / "skills/ithrl" / name).read_bytes()
            )
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
        for name in SKILL_FILES:
            source = archive.extractfile(f"{members[0].parts[0]}/skills/ithrl/{name}")
            assert source is not None
            with source:
                assert source.read() == (PROJECT_ROOT / "skills/ithrl" / name).read_bytes()

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
    assert {
        member for member in relative_members if member.parts[0] == "examples"
    } == PUBLIC_EXAMPLES
    assert {member for member in relative_members if member.parts[0] == "skills"} == {
        PurePosixPath("skills/ithrl", name) for name in SKILL_FILES
    }
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
        "examples",
        "skills",
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


def _installed_environment(runtime: Path) -> dict[str, str]:
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
    return child_environment


def _assert_installed_workflow(wheel: Path, tmp_path: Path, expected_version: str) -> None:
    environment = tmp_path / "environment"
    _run("uv", "venv", "--python", sys.executable, str(environment), cwd=tmp_path)
    executable = environment / ("Scripts/ithrl.exe" if os.name == "nt" else "bin/ithrl")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run("uv", "pip", "install", "--python", str(python), str(wheel), cwd=tmp_path)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    case = runtime / "case"
    child_environment = _installed_environment(runtime)

    help_result = _run(str(executable), "--help", cwd=runtime, env=child_environment)
    assert "Test how agent context affects a model decision" in help_result.stdout
    version_result = _run(str(executable), "--version", cwd=runtime, env=child_environment)
    assert version_result.stdout == f"ithrl {expected_version}\n"
    skill_result = _run(
        str(python),
        "-I",
        "-c",
        "import json\nfrom importlib.resources import files\n"
        "skill = files('ithrl').joinpath('.agents/skills/ithrl')\n"
        "print(json.dumps({name: skill.joinpath(name).read_bytes().hex() for name in "
        f"{SKILL_FILES!r}}}))\n",
        cwd=runtime,
        env=child_environment,
    )
    assert json.loads(skill_result.stdout) == {
        name: (PROJECT_ROOT / "skills/ithrl" / name).read_bytes().hex() for name in SKILL_FILES
    }
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
    inspected = _run(
        str(executable),
        "inspect",
        "retained",
        str(case),
        "--result",
        str(summary_path.parent),
        "--format",
        "json",
        cwd=runtime,
        env=child_environment,
    )
    report = json.loads(inspected.stdout)
    assert report["command"] == "inspect"
    assert report["confirmation"]["status"] == "confirmed-preserving"
    assert report["candidate_digest"] == summary["candidate_digest"]
    assert all("text" not in node for node in report["nodes"])
    _assert_installed_costs(python, runtime, child_environment, summary)
    _assert_installed_sampled_inspection(python, case, runtime, child_environment)
    _assert_installed_preparation(executable, runtime, child_environment)
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


def _assert_installed_costs(
    python: Path, runtime: Path, environment: dict[str, str], summary: dict[str, str]
) -> None:
    pricing = runtime / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "provider": "openai",
                "model": "gpt-6.1-sol",
                "as_of": "2026-11-19T00:00:00Z",
                "context": {"service_tier": "default"},
                "recorded_source": {
                    "provider": "fixture",
                    "model": "fixture",
                    "endpoint": "https://example.invalid/inference",
                },
            }
        )
    )
    pricing.chmod(0o600)
    deny = (
        "import socket, subprocess\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('cost reporting attempted network or process execution')\n"
        "socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = forbidden\n"
        "subprocess.Popen = forbidden\n"
        "from ithrl.cli import app\n"
        "app()\n"
    )
    recorded = runtime / "recorded-costs.json"
    created = _run(
        str(python),
        "-I",
        "-c",
        deny,
        "cost",
        str(runtime / "data/ithrl/observations.sqlite"),
        "--case-digest",
        summary["case_digest"],
        "--experiment-digest",
        summary["experiment_digest"],
        "--pricing",
        str(pricing),
        "--output",
        str(recorded),
        "--format",
        "json",
        cwd=runtime,
        env=environment,
    )
    report = json.loads(created.stdout)
    assert report["mode"] == "recorded-usage"
    assert report["summary"]["subtotal_usd"] == "0"
    assert report["summary"]["known_zero_provider_observations"] > 0
    usage = runtime / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached_input_tokens": 200,
                "cache_write_tokens": 0,
            }
        )
    )
    usage.chmod(0o600)
    scenario = runtime / "scenario-costs.json"
    hypothetical = _run(
        str(python),
        "-I",
        "-c",
        deny,
        "cost",
        "--scenario",
        str(usage),
        "--pricing",
        str(pricing),
        "--output",
        str(scenario),
        "--format",
        "json",
        cwd=runtime,
        env=environment,
    )
    assert json.loads(hypothetical.stdout)["mode"] == "hypothetical"
    assert json.loads(hypothetical.stdout)["summary"]["subtotal_usd"] == "0.00262"
    pricing.unlink()
    inspect = deny.replace(
        "app()\n",
        "from ithrl.costs import PriceBook\n"
        "from ithrl.observations import ObservationStore\n"
        "PriceBook.__init__ = ObservationStore.__init__ = forbidden\napp()\n",
    )
    saved = _run(
        str(python),
        "-I",
        "-c",
        inspect,
        "inspect",
        "costs",
        str(recorded),
        "--format",
        "json",
        cwd=runtime,
        env=environment,
    )
    assert json.loads(saved.stdout) == report


def _assert_installed_sampled_inspection(
    python: Path, case: Path, runtime: Path, environment: dict[str, str]
) -> None:
    """Inspect a saved sampled result after its external state has been removed."""
    from ithrl.case import CaseWorkspace
    from ithrl.cli.sampled_session import save_cli_result, save_cli_session
    from ithrl.experiment import (
        ExperimentDefinition,
        FakeRunnerConfig,
        SampledCalibrationPhase,
        SampledConfirmationPhase,
        SampledExperimentDefinition,
        SampledReductionPlan,
        SampledSearchPhase,
        SamplingArmTarget,
    )
    from ithrl.observations import ObservationStore, SamplingBudgetAllowance
    from ithrl.reduce import SampledReductionCheckpoint
    from ithrl.results import SampledReductionEvidence
    from ithrl.run_state import SampledRunStateFile, SamplingBudgetApproval
    from ithrl.runner import prepare_candidate
    from ithrl.sampled_run_metadata import SampledRunMetadata
    from ithrl.workflow import prepare_experiment

    workspace = CaseWorkspace.open(case)
    evidence = workspace.load()
    default = ExperimentDefinition.from_toml(evidence.default_experiment)
    assert isinstance(default.runner, FakeRunnerConfig)
    arm = SamplingArmTarget(required_valid=1, max_attempts=1)
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(original=arm, candidate=arm),
        confirmation=SampledConfirmationPhase(
            original=arm, candidate=arm, one_sided_confidence=0.95
        ),
        margin=0.2,
        freshness_seconds=60,
    )
    experiment = SampledExperimentDefinition(
        target=default.target,
        oracle=default.oracle,
        runner=default.runner,
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
        reduction=reduction,
    )
    prepared = prepare_experiment(evidence, experiment=experiment)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    now = datetime(2026, 10, 7, tzinfo=UTC)
    store = ObservationStore(runtime / "sampled-observations.sqlite")
    store.initialize()
    state_file = SampledRunStateFile.create(
        case_root=case,
        run_id="installed-sampled",
        store=store,
        started_at=now,
        original_identity=prepared.identity,
        original_candidate_digest=candidate.digest,
        provider_calls_per_attempt=0,
        approval=SamplingBudgetApproval(
            approval_id="offline-fixture",
            approved_at=now,
            allowance=SamplingBudgetAllowance(
                run_id="installed-sampled",
                max_provider_calls=0,
                max_wall_time_seconds=300,
                max_candidate_comparisons=1,
                confirmation_provider_calls=0,
                confirmation_runner_seconds=60,
            ),
        ),
    )
    checkpoint = SampledReductionCheckpoint(
        reduction=reduction,
        original_identity=prepared.identity,
        original_candidate_digest=candidate.digest,
        best_candidate_digest=candidate.digest,
        removed_context_ids=(),
    )
    with state_file.acquire_lock(store):
        state = state_file.save_checkpoint(store, checkpoint)
    session = SampledRunMetadata(
        run_id=state.run_id,
        run_directory=state_file.path.parent,
        source_case=case,
        original_workspace=runtime / "unavailable-original-workspace",
        observation_store=store.path,
        experiment=experiment,
        execution_metadata=prepared.execution_plan.inspection_metadata,
        original_identity=prepared.identity,
        started_at=now,
    )
    save_cli_session(state_file.path.parent, session)
    result = SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-incomplete",
        selected_candidate_digest=candidate.digest,
        selected_at=now,
    )
    destination = save_cli_result(
        workspace,
        evidence,
        state,
        store,
        result,
        session,
        {
            "schema_version": 1,
            "kind": "sampled",
            "command": "shrink",
            "plan_digest": prepared.execution_plan.digest,
            "target": experiment.target.type,
            "original_context_count": len(evidence.context.nodes),
            "retained_context_count": len(evidence.context.nodes),
            "removed_context_count": 0,
            "search_result": result.search_result,
            "confirmation_result": result.confirmation_result,
            "margin": reduction.margin,
            "one_sided_confidence": reduction.confirmation.one_sided_confidence,
        },
    )
    shutil.rmtree(case / ".runs")
    store.path.unlink()
    before = {path: path.read_bytes() for path in case.rglob("*") if path.is_file()}
    inspected = _run(
        str(python),
        "-I",
        "-c",
        "import socket, subprocess\n"
        "from ithrl.cli import app\n"
        "from ithrl.observations import ObservationStore\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('inspection attempted execution or database access')\n"
        "socket.socket = subprocess.Popen = ObservationStore.__init__ = forbidden\n"
        "app()\n",
        "inspect",
        "retained",
        str(case),
        "--result",
        str(destination),
        "--format",
        "json",
        cwd=runtime,
        env=environment,
    )
    report = json.loads(inspected.stdout)
    assert report["mode"] == "sampled"
    assert report["candidate_digest"] == candidate.digest
    assert report["execution_plan_digest"] == prepared.execution_plan.digest
    assert (
        report["reconstruction_method"] == prepared.runner.capability_profile.reconstruction_method
    )
    assert report["capture_type"] == "provider-request"
    assert report["candidate_materialization"] == "exact-request"
    assert report["declared_policy"] == session.execution_metadata.declared_policy.model_dump()
    assert report["resource_limits"] == session.resource_limits.model_dump()
    assert report["recorded_policy_evidence"]["observations_with_digest"] == 0
    assert report["recorded_policy_evidence"]["observations_without_digest"] == 0
    assert report["recorded_policy_evidence"]["referenced_observations_unavailable"] == 0
    assert report["recorded_policy_evidence"]["effective_policy_digests"] == []
    assert report["confirmation"]["status"] == "not-run"
    assert report["provider_calls"] == report["actions_executed"] == 0
    assert all("text" not in node for node in report["nodes"])
    assert evidence.manifest.endpoint is not None
    assert evidence.manifest.endpoint not in inspected.stdout
    assert not session.original_workspace.exists()
    assert not store.path.exists()
    assert not session.run_directory.exists()
    assert {path: path.read_bytes() for path in case.rglob("*") if path.is_file()} == before


def _assert_installed_preparation(
    executable: Path, runtime: Path, child_environment: dict[str, str]
) -> None:
    """Exercise installed offline commands with recorded sanitized native facts."""

    from ithrl.case.filesystem import atomic_write_bytes
    from ithrl.cli.native import native_resource_limits
    from ithrl.execution import SafetyPolicy, resolve_execution_plan
    from ithrl.experiment import (
        NativeRunnerConfig,
        SampledCalibrationPhase,
        SampledExperimentDefinition,
        parse_experiment_toml,
    )
    from ithrl.preparation import NativePreparationReceipt
    from native_support import codex, copilot

    for harness in ("codex", "copilot"):
        parent = runtime / f"prepare-{harness}"
        parent.mkdir(mode=0o700)
        if harness == "codex":
            case, evidence = codex._case(parent, context_sensitive=True)
        else:
            _, case, evidence = copilot._case(parent, "final-response", context_sensitive=True)
        source = parent / "passive-source.jsonl"
        atomic_write_bytes(source, evidence.source_artifacts[0].content)
        passive = _run(
            str(executable),
            "inspect",
            "reacquire",
            str(source),
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        )
        passive_report = json.loads(passive.stdout)
        assert passive_report["harness"] == harness
        assert passive_report["boundaries"] == []
        assert passive_report["provider_calls"] == passive_report["actions_executed"] == 0
        capability = copilot._capability("final-response")
        if harness == "codex":
            capability = capability.model_copy(
                update={
                    "runner_type": "codex",
                    "harness": "codex",
                    "source_schema": "codex-rollout-jsonl",
                    "reconstruction_method": "codex-app-server-inject-checkpoint-v1",
                    "provider_calls_per_attempt": 4,
                }
            )
        default = parse_experiment_toml(evidence.default_experiment)
        experiment = SampledExperimentDefinition(
            target=default.target,
            oracle=default.oracle,
            runner=NativeRunnerConfig(type=harness, required_capability_digest=capability.digest),
            calibration=SampledCalibrationPhase(
                required_valid=3, minimum_matches=3, max_attempts=3
            ),
        )
        plan = resolve_execution_plan(
            capability,
            experiment,
            evidence=evidence,
            runner_fingerprint="packaging-fixture",
            safety_policy=SafetyPolicy(
                provider_transport="allowed",
                allowed_provider=capability.provider,
                allowed_endpoint=capability.endpoint,
            ),
            resource_limits=native_resource_limits(
                harness,
                max_provider_calls=capability.provider_calls_per_attempt,
                attempt_timeout_seconds=120,
            ),
        )
        assert plan.ready
        assert plan.plan is not None
        # This directory deliberately does not exist: preparation must not inspect it.
        original = parent / "unavailable-original"
        receipt = parent / "preflight.json"
        atomic_write_bytes(
            receipt,
            NativePreparationReceipt.create(plan.plan, experiment, original)
            .model_dump_json()
            .encode(),
        )
        inspected = _run(
            str(executable),
            "inspect",
            "sections",
            str(case.root),
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        )
        catalog = next(
            field for field in json.loads(inspected.stdout)["fields"] if len(field["sections"]) >= 2
        )
        settings = parent / "settings.toml"
        policy = (
            'schema_version = 1\nkind = "reduction-preparation"\n'
            "[calibration]\nrequired_valid = 3\nminimum_matches = 3\nmax_attempts = 3\n"
            "[reduction]\nmargin = 0.2\nfreshness_seconds = 7200\n"
            'eligibility_policy = "independent-safe-preaccept-only"\n'
            "[reduction.search.original]\nrequired_valid = 1\nmax_attempts = 1\n"
            "[reduction.search.candidate]\nrequired_valid = 1\nmax_attempts = 1\n"
            "[reduction.confirmation]\none_sided_confidence = 0.95\n"
            "[reduction.confirmation.original]\nrequired_valid = 11\nmax_attempts = 11\n"
            "[reduction.confirmation.candidate]\nrequired_valid = 11\nmax_attempts = 11\n"
            "[sections]\nmax_coarse_comparisons = 1\nmax_fine_comparisons = 1\n"
            "minimum_removed_utf8_bytes = 1\nrequire_criterion_for_confirmation = true\n"
            "stop_when_no_retained_fields = true\n[[sections.fields]]\n"
            f"context_id = {json.dumps(catalog['context_id'])}\n"
            f"content_index = {catalog['content_index']}\n"
            f"section_ids = [{json.dumps(catalog['sections'][0]['id'])}]\n"
        )
        atomic_write_bytes(settings, policy.encode())
        output = parent / "reduction.toml"
        arguments = (
            str(executable),
            "prepare",
            str(case.root),
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
        )
        prepared = _run(*arguments, cwd=runtime, env=child_environment)
        report = json.loads(prepared.stdout)
        generated = parse_experiment_toml(output.read_text())
        assert report["status"] == "prepared"
        assert report["experiment_digest"] == generated.digest
        assert generated.target == default.target
        assert generated.oracle == default.oracle
        assert "--dry-run" in shlex.split(report["next_command"])
        assert "--acknowledge-model-execution" not in shlex.split(report["next_command"])
        assert not original.exists()
        assert not (parent / "observations.sqlite").exists()
        repeated = _run(*arguments, cwd=runtime, env=child_environment, expected_exits=(2,))
        assert json.loads(repeated.stdout)["error_code"] == "preparation-invalid"


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


@pytest.mark.parametrize("origin", ["wheel", "sdist"])
def test_installed_comparison_collects_confirms_and_inspects(
    distributions: tuple[Path, Path], tmp_path: Path, origin: str
) -> None:
    wheel, sdist = distributions
    if origin == "sdist":
        wheel = _rebuild_wheel(sdist, tmp_path)
    environment = tmp_path / "comparison-environment"
    _run("uv", "venv", "--python", sys.executable, str(environment), cwd=tmp_path)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = environment / ("Scripts/ithrl.exe" if os.name == "nt" else "bin/ithrl")
    _run("uv", "pip", "install", "--python", str(python), f"{wheel}[compare]", cwd=tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    case = runtime / "case"
    child_environment = _installed_environment(runtime)
    _run(
        str(executable),
        "extract",
        "--example",
        "--output",
        str(case),
        cwd=runtime,
        env=child_environment,
    )
    config = runtime / "comparison.toml"
    config.write_text(
        """schema_version = 1
kind = "comparison"
objective = "repair"
minimum_effect = 0.1
seed = 1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400
[reference]
removed_context_ids = ["ctx-4c4e9663bfd5daae", "ctx-8a6964db1606f4e7"]
[candidate]
removed_context_ids = []
[calibration]
positive_minimum_matches = 2
negative_maximum_matches = 0
[calibration.reference]
required_valid = 2
max_attempts = 2
[calibration.candidate]
required_valid = 2
max_attempts = 2
[confirmation]
alpha = 0.05
[confirmation.reference]
required_valid = 20
max_attempts = 20
[confirmation.candidate]
required_valid = 20
max_attempts = 20
[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
"""
    )
    config.chmod(0o600)
    arguments = (
        str(executable),
        "compare",
        str(case),
        "--config",
        str(config),
        "--observation-store",
        str(runtime / "observations.sqlite"),
        "--format",
        "json",
    )
    dry = json.loads(_run(*arguments, "--dry-run", cwd=runtime, env=child_environment).stdout)
    assert dry["status"] == "ready"
    assert dry["planned_attempts"] == 44
    pilot = json.loads(
        _run(*arguments, "--calibration-only", cwd=runtime, env=child_environment).stdout
    )
    assert pilot["status"] == "calibrated"
    repeated = json.loads(
        _run(
            str(executable),
            "compare",
            pilot["run_state_path"],
            "--resume",
            "--calibration-only",
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        ).stdout
    )
    assert repeated["status"] == "calibrated"
    assert {k: v for k, v in repeated["usage"].items() if k != "wall_time_seconds"} == {
        k: v for k, v in pilot["usage"].items() if k != "wall_time_seconds"
    }
    assert repeated["usage"]["wall_time_seconds"] >= pilot["usage"]["wall_time_seconds"]
    collected = json.loads(
        _run(
            str(executable),
            "compare",
            pilot["run_state_path"],
            "--resume",
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        ).stdout
    )
    assert collected["status"] == "confirmed-improvement"
    assert collected["difference"] == 1
    assert collected["lower_bound"] > 0.1
    resumed = json.loads(
        _run(
            str(executable),
            "compare",
            collected["run_state_path"],
            "--resume",
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        ).stdout
    )
    assert resumed["status"] == collected["status"]
    assert resumed["usage"] == collected["usage"]
    assert resumed["result_path"] == collected["result_path"]
    for path in runtime.glob("observations.sqlite*"):
        path.unlink()
    inspected = json.loads(
        _run(
            str(executable),
            "inspect",
            "comparison",
            collected["result_path"],
            "--case",
            str(case),
            "--format",
            "json",
            cwd=runtime,
            env=child_environment,
        ).stdout
    )
    assert inspected["source_validation"] == "verified"
    assert inspected["status"] == collected["status"]
    assert inspected["usage"] == collected["usage"]


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
