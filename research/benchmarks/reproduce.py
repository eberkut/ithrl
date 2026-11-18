"""Replay reviewed public-task fixtures offline; never accept model-supplied patches.

This research helper does not integrate task grading into ithrl or qualify a native runner.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ARCHIVE_NAMES = {
    "iniconfig": "iniconfig-source.tar.gz",
    "migration-linter": "migration-source.tar.gz",
}
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 50 * 1024 * 1024
UNITTEST_SUITE = """
import sys
import unittest


class ReferenceResult(unittest.TextTestResult):
    fixture_error = False

    def _classify(self, error, method):
        body = getattr(method, '__code__', None)
        trace = error[2]
        while trace is not None:
            if body is not None and trace.tb_frame.f_code is body:
                return
            trace = trace.tb_next
        self.fixture_error = True

    def addError(self, test, error):
        self._classify(error, getattr(type(test), '_callTestMethod', None))
        super().addError(test, error)

    def addFailure(self, test, error):
        self._classify(error, getattr(type(test), '_callTestMethod', None))
        super().addFailure(test, error)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            method = getattr(test, test._testMethodName)
            self._classify(error, method)
        super().addSubTest(test, subtest, error)


loader = unittest.TestLoader()
if sys.argv[1] == 'discover':
    suite = loader.discover('/checks', pattern=sys.argv[2])
else:
    suite = loader.loadTestsFromNames(sys.argv[2:])
if loader.errors:
    sys.stderr.write('Reference test loading failed\\n')
    sys.exit(2)
result = unittest.TextTestRunner(verbosity=0, resultclass=ReferenceResult).run(suite)
if result.fixture_error:
    sys.stderr.write('Reference test fixture failed\\n')
    sys.exit(2)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("file path must stay inside its owning directory")
    base = os.path.realpath(root)
    target = os.path.realpath(Path(base) / path)
    if target != base and not target.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError("file path escapes its owning directory")
    return Path(target)


def archive_bytes(cache: Path, task_key: str) -> bytes:
    if task_key not in ARCHIVE_NAMES:
        raise ValueError("source archive is not a reviewed task")
    name = ARCHIVE_NAMES[task_key]
    directory = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("source archive exceeds the compressed-size limit or is not regular")
        content = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
    if (
        len(content) != before.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("source archive changed during its bounded read")
    return content


def unpack(archive: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        members = bundle.getmembers()
        if len(members) > 5000 or sum(item.size for item in members) > MAX_SOURCE_BYTES:
            raise ValueError("source archive exceeds the extraction limit")
        prefixes = {Path(item.name).parts[0] for item in members}
        if len(prefixes) != 1:
            raise ValueError("source archive requires one enclosing directory")
        for item in members:
            member_path = Path(item.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("source archive contains an unsafe path")
            if not (item.isdir() or item.isfile()):
                raise ValueError("source archive contains a link or special entry")
            target = contained(destination, str(Path(*member_path.parts[1:])))
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(item)
                if stream is None:
                    raise ValueError("source archive has no regular-file payload")
                target.write_bytes(stream.read())


def apply_changes(source: Path, changes: dict[str, Any], fixtures: dict[str, str]) -> None:
    for patch in changes.get("patches", []):
        path = contained(ROOT, patch["file"])
        data = path.read_bytes()
        identity = hashlib.sha256(data).hexdigest()
        if fixtures.setdefault(patch["file"], identity) != identity:
            raise ValueError("reviewed fixture changed between variants")
        for line in data.decode("utf-8").split("\n"):
            if line.startswith(("--- ", "+++ ")):
                name = line[4:].split("\t", 1)[0]
                if not name.startswith(("a/", "b/")):
                    raise ValueError("reviewed patches may edit only existing source files")
                if not contained(source, name[2:]).is_file():
                    raise ValueError("reviewed patch refers to an absent source file")
        command = ["patch", "--force", "--fuzz=0", "--no-backup-if-mismatch", "-p1"]
        if patch.get("reverse", False):
            command.append("--reverse")
        result = subprocess.run(  # noqa: S603 - fixed patch executable; reviewed local fixture input.
            command, input=data, cwd=source, capture_output=True, timeout=10
        )
        if result.returncode:
            raise ValueError("reviewed patch does not apply to the pinned source")
    for change in changes.get("replacements", []):
        path = contained(source, change["path"])
        text = path.read_text()
        if not change["old"] or text.count(change["old"]) != 1:
            raise ValueError("reviewed replacement requires exactly one source match")
        path.write_text(text.replace(change["old"], change["new"]))


def environment(python: Path, expected: dict[str, object]) -> tuple[Path, dict[str, object]]:
    if not (python.parent.parent / "pyvenv.cfg").is_file():
        raise ValueError("an explicitly prepared virtual environment is required")
    short = ".".join(str(expected["python"]).split(".")[:2])
    if python.resolve(strict=True) != Path(f"/usr/bin/python{short}"):
        raise ValueError("the pinned system Python interpreter is required")
    code = (
        "import importlib.metadata as m,json,sys;from pathlib import Path;"
        "site=Path(sys.argv[1])/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages';"
        "print(json.dumps({'base':sys.base_prefix,'python':sys.version.split()[0],"
        "'packages':{d.metadata['Name']:d.version for d in m.distributions(path=[str(site)])}}))"
    )
    result = subprocess.run(  # noqa: S603 - explicitly prepared, pinned validation environment.
        [f"/usr/bin/python{short}", "-I", "-S", "-c", code, str(python.parent.parent)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        timeout=10,
    )
    actual = json.loads(result.stdout)
    base = Path(actual.pop("base"))
    actual["packages"] = {
        re.sub(r"[-_.]+", "-", name).lower(): value for name, value in actual["packages"].items()
    }
    if actual != expected:
        raise ValueError("virtual environment differs from the pinned Python/package versions")
    return base, actual


def run_suite(
    python: Path,
    base: Path,
    source: Path,
    checks: Path,
    python_path: str,
    arguments: list[str],
    count: int,
    log: Path,
) -> bool:
    command = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session", "--clearenv"]
    for directory in ("/usr", "/lib", "/lib64"):
        command += ["--ro-bind", directory, directory]
    if not base.is_relative_to("/usr"):
        command += ["--ro-bind", str(base), str(base)]
    for host, target in (
        (python.parent.parent, "/env"),
        (source, "/work"),
        (checks.parent, "/checks"),
    ):
        command += ["--ro-bind", str(host), target]
    command += ["--proc", "/proc", "--dev", "/dev", "--chdir", "/work"]
    for directory in ("/tmp", "/var/tmp"):  # noqa: S108 - private namespace tmpfs mounts.
        command += ["--tmpfs", directory]
    settings = {
        "PATH": "/env/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(Path("/work") / python_path),
    }
    for name, value in settings.items():
        command += ["--setenv", name, value]
    invocation = arguments
    if arguments[:2] == ["-m", "unittest"]:
        if len(arguments) != 4 or arguments[-1] != "-q":
            raise ValueError("reference unittest suite has no reviewed fixed invocation")
        invocation = ["-c", UNITTEST_SUITE, "names", arguments[2]]
    command += ["/env/bin/python", *invocation]
    result = subprocess.run(  # noqa: S603 - fixed sandbox command; reviewed fixtures only.
        command, capture_output=True, text=True, timeout=30
    )
    output = result.stdout + result.stderr
    log.write_text(output)
    if result.returncode not in (0, 1):
        raise ValueError("test process did not return a behavioral pass/failure")
    if arguments[:2] == ["-m", "pytest"]:
        final = output.strip().splitlines()[-1] if output.strip() else ""
        outcomes = re.findall(r"\b(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b", final)
        if any(kind not in {"passed", "failed"} for _, kind in outcomes):
            raise ValueError("reference suite must complete every declared assertion")
        collected = sum(int(value) for value, _ in outcomes)
    else:
        match = re.search(r"(?m)^Ran (\d+) tests? in ", output)
        collected = int(match[1]) if match else 0
        final = output.strip().splitlines()[-1] if output.strip() else ""
        if re.search(r"\b(?:skipped|expected failures|unexpected successes)=", final):
            raise ValueError("reference suite must complete every declared assertion")
    if collected != count:
        raise ValueError("test count changed; do not classify collection/import errors as outcomes")
    return result.returncode == 0


def incident_input(
    source: Path, task: dict[str, Any], variant: str, target: Path
) -> dict[str, object]:
    """Render declared incident prose and exact source snapshots, without verifier data."""

    sections = [task["input_intro"][variant].rstrip()]
    sources = {}
    for relative in task["context_files"]:
        path = contained(source, relative)
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        snapshot = content if content.endswith("\n") else content + "\n"
        sections.append(f"## {relative}\n\n````python\n{snapshot}````")
        sources[relative] = hashlib.sha256(raw).hexdigest()
    rendered = ("\n\n".join(sections) + "\n").encode("utf-8")
    target.write_bytes(rendered)
    return {"sha256": hashlib.sha256(rendered).hexdigest(), "source_files_sha256": sources}


def prepare_task(
    cache: Path,
    task: dict[str, Any],
    tree: Path,
    source_archives: dict[str, str],
    fixtures: dict[str, str],
) -> tuple[Path, Path]:
    """Bind and prepare one original source for either independent replay path."""

    if task["archive"]["file"] != ARCHIVE_NAMES.get(task["key"]):
        raise ValueError("source archive name differs from its reviewed task")
    content = archive_bytes(cache, task["key"])
    archive_digest = hashlib.sha256(content).hexdigest()
    if archive_digest != task["archive"]["sha256"]:
        raise ValueError("task source archive differs from its frozen identity")
    source_archives[task["archive"]["file"]] = archive_digest
    check = contained(ROOT, task["independent_file"])
    check_bytes = check.read_bytes()
    check_digest = hashlib.sha256(check_bytes).hexdigest()
    if fixtures.setdefault(task["independent_file"], check_digest) != check_digest:
        raise ValueError("reviewed fixture changed between tasks")
    checks = tree / "checks"
    checks.mkdir()
    check = checks / check.name
    check.write_bytes(check_bytes)
    original = tree / "original"
    unpack(content, original)
    apply_changes(original, {"patches": task.get("base_patches", [])}, fixtures)
    return original, check


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    task_bytes = (ROOT / "tasks.json").read_bytes()
    data = json.loads(task_bytes)
    python = args.python.absolute()
    base, actual_environment = environment(python, data["environment"])
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "inputs").mkdir()
    fixtures = {}
    source_archives = {}
    incident_inputs = {}
    rows = []
    for task in data["tasks"]:
        with tempfile.TemporaryDirectory(prefix="ithrl-public-task-") as temporary:
            tree = Path(temporary)
            original, check = prepare_task(args.cache, task, tree, source_archives, fixtures)
            for variant in task["variants"]:
                source = tree / variant["name"]
                shutil.copytree(original, source)
                apply_changes(source, variant, fixtures)
                results = {}
                suites = {
                    "official": (task["official_command"], task["official_test_count"]),
                    "independent": (
                        ["-c", UNITTEST_SUITE, "discover", check.name],
                        task["independent_test_count"],
                    ),
                }
                for name, (command, count) in suites.items():
                    log = args.output / f"{task['key']}-{variant['name']}-{name}.log"
                    results[name] = run_suite(
                        python, base, source, check, task["python_path"], command, count, log
                    )
                rows.append({"task": task["key"], "variant": variant["name"], **results})
                if results != variant["expected"]:
                    raise ValueError("reviewed fixture no longer has its expected outcomes")
                if variant["name"] in ("buggy", "changed-defect"):
                    name = f"inputs/{task['key']}-{variant['name']}.md"
                    incident_inputs[name] = incident_input(
                        source, task, variant["name"], args.output / name
                    )
    receipt = {
        "tasks_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "reproducer_sha256": digest(Path(__file__)),
        "environment": actual_environment,
        "source_archives_sha256": source_archives,
        "fixtures_sha256": fixtures,
        "incident_inputs": incident_inputs,
        "results": rows,
        "native_agents_started": 0,
        "provider_inference_requests": 0,
    }
    (args.output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    sys.stdout.write(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
