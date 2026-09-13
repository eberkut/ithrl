"""Adapter for local synthetic traces."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ithrl.behavior import MAX_TOOL_NAME_LENGTH, ShellActionTarget
from ithrl.case import (
    CaptureFacts,
    CaseManifest,
    CaseWorkspace,
    ProvenanceFacts,
    ReplayFacts,
)
from ithrl.context import (
    ContextNode,
    JsonObject,
    JsonValue,
    content_digest,
    stable_context_id,
    validate_json_limits,
)
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    FakeRunnerConfig,
    StructuralOracleConfig,
)
from ithrl.timestamps import validate_rfc3339_timestamp

_ADAPTER_VERSION = "1"
_NODE_SOURCE = "request.json"
_MAX_TRACE_BYTES = 16 * 1024 * 1024


class SyntheticTraceError(ValueError):
    """Base error for synthetic trace operations."""


class SyntheticTraceFormatError(SyntheticTraceError):
    """A synthetic trace does not satisfy the versioned fixture contract."""


class InferenceSelectionError(SyntheticTraceError):
    """An inference was not selected explicitly and unambiguously."""


class _ObservedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_LENGTH)]
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class _SyntheticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_returned: Annotated[str, Field(min_length=1)]
    tool_calls: Annotated[tuple[_ObservedToolCall, ...], Field(min_length=1)]


class _FakeBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_LENGTH)]
    required_markers: Annotated[tuple[str, ...], Field(min_length=1)]
    alternative_marker_sets: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def validate_marker_sets(self) -> _FakeBehavior:
        marker_sets = (self.required_markers, *self.alternative_marker_sets)
        if any(not marker_set for marker_set in marker_sets):
            raise ValueError("fake behavior marker sets must not be empty")
        if any(any(not marker for marker in marker_set) for marker_set in marker_sets):
            raise ValueError("fake behavior markers must not be empty")
        if any(len(set(marker_set)) != len(marker_set) for marker_set in marker_sets):
            raise ValueError("fake behavior marker sets must not contain duplicates")
        normalized_sets = tuple(frozenset(marker_set) for marker_set in marker_sets)
        if len(set(normalized_sets)) != len(normalized_sets):
            raise ValueError("fake behavior marker sets must be distinct")
        return self


class _SyntheticInference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inference_call_id: Annotated[str, Field(min_length=1)]
    captured_at: Annotated[str, Field(min_length=1)]
    provider: Annotated[str, Field(min_length=1)]
    endpoint: Annotated[str, Field(min_length=1)]
    request: JsonObject
    response: _SyntheticResponse
    fake_behavior: _FakeBehavior

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="captured_at")

    @model_validator(mode="after")
    def validate_decision(self) -> _SyntheticInference:
        input_items, tools = _validate_synthetic_request(self.request)
        _validate_observed_behavior(
            behavior=self.fake_behavior,
            response=self.response,
            input_items=input_items,
            tools=tools,
        )
        return self


class _SyntheticTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    trace_schema: Annotated[str, Field(min_length=1)]
    inferences: Annotated[tuple[_SyntheticInference, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_unique_inference_ids(self) -> _SyntheticTrace:
        inference_ids = [inference.inference_call_id for inference in self.inferences]
        if len(set(inference_ids)) != len(inference_ids):
            raise ValueError("synthetic trace contains duplicate inference_call_id values")
        return self


@dataclass(frozen=True, slots=True)
class InferenceSummary:
    """Concise, non-sensitive metadata for one selectable inference."""

    inference_call_id: str
    captured_at: str
    provider: str
    endpoint: str
    model_requested: str
    model_returned: str
    input_items: int
    observed_tool_names: tuple[str, ...]


def list_inferences(trace_path: str | Path) -> tuple[InferenceSummary, ...]:
    """List the inferences in a local synthetic trace without selecting one."""

    trace = _load_trace(Path(trace_path))
    return tuple(_summarize(inference) for inference in trace.inferences)


def extract_inference(
    trace_path: str | Path,
    *,
    inference_call_id: str | None,
    case_path: str | Path,
) -> CaseWorkspace:
    """Extract the selected inference into a new case directory."""

    trace = _load_trace(Path(trace_path))
    inference = _select_inference(trace, inference_call_id)
    request = inference.request
    nodes = _context_nodes(request, oracle_tool_name=inference.fake_behavior.tool_name)
    manifest = CaseManifest(
        adapter="synthetic-trace",
        adapter_version=_ADAPTER_VERSION,
        captured_at=inference.captured_at,
        provider=inference.provider,
        endpoint=inference.endpoint,
        model_requested=_requested_model(request),
        model_returned=inference.response.model_returned,
        capture=CaptureFacts(request="provider-request", completeness="complete"),
        replay=ReplayFacts(state="self-contained", environment="local"),
        provenance=ProvenanceFacts(
            source="synthetic-trace",
            inference_call_id=inference.inference_call_id,
            trace_schema=trace.trace_schema,
        ),
    )
    experiment = _render_experiment(inference.fake_behavior, nodes)
    return CaseWorkspace.create(
        case_path,
        manifest=manifest,
        request=request,
        nodes=nodes,
        default_experiment=experiment,
    )


def _load_trace(path: Path) -> _SyntheticTrace:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SyntheticTraceFormatError(
                f"synthetic trace must be a regular file, not a link: {path}"
            )
        if metadata.st_size > _MAX_TRACE_BYTES:
            raise SyntheticTraceFormatError(
                f"synthetic trace exceeds the {_MAX_TRACE_BYTES}-byte limit: {path}"
            )
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SyntheticTraceFormatError(f"cannot read synthetic trace: {path}") from error

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SyntheticTraceFormatError(
                    f"synthetic trace contains duplicate key {key!r}: {path}"
                )
            result[key] = value
        return result

    try:
        payload: Any = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, RecursionError) as error:
        raise SyntheticTraceFormatError(f"invalid synthetic trace JSON: {path}: {error}") from error
    try:
        return _SyntheticTrace.model_validate(payload)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_url=False, include_input=False)
        )
        raise SyntheticTraceFormatError(f"invalid synthetic trace: {path}: {details}") from error


def _summarize(inference: _SyntheticInference) -> InferenceSummary:
    input_items = cast(list[JsonValue], inference.request["input"])
    return InferenceSummary(
        inference_call_id=inference.inference_call_id,
        captured_at=inference.captured_at,
        provider=inference.provider,
        endpoint=inference.endpoint,
        model_requested=_requested_model(inference.request),
        model_returned=inference.response.model_returned,
        input_items=len(input_items),
        observed_tool_names=tuple(call.name for call in inference.response.tool_calls),
    )


def _select_inference(
    trace: _SyntheticTrace,
    inference_call_id: str | None,
) -> _SyntheticInference:
    available = ", ".join(inference.inference_call_id for inference in trace.inferences)
    if inference_call_id is None or not inference_call_id.strip():
        raise InferenceSelectionError(
            f"inference_call_id is required; available inferences: {available}"
        )
    for inference in trace.inferences:
        if inference.inference_call_id == inference_call_id:
            return inference
    raise InferenceSelectionError(
        f"inference_call_id not found: {inference_call_id}; available inferences: {available}"
    )


def _requested_model(request: JsonObject) -> str:
    return cast(str, request["model"])


def _validate_synthetic_request(
    request: JsonObject,
) -> tuple[list[JsonValue], list[JsonValue]]:
    if "fake_behavior" in request:
        raise ValueError("fake behavior metadata must not appear in the provider request")
    validate_json_limits(request)

    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("synthetic request model must be a non-empty string")
    input_items = request.get("input")
    if not isinstance(input_items, list):
        raise ValueError("synthetic request input must be a list")
    tools = request.get("tools")
    if not isinstance(tools, list):
        raise ValueError("synthetic request tools must be a list")
    return input_items, tools


def _validate_observed_behavior(
    *,
    behavior: _FakeBehavior,
    response: _SyntheticResponse,
    input_items: list[JsonValue],
    tools: list[JsonValue],
) -> None:
    target = behavior.tool_name
    declared_names = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and tool.get("type") == "function"
    }
    if target not in declared_names:
        raise ValueError("fake behavior tool must have a declared custom function schema")
    if target not in {call.name for call in response.tool_calls}:
        raise ValueError("fake behavior tool must be present in the observed response")
    if not set(behavior.required_markers) <= _collect_strings(input_items):
        raise ValueError("required fake behavior markers must be present in the request input")


def _context_nodes(
    request: JsonObject,
    *,
    oracle_tool_name: str,
) -> tuple[ContextNode, ...]:
    input_items = cast(list[JsonValue], request["input"])
    return (
        *_input_nodes(input_items),
        *_frozen_request_nodes(request, oracle_tool_name=oracle_tool_name),
    )


def _input_nodes(input_items: list[JsonValue]) -> tuple[ContextNode, ...]:
    nodes: list[ContextNode] = []
    call_nodes: dict[str, str] = {}
    output_call_ids: set[str] = set()
    for index, item in enumerate(input_items):
        path = f"/input/{index}"
        kind = _input_kind(item)
        digest = content_digest(item)
        node_id = stable_context_id(path=path, kind=kind, digest=digest)
        dependencies = _input_dependencies(
            item=item,
            kind=kind,
            path=path,
            node_id=node_id,
            call_nodes=call_nodes,
            output_call_ids=output_call_ids,
        )
        nodes.append(
            ContextNode(
                id=node_id,
                kind=kind,
                path=path,
                mutable=True,
                depends_on=dependencies,
                source=_NODE_SOURCE,
                digest=digest,
            )
        )
    return tuple(nodes)


def _input_dependencies(
    *,
    item: JsonValue,
    kind: str,
    path: str,
    node_id: str,
    call_nodes: dict[str, str],
    output_call_ids: set[str],
) -> tuple[str, ...]:
    if kind == "function_call":
        call_id = _call_identifier(item)
        if call_id is None:
            raise SyntheticTraceFormatError(
                f"function call at {path} must have a non-empty call_id or id"
            )
        if call_id in call_nodes:
            raise SyntheticTraceFormatError(f"duplicate function call ID {call_id!r}")
        call_nodes[call_id] = node_id
        return ()

    if kind != "function_call_output":
        return ()

    call_id = _output_call_identifier(item)
    if call_id is None:
        raise SyntheticTraceFormatError(
            f"function call output at {path} must have a non-empty call_id"
        )
    if call_id not in call_nodes:
        raise SyntheticTraceFormatError(
            f"function call output at {path} has no preceding call {call_id!r}"
        )
    if call_id in output_call_ids:
        raise SyntheticTraceFormatError(f"duplicate function call output for call {call_id!r}")
    output_call_ids.add(call_id)
    return (call_nodes[call_id],)


def _frozen_request_nodes(
    request: JsonObject,
    *,
    oracle_tool_name: str,
) -> tuple[ContextNode, ...]:
    frozen_nodes: list[ContextNode] = []
    for key in sorted(request):
        value = request[key]
        if key == "input":
            continue
        if key == "tools" and isinstance(value, list):
            for index, tool in enumerate(value):
                protected = (
                    isinstance(tool, dict)
                    and tool.get("type") == "function"
                    and tool.get("name") == oracle_tool_name
                )
                frozen_nodes.append(
                    _frozen_node(
                        path=f"/tools/{index}",
                        kind="tool_schema",
                        value=tool,
                        protected=protected,
                    )
                )
            if not value:
                frozen_nodes.append(_frozen_node(path="/tools", kind="request_field", value=value))
            continue
        frozen_nodes.append(
            _frozen_node(
                path=f"/{_encode_pointer_token(key)}",
                kind="request_field",
                value=value,
            )
        )
    return tuple(frozen_nodes)


def _frozen_node(
    *,
    path: str,
    kind: str,
    value: JsonValue,
    protected: bool = False,
) -> ContextNode:
    digest = content_digest(value)
    return ContextNode(
        id=stable_context_id(path=path, kind=kind, digest=digest),
        kind=kind,
        path=path,
        mutable=False,
        protected=protected,
        source=_NODE_SOURCE,
        digest=digest,
    )


def _input_kind(item: JsonValue) -> str:
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type:
            return item_type
    return "message"


def _call_identifier(item: JsonValue) -> str | None:
    if not isinstance(item, dict):
        return None
    for field in ("call_id", "id"):
        value = item.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _output_call_identifier(item: JsonValue) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("call_id")
    return value if isinstance(value, str) and value else None


def _encode_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _collect_strings(value: JsonValue) -> set[str]:
    strings: set[str] = set()
    if isinstance(value, str):
        strings.add(value)
    elif isinstance(value, dict):
        for child in value.values():
            strings.update(_collect_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.update(_collect_strings(child))
    return strings


def _render_experiment(
    behavior: _FakeBehavior,
    nodes: tuple[ContextNode, ...],
) -> str:
    experiment = ExperimentDefinition(
        target=ShellActionTarget(tool_name_equals=behavior.tool_name),
        oracle=StructuralOracleConfig(
            protected_context_ids=tuple(sorted(node.id for node in nodes if node.protected))
        ),
        runner=FakeRunnerConfig(
            required_markers=behavior.required_markers,
            alternative_marker_sets=behavior.alternative_marker_sets,
            tool_name=behavior.tool_name,
        ),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )
    return experiment.to_toml()
