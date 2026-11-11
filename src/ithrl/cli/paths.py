"""Private application data paths used by CLI commands."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from platformdirs import user_data_path

from ithrl.case.filesystem import prepare_private_directory


def native_storage_paths() -> tuple[Path, Path]:
    """Describe reserved native storage without creating it."""

    root = user_data_path("ithrl", ensure_exists=False).absolute().resolve(strict=False)
    return root / "native-attempts", root / "native-output"


def observation_storage_paths(requested: Path | None = None) -> tuple[Path, ...]:
    """Describe a store and its lifecycle files without opening them."""

    path = requested if requested is not None else user_data_path("ithrl") / "observations.sqlite"
    if ".." in path.parts:
        raise ValueError("unsafe observation path")
    return tuple(
        path.with_name(path.name + suffix)
        for suffix in (
            "",
            ".calibration.lock",
            ".calibration.inflight",
            ".sampling.lock",
            "-journal",
            "-wal",
            "-shm",
        )
    )


def reserved_native_paths(observation_store: Path | None = None) -> tuple[Path, ...]:
    """Keep preparation artifacts outside runner state and SQLite lifecycle paths."""

    paths = native_storage_paths() + observation_storage_paths()
    return (
        paths if observation_store is None else paths + observation_storage_paths(observation_store)
    )


def private_data_root() -> Path:
    """Return the private application data directory, creating it if needed."""

    root = user_data_path("ithrl", ensure_exists=False)
    prepare_private_directory(root)
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"ithrl data path must be a directory, not a link: {root}")
    if os.name == "posix":
        root.chmod(0o700)
    return root
