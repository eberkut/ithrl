from __future__ import annotations

import socket
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner
from git import Actor, Repo
from semantic_release.cli.commands.main import main as semantic_release

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def release_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    original = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = tomllib.loads(original)["project"]["version"]
    source = original.replace(f'version = "{version}"', 'version = "0.0.0"', 1)
    (tmp_path / "pyproject.toml").write_text(source, encoding="utf-8")
    actor = Actor("Release fixture", "release@example.invalid")
    with Repo.init(tmp_path, initial_branch="main") as repo:
        repo.index.add(["pyproject.toml"])
        repo.index.commit(
            "feat: fixture capability", author=actor, committer=actor, skip_hooks=True
        )
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_OUTPUT",
    ):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("release preparation attempted a network connection")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_first_version_preview_does_not_change_files_index_or_refs(release_source: Path) -> None:
    project = release_source / "pyproject.toml"
    index = release_source / ".git/index"
    original, original_index = project.read_bytes(), index.read_bytes()
    with Repo(release_source) as repo:
        head = repo.head.commit.hexsha

    result = CliRunner().invoke(semantic_release, ["--noop", "version", "--print"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "0.1.0"
    assert project.read_bytes() == original
    assert index.read_bytes() == original_index
    assert not (release_source / "CHANGELOG.md").exists()
    with Repo(release_source) as repo:
        assert repo.head.commit.hexsha == head
        assert not repo.tags


def test_preparation_stages_only_generated_files_and_does_not_commit_or_tag(
    release_source: Path,
) -> None:
    with Repo(release_source) as repo:
        head = repo.head.commit.hexsha
    command = [
        "version",
        "--no-commit",
        "--no-tag",
        "--no-push",
        "--no-vcs-release",
        "--skip-build",
    ]

    for _ in range(2):
        result = CliRunner().invoke(semantic_release, command)
        assert result.exit_code == 0, result.output

    project = tomllib.loads((release_source / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == "0.1.0"
    changelog = (release_source / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.count("## v0.1.0 (") == 1
    assert "Fixture capability" in changelog
    assert "release@example.invalid" not in changelog
    with Repo(release_source) as repo:
        assert repo.head.commit.hexsha == head
        assert not repo.tags
        assert set(repo.git.diff("--cached", "--name-only").splitlines()) == {
            "CHANGELOG.md",
            "pyproject.toml",
        }
