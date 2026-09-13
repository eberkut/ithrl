"""Validate context edits and build requests deterministically."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_REMOVED = object()
_TERMINAL = object()
MAX_CONTEXT_NODES = 20_000
MAX_DEPENDENCIES_PER_NODE = 256
MAX_DEPENDENCY_DEPTH = 256
MAX_JSON_DEPTH = 128
MAX_JSON_ITEMS = 1_000_000


class ContextGraphError(ValueError):
    """The context edit graph is malformed."""


class UnsafeRemovalError(ContextGraphError):
    """A requested removal would remove protected or frozen evidence."""


class MaterializationError(ContextGraphError):
    """A context edit cannot be applied without violating request invariants."""


def _canonical_json(value: Any) -> bytes:
    validate_json_limits(value)
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError("value is not canonical JSON") from error
    return rendered.encode("utf-8")


def content_digest(value: Any) -> str:
    """Return a deterministic SHA-256 digest for JSON content."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def validate_json_limits(value: object) -> None:
    """Reject invalid JSON values and structures outside local resource limits."""

    pending: list[tuple[object, int]] = [(value, 0)]
    items = 0
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level limit")
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise ValueError("JSON objects must use string keys")
            items += len(current)
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            items += len(current)
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("JSON numbers must be finite")
        elif current is None or isinstance(current, (bool, int, str)):
            pass
        else:
            raise ValueError(f"unsupported JSON value type: {type(current).__name__}")
        if items > MAX_JSON_ITEMS:
            raise ValueError(f"JSON structure exceeds the {MAX_JSON_ITEMS}-item limit")


def decode_pointer(pointer: str) -> tuple[str, ...]:
    """Validate and decode an RFC 6901 JSON Pointer."""

    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or begin with '/'")

    tokens: list[str] = []
    for raw_token in pointer[1:].split("/"):
        token: list[str] = []
        index = 0
        while index < len(raw_token):
            character = raw_token[index]
            if character != "~":
                token.append(character)
                index += 1
                continue
            if index + 1 >= len(raw_token) or raw_token[index + 1] not in {"0", "1"}:
                raise ValueError("JSON Pointer contains an invalid '~' escape")
            token.append("~" if raw_token[index + 1] == "0" else "/")
            index += 2
        tokens.append("".join(token))
    return tuple(tokens)


def stable_context_id(*, path: str, kind: str, digest: str) -> str:
    """Derive a stable, opaque context identity from immutable node facts."""

    decode_pointer(path)
    payload = {"digest": digest, "kind": kind, "path": path}
    return f"ctx-{hashlib.sha256(_canonical_json(payload)).hexdigest()[:16]}"


class SourceRecordSelector(BaseModel):
    """Stable location of one record in immutable JSONL source evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    type: Literal["jsonl-record"] = "jsonl-record"
    line: Annotated[int, Field(ge=1, le=250_000)]
    identity_key: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    identity_value: int | Annotated[str, Field(min_length=1, max_length=1024)] | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> SourceRecordSelector:
        if (self.identity_key is None) != (self.identity_value is None):
            raise ValueError("source record identity key and value must be set together")
        return self


def stable_source_context_id(
    *,
    source: str,
    selector: SourceRecordSelector,
    kind: str,
    digest: str,
) -> str:
    """Derive a stable context identity from immutable source record facts."""

    payload = {
        "digest": digest,
        "kind": kind,
        "selector": selector.model_dump(mode="json"),
        "source": source,
    }
    return f"ctx-{hashlib.sha256(_canonical_json(payload)).hexdigest()[:16]}"


def validate_source_reference(value: str) -> str:
    """Require a portable relative path inside a case workspace."""

    if "\\" in value:
        raise ValueError("source must use portable POSIX path separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("source must be a safe relative path inside the case")
    return value


class ContextNode(BaseModel):
    """Versioned edit metadata for request or native source evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: Annotated[
        str,
        Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    ]
    kind: Annotated[str, Field(min_length=1, max_length=256)]
    path: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    selector: SourceRecordSelector | None = None
    mutable: bool
    protected: bool = False
    depends_on: Annotated[tuple[str, ...], Field(max_length=MAX_DEPENDENCIES_PER_NODE)] = ()
    source: Annotated[str, Field(min_length=1, max_length=1024)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("path")
    @classmethod
    def validate_pointer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decode_pointer(value)
        except ValueError as error:
            raise ValueError(f"invalid RFC 6901 JSON Pointer: {error}") from error
        if value == "":
            raise ValueError("a context node cannot target the request root")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return validate_source_reference(value)

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("depends_on contains duplicate node IDs")
        return value

    @model_validator(mode="after")
    def validate_node(self) -> ContextNode:
        if self.id in self.depends_on:
            raise ValueError("a context node cannot depend on itself")
        if (self.path is None) == (self.selector is None):
            raise ValueError("a context node requires exactly one request path or source selector")
        return self


class ContextGraph(BaseModel):
    """Validated dependencies between locations in the original request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    nodes: Annotated[tuple[ContextNode, ...], Field(max_length=MAX_CONTEXT_NODES)]

    @model_validator(mode="after")
    def validate_graph(self) -> ContextGraph:
        by_id = _index_unique_nodes(self.nodes)
        _validate_non_overlapping_paths(self.nodes)
        _validate_source_order(self.nodes)
        _validate_known_dependencies(self.nodes, by_id)
        _validate_dependency_order(self.nodes)
        return self

    @property
    def by_id(self) -> Mapping[str, ContextNode]:
        return {node.id: node for node in self.nodes}

    def dependency_closure(self, remove_ids: Iterable[str]) -> frozenset[str]:
        """Also remove dependent items so the remaining graph stays valid."""

        requested = frozenset(remove_ids)
        unknown = sorted(requested - self.by_id.keys())
        if unknown:
            raise ContextGraphError(f"unknown context node IDs: {', '.join(unknown)}")

        dependants = _index_dependants(self.nodes)

        closure = set(requested)
        pending = deque(sorted(requested))
        while pending:
            removed = pending.popleft()
            for dependant in sorted(dependants[removed]):
                if dependant not in closure:
                    closure.add(dependant)
                    pending.append(dependant)

        unsafe = sorted(
            node.id
            for node in self.nodes
            if node.id in closure and (node.protected or not node.mutable)
        )
        if unsafe:
            raise UnsafeRemovalError(
                "dependency closure reaches protected or frozen nodes: " + ", ".join(unsafe)
            )
        return frozenset(closure)

    def validate_request(self, request: JsonValue) -> None:
        """Verify that every node still identifies its captured request content."""

        validate_json_limits(request)

        for node in self.nodes:
            if node.path is None:
                continue
            value = _resolve_pointer(request, decode_pointer(node.path), node.path)
            if content_digest(value) != node.digest:
                raise MaterializationError(
                    f"context digest does not match request content at {node.path}"
                )

    def materialize(
        self,
        request: JsonObject,
        remove_ids: Iterable[str] = (),
    ) -> JsonObject:
        """Build a candidate request without changing the original request."""

        closure = self.dependency_closure(remove_ids)
        self.validate_request(request)
        if not closure:
            return copy.deepcopy(request)

        source_only = sorted(
            node.id for node in self.nodes if node.id in closure and node.path is None
        )
        if source_only:
            raise MaterializationError(
                "source-backed context edits require a compatible harness runner: "
                + ", ".join(source_only)
            )

        trie: dict[object, Any] = {}
        for node in self.nodes:
            if node.id not in closure:
                continue
            path = cast(str, node.path)
            tokens = decode_pointer(path)
            _resolve_pointer(request, tokens, path)
            branch = trie
            for token in tokens:
                branch = branch.setdefault(token, {})
            branch[_TERMINAL] = True

        materialized = _prune(copy.deepcopy(request), trie)
        if materialized is _REMOVED or not isinstance(materialized, dict):
            raise MaterializationError("a context edit cannot remove the request root")
        return materialized


def _index_unique_nodes(nodes: Sequence[ContextNode]) -> dict[str, ContextNode]:
    by_id: dict[str, ContextNode] = {}
    paths: set[str] = set()
    for node in nodes:
        if node.id in by_id:
            raise ValueError(f"duplicate context node ID: {node.id}")
        if node.path is not None:
            if node.path in paths:
                raise ValueError(f"duplicate context node path: {node.path}")
            paths.add(node.path)
        by_id[node.id] = node
    return by_id


def _validate_non_overlapping_paths(nodes: Sequence[ContextNode]) -> None:
    ordered_paths = sorted(
        (decode_pointer(node.path), node) for node in nodes if node.path is not None
    )
    for (parent_path, parent), (child_path, child) in pairwise(ordered_paths):
        if len(parent_path) < len(child_path) and child_path[: len(parent_path)] == parent_path:
            raise ValueError(f"context node paths overlap: {parent.id!r} contains {child.id!r}")


def _validate_source_order(nodes: Sequence[ContextNode]) -> None:
    previous_lines: dict[str, int] = {}
    for node in nodes:
        if node.selector is None:
            continue
        previous = previous_lines.get(node.source, 0)
        if node.selector.line <= previous:
            raise ValueError("context source selectors must follow source record order")
        previous_lines[node.source] = node.selector.line


def _validate_known_dependencies(
    nodes: Sequence[ContextNode], by_id: Mapping[str, ContextNode]
) -> None:
    for node in nodes:
        missing = sorted(dependency for dependency in node.depends_on if dependency not in by_id)
        if missing:
            raise ValueError(
                f"context node {node.id!r} has unknown dependencies: {', '.join(missing)}"
            )


def _index_dependants(nodes: Sequence[ContextNode]) -> Mapping[str, set[str]]:
    dependants: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        for dependency in node.depends_on:
            dependants[dependency].add(node.id)
    return dependants


def _validate_dependency_order(nodes: Sequence[ContextNode]) -> None:
    dependants = _index_dependants(nodes)
    remaining_dependencies = {node.id: len(node.depends_on) for node in nodes}
    depth = {node.id: 1 for node in nodes if not node.depends_on}
    ready = deque(depth)

    while ready:
        node_id = ready.popleft()
        remaining_dependencies.pop(node_id)
        for dependant in dependants[node_id]:
            depth[dependant] = max(depth.get(dependant, 1), depth[node_id] + 1)
            if depth[dependant] > MAX_DEPENDENCY_DEPTH:
                raise ValueError(
                    f"context dependency depth exceeds the {MAX_DEPENDENCY_DEPTH}-level limit"
                )
            remaining_dependencies[dependant] -= 1
            if remaining_dependencies[dependant] == 0:
                ready.append(dependant)

    if remaining_dependencies:
        cyclic = next(iter(remaining_dependencies))
        raise ValueError(f"context dependency cycle reaches {cyclic!r}")


def _resolve_pointer(request: JsonValue, tokens: Sequence[str], pointer: str) -> JsonValue:
    current: JsonValue = request
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                raise MaterializationError(f"context path does not exist in request: {pointer}")
            current = current[token]
            continue
        if isinstance(current, list):
            if token == "0":
                index = 0
            elif token.isascii() and token.isdigit() and not token.startswith("0"):
                index = int(token)
            else:
                raise MaterializationError(
                    f"context path uses a non-canonical list index: {pointer}"
                )
            if index >= len(current):
                raise MaterializationError(f"context path does not exist in request: {pointer}")
            current = current[index]
            continue
        raise MaterializationError(f"context path traverses a scalar value: {pointer}")
    return current


def _prune(value: JsonValue, trie: dict[object, Any]) -> JsonValue | object:
    if _TERMINAL in trie:
        return _REMOVED
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            branch = trie.get(key)
            transformed = copy.deepcopy(child) if branch is None else _prune(child, branch)
            if transformed is not _REMOVED:
                result[key] = transformed  # type: ignore[assignment]
        return result
    if isinstance(value, list):
        result_list: list[JsonValue] = []
        for index, child in enumerate(value):
            branch = trie.get(str(index))
            transformed = copy.deepcopy(child) if branch is None else _prune(child, branch)
            if transformed is not _REMOVED:
                result_list.append(transformed)  # type: ignore[arg-type]
        return result_list
    if trie:
        raise MaterializationError("context path traverses a scalar value")
    return value
