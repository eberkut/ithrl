"""POSIX process-group checks and synchronous capability-probe cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import time

_PROBE_TERMINATION_GRACE_SECONDS = 1.0
_PROBE_POLL_SECONDS = 0.05


def stop_probe_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop every process in one probe group and confirm that the group is gone."""

    _signal_probe_group(process.pid, signal.SIGTERM)
    if _wait_probe_group_gone(process):
        return True
    _signal_probe_group(process.pid, signal.SIGKILL)
    return _wait_probe_group_gone(process)


def _wait_probe_group_gone(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + _PROBE_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not process_group_exists(process.pid):
            return True
        time.sleep(_PROBE_POLL_SECONDS)
    process.poll()
    return not process_group_exists(process.pid)


def _signal_probe_group(process_group: int, selected_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, selected_signal)
    except OSError:
        return


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
