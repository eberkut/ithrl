"""Deterministic child-process behaviors for isolated runtime tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    action, *arguments = sys.argv[1:]
    if action == "describe":
        sys.stdout.write(
            json.dumps(
                {
                    "args": arguments,
                    "cwd": Path.cwd().name,
                    "marker": os.environ.get("ITHRL_TEST_MARKER"),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return
    if action == "output":
        stdout_bytes, stderr_bytes = map(int, arguments)
        sys.stdout.buffer.write(b"o" * stdout_bytes)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(b"e" * stderr_bytes)
        sys.stderr.buffer.flush()
        return
    if action == "sleep":
        time.sleep(float(arguments[0]))
        return
    if action == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if len(arguments) > 1:
            Path(arguments[1]).touch()
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        time.sleep(float(arguments[0]))
        return
    if action == "spawn-child":
        child_pid_file = Path(arguments[0])
        child = subprocess.Popen(  # noqa: S603 - fixed interpreter and fixture command
            [sys.executable, __file__, "ignore-term", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid_file.write_text(str(child.pid), encoding="ascii")
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        time.sleep(60)
        return
    if action == "write":
        Path(arguments[0]).write_text(arguments[1], encoding="utf-8")
        return
    if action == "write-and-sleep":
        Path(arguments[0]).write_bytes(b"x" * int(arguments[1]))
        time.sleep(float(arguments[2]))
        return
    if action == "deep-tree":
        current = Path.cwd()
        for _ in range(int(arguments[0])):
            current /= "d"
            current.mkdir()
        return
    if action == "exit":
        raise SystemExit(int(arguments[0]))
    raise SystemExit("unknown test action")


if __name__ == "__main__":
    main()
