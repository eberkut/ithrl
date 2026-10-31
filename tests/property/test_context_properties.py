from __future__ import annotations

import copy

from hypothesis import given
from hypothesis import strategies as st

from ithrl.context import ContextGraph, ContextNode, JsonObject, JsonValue, content_digest


@given(
    markers=st.lists(
        st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1),
        min_size=1,
        max_size=20,
        unique=True,
    ),
    requested_indexes=st.sets(st.integers(min_value=0, max_value=19)),
    dependency_indexes=st.lists(
        st.sets(st.integers(min_value=0, max_value=19), max_size=3),
        min_size=20,
        max_size=20,
    ),
)
def test_dependency_closure_materialization_never_leaves_an_orphan(
    markers: list[str], requested_indexes: set[int], dependency_indexes: list[set[int]]
) -> None:
    items: list[JsonValue] = [{"marker": marker} for marker in markers]
    request: JsonObject = {"input": items}
    nodes = tuple(
        ContextNode(
            id=f"ctx-{index}",
            kind="message",
            path=f"/input/{index}",
            mutable=True,
            depends_on=tuple(
                f"ctx-{parent}" for parent in sorted(dependency_indexes[index]) if parent < index
            ),
            source="request.json",
            digest=content_digest(item),
        )
        for index, item in enumerate(items)
    )
    graph = ContextGraph(nodes=nodes)
    requested = {f"ctx-{index}" for index in requested_indexes if index < len(nodes)}
    original = copy.deepcopy(request)

    closure = graph.dependency_closure(requested)
    materialized = graph.materialize(request, closure)

    assert request == original
    retained = {node.id for node in nodes} - closure
    assert all(set(node.depends_on) <= retained for node in nodes if node.id in retained)
    assert materialized["input"] == [
        item for node, item in zip(nodes, items, strict=True) if node.id in retained
    ]


@given(value=st.dictionaries(st.text(min_size=1), st.integers(), max_size=20))
def test_content_digest_is_independent_of_mapping_insertion_order(value: dict[str, int]) -> None:
    assert content_digest(value) == content_digest(dict(reversed(tuple(value.items()))))
