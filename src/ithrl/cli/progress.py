"""Content-safe sampling progress for terminals and redirected stderr."""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager

from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from ithrl.cli.output import error_console, print_plain
from ithrl.observations import SamplingArm, SamplingBlockEvidence


@contextmanager
def progress_display() -> Generator[Callable[[str], None], None, None]:
    started = time.monotonic()
    previous: str | None = None
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TimeElapsedColumn(),
        console=error_console,
        disable=not error_console.is_terminal,
    ) as progress:
        task: TaskID | None = None

        def update(message: str) -> None:
            nonlocal previous, task
            if message == previous:
                return
            previous = message
            if error_console.is_terminal:
                if task is None:
                    task = progress.add_task(message, total=None)
                else:
                    progress.update(task, description=message)
            else:
                print_plain(
                    error_console, f"{message} [{time.monotonic() - started:.0f}s]", soft_wrap=True
                )

        yield update


def sampling_status(
    block: SamplingBlockEvidence, *, phase: str, candidate_label: str = "candidate"
) -> str:
    arms = []
    labels: tuple[tuple[SamplingArm, str], ...] = (
        ("baseline", "original"),
        ("candidate", candidate_label),
    )
    for arm, label in labels:
        counts = block.counts_for(arm)
        required = block.plan.targets_for(arm).required_valid
        arms.append(f"{label} {counts.valid}/{required} valid ({counts.dispatched} attempts)")
    return f"{phase}: {'; '.join(arms)}"


def calibration_status(valid: int, required: int, attempts: int) -> str:
    return f"Calibration: original {valid}/{required} valid ({attempts} attempts)"
