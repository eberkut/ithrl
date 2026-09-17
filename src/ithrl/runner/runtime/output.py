"""Private storage for complete or partial native runner output."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex

from ithrl.case.contracts import CaseError
from ithrl.case.filesystem import (
    atomic_write_bytes,
    file_identity,
    prepare_private_directory,
    read_safe_bytes,
)
from ithrl.execution.contracts import MAX_NATIVE_OUTPUT_BYTES
from ithrl.runner.contracts import NativeOutputReference
from ithrl.runner.runtime.state import OwnedAttemptState

type RootIdentity = tuple[int, int]

_ARTIFACT_ID = re.compile(r"^output-[0-9a-f]{32}$")
_MEDIA_TYPE = "application/vnd.ithrl.native-output"
_MAGIC = b"ITHRL-OUTPUT-1\n"
_HEADER = struct.Struct(">BQQ")
_HEADER_SIZE = len(_MAGIC) + _HEADER.size


class NativeOutputStoreError(RuntimeError):
    """Protected native output cannot be published or read safely."""


class NativeOutputLimitError(NativeOutputStoreError):
    """A native output artifact exceeds its configured byte limit."""


@dataclass(frozen=True, slots=True)
class ProtectedOutput:
    """Native output returned only through the protected store interface."""

    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    complete: bool


@dataclass(frozen=True, slots=True)
class ProtectedOutputStore:
    """Private atomic artifact storage outside disposable attempt state."""

    root: Path
    identity: RootIdentity

    @staticmethod
    def payload_capacity(max_bytes: int) -> int:
        """Return the stdout and stderr capacity within an artifact byte limit."""

        if max_bytes < _HEADER_SIZE or max_bytes > MAX_NATIVE_OUTPUT_BYTES:
            raise NativeOutputLimitError("native output byte limit is outside the supported range")
        return max_bytes - _HEADER_SIZE

    @classmethod
    def open(cls, root: Path) -> ProtectedOutputStore:
        """Create or validate a private application-owned output directory."""

        absolute_root = root.absolute()
        try:
            prepare_private_directory(absolute_root)
            metadata = absolute_root.lstat()
        except (CaseError, OSError, ValueError) as error:
            raise NativeOutputStoreError(
                "cannot prepare protected native-output storage"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise NativeOutputStoreError("native-output storage is not a directory")
        return cls(root=absolute_root, identity=file_identity(metadata))

    def publish(
        self,
        state: OwnedAttemptState,
        *,
        stdout_name: str,
        stderr_name: str,
        max_bytes: int,
        complete: bool,
    ) -> NativeOutputReference:
        """Atomically preserve bounded stdout and stderr from one owned attempt."""

        payload_capacity = self.payload_capacity(max_bytes)
        self._assert_unchanged()
        stdout = _read_output_file(state, stdout_name, payload_capacity)
        remaining = payload_capacity - len(stdout)
        stderr = _read_output_file(state, stderr_name, remaining)
        artifact = _encode(stdout, stderr, complete=complete)
        artifact_id = f"output-{token_hex(16)}"
        destination = self.root / f"{artifact_id}.bin"
        try:
            atomic_write_bytes(destination, artifact)
        except CaseError as error:
            raise NativeOutputStoreError("cannot publish protected native output") from error
        return NativeOutputReference(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(artifact).hexdigest(),
            media_type=_MEDIA_TYPE,
            size_bytes=len(artifact),
            complete=complete,
        )

    def read(self, reference: NativeOutputReference) -> ProtectedOutput:
        """Read and authenticate one protected artifact by opaque reference."""

        self._assert_unchanged()
        if (
            _ARTIFACT_ID.fullmatch(reference.artifact_id) is None
            or reference.media_type != _MEDIA_TYPE
            or reference.size_bytes > MAX_NATIVE_OUTPUT_BYTES
        ):
            raise NativeOutputStoreError("native output reference is incompatible with this store")
        try:
            artifact = read_safe_bytes(
                self.root / f"{reference.artifact_id}.bin",
                max_bytes=reference.size_bytes,
                label="native output artifact",
            )
        except CaseError as error:
            raise NativeOutputStoreError("cannot read protected native output") from error
        if len(artifact) != reference.size_bytes or not hashlib.sha256(artifact).hexdigest() == (
            reference.sha256
        ):
            raise NativeOutputStoreError("protected native output does not match its reference")
        output = _decode(artifact)
        if output.complete != reference.complete:
            raise NativeOutputStoreError("protected native output completeness does not match")
        return output

    def _assert_unchanged(self) -> None:
        try:
            metadata = self.root.lstat()
        except OSError as error:
            raise NativeOutputStoreError("native-output storage is unavailable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or file_identity(metadata) != self.identity
        ):
            raise NativeOutputStoreError("native-output storage was replaced")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise NativeOutputStoreError("native-output storage must have mode 0700")


def _read_output_file(state: OwnedAttemptState, name: str, max_bytes: int) -> bytes:
    path = state.member(name)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise NativeOutputStoreError("cannot read native output from attempt state") from error
    if stat.S_ISREG(metadata.st_mode) and metadata.st_size > max_bytes:
        raise NativeOutputLimitError("native output exceeds its byte limit")
    try:
        return read_safe_bytes(path, max_bytes=max_bytes, label=f"native output {name}")
    except CaseError as error:
        raise NativeOutputStoreError("cannot read native output from attempt state") from error


def _encode(stdout: bytes, stderr: bytes, *, complete: bool) -> bytes:
    return _MAGIC + _HEADER.pack(int(complete), len(stdout), len(stderr)) + stdout + stderr


def _decode(artifact: bytes) -> ProtectedOutput:
    if len(artifact) < _HEADER_SIZE or not artifact.startswith(_MAGIC):
        raise NativeOutputStoreError("protected native output has an invalid format")
    complete_value, stdout_size, stderr_size = _HEADER.unpack_from(artifact, len(_MAGIC))
    if complete_value not in {0, 1} or _HEADER_SIZE + stdout_size + stderr_size != len(artifact):
        raise NativeOutputStoreError("protected native output has an invalid format")
    stdout_end = _HEADER_SIZE + stdout_size
    return ProtectedOutput(
        stdout=artifact[_HEADER_SIZE:stdout_end],
        stderr=artifact[stdout_end:],
        complete=bool(complete_value),
    )
