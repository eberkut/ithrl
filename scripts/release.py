"""Prepare public release inputs; publishing remains in isolated workflow jobs."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        value = tomllib.load(source)["project"]["version"]
    if not isinstance(value, str) or VERSION.fullmatch(value) is None:
        raise ValueError("release requires a final X.Y.Z project version")
    if value != os.environ.get("EXPECTED_VERSION"):
        raise ValueError("expected version differs from project metadata")
    return value


def output(name: str, value: str) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as destination:
        destination.write(f"{name}={value}\n")


def dispatch() -> None:
    required = {
        "GITHUB_REPOSITORY": "eberkut/ithrl",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_ACTOR_ID": "24830691",
        "GITHUB_TRIGGERING_ACTOR": "eberkut",
    }
    if any(os.environ.get(key) != value for key, value in required.items()):
        raise ValueError("release requires an authorized maintainer dispatch on main")
    expected = os.environ.get("EXPECTED_SHA", "")
    if SHA.fullmatch(expected) is None or expected != os.environ.get("GITHUB_SHA"):
        raise ValueError("expected source commit differs from this workflow run")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 - Git is provided by the workflow runner.
        cwd=ROOT,
        text=True,
    ).strip()
    if head != expected:
        raise ValueError("checkout differs from the approved source commit")
    output("version", project_version())


def prepare() -> None:
    version = project_version()
    names = (f"ithrl-{version}-py3-none-any.whl", f"ithrl-{version}.tar.gz")
    distributions = ROOT / "dist"
    # uv build adds this control file; only the two named distributions are copied.
    if {entry.name for entry in distributions.iterdir()} - {".gitignore"} != set(names):
        raise ValueError("release requires exactly the expected wheel and source distribution")
    if any(
        not (distributions / name).is_file() or (distributions / name).is_symlink()
        for name in names
    ):
        raise ValueError("release distributions must be regular files")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## v{version} ("
    sections = changelog.split("\n## ")
    selected = [section for section in sections if f"## {section}".startswith(heading)]
    if len(selected) != 1:
        raise ValueError("changelog must contain exactly one entry for the release version")
    release = ROOT / ".local/release"
    (release / "dist").mkdir(parents=True, exist_ok=False)
    for name in names:
        shutil.copyfile(distributions / name, release / "dist" / name)
    (release / "release-notes.md").write_text(f"## {selected[0]}".rstrip() + "\n", encoding="utf-8")
    files = sorted([*(Path("dist") / name for name in names), Path("release-notes.md")])
    manifest = "".join(
        f"{hashlib.sha256((release / path).read_bytes()).hexdigest()}  {path.as_posix()}\n"
        for path in files
    )
    (release / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    output("manifest-sha256", hashlib.sha256(manifest.encode()).hexdigest())


if __name__ == "__main__":
    try:
        match sys.argv[1:]:
            case ["dispatch"]:
                dispatch()
            case ["prepare"]:
                prepare()
            case _:
                raise ValueError("usage: release.py dispatch|prepare")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
