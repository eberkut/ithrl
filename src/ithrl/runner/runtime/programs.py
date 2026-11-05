"""Bounded reads of stable installed worker-program sources."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_MAX_WORKER_BYTES = 1024 * 1024


def read_worker_program(source: Path, *, harness: str, error_type: type[RuntimeError]) -> bytes:
    """Read a resolved installation file, retaining the runner's error contract."""

    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_WORKER_BYTES:
            raise error_type(f"the {harness} worker source is invalid")
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if file_state(opened) != file_state(metadata):
                raise error_type(f"the {harness} worker source changed while opening")
            content = stream.read(_MAX_WORKER_BYTES + 1)
            finished = os.fstat(stream.fileno())
        if (
            len(content) != metadata.st_size
            or file_state(finished) != file_state(metadata)
            or file_state(resolved.stat()) != file_state(metadata)
        ):
            raise error_type(f"the {harness} worker source changed while it was read")
    except error_type:
        raise
    except OSError as error:
        raise error_type(f"cannot read the private {harness} worker") from error
    return content


def file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Identity and mutable metadata checked around an installation-file read."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def executable_digest(
    target: Path,
    metadata: os.stat_result,
    *,
    harness: str,
    error_type: type[RuntimeError],
) -> str:
    """Fingerprint one opened executable, checking its descriptor before and after reads.

    The harness owns pathname validation and OSError translation around this step.
    """

    digest = hashlib.sha256()
    with target.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if file_state(opened) != file_state(metadata):
            raise error_type(f"a required {harness} runtime executable changed")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        finished = os.fstat(stream.fileno())
    if file_state(finished) != file_state(metadata):
        raise error_type(f"a required {harness} runtime executable changed")
    return digest.hexdigest()
