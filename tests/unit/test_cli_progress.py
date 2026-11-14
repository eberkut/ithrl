from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ithrl.cli import progress as progress_module


def test_terminal_progress_reuses_one_task_and_skips_repeated_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = MagicMock()
    display.__enter__.return_value = display
    display.add_task.return_value = 7
    monkeypatch.setattr(progress_module, "Progress", MagicMock(return_value=display))
    monkeypatch.setattr(progress_module, "error_console", MagicMock(is_terminal=True))

    with progress_module.progress_display() as update:
        update("Calibration")
        update("Calibration")
        update("Search")

    display.add_task.assert_called_once_with("Calibration", total=None)
    display.update.assert_called_once_with(7, description="Search")
