from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from ithrl.case import CaseError
from ithrl.runner.contracts import NativeOutputReference
from ithrl.runner.runtime import output as output_module
from ithrl.runner.runtime.output import (
    NativeOutputLimitError,
    NativeOutputStoreError,
    ProtectedOutputStore,
)
from ithrl.runner.runtime.state import OwnedAttemptState

ATTEMPT_ID = "attempt-00000000000000000000000000000001"


def test_output_store_open_normalizes_unsafe_directory_failure(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(NativeOutputStoreError, match="cannot prepare protected"):
        ProtectedOutputStore.open(alias)
    assert target.is_dir()


def test_output_store_refuses_a_directory_replaced_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = output_module.prepare_private_directory
    target = tmp_path / "outputs"

    def replace_after_preparation(path: Path) -> None:
        prepare(path)
        path.rmdir()
        path.write_bytes(b"fixture replacement")

    monkeypatch.setattr(output_module, "prepare_private_directory", replace_after_preparation)
    with pytest.raises(NativeOutputStoreError, match="not a directory"):
        ProtectedOutputStore.open(target)
    assert target.read_bytes() == b"fixture replacement"


@pytest.mark.parametrize("fault", ["missing", "symlink", "publish-error"])
def test_failed_publication_leaves_no_artifact_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    source = tmp_path / "source-output"
    source.write_bytes(b"fixture output")
    if fault == "symlink":
        state.member("stdout").symlink_to(source)
    elif fault == "publish-error":
        _write_private(state.member("stdout"), source.read_bytes())
        _write_private(state.member("stderr"), b"")
        monkeypatch.setattr(
            output_module, "atomic_write_bytes", Mock(side_effect=CaseError("private detail"))
        )
    store = ProtectedOutputStore.open(tmp_path / "outputs")
    with pytest.raises(NativeOutputStoreError) as refused:
        store.publish(
            state, stdout_name="stdout", stderr_name="stderr", max_bytes=1024, complete=True
        )
    assert not tuple(store.root.iterdir())
    assert source.read_bytes() == b"fixture output"
    assert "private detail" not in str(refused.value)


@pytest.fixture
def published_output(tmp_path: Path) -> tuple[ProtectedOutputStore, NativeOutputReference]:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_ID)
    _write_private(state.member("stdout"), b"fixture output")
    _write_private(state.member("stderr"), b"")
    store = ProtectedOutputStore.open(tmp_path / "native-output")
    return store, store.publish(
        state, stdout_name="stdout", stderr_name="stderr", max_bytes=1024, complete=True
    )


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


@pytest.mark.parametrize("malformed", ["magic", "header", "length", "completeness"])
def test_authenticated_artifact_still_requires_valid_framing(
    published_output: tuple[ProtectedOutputStore, NativeOutputReference], malformed: str
) -> None:
    store, reference = published_output
    path = store.root / f"{reference.artifact_id}.bin"
    original = path.read_bytes()
    if malformed == "magic":
        content = b"x" + original[1:]
    elif malformed == "header":
        content = original[:10]
    elif malformed == "length":
        content = original + b"extra"
    else:
        prefix = b"ITHRL-OUTPUT-1\n"
        content = prefix + b"\x02" + original[len(prefix) + 1 :]
    _write_private(path, content)
    reference = reference.model_copy(
        update={"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    )

    with pytest.raises(NativeOutputStoreError, match="invalid format"):
        store.read(reference)


def test_reference_completeness_must_match_the_artifact(
    published_output: tuple[ProtectedOutputStore, NativeOutputReference],
) -> None:
    store, reference = published_output

    with pytest.raises(NativeOutputStoreError, match="completeness does not match"):
        store.read(reference.model_copy(update={"complete": False}))


@pytest.mark.parametrize("unavailable", ["artifact", "root", "replaced-root", "permissions"])
def test_read_refuses_missing_or_replaced_storage(
    published_output: tuple[ProtectedOutputStore, NativeOutputReference], unavailable: str
) -> None:
    store, reference = published_output
    if unavailable == "artifact":
        (store.root / f"{reference.artifact_id}.bin").unlink()
    elif unavailable in {"root", "replaced-root"}:
        store.root.rename(store.root.with_name("old-output"))
        if unavailable == "replaced-root":
            store.root.mkdir(mode=0o700)
            artifact = f"{reference.artifact_id}.bin"
            _write_private(
                store.root / artifact, (store.root.with_name("old-output") / artifact).read_bytes()
            )
    else:
        if os.name != "posix":
            pytest.skip("private directory modes require POSIX")
        store.root.chmod(0o755)

    with pytest.raises(NativeOutputStoreError):
        store.read(reference)
