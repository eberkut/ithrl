"""Private application data paths used by CLI commands."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from platformdirs import user_data_path

from ithrl.case.filesystem import prepare_private_directory


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
