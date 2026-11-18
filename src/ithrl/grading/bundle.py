"""Read bounded task files and bind a prepared system-Python virtual environment."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ithrl.context import content_digest
from ithrl.grading.contracts import (
    BundleManifest,
    PatchGradingConfig,
    TestSuite,
    _implementation_files,
)
from ithrl.path_safety import validate_directory_chain

MAX_DEPTH = 64
MAX_FILES = 10_000
MAX_BYTES = 256 * 1024 * 1024


class GradingError(ValueError):
    """A content-safe refusal before or during grading."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_tree(root: Path) -> dict[str, bytes]:
    """Snapshot regular files only, refusing links and races while opening each file."""

    files: dict[str, bytes] = {}
    total = 0
    entries = 0

    def visit(directory_fd: int, prefix: str) -> None:
        nonlocal total, entries
        if prefix.count("/") > MAX_DEPTH:
            raise GradingError("grading-input-limit")
        names = []
        with os.scandir(directory_fd) as scan:
            for entry in scan:
                entries += 1
                if entries > MAX_FILES:
                    raise GradingError("grading-input-limit")
                names.append(entry.name)
        for name in sorted(names):
            relative = f"{prefix}/{name}" if prefix else name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                try:
                    visit(fd, relative)
                finally:
                    os.close(fd)
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise GradingError("grading-unsafe-file")
                if len(files) >= MAX_FILES or total + before.st_size > MAX_BYTES:
                    raise GradingError("grading-input-limit")
                data = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
            if (
                len(data) != before.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise GradingError("grading-input-changed")
            total += len(data)
            files[relative] = data

    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.absolute().parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        visit(fd, "")
    finally:
        os.close(fd)
    return files


def _hash_files(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}


def _environment(path: Path) -> tuple[str, str, dict[str, bytes]]:
    validate_directory_chain(path)
    if not path.is_absolute() or path.is_symlink():
        raise GradingError("grading-unsafe-environment")
    cfg = path / "pyvenv.cfg"
    if cfg.is_symlink() or not cfg.is_file():
        raise GradingError("grading-unsafe-environment")
    descriptor = os.open(cfg, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise GradingError("grading-unsafe-environment")
        cfg_bytes = stream.read(4097)
        after = os.fstat(stream.fileno())
    if (
        len(cfg_bytes) != metadata.st_size
        or metadata.st_mtime_ns != after.st_mtime_ns
        or metadata.st_ctime_ns != after.st_ctime_ns
    ):
        raise GradingError("grading-environment-changed")
    values = dict(line.split(" = ", 1) for line in cfg_bytes.decode().splitlines() if " = " in line)
    version = values.get("version_info", values.get("version", ""))
    parts = version.split(".")
    if (
        len(parts) != 3
        or not all(value.isdigit() for value in parts)
        or tuple(map(int, parts)) < (3, 12, 0)
    ):
        raise GradingError("grading-environment-version")
    short = ".".join(parts[:2])
    interpreter = (path / "bin/python").resolve(strict=True)
    if (
        interpreter != Path(f"/usr/bin/python{short}")
        or values.get("home") != "/usr/bin"
        or values.get("include-system-site-packages") != "false"
    ):
        raise GradingError("grading-system-python-required")
    files = _read_tree(path / f"lib/python{short}/site-packages")
    executables = {}
    for name, executable in (
        ("python-binary", interpreter),
        ("git-binary", Path("/usr/bin/git")),
        ("bubblewrap-binary", Path("/usr/bin/bwrap")),
    ):
        with executable.open("rb") as stream:
            executables[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    identity = content_digest(
        {
            "python": version,
            "packages": _hash_files(files),
            "metadata": {"pyvenv.cfg": hashlib.sha256(cfg_bytes).hexdigest()},
            "executables": executables,
        }
    )
    return short, identity, files


def bind_environment(path: Path) -> str:
    """Bind environment bytes without starting its interpreter or invoking a harness."""

    return _environment(path)[1]


@dataclass(frozen=True, slots=True)
class ValidatedGrading:
    config: PatchGradingConfig
    manifest: BundleManifest
    files: dict[str, bytes] = field(repr=False)
    python_version: str
    packages: dict[str, bytes] = field(repr=False)
    driver: bytes = field(repr=False)
    patch_driver: bytes = field(repr=False)


def validate_grading(config: PatchGradingConfig) -> ValidatedGrading:
    """Read and validate inputs; this starts no process, native harness or provider call."""

    implementation = _implementation_files()
    if config.implementation_digest != content_digest(_hash_files(implementation)):
        raise GradingError("grading-implementation-changed")
    if sys.platform != "linux" or (
        any(not os.access(path, os.X_OK) for path in ("/usr/bin/bwrap", "/usr/bin/git"))
    ):
        raise GradingError("grading-runtime-unavailable")
    files = _read_tree(Path(config.bundle_path))
    try:
        manifest = BundleManifest.model_validate_json(files.pop("manifest.json"))
    except (KeyError, ValueError):
        raise GradingError("grading-invalid-manifest") from None
    if manifest.digest != config.bundle_digest or _hash_files(files) != manifest.files:
        raise GradingError("grading-bundle-changed")
    _validate_source_paths(files)
    version, identity, packages = _environment(Path(config.environment_path))
    if identity != config.environment_digest:
        raise GradingError("grading-environment-changed")
    return ValidatedGrading(
        config,
        manifest,
        files,
        version,
        packages,
        implementation["driver.py"],
        implementation["patch_driver.py"],
    )


def _validate_source_paths(files: dict[str, bytes]) -> None:
    # The apply tree must not supply repository configuration or conversion rules.
    if any(
        name.startswith("source/") and {".git", ".gitattributes"}.intersection(Path(name).parts)
        for name in files
    ):
        raise GradingError("grading-unsafe-source")


def write_bundle(
    *,
    source: Path,
    checks: Path,
    destination: Path,
    editable_paths: tuple[str, ...],
    python_path: str,
    source_suite: TestSuite,
    independent_suite: TestSuite,
) -> str:
    """Publish a new private bundle under an existing controlled directory."""

    files = {
        **{f"source/{name}": data for name, data in _read_tree(source).items()},
        **{f"checks/{name}": data for name, data in _read_tree(checks).items()},
    }
    _validate_source_paths(files)
    manifest = BundleManifest(
        files=_hash_files(files),
        editable_paths=editable_paths,
        python_path=python_path,
        source_suite=source_suite,
        independent_suite=independent_suite,
    )
    manifest_bytes = manifest.model_dump_json().encode("utf-8")
    directories = {parent for name in files for parent in Path(name).parents if parent != Path()}
    if (
        len(files) + len(directories) + 1 > MAX_FILES
        or sum(map(len, files.values())) + len(manifest_bytes) > MAX_BYTES
        or any(parent.as_posix().count("/") > MAX_DEPTH for parent in directories)
    ):
        raise GradingError("grading-input-limit")
    validate_directory_chain(destination.parent)
    destination.mkdir(mode=0o700)
    for directory in sorted(directories):
        (destination / directory).mkdir(mode=0o700)
    files["manifest.json"] = manifest_bytes
    for name, data in files.items():
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    return manifest.digest
