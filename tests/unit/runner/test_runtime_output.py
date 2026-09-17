from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from ithrl.runner.contracts import NativeOutputReference
from ithrl.runner.runtime.output import (
    NativeOutputLimitError,
    NativeOutputStoreError,
    ProtectedOutputStore,
)
from ithrl.runner.runtime.state import OwnedAttemptState

ATTEMPT_ID = "attempt-00000000000000000000000000000001"


def _write_private(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    if os.name == "posix":
        path.chmod(0o600)


@pytest.mark.parametrize("complete", [True, False])
def test_publish_preserves_complete_or_partial_native_output(
    tmp_path: Path, complete: bool
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    _write_private(state.member("stdout"), b"answer\n")
    _write_private(state.member("stderr"), b"warning\n")
    store = ProtectedOutputStore.open(tmp_path / "evidence" / "native-output")

    reference = store.publish(
        state,
        stdout_name="stdout",
        stderr_name="stderr",
        max_bytes=1024,
        complete=complete,
    )

    output = store.read(reference)
    artifact = store.root / f"{reference.artifact_id}.bin"
    assert output.stdout == b"answer\n"
    assert output.stderr == b"warning\n"
    assert output.complete is complete
    assert reference.complete is complete
    assert reference.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert reference.size_bytes == artifact.stat().st_size
    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_reference_contains_no_output_or_filesystem_path(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    secret = b"content-that-must-not-appear-in-the-reference"
    _write_private(state.member("stdout"), secret)
    _write_private(state.member("stderr"), b"")
    store = ProtectedOutputStore.open(tmp_path / "private-evidence")

    reference = store.publish(
        state,
        stdout_name="stdout",
        stderr_name="stderr",
        max_bytes=1024,
        complete=True,
    )
    serialized = reference.model_dump_json()
    protected = store.read(reference)

    assert secret.decode() not in serialized
    assert secret.decode() not in repr(protected)
    assert str(tmp_path) not in serialized
    assert "/" not in reference.artifact_id
    assert "\\" not in reference.artifact_id


def test_publish_enforces_the_encoded_artifact_limit(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    _write_private(state.member("stdout"), b"12345")
    _write_private(state.member("stderr"), b"67890")
    store = ProtectedOutputStore.open(tmp_path / "native-output")

    with pytest.raises(NativeOutputLimitError, match="exceeds"):
        store.publish(
            state,
            stdout_name="stdout",
            stderr_name="stderr",
            max_bytes=39,
            complete=False,
        )

    assert tuple(store.root.iterdir()) == ()


def test_payload_capacity_reserves_the_artifact_framing() -> None:
    assert ProtectedOutputStore.payload_capacity(42) == 10

    with pytest.raises(NativeOutputLimitError, match="supported range"):
        ProtectedOutputStore.payload_capacity(31)


def test_output_remains_available_after_attempt_cleanup(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    _write_private(state.member("stdout"), b"kept")
    _write_private(state.member("stderr"), b"")
    store = ProtectedOutputStore.open(tmp_path / "native-output")
    reference = store.publish(
        state,
        stdout_name="stdout",
        stderr_name="stderr",
        max_bytes=1024,
        complete=True,
    )

    state.cleanup()

    assert store.read(reference).stdout == b"kept"


def test_read_rejects_artifact_tampering(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    _write_private(state.member("stdout"), b"original")
    _write_private(state.member("stderr"), b"")
    store = ProtectedOutputStore.open(tmp_path / "native-output")
    reference = store.publish(
        state,
        stdout_name="stdout",
        stderr_name="stderr",
        max_bytes=1024,
        complete=True,
    )
    artifact = store.root / f"{reference.artifact_id}.bin"
    _write_private(artifact, artifact.read_bytes()[:-1] + b"x")

    with pytest.raises(NativeOutputStoreError, match="match"):
        store.read(reference)


def test_read_rejects_an_unrecognized_reference(tmp_path: Path) -> None:
    store = ProtectedOutputStore.open(tmp_path / "native-output")
    reference = NativeOutputReference(
        artifact_id="foreign-artifact",
        sha256="0" * 64,
        media_type="application/octet-stream",
        size_bytes=1,
    )

    with pytest.raises(NativeOutputStoreError, match="incompatible"):
        store.read(reference)
