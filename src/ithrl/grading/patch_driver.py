"""Apply content-only repairs and verify tree structure before compiling Python.

Executed as a standalone, stdlib-only script inside the grading namespace.
"""

from __future__ import annotations

import json
import os
import resource
import stat
import subprocess
import sys
from pathlib import Path


def tree_structure(root: Path) -> dict[str, int]:
    """Snapshot directories and single-link regular files, including their modes."""

    result: dict[str, int] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(directory) / name
            metadata = path.lstat()
            if not (
                stat.S_ISDIR(metadata.st_mode)
                or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1)
            ) or name in (".git", ".gitattributes"):
                raise ValueError("unsafe apply tree")
            result[path.relative_to(root).as_posix()] = metadata.st_mode
    return result


def apply_patch(root: Path, patch: Path, editable: list[str]) -> int:
    before = tree_structure(root)
    # No repository discovery, system/global configuration, attributes or hooks.
    command = [
        "/usr/bin/git",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.hooksPath=/dev/null",
        "apply",
        "--whitespace=nowarn",
        "-p1",
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }

    def git(options: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - fixed executable/options; patch is data.
            [*command, *options, str(patch)],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    # NUL-delimited numstat gives exact unquoted paths. A nonempty summary reports
    # creation, deletion, renames, copies or mode changes; binary counts use '-'.
    parsed = git(["--numstat", "--summary", "-z"], capture=True)
    if parsed.returncode:
        return parsed.returncode
    records = parsed.stdout.split(b"\0")
    allowed = {name.encode("utf-8") for name in editable}
    if len(records) < 2 or records[-1]:
        return 1
    for record in records[:-1]:
        fields = record.split(b"\t", 2)
        if (
            len(fields) != 3
            or not fields[0].isdigit()
            or not fields[1].isdigit()
            or fields[2] not in allowed
        ):
            return 1
    code = git(["--check"]).returncode
    if code:
        return code
    if git([]).returncode:
        # A checked patch that cannot be written is an operational failure.
        raise OSError("patch application failed")
    try:
        if tree_structure(root) != before:
            return 1
        for name in editable:
            if name.endswith(".py"):
                compile((root / name).read_bytes(), name, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, UnicodeError):
        return 1
    return 0


def main() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    spec = json.loads(Path("/driver/suite.json").read_text())
    resource.setrlimit(resource.RLIMIT_FSIZE, (spec["max_bytes"],) * 2)
    code = apply_patch(Path("/work"), Path("/repair.patch"), spec["editable_paths"])
    sys.stdout.write(json.dumps({"nonce": spec["nonce"], "patch_exit": code}) + "\n")


if __name__ == "__main__":
    main()
