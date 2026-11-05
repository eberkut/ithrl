from __future__ import annotations

import signal
from unittest.mock import Mock

import pytest

from ithrl.runner.runtime import probes


@pytest.mark.parametrize("exit_after", [0, 1, 2, None])
def test_probe_group_cleanup_signal_order_deadlines_and_polling(
    monkeypatch: pytest.MonkeyPatch, exit_after: int | None
) -> None:
    now = 0.0
    signals: list[int] = []
    polls: list[float] = []
    sleeps: list[float] = []

    def killpg(group: int, selected: int) -> None:
        assert group == 1234
        if selected:
            signals.append(selected)
        elif exit_after is not None and len(signals) >= exit_after:
            raise ProcessLookupError

    def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(probes.os, "killpg", killpg)
    monkeypatch.setattr(probes.time, "monotonic", lambda: now)
    monkeypatch.setattr(probes.time, "sleep", sleep)
    process = Mock(pid=1234)
    process.poll.side_effect = lambda: polls.append(now)
    assert probes.stop_probe_group(process) is (exit_after is not None)
    assert signals == (
        [signal.SIGTERM] if exit_after in {0, 1} else [signal.SIGTERM, signal.SIGKILL]
    )
    assert polls
    assert all(delay == 0.05 for delay in sleeps)
    assert now == pytest.approx(0.0 if exit_after in {0, 1} else 1.0 if exit_after == 2 else 2.0)


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
def test_probe_group_signal_and_permission_semantics(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    def killpg(_group: int, _selected: int) -> None:
        raise error("sanitized refusal")

    monkeypatch.setattr(probes.os, "killpg", killpg)
    probes._signal_probe_group(1234, signal.SIGTERM)
    if error is OSError:
        with pytest.raises(OSError, match="sanitized refusal"):
            probes.process_group_exists(1234)
    else:
        assert probes.process_group_exists(1234) is (error is PermissionError)
