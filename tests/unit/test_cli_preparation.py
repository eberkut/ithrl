from __future__ import annotations

from pathlib import Path

import pytest

from ithrl.cli.preparation import _publish


def test_output_byte_limit_is_checked_before_creating_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "absent" / "prepared.toml"
    monkeypatch.setattr("ithrl.cli.preparation.MAX_PREPARATION_BYTES", 3)
    with pytest.raises(ValueError, match="size limit"):
        _publish(destination, "éé", protected=())
    assert not destination.parent.exists()
