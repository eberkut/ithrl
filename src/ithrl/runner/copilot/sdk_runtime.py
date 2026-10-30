"""Expose the selected Python and installed SDK dependency closure read-only."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import stat
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import Requirement

_MAX_FILES = 20_000
_MAX_BYTES = 128 * 1024 * 1024


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class SDKRuntime:
    """The current interpreter and content-bound installed SDK requirements."""

    python: Path = field(default_factory=lambda: Path(sys.executable), repr=False)
    stdlib: Path = field(init=False, repr=False)
    shared_library: Path | None = field(init=False, repr=False)
    packages: tuple[Path, ...] = field(init=False, repr=False)
    _files: tuple[Path, ...] = field(init=False, repr=False)
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        python = self.python.resolve(strict=True)
        stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
        if (
            python != Path(sys.executable).resolve(strict=True)
            or not (stdlib / "encodings" / "__init__.py").is_file()
        ):
            raise ValueError("selected Python runtime layout cannot be exposed safely")
        library = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
        library = library.resolve(strict=True) if library.is_file() else None
        packages: set[Path] = set()
        files: set[Path] = set()
        pending = ["github-copilot-sdk"]
        seen: set[str] = set()
        while pending:
            distribution = importlib.metadata.distribution(pending.pop())
            name = distribution.metadata["Name"]
            if name in seen:
                continue
            seen.add(name)
            for dependency in distribution.requires or ():
                requirement = Requirement(dependency)
                if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                    pending.append(requirement.name)
            for entry in distribution.files or ():
                # Console entrypoints are outside the import tree and are never exposed.
                if not entry.parts or entry.parts[0] == ".." or "__pycache__" in entry.parts:
                    continue
                path = Path(str(distribution.locate_file(entry)))
                root = Path(str(distribution.locate_file(entry.parts[0])))
                if path.is_symlink() or root.is_symlink() or not path.is_file():
                    raise ValueError("Copilot SDK distribution contains an unsafe file")
                packages.add(root.resolve(strict=True))
                files.add(path.resolve(strict=True))
        object.__setattr__(self, "python", python)
        object.__setattr__(self, "stdlib", stdlib)
        object.__setattr__(self, "shared_library", library)
        object.__setattr__(self, "packages", tuple(sorted(packages)))
        object.__setattr__(self, "_files", tuple(sorted(files)))
        object.__setattr__(self, "identity", self._digest())

    def _digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(repr((sys.version, os.fspath(self.stdlib), _signature(self.stdlib))).encode())
        size = 0
        files = (
            self.python,
            *((self.shared_library,) if self.shared_library else ()),
            *self._files,
        )
        if len(files) > _MAX_FILES:
            raise ValueError("Copilot SDK runtime exceeds its file inspection limit")
        for path in files:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Copilot SDK runtime contains an unsafe file")
            size += metadata.st_size
            if size > _MAX_BYTES:
                raise ValueError("Copilot SDK runtime exceeds its byte inspection limit")
            before = _signature(path)
            digest.update(os.fsencode(path))
            with path.open("rb") as stream:
                if _signature(path) != before:
                    raise ValueError("Copilot SDK runtime changed during inspection")
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if _signature(path) != before:
                raise ValueError("Copilot SDK runtime changed during inspection")
        return digest.hexdigest()

    def assert_unchanged(self) -> None:
        if self._digest() != self.identity:
            raise ValueError("Copilot SDK runtime changed after selection")

    def mounts(self, sandbox: str) -> tuple[str, ...]:
        """Mount the interpreter libraries and declared SDK imports, without home roots."""
        result = (
            "--ro-bind",
            os.fspath(self.python),
            f"{sandbox}/runner/python-runtime/bin/python",
            "--ro-bind",
            os.fspath(self.stdlib),
            f"{sandbox}/runner/python-runtime/lib/python{sys.version_info.major}.{sys.version_info.minor}",
        )
        if self.shared_library:
            result += (
                "--ro-bind",
                os.fspath(self.shared_library),
                f"{sandbox}/runner/python-runtime/lib/{self.shared_library.name}",
            )
        for package in self.packages:
            result += (
                "--ro-bind",
                os.fspath(package),
                f"{sandbox}/runner/sdk-packages/{package.name}",
            )
        return result
