from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from ithrl.case import UnsafeCasePathError
from ithrl.cli import paths


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_data_root_uses_private_directory_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "data"
    if kind == "directory":
        root.mkdir(mode=0o755)
    elif kind == "file":
        root.write_bytes(b"fixture")
    else:
        root.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(paths, "user_data_path", Mock(return_value=root))

    if kind == "directory":
        assert paths.private_data_root() == root
        if os.name == "posix":
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
    else:
        with pytest.raises(UnsafeCasePathError):
            paths.private_data_root()
