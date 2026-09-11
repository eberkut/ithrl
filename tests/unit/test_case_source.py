from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from operator import attrgetter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.adapters import parse_session
from ithrl.case import (
    CaptureFacts,
    CaseExistsError,
    CaseFormatError,
    CaseManifest,
    CaseWorkspace,
    ProvenanceFacts,
    ReplayFacts,
    SourceArtifact,
    UnsafeCasePathError,
    case_digest,
)
from ithrl.case import filesystem as case_filesystem
from ithrl.context import ContextNode, content_digest

EXPERIMENT = """\
schema_version = 1

[oracle]
type = "tool-call"
name = "exec_command"
"""
FIXTURES = Path(__file__).parents[1] / "fixtures"


def _manifest() -> CaseManifest:
    return CaseManifest(
        adapter="synthetic",
        adapter_version="0.1",
        captured_at="2026-09-01T12:00:00+02:00",
        provider="fake",
        endpoint="local",
        model_requested="deterministic",
        capture=CaptureFacts(request="provider-request", completeness="complete"),
        replay=ReplayFacts(state="self-contained", environment="local"),
        provenance=ProvenanceFacts(
            source="synthetic-fixture",
            inference_call_id="call-1",
            trace_schema="synthetic-v1",
        ),
    )


def _request() -> dict[str, Any]:
    return {
        "model": "deterministic",
        "input": [
            {"id": "A", "content": "irrelevant"},
            {"id": "B", "type": "function_call", "name": "exec_command"},
        ],
    }


def _nodes(request: dict[str, Any]) -> tuple[ContextNode, ...]:
    return tuple(
        ContextNode(
            id=node_id,
            kind=str(item.get("type", "message")),
            path=f"/input/{index}",
            mutable=True,
            source="source/events.jsonl",
            digest=content_digest(item),
        )
        for index, (node_id, item) in enumerate(zip(("A", "B"), request["input"], strict=True))
    )


def _artifacts() -> tuple[SourceArtifact, ...]:
    return (
        SourceArtifact(
            path="source/metadata/session.json",
            media_type="application/json",
            content=b'{"session":"native-1"}\n',
        ),
        SourceArtifact(
            path="source/events.jsonl",
            media_type="application/x-ndjson",
            content=b'{"type":"message","text":"caf\xc3\xa9"}\r\n\x00',
        ),
    )


def _create_case(
    path: Path,
    *,
    source_artifacts: Iterable[SourceArtifact] = (),
) -> CaseWorkspace:
    request = _request()
    return CaseWorkspace.create(
        path,
        manifest=_manifest(),
        request=request,
        nodes=_nodes(request),
        default_experiment=EXPERIMENT,
        source_artifacts=source_artifacts,
    )


def _render_manifest_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def test_source_artifacts_round_trip_exact_bytes_in_deterministic_order(tmp_path: Path) -> None:
    supplied = _artifacts()
    workspace = _create_case(tmp_path / "case", source_artifacts=supplied)

    evidence = workspace.load()
    expected = tuple(sorted(supplied, key=attrgetter("path")))

    assert evidence.source_artifacts == expected
    assert tuple(artifact.path for artifact in evidence.source_artifacts) == (
        "source/events.jsonl",
        "source/metadata/session.json",
    )
    for artifact in expected:
        assert (workspace.root / artifact.path).read_bytes() == artifact.content
        assert artifact.size_bytes == len(artifact.content)
        assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()

    manifest = json.loads((workspace.root / "source/manifest.json").read_bytes())
    assert [record["path"] for record in manifest["artifacts"]] == [
        artifact.path for artifact in expected
    ]


def test_case_digest_sorts_sources_and_includes_every_identity() -> None:
    request = _request()
    nodes = _nodes(request)
    sources = _artifacts()
    original = case_digest(_manifest(), request, nodes, source_artifacts=sources)

    assert original == case_digest(_manifest(), request, nodes, source_artifacts=reversed(sources))
    assert original != case_digest(
        _manifest(),
        request,
        nodes,
        source_artifacts=(
            sources[0],
            SourceArtifact(
                path=sources[1].path,
                media_type=sources[1].media_type,
                content=sources[1].content + b"changed",
            ),
        ),
    )
    assert original != case_digest(
        _manifest(),
        request,
        nodes,
        source_artifacts=(
            sources[0],
            SourceArtifact(
                path=sources[1].path,
                media_type="application/octet-stream",
                content=sources[1].content,
            ),
        ),
    )
    assert original != case_digest(_manifest(), request, nodes, source_artifacts=())


def test_no_source_call_shape_remains_supported(tmp_path: Path) -> None:
    request = _request()
    workspace = CaseWorkspace.create(
        tmp_path / "case",
        manifest=_manifest(),
        request=request,
        nodes=_nodes(request),
        default_experiment=EXPERIMENT,
    )

    evidence = workspace.load()

    assert evidence.source_artifacts == ()
    assert evidence.digest == case_digest(_manifest(), request, _nodes(request))
    assert not (workspace.root / "source").exists()


@pytest.mark.parametrize(
    "path",
    [
        "events.jsonl",
        "/source/events.jsonl",
        "../source/events.jsonl",
        "source/../events.jsonl",
        "source//events.jsonl",
        "source/./events.jsonl",
        "source\\events.jsonl",
        "source/events.jsonl/",
        "source",
        "source/manifest.json",
        "source/MANIFEST.JSON",
        "source/CON",
        "source/nul.txt",
        "source/trailing.",
        "source/trailing ",
        "source/invalid:name",
        "source/cafe\u0301.jsonl",
    ],
)
def test_source_artifact_rejects_unsafe_or_reserved_paths(path: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=r"path|source|reserved"):
        SourceArtifact(path=path, media_type="application/x-ndjson", content=b"{}\n")


@pytest.mark.parametrize(
    "path",
    [
        f"source/{'x' * 256}",
        "source/" + "/".join("abcdefgh" for _ in range(114)),
    ],
)
def test_source_artifact_rejects_overlong_paths(path: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=r"path|component|long"):
        SourceArtifact(path=path, media_type="application/octet-stream", content=b"x")


@pytest.mark.parametrize(
    "media_type",
    [
        "",
        "application",
        "/json",
        "application/",
        "application /json",
        "application/json; charset=utf-8",
        "application/json\n",
    ],
)
def test_source_artifact_rejects_invalid_media_types(media_type: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=r"media|type|string"):
        SourceArtifact(path="source/events.jsonl", media_type=media_type, content=b"{}\n")


def test_source_artifact_normalizes_media_type_for_stable_identity() -> None:
    artifact = SourceArtifact(
        path="source/events.jsonl",
        media_type="Application/JSON",
        content=b"{}\n",
    )

    assert artifact.media_type == "application/json"
    assert artifact.identity.media_type == "application/json"


@pytest.mark.parametrize("content", ["{}\n", bytearray(b"{}\n"), memoryview(b"{}\n"), None])
def test_source_artifact_requires_exact_bytes(content: object) -> None:
    with pytest.raises((ValidationError, TypeError, ValueError), match=r"bytes|content|input"):
        SourceArtifact(
            path="source/events.jsonl",
            media_type="application/x-ndjson",
            content=content,  # type: ignore[arg-type]
        )


def test_source_artifact_rejects_empty_content() -> None:
    with pytest.raises((ValidationError, ValueError), match=r"empty|content|byte"):
        SourceArtifact(
            path="source/events.jsonl",
            media_type="application/x-ndjson",
            content=b"",
        )


def test_source_artifact_rejects_content_above_per_file_limit() -> None:
    with pytest.raises((ValidationError, ValueError), match=r"limit|byte|large"):
        SourceArtifact(
            path="source/events.bin",
            media_type="application/octet-stream",
            content=b"x" * (64 * 1024 * 1024 + 1),
        )


def test_create_rejects_more_than_64_source_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "case"
    artifacts = tuple(
        SourceArtifact(
            path=f"source/{index:02d}.bin",
            media_type="application/octet-stream",
            content=b"x",
        )
        for index in range(65)
    )

    with pytest.raises(CaseFormatError, match=r"source|artifact|count|64|limit"):
        _create_case(target, source_artifacts=artifacts)

    assert not target.exists()


def test_create_stops_reading_source_iterable_at_the_count_limit(tmp_path: Path) -> None:
    yielded = 0

    def artifacts() -> Iterator[SourceArtifact]:
        nonlocal yielded
        for index in range(1_000):
            yielded += 1
            yield SourceArtifact(
                path=f"source/{index:03d}.bin",
                media_type="application/octet-stream",
                content=b"x",
            )

    with pytest.raises(CaseFormatError, match=r"source|artifact|count|limit"):
        _create_case(tmp_path / "case", source_artifacts=artifacts())

    assert yielded == 65


def test_create_rejects_source_content_above_aggregate_limit(tmp_path: Path) -> None:
    target = tmp_path / "case"
    large_content = b"x" * (64 * 1024 * 1024)
    artifacts = (
        *(
            SourceArtifact(
                path=f"source/{index}.bin",
                media_type="application/octet-stream",
                content=large_content,
            )
            for index in range(4)
        ),
        SourceArtifact(
            path="source/overflow.bin",
            media_type="application/octet-stream",
            content=b"x",
        ),
    )

    with pytest.raises(CaseFormatError, match=r"source|artifact|total|byte|limit"):
        _create_case(target, source_artifacts=artifacts)

    assert not target.exists()


def test_create_rejects_duplicate_source_paths_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "case"
    duplicate = SourceArtifact(
        path="source/events.jsonl",
        media_type="application/octet-stream",
        content=b"other",
    )

    with pytest.raises((CaseFormatError, ValueError), match=r"source|artifact|duplicate|unique"):
        _create_case(target, source_artifacts=(_artifacts()[1], duplicate))

    assert not target.exists()


def test_create_rejects_overlapping_source_paths_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "case"
    artifacts = (
        SourceArtifact(path="source/record", media_type="application/octet-stream", content=b"a"),
        SourceArtifact(
            path="source/record/child",
            media_type="application/octet-stream",
            content=b"b",
        ),
    )

    with pytest.raises(CaseFormatError, match="invalid source artifacts"):
        _create_case(target, source_artifacts=artifacts)

    assert not target.exists()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("source/Event.jsonl", "source/event.jsonl"),
        ("source/Straße.jsonl", "source/STRASSE.jsonl"),
    ],
)
def test_create_rejects_source_paths_that_alias_on_supported_platforms(
    tmp_path: Path,
    first: str,
    second: str,
) -> None:
    artifacts = (
        SourceArtifact(path=first, media_type="application/octet-stream", content=b"a"),
        SourceArtifact(path=second, media_type="application/octet-stream", content=b"b"),
    )

    with pytest.raises(CaseFormatError, match="invalid source artifacts"):
        _create_case(tmp_path / "case", source_artifacts=artifacts)


def test_creation_failure_removes_partial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "case"

    def fail_after_root_creation(
        _root: Path,
        _sources: tuple[SourceArtifact, ...],
        _published: list[case_filesystem.PublishedFile],
    ) -> None:
        raise OSError("injected write failure")

    monkeypatch.setattr("ithrl.case.storage.write_sources", fail_after_root_creation)

    with pytest.raises(OSError, match="injected write failure"):
        _create_case(target, source_artifacts=_artifacts())

    assert not target.exists()


def test_atomic_publish_does_not_remove_a_concurrent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "evidence.bin"

    def create_competing_file(
        _source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        follow_symlinks: bool,
    ) -> None:
        assert not follow_symlinks
        Path(target).write_bytes(b"competing content")
        raise FileExistsError

    monkeypatch.setattr(os, "link", create_competing_file)

    with pytest.raises(CaseExistsError, match="overwrite"):
        case_filesystem.atomic_write_bytes(destination, b"case content")

    assert destination.read_bytes() == b"competing content"
    assert tuple(tmp_path.glob(".*.tmp")) == ()


def test_failed_creation_preserves_a_concurrent_case_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "case"
    competing_content = b"owned by another process"
    original_link = os.link

    def publish_or_compete(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        follow_symlinks: bool,
    ) -> None:
        destination_path = Path(destination)
        if destination_path.name == "events.jsonl":
            destination_path.write_bytes(competing_content)
            raise FileExistsError
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", publish_or_compete)

    with pytest.raises(CaseExistsError, match="overwrite"):
        _create_case(target, source_artifacts=_artifacts())

    assert (target / "source/events.jsonl").read_bytes() == competing_content


def test_load_rejects_source_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    artifact_inode = (workspace.root / "source/events.jsonl").stat().st_ino
    original_signature = case_filesystem.file_signature
    artifact_calls = 0

    def changed_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        nonlocal artifact_calls
        signature = original_signature(metadata)
        if metadata.st_ino == artifact_inode:
            artifact_calls += 1
            if artifact_calls == 6:
                return (*signature[:4], signature[4] + 1)
        return signature

    monkeypatch.setattr(case_filesystem, "file_signature", changed_signature)

    with pytest.raises(UnsafeCasePathError, match="changed while reading"):
        workspace.load()


def test_creation_does_not_mutate_source_material(tmp_path: Path) -> None:
    native_source = tmp_path / "native.jsonl"
    native_bytes = b'{"native":true}\r\n\x00'
    native_source.write_bytes(native_bytes)
    before = native_source.stat()
    artifact = SourceArtifact(
        path="source/events.jsonl",
        media_type="application/x-ndjson",
        content=native_source.read_bytes(),
    )

    workspace = _create_case(tmp_path / "case", source_artifacts=(artifact,))

    after = native_source.stat()
    assert native_source.read_bytes() == native_bytes
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert (workspace.root / artifact.path).read_bytes() == native_bytes


@pytest.mark.parametrize(
    ("fixture", "case_path"),
    [
        (FIXTURES / "codex-session" / "rollout.jsonl", "source/rollout.jsonl"),
        (FIXTURES / "copilot-session" / "events.jsonl", "source/events.jsonl"),
    ],
)
def test_parsed_native_session_bytes_round_trip_through_case_source(
    tmp_path: Path, fixture: Path, case_path: str
) -> None:
    before = fixture.stat()
    session = parse_session(fixture)
    artifact = SourceArtifact(
        path=case_path,
        media_type="application/x-ndjson",
        content=session.native_bytes,
    )

    evidence = _create_case(tmp_path / "case", source_artifacts=(artifact,)).load()

    after = fixture.stat()
    assert evidence.source_artifacts == (artifact,)
    assert evidence.source_artifacts[0].sha256 == session.source.sha256
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_source_tree_uses_private_permissions(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())

    for relative in ("source", "source/metadata"):
        assert stat.S_IMODE((workspace.root / relative).stat().st_mode) == 0o700
    for relative in (
        "source/manifest.json",
        "source/events.jsonl",
        "source/metadata/session.json",
    ):
        assert stat.S_IMODE((workspace.root / relative).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
@pytest.mark.parametrize(
    "relative",
    ["source", "source/metadata", "source/manifest.json", "source/events.jsonl"],
)
def test_load_rejects_insecure_source_permissions(tmp_path: Path, relative: str) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    target = workspace.root / relative
    target.chmod(0o755 if target.is_dir() else 0o644)

    with pytest.raises(UnsafeCasePathError, match=r"0700|0600|mode|permission"):
        workspace.load()


def test_load_rejects_missing_source_artifact(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    (workspace.root / "source/events.jsonl").unlink()

    with pytest.raises(
        (CaseFormatError, UnsafeCasePathError), match=r"source|artifact|missing|regular"
    ):
        workspace.load()


def test_load_rejects_tampered_source_artifact(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    path = workspace.root / "source/events.jsonl"
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

    with pytest.raises(CaseFormatError, match=r"source|artifact|digest|identity"):
        workspace.load()


def test_load_rejects_oversized_source_artifact_before_reading(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    path = workspace.root / "source/events.jsonl"
    with path.open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(CaseFormatError, match=r"source artifact exceeds the .*byte limit"):
        workspace.load()


def test_load_rejects_unexpected_source_artifact(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    unexpected = workspace.root / "source/unexpected.bin"
    unexpected.write_bytes(b"unexpected")
    unexpected.chmod(0o600)

    with pytest.raises(CaseFormatError, match=r"unexpected|source|artifact"):
        workspace.load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_load_rejects_symlinked_source_artifact(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    artifact = workspace.root / "source/events.jsonl"
    artifact.unlink()
    artifact.symlink_to(tmp_path / "outside.jsonl")

    with pytest.raises(UnsafeCasePathError, match=r"link|regular|source"):
        workspace.load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_load_rejects_symlinked_source_directory(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    source = workspace.root / "source"
    moved_source = tmp_path / "moved-source"
    source.rename(moved_source)
    source.symlink_to(moved_source, target_is_directory=True)

    with pytest.raises(UnsafeCasePathError, match=r"link|source"):
        workspace.load()


def test_load_rejects_nonregular_source_artifact(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    artifact = workspace.root / "source/events.jsonl"
    artifact.unlink()
    artifact.mkdir(mode=0o700)

    with pytest.raises(
        (CaseFormatError, UnsafeCasePathError), match=r"regular|source|artifact|unexpected"
    ):
        workspace.load()


def test_load_rejects_missing_source_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    (workspace.root / "source/manifest.json").unlink()

    with pytest.raises(
        (CaseFormatError, UnsafeCasePathError), match=r"manifest|source|missing|regular"
    ):
        workspace.load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_load_rejects_symlinked_source_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest = workspace.root / "source/manifest.json"
    manifest.unlink()
    manifest.symlink_to(tmp_path / "outside.json")

    with pytest.raises(UnsafeCasePathError, match=r"link|regular|manifest"):
        workspace.load()


def test_load_rejects_nonregular_source_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest = workspace.root / "source/manifest.json"
    manifest.unlink()
    manifest.mkdir(mode=0o700)

    with pytest.raises(UnsafeCasePathError, match=r"regular|manifest|source"):
        workspace.load()


def test_load_rejects_oversized_source_manifest_before_reading(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest = workspace.root / "source/manifest.json"
    with manifest.open("r+b") as stream:
        stream.truncate(1024 * 1024 + 1)

    with pytest.raises(CaseFormatError, match=r"source manifest exceeds the .*byte limit"):
        workspace.load()


@pytest.mark.parametrize(
    "replacement",
    [b'{"schema_version":', b"[]\n", b'{"schema_version":1,"schema_version":1}\n'],
)
def test_load_rejects_malformed_or_duplicate_key_source_manifest(
    tmp_path: Path, replacement: bytes
) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    (workspace.root / "source/manifest.json").write_bytes(replacement)

    with pytest.raises(CaseFormatError, match=r"manifest|JSON|duplicate|object"):
        workspace.load()


def test_load_rejects_noncanonical_source_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest_path = workspace.root / "source/manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(CaseFormatError, match=r"canonical|manifest"):
        workspace.load()


def test_load_rejects_duplicate_artifact_paths_in_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest_path = workspace.root / "source/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][1]["path"] = manifest["artifacts"][0]["path"]
    manifest_path.write_bytes(_render_manifest_json(manifest))

    with pytest.raises(CaseFormatError, match=r"duplicate|unique|manifest"):
        workspace.load()


@pytest.mark.parametrize("path", ["../outside", "/outside", "source/../outside"])
def test_load_rejects_unsafe_artifact_path_in_manifest(tmp_path: Path, path: str) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest_path = workspace.root / "source/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][0]["path"] = path
    manifest_path.write_bytes(_render_manifest_json(manifest))

    with pytest.raises(CaseFormatError, match=r"path|source|manifest"):
        workspace.load()


def test_load_rejects_manifest_identity_tampering(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case", source_artifacts=_artifacts())
    manifest_path = workspace.root / "source/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    manifest_path.write_bytes(_render_manifest_json(manifest))

    with pytest.raises(CaseFormatError, match=r"digest|identity|source|artifact"):
        workspace.load()


def test_load_rejects_source_directory_without_manifest(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    source = workspace.root / "source"
    source.mkdir(mode=0o700)
    artifact = source / "events.jsonl"
    artifact.write_bytes(b"{}\n")
    artifact.chmod(0o600)

    with pytest.raises(CaseFormatError, match=r"source|manifest|unexpected"):
        workspace.load()
