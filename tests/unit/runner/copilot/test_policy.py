from __future__ import annotations

import pytest

from ithrl.runner.copilot.policy import target_policy


def test_unsupported_target_cannot_receive_an_execution_policy() -> None:
    with pytest.raises(ValueError, match="unsupported Copilot target"):
        target_policy("unknown")  # type: ignore[arg-type]
