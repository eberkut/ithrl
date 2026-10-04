from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.context import (
    MAX_CONTEXT_NODES,
    MAX_DEPENDENCIES_PER_NODE,
    MAX_DEPENDENCY_DEPTH,
    ContextGraph,
    ContextGraphError,
    ContextNode,
    JsonObject,
    JsonScalar,
    MaterializationError,
    SourceRecordSelector,
    UnsafeRemovalError,
    content_digest,
    stable_context_id,
    validate_json_limits,
)


def _source_node(node_id: str, line: int) -> ContextNode:
    selector = SourceRecordSelector(line=line, identity_key="ordinal", identity_value=line - 1)
    return ContextNode(
        id=node_id,
        kind="message",
        selector=selector,
        mutable=True,
        source="source/session.jsonl",
        digest="0" * 64,
    )


def _request() -> dict[str, Any]:
    return {
        "model": "test-model",
        "input": [
            {"id": "A", "role": "user", "content": "irrelevant history"},
            {
                "id": "B",
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{}",
            },
            {"id": "C", "type": "function_call_output", "call_id": "B", "output": "ok"},
        ],
    }


def _node(
    request: dict[str, Any],
    node_id: str,
    index: int,
    *,
    depends_on: tuple[str, ...] = (),
    mutable: bool = True,
    protected: bool = False,
) -> ContextNode:
    value = request["input"][index]
    return ContextNode(
        id=node_id,
        kind=str(value.get("type", "message")),
        path=f"/input/{index}",
        mutable=mutable,
        protected=protected,
        depends_on=depends_on,
        source="source/inference.json",
        digest=content_digest(value),
    )


def _graph(request: dict[str, Any]) -> ContextGraph:
    return ContextGraph(
        nodes=(
            _node(request, "A", 0),
            _node(request, "B", 1),
            _node(request, "C", 2, depends_on=("B",)),
        )
    )


def test_dependency_closure_removes_dependants() -> None:
    request = _request()
    graph = _graph(request)

    assert graph.dependency_closure({"B"}) == frozenset({"B", "C"})
    assert graph.dependency_closure({"C"}) == frozenset({"C"})
    assert graph.materialize(request, {"B"})["input"] == [request["input"][0]]


def test_dependency_closure_refuses_unknown_node() -> None:
    graph = _graph(_request())

    with pytest.raises(ContextGraphError, match="unknown context node IDs: missing"):
        graph.dependency_closure({"missing"})


@pytest.mark.parametrize(
    ("mutable", "protected"),
    [(False, False), (True, True)],
)
def test_removal_refuses_frozen_or_protected_node(mutable: bool, protected: bool) -> None:
    request = _request()
    graph = ContextGraph(
        nodes=(
            _node(request, "B", 1),
            _node(
                request,
                "C",
                2,
                depends_on=("B",),
                mutable=mutable,
                protected=protected,
            ),
        )
    )

    with pytest.raises(UnsafeRemovalError, match="C"):
        graph.dependency_closure({"B"})


def test_graph_rejects_cycle() -> None:
    request = _request()

    with pytest.raises(ValidationError, match="dependency cycle"):
        ContextGraph(
            nodes=(
                _node(request, "B", 1, depends_on=("C",)),
                _node(request, "C", 2, depends_on=("B",)),
            )
        )


def test_graph_rejects_unknown_dependency() -> None:
    request = _request()

    with pytest.raises(ValidationError, match="unknown dependencies: missing"):
        ContextGraph(nodes=(_node(request, "B", 1, depends_on=("missing",)),))


def test_graph_rejects_duplicate_ids_and_pointers() -> None:
    request = _request()
    node = _node(request, "A", 0)

    with pytest.raises(ValidationError, match="duplicate context node ID"):
        ContextGraph(nodes=(node, node.model_copy(update={"path": "/input/1"})))
    with pytest.raises(ValidationError, match="duplicate context node path"):
        ContextGraph(nodes=(node, node.model_copy(update={"id": "other"})))


def test_graph_requires_ordered_unique_source_records() -> None:
    with pytest.raises(ValidationError, match="source record order"):
        ContextGraph(nodes=(_source_node("later", 2), _source_node("earlier", 1)))
    with pytest.raises(ValidationError, match="source record order"):
        ContextGraph(nodes=(_source_node("first", 1), _source_node("again", 1)))


def test_source_backed_removal_requires_a_harness_runner() -> None:
    graph = ContextGraph(nodes=(_source_node("source-item", 1),))

    with pytest.raises(MaterializationError, match="compatible harness runner"):
        graph.materialize({"input": []}, {"source-item"})


def test_graph_rejects_overlapping_node_paths() -> None:
    request = _request()
    parent = _node(request, "A", 0)
    child = ContextNode(
        id="A-content",
        kind="content",
        path="/input/0/content",
        mutable=True,
        source="source/inference.json",
        digest=content_digest("irrelevant history"),
    )

    with pytest.raises(ValidationError, match="context node paths overlap"):
        ContextGraph(nodes=(child, parent))


def test_graph_reports_overlapping_paths_before_unknown_dependencies() -> None:
    request = _request()
    parent = _node(request, "A", 0)
    child = ContextNode(
        id="A-content",
        kind="content",
        path="/input/0/content",
        mutable=True,
        depends_on=("missing",),
        source="source/inference.json",
        digest=content_digest("irrelevant history"),
    )

    with pytest.raises(ValidationError, match="context node paths overlap"):
        ContextGraph(nodes=(parent, child))


@pytest.mark.parametrize("path", ["input/0", "/input/~2", "/input/~"])
def test_node_rejects_invalid_json_pointer(path: str) -> None:
    with pytest.raises(ValidationError, match="invalid RFC 6901"):
        ContextNode(
            id="A",
            kind="message",
            path=path,
            mutable=True,
            source="source/inference.json",
            digest="0" * 64,
        )


def test_node_requires_request_path_or_source_selector() -> None:
    with pytest.raises(ValidationError, match="request path or source selector"):
        ContextNode(
            id="A",
            kind="message",
            mutable=True,
            source="source/session.jsonl",
            digest="0" * 64,
        )


def test_node_cannot_mix_request_path_and_source_selector() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ContextNode(
            id="A",
            kind="message",
            path="/input/0",
            selector=SourceRecordSelector(
                line=1,
                identity_key="ordinal",
                identity_value=0,
            ),
            mutable=True,
            source="source/session.jsonl",
            digest="0" * 64,
        )


@pytest.mark.parametrize("source", ["../trace.json", "source/../trace.json", "/trace.json", "a//b"])
def test_node_rejects_unsafe_source(source: str) -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        ContextNode(
            id="A",
            kind="message",
            path="/input/0",
            mutable=True,
            source=source,
            digest="0" * 64,
        )


def test_unchanged_materialization_is_equal_but_independent() -> None:
    request = _request()
    result = _graph(request).materialize(request)

    assert result == request
    assert result is not request
    assert result["input"] is not request["input"]


def test_materialization_uses_original_list_indices() -> None:
    request = _request()
    graph = _graph(request)

    result = graph.materialize(request, {"A", "C"})

    result_input = result["input"]
    assert isinstance(result_input, list)
    assert [item["id"] for item in result_input if isinstance(item, dict)] == ["B"]


@pytest.mark.parametrize("retained", [None, False, True, 0, 1, 1.5, ""])
def test_materialization_preserves_json_scalars_when_removing_siblings(
    retained: JsonScalar,
) -> None:
    request: JsonObject = {
        "object": {"remove": "discard", "keep": retained},
        "array": ["discard", retained],
    }
    graph = ContextGraph(
        nodes=tuple(
            ContextNode(
                id=node_id,
                kind="field",
                path=path,
                mutable=True,
                source="request.json",
                digest=content_digest("discard"),
            )
            for node_id, path in (("field", "/object/remove"), ("item", "/array/0"))
        )
    )

    assert graph.materialize(request, {"field", "item"}) == {
        "object": {"keep": retained},
        "array": [retained],
    }
    assert request == {
        "object": {"remove": "discard", "keep": retained},
        "array": ["discard", retained],
    }


@pytest.mark.parametrize("remove", [False, True])
def test_materialization_keeps_nested_retained_data_independent(remove: bool) -> None:
    shared = {"values": [{"name": "original"}]}
    request: dict[str, Any] = {
        "input": [{"remove": "discard", "keep": shared}, {"content": [shared]}],
        "metadata": shared,
    }
    graph = ContextGraph(
        nodes=(
            ContextNode(
                id="discard",
                kind="field",
                path="/input/0/remove",
                mutable=True,
                source="request.json",
                digest=content_digest("discard"),
            ),
        )
    )

    result: Any = graph.materialize(request, {"discard"} if remove else ())

    assert list(result) == list(request)
    assert list(result["input"][0]) == (["keep"] if remove else ["remove", "keep"])
    assert request["input"][0]["remove"] == "discard"
    result["input"][0]["keep"]["values"][0]["name"] = "candidate"
    assert shared == {"values": [{"name": "original"}]}
    shared["values"].append({"name": "source"})
    assert len(result["input"][0]["keep"]["values"]) == 1
    assert len(result["input"][1]["content"][0]["values"]) == 1
    assert len(result["metadata"]["values"]) == 1


def test_materialization_supports_escaped_object_keys() -> None:
    request: dict[str, Any] = {"a/b": {"~key": "remove", "keep": True}}
    node = ContextNode(
        id="escaped",
        kind="field",
        path="/a~1b/~0key",
        mutable=True,
        source="source/inference.json",
        digest=content_digest("remove"),
    )

    assert ContextGraph(nodes=(node,)).materialize(request, {"escaped"}) == {"a/b": {"keep": True}}


def test_materialization_rejects_stale_pointer_or_digest() -> None:
    request = _request()
    graph = _graph(request)
    changed = _request()
    changed["input"][1]["name"] = "other"

    with pytest.raises(MaterializationError, match="digest does not match"):
        graph.materialize(changed, {"B"})
    with pytest.raises(MaterializationError, match="does not exist"):
        graph.materialize({"model": "test-model", "input": []}, {"B"})


def test_content_digest_and_stable_id_are_deterministic() -> None:
    first = {"b": [2, 3], "a": 1}
    second = {"a": 1, "b": [2, 3]}

    assert content_digest(first) == content_digest(second)
    digest = content_digest(first)
    assert stable_context_id(path="/input/0", kind="message", digest=digest) == stable_context_id(
        path="/input/0", kind="message", digest=digest
    )


def test_context_graph_resource_limits_are_enforced() -> None:
    def node(index: int, *, depends_on: tuple[str, ...] = ()) -> ContextNode:
        return ContextNode(
            id=f"ctx-{index}",
            kind="message",
            path=f"/input/{index}",
            mutable=True,
            depends_on=depends_on,
            source="request.json",
            digest="0" * 64,
        )

    with pytest.raises(ValidationError, match="at most"):
        ContextGraph(nodes=tuple(node(index) for index in range(MAX_CONTEXT_NODES + 1)))

    with pytest.raises(ValidationError, match="at most"):
        node(0, depends_on=tuple(f"ctx-{index}" for index in range(MAX_DEPENDENCIES_PER_NODE + 1)))

    chain = tuple(
        node(index, depends_on=(f"ctx-{index - 1}",) if index else ())
        for index in range(MAX_DEPENDENCY_DEPTH + 1)
    )
    with pytest.raises(ValidationError, match="dependency depth exceeds"):
        ContextGraph(nodes=chain)


def test_json_nesting_limit_is_enforced() -> None:
    value: list[object] = []
    current = value
    for _ in range(130):
        child: list[object] = []
        current.append(child)
        current = child

    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        validate_json_limits(value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"items": (1, 2)}, "unsupported JSON value type: tuple"),
        ({1: "value"}, "JSON objects must use string keys"),
        ({"value": float("nan")}, "JSON numbers must be finite"),
    ],
)
def test_json_validation_rejects_non_json_python_values(
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_json_limits(value)
