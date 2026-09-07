from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.case import (
    CaptureFacts,
    CaseExistsError,
    CaseFormatError,
    CaseManifest,
    CaseWorkspace,
    ProvenanceFacts,
    ReplayFacts,
    UnsafeCasePathError,
    case_digest,
)
from ithrl.context import ContextNode, content_digest

EXPERIMENT = """\
schema_version = 1

[oracle]
type = "tool-call"
name = "exec_command"
"""


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
            {"id": "C", "type": "function_call_output", "call_id": "B"},
        ],
    }


def _nodes(request: dict[str, Any]) -> tuple[ContextNode, ...]:
    return tuple(
        ContextNode(
            id=node_id,
            kind=str(item.get("type", "message")),
            path=f"/input/{index}",
            mutable=True,
            depends_on=("B",) if node_id == "C" else (),
            source="source/inference.json",
            digest=content_digest(item),
        )
        for index, (node_id, item) in enumerate(zip(("A", "B", "C"), request["input"], strict=True))
    )


def _create_case(path: Path) -> CaseWorkspace:
    request = _request()
    return CaseWorkspace.create(
        path,
        manifest=_manifest(),
        request=request,
        nodes=_nodes(request),
        default_experiment=EXPERIMENT,
    )


def test_workspace_round_trip_and_defensive_request_copy(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")

    evidence = workspace.load()
    request_copy = evidence.request
    copied_input = request_copy["input"]
    assert isinstance(copied_input, list)
    copied_input.clear()

    assert evidence.request == _request()
    assert evidence.materialize({"B"})["input"] == [_request()["input"][0]]
    assert evidence.default_experiment == EXPERIMENT
    assert len(evidence.digest) == 64
    assert (workspace.root / ".gitignore").read_text() == "*\n!.gitignore\n"


def test_workspace_writes_expected_schema_versions(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")

    assert "schema_version = 1" in (workspace.root / "case.toml").read_text()
    records = (workspace.root / "context.jsonl").read_text().splitlines()
    assert len(records) == 3
    assert all('"schema_version":1' in record for record in records)
    assert (workspace.root / "experiments/default.toml").read_text() == EXPERIMENT


def test_case_digest_includes_context_node_order() -> None:
    request = _request()
    nodes = _nodes(request)

    assert case_digest(_manifest(), request, nodes) != case_digest(
        _manifest(), request, reversed(nodes)
    )


def test_create_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "case"
    _create_case(target)

    with pytest.raises(CaseExistsError, match="already exists"):
        _create_case(target)


def test_create_refuses_parent_traversal(tmp_path: Path) -> None:
    target = tmp_path / "parent" / ".." / "case"

    with pytest.raises(UnsafeCasePathError, match=r"cannot contain '\.\.'"):
        _create_case(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_workspace_refuses_symlink_root_and_entries(tmp_path: Path) -> None:
    real = tmp_path / "real"
    workspace = _create_case(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(UnsafeCasePathError, match="symlink"):
        CaseWorkspace.open(alias)

    request_path = workspace.root / "request.json"
    request_path.unlink()
    request_path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(UnsafeCasePathError, match="regular file"):
        workspace.load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_workspace_refuses_symlinked_experiments_directory(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    experiment = workspace.root / "experiments/default.toml"
    experiment.unlink()
    (workspace.root / "experiments").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "default.toml").write_text(EXPERIMENT)
    (workspace.root / "experiments").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeCasePathError, match="symbolic link"):
        workspace.load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_workspace_uses_private_permissions(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")

    assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((workspace.root / "experiments").stat().st_mode) == 0o700
    for path in (
        workspace.root / "case.toml",
        workspace.root / "request.json",
        workspace.root / "context.jsonl",
        workspace.root / "experiments/default.toml",
        workspace.root / ".gitignore",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
@pytest.mark.parametrize(
    ("relative", "mode", "message"),
    [
        (".", 0o755, "case directory must have mode 0700"),
        ("experiments", 0o755, "case directory must have mode 0700"),
        ("request.json", 0o644, "case file must have mode 0600"),
    ],
)
def test_workspace_refuses_insecure_existing_permissions(
    tmp_path: Path, relative: str, mode: int, message: str
) -> None:
    workspace = _create_case(tmp_path / "case")
    target = workspace.root if relative == "." else workspace.root / relative
    target.chmod(mode)

    with pytest.raises(UnsafeCasePathError, match=message):
        workspace.load()


def test_workspace_rejects_invalid_experiment_before_writing(tmp_path: Path) -> None:
    request = _request()

    with pytest.raises(CaseFormatError, match="schema_version = 1"):
        CaseWorkspace.create(
            tmp_path / "case",
            manifest=_manifest(),
            request=request,
            nodes=_nodes(request),
            default_experiment="schema_version = 2\n",
        )
    assert not (tmp_path / "case").exists()


@pytest.mark.parametrize(
    ("request_value", "message"),
    [
        ({"model": "fake", "input": ("not", "json")}, "unsupported JSON value type: tuple"),
        ({"model": "fake", 1: "not-json"}, "JSON objects must use string keys"),
    ],
)
def test_workspace_rejects_non_json_requests_before_writing(
    tmp_path: Path,
    request_value: dict[object, object],
    message: str,
) -> None:
    target = tmp_path / "case"

    with pytest.raises(CaseFormatError, match=message):
        CaseWorkspace.create(
            target,
            manifest=_manifest(),
            request=request_value,  # type: ignore[arg-type]
            nodes=(),
            default_experiment=EXPERIMENT,
        )

    assert not target.exists()


def test_workspace_detects_tampered_request(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    request_path = workspace.root / "request.json"
    request_path.write_text('{"input": [], "model": "deterministic"}\n')

    with pytest.raises(CaseFormatError, match="invalid case workspace"):
        workspace.load()


def test_workspace_rejects_duplicate_context_record_keys(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    context_path = workspace.root / "context.jsonl"
    context_path.write_text(
        '{"schema_version":1,"id":"A","id":"B"}\n',
        encoding="utf-8",
    )

    with pytest.raises(CaseFormatError, match="duplicate key 'id'"):
        workspace.load()


def test_workspace_rejects_oversized_case_file_before_reading(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    request_path = workspace.root / "request.json"
    with request_path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(CaseFormatError, match=r"request\.json exceeds the .*byte limit"):
        workspace.load()


def test_workspace_detects_weakened_gitignore(tmp_path: Path) -> None:
    workspace = _create_case(tmp_path / "case")
    (workspace.root / ".gitignore").write_text("*.key\n")

    with pytest.raises(CaseFormatError, match="deny-by-default"):
        workspace.load()


@pytest.mark.parametrize(
    "captured_at",
    [
        "2026-09-01 12:00:00+02:00",
        "2026-09-01T12:00:00+02",
        "2026-09-01T12:00:00+02:00:30",
        "2026-02-30T12:00:00Z",
    ],
)
def test_manifest_rejects_timestamps_outside_supported_rfc3339_grammar(
    captured_at: str,
) -> None:
    with pytest.raises(ValidationError, match="RFC 3339"):
        CaseManifest.model_validate({**_manifest().model_dump(), "captured_at": captured_at})


def test_manifest_accepts_supported_rfc3339_offsets_and_utc() -> None:
    for captured_at in (
        "2026-09-01T12:00:00+02:00",
        "2026-09-01T10:00:00Z",
        "2026-09-01T10:00:00.123456Z",
    ):
        manifest = CaseManifest.model_validate(
            {**_manifest().model_dump(), "captured_at": captured_at}
        )
        assert manifest.captured_at == captured_at


def test_manifest_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        CaseManifest.model_validate({**_manifest().model_dump(), "schema_version": 2})
