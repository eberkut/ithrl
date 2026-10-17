from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from html.parser import HTMLParser
from importlib import import_module
from pathlib import Path
from typing import override
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from git import Actor, Repo
from typer.cli import app as typer_cli
from typer.testing import CliRunner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SITE_PAGES = frozenset(
    {"index.md", "native-runs.md", "results.md", "cli.md", "troubleshooting.md", "measurements.md"}
)
SITE_ASSETS = frozenset({"third-party-notices.txt"})


def test_cli_reference_is_current_without_external_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("documentation generation attempted an external operation")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(import_module("ithrl.cli.extract"), "discover_native_sessions", forbidden)
    result = CliRunner().invoke(
        typer_cli,
        ["ithrl.cli", "utils", "docs", "--name", "ithrl", "--title", "CLI reference"],
    )
    assert result.exit_code == 0, result.output
    assert (PROJECT_ROOT / "docs/cli.md").read_text(
        encoding="utf-8"
    ) == result.stdout.rstrip() + "\n"


@pytest.fixture
def site_sources(tmp_path: Path) -> Path:
    # The theme reads Git timestamps and warns in strict mode without a commit.
    actor = Actor("Documentation fixture", "docs@example.invalid")
    with Repo.init(tmp_path) as repo:
        repo.index.commit("Documentation fixture", author=actor, committer=actor, skip_hooks=True)
    shutil.copy(PROJECT_ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
    (tmp_path / "scripts").mkdir()
    shutil.copy(PROJECT_ROOT / "scripts/docs_theme.py", tmp_path / "scripts/docs_theme.py")
    docs = tmp_path / "docs"
    docs.mkdir()
    for page in SITE_PAGES | SITE_ASSETS:
        shutil.copy(PROJECT_ROOT / "docs" / page, docs / page)
    return tmp_path


def _build_site(source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict"],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_site_build_publishes_only_allowlisted_sources(site_sources: Path) -> None:
    docs = site_sources / "docs"
    (docs / "private.md").write_text("# Excluded\nPRIVATE_SENTINEL", encoding="utf-8")
    (docs / "receipt.json").write_text('{"private": "PRIVATE_SENTINEL"}', encoding="utf-8")
    (docs / "nested").mkdir()
    (docs / "nested/private.md").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (docs / "css").mkdir()
    (docs / "css/private.css").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (site_sources / "plan.md").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    result = _build_site(site_sources)
    assert result.returncode == 0, result.stdout + result.stderr
    site = site_sources / ".local/site"
    for page in SITE_PAGES:
        target = site / ("index.html" if page == "index.md" else f"{Path(page).stem}/index.html")
        assert target.is_file()
        assets = _PageAssets()
        assets.feed(target.read_text(encoding="utf-8"))
        assert assets.urls
        for url in assets.urls:
            parsed = urlsplit(url)
            assert not parsed.scheme, f"External asset: {url}"
            assert not parsed.netloc, f"External asset: {url}"
            asset = target.parent / unquote(parsed.path)
            assert asset.is_file(), f"Missing asset in {page}: {url}"
            assert asset.stat().st_size > 0
    assert not (site / "receipt.json").exists()
    assert not list(site.rglob("*.md"))
    search = json.loads((site / "search/search_index.json").read_text(encoding="utf-8"))
    assert search["docs"]
    assert {path.name for path in (site / "fonts").iterdir()} == {
        "Geist.woff2",
        "GeistMono.woff2",
        "Inter.woff2",
        "Inter-Italic.woff2",
    }
    assert {path.name for path in (site / "css/pygments").iterdir()} == {
        "shadcn-light.css",
        "github-dark.css",
    }
    assert {path.name for path in (site / "js").iterdir()} == {
        "callbacks.js",
        "copy-button.js",
        "dark.js",
        "sidebar.js",
        "slider.js",
        "toc.js",
    }
    for name in SITE_ASSETS:
        assert (site / name).read_bytes() == (PROJECT_ROOT / "docs" / name).read_bytes()
    assert "fetchStargazers(" not in (site / "index.html").read_text(encoding="utf-8")
    assert "cdnjs.cloudflare.com" not in (site / "index.html").read_text(encoding="utf-8")
    assert all(
        b"PRIVATE_SENTINEL" not in path.read_bytes() for path in site.rglob("*") if path.is_file()
    )


class _PageAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script":
            url = attributes.get("src")
        elif tag == "link" and attributes.get("rel") in {"stylesheet", "shortcut icon", "icon"}:
            url = attributes.get("href")
        else:
            return
        if url:
            self.urls.append(url)


@pytest.mark.parametrize("link", ["missing.md", "results.md#missing-anchor"])
def test_strict_site_build_refuses_broken_links(site_sources: Path, link: str) -> None:
    index = site_sources / "docs/index.md"
    index.write_text(index.read_text(encoding="utf-8") + f"\n[Broken]({link})\n", encoding="utf-8")
    result = _build_site(site_sources)
    assert result.returncode != 0
    assert link in result.stderr


def test_issue_forms_require_sanitized_reports_and_offer_private_security() -> None:
    templates = PROJECT_ROOT / ".github/ISSUE_TEMPLATE"
    config = yaml.safe_load((templates / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is False
    assert any(
        link["url"] == "https://github.com/eberkut/ithrl/security/advisories/new"
        for link in config["contact_links"]
    )
    for name in ("bug.yml", "compatibility.yml", "proposal.yml"):
        form = yaml.safe_load((templates / name).read_text(encoding="utf-8"))
        assert form["name"]
        assert form["description"]
        fields = [item for item in form["body"] if item["type"] != "markdown"]
        ids = [item["id"] for item in fields]
        assert len(ids) == len(set(ids))
        assert any(
            field["type"] == "checkboxes"
            and any(option.get("required") for option in field["attributes"]["options"])
            for field in fields
        )
        # No labels/assignees that require unverified remote setup.
        assert not form.get("labels")
        assert not form.get("assignees")
        assert "credentials" in json.dumps(form).lower()


def test_pages_workflow_deploys_only_generated_site_from_trusted_main() -> None:
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    )
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    build = workflow["jobs"]["build"]
    assert build["if"] == "github.repository == 'eberkut/ithrl' && github.ref == 'refs/heads/main'"
    assert build["permissions"] == {"contents": "read"}
    upload = next(
        step
        for step in build["steps"]
        if step.get("uses", "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["with"] == {"path": ".local/site", "retention-days": 1}
    deploy = workflow["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert "if" not in deploy  # The default success gate must block failed/skipped builds.
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    assert len(deploy["steps"]) == 1
    assert deploy["steps"][0]["uses"].startswith("actions/deploy-pages@")
