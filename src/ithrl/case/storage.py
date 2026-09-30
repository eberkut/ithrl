"""Private workspace storage for complete case evidence."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ithrl.case.contracts import CaseError, CaseExistsError, CaseFormatError, CaseManifest
from ithrl.case.evidence import CaseEvidence, validate_case_inputs
from ithrl.case.filesystem import (
    PublishedFile,
    atomic_write,
    create_private_directory,
    prepare_target_path,
    read_safe_text,
    remove_published_file,
    require_private_directory,
    set_private_mode,
    validate_existing_root,
    validate_target_path,
)
from ithrl.case.source import SourceArtifact, prepare_source_artifacts
from ithrl.case.source_storage import load_sources, source_parent_paths, write_sources
from ithrl.context import (
    MAX_CONTEXT_NODES,
    ContextGraph,
    ContextNode,
    JsonObject,
    JsonValue,
    validate_json_limits,
)
from ithrl.decisions import DecisionEvidence

_EXPERIMENT_PATH = Path("experiments/default.toml")
_GITIGNORE = "*\n!.gitignore\n"
_MAX_CONTEXT_RECORD_BYTES = 1024 * 1024
_CASE_FILE_LIMITS = {
    "case.toml": 1024 * 1024,
    "request.json": 64 * 1024 * 1024,
    "context.jsonl": 64 * 1024 * 1024,
    "decision.json": 1024 * 1024,
    "default.toml": 1024 * 1024,
    ".gitignore": 1024,
}


class CaseWorkspace:
    """Safe reader and creator for one plain-directory case workspace."""

    __slots__ = ("root",)

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def prepare_destination(cls, path: str | os.PathLike[str]) -> Path:
        """Validate a new case path and create any missing private parents."""

        return prepare_target_path(Path(path))

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        *,
        manifest: CaseManifest,
        request: JsonObject | None,
        nodes: Iterable[ContextNode],
        default_experiment: str,
        source_artifacts: Iterable[SourceArtifact] = (),
        decision: DecisionEvidence | None = None,
    ) -> CaseWorkspace:
        root = validate_target_path(Path(path))
        node_tuple = tuple(nodes)
        graph = ContextGraph(nodes=node_tuple)
        sources = _validate_sources(source_artifacts)
        try:
            validate_case_inputs(
                manifest=manifest,
                request=request,
                context=graph,
                source_artifacts=sources,
                decision=decision,
            )
        except CaseFormatError:
            raise
        except ValueError as error:
            raise CaseFormatError(f"case evidence is invalid: {error}") from error
        _validate_experiment(default_experiment)

        contents = _case_contents(manifest, request, node_tuple, default_experiment, decision)
        _validate_case_file_limits(contents)
        _create_root(root)
        published: list[PublishedFile] = []
        try:
            experiments = create_private_directory(root / "experiments")
            published.append(atomic_write(root / ".gitignore", contents[".gitignore"]))
            write_sources(root, sources, published)
            published.append(atomic_write(root / "case.toml", contents["case.toml"]))
            if request_content := contents.get("request.json"):
                published.append(atomic_write(root / "request.json", request_content))
            if decision_content := contents.get("decision.json"):
                published.append(atomic_write(root / "decision.json", decision_content))
            published.append(atomic_write(root / "context.jsonl", contents["context.jsonl"]))
            published.append(atomic_write(experiments / "default.toml", contents["default.toml"]))
        except Exception:
            with contextlib.suppress(OSError):
                _remove_incomplete_workspace(root, sources, published)
            raise
        return cls(root)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> CaseWorkspace:
        return cls(validate_existing_root(Path(path)))

    def load(self) -> CaseEvidence:
        """Load and structurally validate case files without adapter interpretation."""

        root = validate_existing_root(self.root)
        require_private_directory(root / "experiments", label="experiments")
        try:
            manifest = _load_case_manifest(root)
            request = _load_optional_request(root)
            decision = _load_optional_decision(root)
            context = _load_context(root)
            experiment = read_safe_text(
                root / _EXPERIMENT_PATH, max_bytes=_CASE_FILE_LIMITS["default.toml"]
            )
            _validate_experiment(experiment)
            _validate_gitignore(root)
            return CaseEvidence(
                manifest=manifest,
                request=request,
                context=context,
                default_experiment=experiment,
                source_artifacts=load_sources(root),
                decision=decision,
            )
        except CaseError:
            raise
        except (
            json.JSONDecodeError,
            RecursionError,
            tomllib.TOMLDecodeError,
            UnicodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise CaseFormatError(f"invalid case workspace: {root}") from error


def _validate_sources(artifacts: Iterable[SourceArtifact]) -> tuple[SourceArtifact, ...]:
    try:
        return prepare_source_artifacts(artifacts)
    except (TypeError, ValidationError, ValueError) as error:
        raise CaseFormatError("invalid source artifacts") from error


def _case_contents(
    manifest: CaseManifest,
    request: JsonObject | None,
    nodes: tuple[ContextNode, ...],
    default_experiment: str,
    decision: DecisionEvidence | None,
) -> dict[str, str]:
    contents = {
        "case.toml": _render_manifest(manifest),
        "context.jsonl": "".join(_render_json_line(node.model_dump(mode="json")) for node in nodes),
        "default.toml": _normalize_text(default_experiment),
        ".gitignore": _GITIGNORE,
    }
    if request is not None:
        contents["request.json"] = _render_json(request)
    if decision is not None:
        contents["decision.json"] = _render_json(decision.model_dump(mode="json"))
    return contents


def _validate_case_file_limits(contents: Mapping[str, str]) -> None:
    for name, content in contents.items():
        limit = _CASE_FILE_LIMITS[name]
        if len(content.encode("utf-8")) > limit:
            raise CaseFormatError(f"{name} exceeds the {limit}-byte limit")
        if name == "context.jsonl":
            _validate_context_record_limits(content)


def _validate_context_record_limits(content: str) -> None:
    for line_number, line in enumerate(io.StringIO(content), start=1):
        record = line.removesuffix("\n").removesuffix("\r")
        if len(record.encode("utf-8")) > _MAX_CONTEXT_RECORD_BYTES:
            raise CaseFormatError(
                f"context.jsonl record at line {line_number} exceeds the byte limit"
            )


def _create_root(root: Path) -> None:
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise CaseExistsError(f"case path already exists: {root}") from error
    except OSError as error:
        raise CaseError(f"cannot create case directory: {root}") from error
    set_private_mode(root, 0o700)


def _load_case_manifest(root: Path) -> CaseManifest:
    source = read_safe_text(root / "case.toml", max_bytes=_CASE_FILE_LIMITS["case.toml"])
    return CaseManifest.model_validate(tomllib.loads(source))


def _load_optional_request(root: Path) -> JsonObject | None:
    if not os.path.lexists(root / "request.json"):
        return None
    request = _load_json(
        read_safe_text(root / "request.json", max_bytes=_CASE_FILE_LIMITS["request.json"]),
        label="request.json",
    )
    if not isinstance(request, dict):
        raise CaseFormatError("request.json must contain a JSON object")
    validate_json_limits(request)
    return request


def _load_optional_decision(root: Path) -> DecisionEvidence | None:
    if not os.path.lexists(root / "decision.json"):
        return None
    source = read_safe_text(root / "decision.json", max_bytes=_CASE_FILE_LIMITS["decision.json"])
    _load_json(source, label="decision.json")
    return DecisionEvidence.model_validate_json(source)


def _load_context(root: Path) -> ContextGraph:
    nodes: list[ContextNode] = []
    source = read_safe_text(root / "context.jsonl", max_bytes=_CASE_FILE_LIMITS["context.jsonl"])
    for line_number, line in enumerate(io.StringIO(source), start=1):
        record = line.removesuffix("\n").removesuffix("\r")
        if line_number > MAX_CONTEXT_NODES:
            raise CaseFormatError("context.jsonl exceeds the context record limit")
        if len(record.encode("utf-8")) > _MAX_CONTEXT_RECORD_BYTES:
            raise CaseFormatError(
                f"context.jsonl record at line {line_number} exceeds the byte limit"
            )
        if not record.strip():
            raise CaseFormatError(f"context.jsonl contains a blank record at line {line_number}")
        try:
            nodes.append(
                ContextNode.model_validate(
                    _load_json(record, label=f"context.jsonl line {line_number}")
                )
            )
        except ValidationError as error:
            raise CaseFormatError(f"invalid context.jsonl record at line {line_number}") from error
    return ContextGraph(nodes=tuple(nodes))


def _validate_gitignore(root: Path) -> None:
    if read_safe_text(root / ".gitignore", max_bytes=_CASE_FILE_LIMITS[".gitignore"]) != _GITIGNORE:
        raise CaseFormatError("case .gitignore is not deny-by-default")


def _load_json(source: str, *, label: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CaseFormatError(f"{label} contains a duplicate object key")
            result[key] = value
        return result

    try:
        return json.loads(source, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, RecursionError) as error:
        raise CaseFormatError(f"invalid {label}") from error


def _remove_incomplete_workspace(
    root: Path,
    sources: tuple[SourceArtifact, ...],
    published: list[PublishedFile],
) -> None:
    for entry in reversed(published):
        remove_published_file(entry)

    directories = {
        root / "experiments",
        *(root / parent for artifact in sources for parent in source_parent_paths(artifact.path)),
    }
    for directory in sorted(directories, key=_path_depth, reverse=True):
        _remove_directory(directory)
    _remove_directory(root)


def _remove_directory(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        path.rmdir()


def _path_depth(path: Path) -> int:
    return len(path.parts)


def _normalize_text(value: str) -> str:
    return value if value.endswith("\n") else value + "\n"


def _render_json(value: JsonValue) -> str:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, ValueError) as error:
        raise CaseFormatError("request is not valid JSON") from error


def _render_json_line(value: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _validate_experiment(value: str) -> None:
    try:
        parsed = tomllib.loads(value)
    except tomllib.TOMLDecodeError as error:
        raise CaseFormatError("default experiment is not valid TOML") from error
    version = parsed.get("schema_version")
    if type(version) is not int or version != 1:
        raise CaseFormatError("default experiment must declare schema_version = 1")
    if parsed.get("kind") not in ("deterministic", "sampled"):
        raise CaseFormatError("default experiment must declare a supported kind")


def _render_manifest(manifest: CaseManifest) -> str:
    data = manifest.model_dump(mode="json", exclude_none=True)
    sections = {
        "capture": data.pop("capture"),
        "replay": data.pop("replay"),
        "provenance": data.pop("provenance"),
    }
    lines = [f"{key} = {_toml_scalar(value)}" for key, value in data.items()]
    for section, values in sections.items():
        lines.extend(("", f"[{section}]"))
        lines.extend(f"{key} = {_toml_scalar(value)}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise CaseFormatError(f"unsupported case manifest value: {type(value).__name__}")
