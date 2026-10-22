from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from git import Actor, Repo

ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    script = tmp_path / "scripts/release.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/release.py", script)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        "# CHANGELOG\n\n## v0.1.0 (2026-10-17)\n\nRelease feature.\n"
        "\n## v0.0.1 (2026-10-16)\n\nEarlier feature.\n"
    )
    (tmp_path / "dist").mkdir()
    for name in ("ithrl-0.1.0-py3-none-any.whl", "ithrl-0.1.0.tar.gz"):
        (tmp_path / "dist" / name).write_bytes(b"synthetic distribution bytes")
    actor = Actor("Release fixture", "release@example.invalid")
    with Repo.init(tmp_path, initial_branch="main") as repo:
        repo.index.add(["pyproject.toml"])
        commit = repo.index.commit("feat: fixture", author=actor, committer=actor, skip_hooks=True)
        sha = commit.hexsha
    environment = os.environ.copy()
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        environment.pop(key, None)
    environment.update(
        GITHUB_REPOSITORY="eberkut/ithrl",
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_ACTOR_ID="24830691",
        GITHUB_TRIGGERING_ACTOR="eberkut",
        GITHUB_SHA=sha,
        EXPECTED_SHA=sha,
        EXPECTED_VERSION="0.1.0",
        GITHUB_OUTPUT=str(tmp_path / "outputs"),
    )
    return tmp_path, environment


def invoke(source: tuple[Path, dict[str, str]], command: str) -> subprocess.CompletedProcess[str]:
    path, environment = source
    return subprocess.run(  # noqa: S603 - fixture script and command are controlled by these tests.
        [sys.executable, str(path / "scripts/release.py"), command],
        cwd=path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dispatch_accepts_only_exact_source_and_version(
    source: tuple[Path, dict[str, str]],
) -> None:
    result = invoke(source, "dispatch")
    assert result.returncode == 0, result.stderr
    assert (source[0] / "outputs").read_text() == "version=0.1.0\n"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REPOSITORY", "someone/ithrl"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_ACTOR_ID", "1"),
        ("GITHUB_TRIGGERING_ACTOR", "someone"),
        ("EXPECTED_SHA", "HEAD"),
        ("EXPECTED_SHA", "0" * 40),
        ("GITHUB_SHA", "0" * 40),
        ("EXPECTED_VERSION", "0.2.0"),
        ("EXPECTED_VERSION", "0.1.0\nforged-output=true"),
    ],
)
def test_dispatch_refuses_mismatches_without_outputs(
    source: tuple[Path, dict[str, str]], key: str, value: str
) -> None:
    source[1][key] = value
    assert invoke(source, "dispatch").returncode != 0
    assert not (source[0] / "outputs").exists()


@pytest.mark.parametrize("version", ["0.1.0rc1", "01.2.3", "1.2", "1.2.3+local"])
def test_preparation_requires_a_final_version(
    source: tuple[Path, dict[str, str]], version: str
) -> None:
    (source[0] / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    source[1]["EXPECTED_VERSION"] = version
    assert invoke(source, "prepare").returncode != 0
    assert not (source[0] / ".local/release").exists()


@pytest.mark.parametrize("uv_marker", [False, True])
def test_preparation_selects_notes_and_binds_exact_public_files(
    source: tuple[Path, dict[str, str]], uv_marker: bool
) -> None:
    if uv_marker:
        (source[0] / "dist/.gitignore").write_text("*")
    result = invoke(source, "prepare")
    assert result.returncode == 0, result.stderr
    release = source[0] / ".local/release"
    notes = (release / "release-notes.md").read_text()
    assert "Release feature." in notes
    assert "Earlier feature." not in notes
    manifest = (release / "SHA256SUMS").read_bytes()
    assert (source[0] / "outputs").read_text() == (
        f"manifest-sha256={hashlib.sha256(manifest).hexdigest()}\n"
    )
    assert {path.name for path in (release / "dist").iterdir()} == {
        "ithrl-0.1.0-py3-none-any.whl",
        "ithrl-0.1.0.tar.gz",
    }
    for name in ("ithrl-0.1.0-py3-none-any.whl", "ithrl-0.1.0.tar.gz"):
        assert (release / "dist" / name).read_bytes() == (source[0] / "dist" / name).read_bytes()


@pytest.mark.parametrize(
    "case", ["extra", "missing", "symlink", "missing-notes", "duplicate-notes"]
)
def test_preparation_refuses_unexpected_inputs(
    source: tuple[Path, dict[str, str]], case: str
) -> None:
    root = source[0]
    if case == "extra":
        (root / "dist/private.txt").write_text("PRIVATE_SENTINEL")
    elif case == "missing":
        (root / "dist/ithrl-0.1.0.tar.gz").unlink()
    elif case == "symlink":
        wheel = root / "dist/ithrl-0.1.0-py3-none-any.whl"
        wheel.unlink()
        wheel.symlink_to(root / "pyproject.toml")
    elif case == "missing-notes":
        (root / "CHANGELOG.md").write_text("# CHANGELOG\n")
    else:
        with (root / "CHANGELOG.md").open("a") as output:
            output.write("\n## v0.1.0 (2026-10-17)\nDuplicate\n")
    assert invoke(source, "prepare").returncode != 0
    assert not (root / "outputs").exists()


@pytest.mark.parametrize("mutation", [None, "wheel", "notes", "manifest", "extra"])
def test_publisher_verification_rejects_changed_or_extra_files(
    source: tuple[Path, dict[str, str]], mutation: str | None
) -> None:
    assert invoke(source, "prepare").returncode == 0
    release = source[0] / ".local/release"
    manifest_sha = (source[0] / "outputs").read_text().strip().split("=", 1)[1]
    if mutation:
        name = {
            "wheel": "dist/ithrl-0.1.0-py3-none-any.whl",
            "notes": "release-notes.md",
            "manifest": "SHA256SUMS",
            "extra": "dist/private.txt",
        }[mutation]
        (release / name).write_text("changed bytes")
    scripts = [
        next(
            step["run"]
            for step in RELEASE["jobs"][job]["steps"]
            if step["name"] == "Verify release files"
        )
        for job in ("testpypi", "pypi", "github-release")
    ]
    assert len(set(scripts)) == 1
    result = subprocess.run(  # noqa: S603 - execute the checked-in workflow verification step.
        ["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", scripts[0]],
        cwd=release,
        env={**source[1], "VERSION": "0.1.0", "MANIFEST_SHA256": manifest_sha},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (mutation is None)


def test_release_uses_required_ci_and_same_run_artifacts_without_privileged_checkout() -> None:
    assert set(RELEASE["on"]) == {"workflow_dispatch"}
    assert RELEASE["permissions"] == {}
    assert RELEASE["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert "workflow_call" in CI["on"]
    assert CI["concurrency"]["group"].startswith("ci-")
    jobs = RELEASE["jobs"]
    assert jobs["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["checks"]["needs"] == "preflight"
    assert jobs["checks"]["permissions"] == {"contents": "read"}
    assert jobs["testpypi"]["needs"] == ["preflight", "checks"]
    assert jobs["pypi"]["needs"] == ["preflight", "checks", "testpypi"]
    assert jobs["github-release"]["needs"] == ["preflight", "checks", "pypi"]
    for name, job in jobs.items():
        assert "if" not in job  # Default success gates block failures, cancellations and skips.
        assert "continue-on-error" not in job
        if name in {"preflight", "checks"}:
            continue
        assert not any("checkout@" in step.get("uses", "") for step in job["steps"])
        download = job["steps"][0]
        assert download["with"]["artifact-ids"] == "${{ needs.checks.outputs.release-artifact-id }}"
        assert "github-token" not in download["with"]
        assert "run-id" not in download["with"]
        if name != "github-release":
            assert job["environment"]["name"] == name
            assert job["permissions"] == {"contents": "read", "id-token": "write"}
        else:
            assert job["permissions"] == {"contents": "write"}
            assert "--verify-tag" in job["steps"][-1]["run"]


@pytest.mark.parametrize("case", ["correct", "missing", "wrong-target", "api-error"])
def test_production_tag_gate_refuses_missing_or_wrong_source(
    source: tuple[Path, dict[str, str]], case: str
) -> None:
    root, environment = source
    binary = root / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(
        '#!/bin/sh\n[ "$CASE" != api-error ] || exit 1\n'
        'case "$2" in\n'
        '  */git/ref/tags/v0.1.0) [ "$CASE" = missing ] || printf "%s\\n" "$TAG_SHA" ;;\n'
        '  */git/tags/*) printf "%s\\n" "$TAG_TARGET" ;;\n'
        "  *) exit 1 ;;\nesac\n"
    )
    gh.chmod(0o700)
    scripts = [
        next(
            step["run"]
            for step in RELEASE["jobs"][job]["steps"]
            if step["name"] == "Verify the existing annotated tag targets the approved source"
        )
        for job in ("pypi", "github-release")
    ]
    assert scripts[0] == scripts[1]
    result = subprocess.run(  # noqa: S603 - checked-in gate with a fixture-only gh executable.
        ["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", scripts[0]],
        cwd=root,
        env={
            **environment,
            "PATH": f"{binary}:{environment['PATH']}",
            "CASE": case,
            "VERSION": "0.1.0",
            "GH_REPO": "eberkut/ithrl",
            "SOURCE_SHA": environment["EXPECTED_SHA"],
            "TAG_SHA": "1" * 40,
            "TAG_TARGET": "0" * 40 if case == "wrong-target" else environment["EXPECTED_SHA"],
        },
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (case == "correct")


@pytest.mark.parametrize("case", ["absent", "exists", "network-error"])
def test_rehearsal_refuses_existing_tag_and_lookup_failure(
    source: tuple[Path, dict[str, str]], case: str
) -> None:
    root, environment = source
    binary = root / "bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(
        '#!/bin/sh\n[ "$CASE" != network-error ] || exit 1\n'
        'if [ "$CASE" = exists ]; then printf "tag-ref\\n"; fi\n'
    )
    git.chmod(0o700)
    scripts = [
        next(step["run"] for step in RELEASE["jobs"][job]["steps"] if step["name"] == name)
        for job, name in (
            ("preflight", "Require the release tag to be absent before rehearsal"),
            ("testpypi", "Require the tag to remain absent"),
        )
    ]
    assert scripts[0] == scripts[1]
    result = subprocess.run(  # noqa: S603 - checked-in gate with a fixture-only git executable.
        ["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", scripts[0]],
        cwd=root,
        env={
            **environment,
            "PATH": f"{binary}:{environment['PATH']}",
            "CASE": case,
            "VERSION": "0.1.0",
        },
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (case == "absent")
