"""Comparison profile publication preserves existing directory permissions."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from ithrl.case.contracts import UnsafeCasePathError
from ithrl.cli.comparison_session import ComparisonProfile, save_comparison_profile


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
@pytest.mark.parametrize("existing_destination", [False, True])
def test_profile_refuses_nonprivate_parent_without_changing_it(
    tmp_path: Path, existing_destination: bool
) -> None:
    parent = tmp_path / "shared-project"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "profile.json"
    if existing_destination:
        path.write_bytes(b"existing profile")
        path.chmod(0o600)
    profile = Mock(spec=ComparisonProfile)

    with pytest.raises(UnsafeCasePathError, match="mode 0700"):
        save_comparison_profile(path, cast(ComparisonProfile, profile))

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    if existing_destination:
        assert path.read_bytes() == b"existing profile"
    else:
        assert not path.exists()
    profile.model_dump_json.assert_not_called()


def test_profile_creates_missing_private_parents(tmp_path: Path) -> None:
    parent = tmp_path / "private" / "profiles"
    path = parent / "profile.json"
    profile = Mock(spec=ComparisonProfile)
    profile.model_dump_json.return_value = "{}"

    save_comparison_profile(path, cast(ComparisonProfile, profile))

    assert path.read_bytes() == b"{}\n"
    if os.name == "posix":
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(parent.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
