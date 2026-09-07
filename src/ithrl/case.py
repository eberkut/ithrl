"""Read and write private case evidence."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import stat
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ithrl.context import (
    ContextGraph,
    ContextNode,
    JsonObject,
    JsonValue,
    content_digest,
    validate_json_limits,
)
from ithrl.timestamps import validate_rfc3339_timestamp

_CASE_FILES = ("case.toml", "request.json", "context.jsonl", ".gitignore")
_EXPERIMENT_PATH = Path("experiments/default.toml")
_GITIGNORE = "*\n!.gitignore\n"
_CASE_FILE_LIMITS = {
    "case.toml": 1024 * 1024,
    "request.json": 64 * 1024 * 1024,
    "context.jsonl": 64 * 1024 * 1024,
    "default.toml": 1024 * 1024,
    ".gitignore": 1024,
}


class CaseError(ValueError):
    """Base error for case workspace operations."""


class CaseExistsError(CaseError):
    """A case cannot be created because its target already exists."""


class UnsafeCasePathError(CaseError):
    """A case path or workspace entry is unsafe."""


class CaseFormatError(CaseError):
    """Persisted case evidence is malformed or internally inconsistent."""


class CaptureFacts(BaseModel):
    """Classification of the captured provider request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    request: Annotated[str, Field(min_length=1)]
    completeness: Annotated[str, Field(min_length=1)]


class ReplayFacts(BaseModel):
    """State and environment requirements known at capture time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    state: Annotated[str, Field(min_length=1)]
    environment: Annotated[str, Field(min_length=1)]


class ProvenanceFacts(BaseModel):
    """Source trace identity without copied credentials or transport headers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source: Annotated[str, Field(min_length=1)]
    inference_call_id: Annotated[str | None, Field(min_length=1)] = None
    trace_schema: Annotated[str, Field(min_length=1)]


class CaseManifest(BaseModel):
    """Versioned immutable facts about an extracted decision case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    adapter: Annotated[str, Field(min_length=1)]
    adapter_version: Annotated[str, Field(min_length=1)]
    captured_at: Annotated[str, Field(min_length=1)]
    provider: Annotated[str, Field(min_length=1)]
    endpoint: Annotated[str, Field(min_length=1)]
    model_requested: Annotated[str, Field(min_length=1)]
    model_returned: Annotated[str | None, Field(min_length=1)] = None
    capture: CaptureFacts
    replay: ReplayFacts
    provenance: ProvenanceFacts

    @field_validator("captured_at")
    @classmethod
    def require_timestamp_offset(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="captured_at")


def case_digest(
    manifest: CaseManifest,
    request: JsonObject,
    nodes: Iterable[ContextNode],
) -> str:
    """Return a deterministic identity for immutable case evidence."""

    ordered_nodes: list[dict[str, Any]] = [node.model_dump(mode="json") for node in nodes]
    return content_digest(
        {
            "manifest": manifest.model_dump(mode="json", exclude_none=True),
            "request": request,
            "context": ordered_nodes,
        }
    )


class CaseEvidence:
    """Loaded immutable evidence with defensive request copies."""

    __slots__ = ("_context", "_default_experiment", "_digest", "_manifest", "_request")

    def __init__(
        self,
        *,
        manifest: CaseManifest,
        request: JsonObject,
        context: ContextGraph,
        default_experiment: str,
    ) -> None:
        validate_json_limits(request)
        context.validate_request(request)
        self._manifest = manifest
        self._request = copy.deepcopy(request)
        self._context = context
        self._default_experiment = default_experiment
        self._digest = case_digest(manifest, request, context.nodes)

    @property
    def manifest(self) -> CaseManifest:
        return self._manifest

    @property
    def request(self) -> JsonObject:
        return copy.deepcopy(self._request)

    @property
    def context(self) -> ContextGraph:
        return self._context

    @property
    def default_experiment(self) -> str:
        return self._default_experiment

    @property
    def digest(self) -> str:
        return self._digest

    def materialize(self, remove_ids: Iterable[str] = ()) -> JsonObject:
        return self._context.materialize(self._request, remove_ids)


class CaseWorkspace:
    """Safe reader and creator for one plain-directory case workspace."""

    __slots__ = ("root",)

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        *,
        manifest: CaseManifest,
        request: JsonObject,
        nodes: Iterable[ContextNode],
        default_experiment: str,
    ) -> CaseWorkspace:
        root = _validate_target_path(Path(path))
        node_tuple = tuple(nodes)
        graph = ContextGraph(nodes=node_tuple)
        try:
            validate_json_limits(request)
        except ValueError as error:
            raise CaseFormatError(f"request is not valid JSON: {error}") from error
        graph.validate_request(request)
        _validate_experiment(default_experiment)

        contents = {
            "case.toml": _render_manifest(manifest),
            "request.json": _render_json(request),
            "context.jsonl": "".join(
                _render_json_line(node.model_dump(mode="json")) for node in node_tuple
            ),
            "default.toml": _normalize_text(default_experiment),
            ".gitignore": _GITIGNORE,
        }
        for name, content in contents.items():
            limit = _CASE_FILE_LIMITS[name]
            if len(content.encode("utf-8")) > limit:
                raise CaseFormatError(f"{name} exceeds the {limit}-byte limit")

        try:
            root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise CaseExistsError(f"case path already exists: {root}") from error
        except OSError as error:
            raise CaseError(f"cannot create case directory: {root}") from error

        try:
            _set_private_mode(root, 0o700)
            experiments = root / "experiments"
            experiments.mkdir(mode=0o700)
            _set_private_mode(experiments, 0o700)

            _atomic_write(root / "case.toml", contents["case.toml"])
            _atomic_write(root / "request.json", contents["request.json"])
            _atomic_write(root / "context.jsonl", contents["context.jsonl"])
            _atomic_write(root / _EXPERIMENT_PATH, contents["default.toml"])
            _atomic_write(root / ".gitignore", contents[".gitignore"])
        except Exception:
            # Preserve the original failure. A retry will refuse any partial
            # workspace left by a concurrent change.
            with contextlib.suppress(OSError):
                _remove_incomplete_workspace(root)
            raise
        return cls(root)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> CaseWorkspace:
        root = _validate_existing_root(Path(path))
        return cls(root)

    def load(self) -> CaseEvidence:
        root = _validate_existing_root(self.root)
        _require_private_directory(root / "experiments", label="experiments")
        try:
            manifest_data = tomllib.loads(
                _read_safe_text(root / "case.toml", max_bytes=_CASE_FILE_LIMITS["case.toml"])
            )
            manifest = CaseManifest.model_validate(manifest_data)
            request = _load_json(
                _read_safe_text(root / "request.json", max_bytes=_CASE_FILE_LIMITS["request.json"]),
                label="request.json",
            )
            if not isinstance(request, dict):
                raise CaseFormatError("request.json must contain a JSON object")
            validate_json_limits(request)

            nodes: list[ContextNode] = []
            for line_number, line in enumerate(
                _read_safe_text(
                    root / "context.jsonl", max_bytes=_CASE_FILE_LIMITS["context.jsonl"]
                ).splitlines(),
                start=1,
            ):
                if not line.strip():
                    raise CaseFormatError(
                        f"context.jsonl contains a blank record at line {line_number}"
                    )
                try:
                    nodes.append(
                        ContextNode.model_validate(
                            _load_json(line, label=f"context.jsonl line {line_number}")
                        )
                    )
                except ValidationError as error:
                    raise CaseFormatError(
                        f"invalid context.jsonl record at line {line_number}"
                    ) from error
            context = ContextGraph(nodes=tuple(nodes))
            experiment = _read_safe_text(
                root / _EXPERIMENT_PATH, max_bytes=_CASE_FILE_LIMITS["default.toml"]
            )
            _validate_experiment(experiment)
            if (
                _read_safe_text(root / ".gitignore", max_bytes=_CASE_FILE_LIMITS[".gitignore"])
                != _GITIGNORE
            ):
                raise CaseFormatError("case .gitignore is not deny-by-default")
            return CaseEvidence(
                manifest=manifest,
                request=request,
                context=context,
                default_experiment=experiment,
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


def _validate_target_path(path: Path) -> Path:
    if ".." in path.parts:
        raise UnsafeCasePathError("case path cannot contain '..'")
    if os.path.lexists(path):
        raise CaseExistsError(f"case path already exists: {path}")
    parent = path.parent if path.parent != Path() else Path()
    _reject_symlink_components(parent)
    if not parent.is_dir():
        raise UnsafeCasePathError(f"case parent directory does not exist: {parent}")
    return path


def _validate_existing_root(path: Path) -> Path:
    if ".." in path.parts:
        raise UnsafeCasePathError("case path cannot contain '..'")
    _reject_symlink_components(path)
    try:
        metadata = path.stat()
    except OSError as error:
        raise UnsafeCasePathError(f"case directory is unavailable: {path}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCasePathError(f"case path is not a directory: {path}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UnsafeCasePathError("case directory must have mode 0700")
    return path


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise UnsafeCasePathError(f"case path contains a symlink: {current}")


def _require_private_directory(path: Path, *, label: str) -> None:
    _reject_symlink_components(path.parent)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CaseFormatError(f"required case directory is unavailable: {label}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must not be a symbolic link: {label}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must be a directory: {label}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UnsafeCasePathError(f"case directory must have mode 0700: {label}")


def _read_safe_text(path: Path, *, max_bytes: int) -> str:
    _reject_symlink_components(path.parent)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CaseFormatError(f"required case file is unavailable: {path.name}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must be a regular file: {path.name}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise UnsafeCasePathError(f"case file must have mode 0600: {path.name}")
    if metadata.st_size > max_bytes:
        raise CaseFormatError(f"{path.name} exceeds the {max_bytes}-byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CaseFormatError(f"cannot read case file: {path.name}") from error


def _load_json(source: str, *, label: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CaseFormatError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(source, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, RecursionError) as error:
        raise CaseFormatError(f"invalid {label}") from error


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _set_private_mode(temporary, 0o600)
        if os.path.lexists(path):
            raise CaseExistsError(f"refusing to overwrite case entry: {path.name}")
        temporary.replace(path)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _remove_incomplete_workspace(root: Path) -> None:
    experiments = root / "experiments"
    for relative in (*_CASE_FILES, str(_EXPERIMENT_PATH)):
        candidate = root / relative
        if os.path.lexists(candidate) and not candidate.is_symlink() and candidate.is_file():
            candidate.unlink()
    if experiments.is_dir() and not experiments.is_symlink():
        experiments.rmdir()
    if root.is_dir() and not root.is_symlink():
        root.rmdir()


def _set_private_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


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
    if parsed.get("schema_version") != 1:
        raise CaseFormatError("default experiment must declare schema_version = 1")


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
