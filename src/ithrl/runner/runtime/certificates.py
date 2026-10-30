"""Snapshot explicitly configured certificate bundles into disposable state."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from ithrl.case import CaseError
from ithrl.case.filesystem import atomic_write_bytes, file_signature

MAX_CERTIFICATE_BUNDLE_BYTES = 1024 * 1024


def snapshot_certificate_files(
    root: Path, supplied: Mapping[str, str], *, sandbox_root: str
) -> dict[str, str]:
    """Preserve configured trust bundles without exposing their parent directories."""

    environment = dict(supplied)
    for name, filename in (
        ("SSL_CERT_FILE", "ssl-cert-file.pem"),
        ("NODE_EXTRA_CA_CERTS", "node-extra-ca-certs.pem"),
    ):
        if name not in supplied:
            continue
        try:
            content = _read_certificate_bundle(Path(supplied[name]))
            atomic_write_bytes(root / filename, content)
        except (CaseError, OSError, RuntimeError, ValueError):
            raise ValueError("cannot snapshot a configured CA bundle") from None
        environment[name] = f"{sandbox_root}/{filename}"
    return environment


def _read_certificate_bundle(source: Path) -> bytes:
    resolved = source.resolve(strict=True)
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= MAX_CERTIFICATE_BUNDLE_BYTES
    ):
        raise ValueError("invalid certificate bundle")
    # Certificates are public inputs; retain source permissions and verify the read identity.
    descriptor = os.open(resolved, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if file_signature(os.fstat(stream.fileno())) != file_signature(metadata):
            raise ValueError("certificate bundle changed before reading")
        content = stream.read(MAX_CERTIFICATE_BUNDLE_BYTES + 1)
        finished = os.fstat(stream.fileno())
    if (
        len(content) != metadata.st_size
        or file_signature(finished) != file_signature(metadata)
        or file_signature(resolved.stat()) != file_signature(metadata)
    ):
        raise ValueError("certificate bundle changed while reading")
    return content
