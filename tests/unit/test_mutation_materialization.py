"""Pure request materialization and rejection before runner execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ithrl.adapters import enumerate_decisions
from ithrl.adapters.registry import parse_session_bytes
from ithrl.case import CaseEvidence, CaseFormatError
from ithrl.context import ContextGraph, JsonObject, content_digest
from ithrl.extraction import extract_decision
from ithrl.mutation import (
    MutationError,
    TextAppend,
    TextMutation,
    append_message_text,
    message_text,
)
from ithrl.runner import prepare_candidate
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment
from native_support.copilot import _source_records


def _evidence(tmp_path: Path) -> CaseEvidence:
    return extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()


def _mutation(
    evidence: CaseEvidence, *, path: str = "/input/0", text: str = "\n\nextra"
) -> TextMutation:
    node = next(node for node in evidence.context.nodes if node.path == path)
    request = evidence.request
    inputs = request["input"]
    assert isinstance(inputs, list)
    message = inputs[int(path.rsplit("/", 1)[1])]
    assert isinstance(message, dict)
    return TextMutation(
        edits=(
            TextAppend(
                context_id=node.id,
                content_index=0,
                original_text_digest=content_digest(message_text(message, 0)),
                text=text,
            ),
        )
    )


def test_exact_request_append_changes_only_selected_original_text(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    before, digest = evidence.request, evidence.digest
    mutation = _mutation(evidence)
    request = evidence.materialize(mutation=mutation)
    expected = evidence.request
    inputs = expected["input"]
    assert isinstance(inputs, list)
    assert isinstance(inputs[0], dict)
    inputs[0]["content"] = "A\n\nextra"

    assert request == expected
    assert evidence.request == before
    assert evidence.digest == digest
    assert evidence.materialize(mutation=TextMutation()) == before
    prepared = prepare_experiment(evidence)
    assert (
        prepare_candidate(evidence, prepared.execution_plan, mutation=mutation).mutation == mutation
    )


def test_request_trigger_and_protected_nodes_are_not_mutation_sites(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    sites = evidence.editable_text()
    assert [site.original_text for site in sites] == ["A", "B", "C", "D", "E", "F"]
    with pytest.raises(MutationError, match="current decision input"):
        evidence.materialize(mutation=_mutation(evidence, path="/input/6"))
    mutation = _mutation(evidence)
    node_id = mutation.edits[0].context_id
    for changes in ({"protected": True}, {"mutable": False}):
        nodes = tuple(
            node.model_copy(update=changes) if node.id == node_id else node
            for node in evidence.context.nodes
        )
        protected = CaseEvidence(
            manifest=evidence.manifest,
            request=evidence.request,
            context=ContextGraph(nodes=nodes),
            default_experiment=evidence.default_experiment,
        )
        with pytest.raises(MutationError, match="editable"):
            protected.materialize(mutation=mutation)
        assert node_id not in {site.context_id for site in protected.editable_text()}


@pytest.mark.parametrize("user_trigger", [False, True])
def test_native_text_discovery_is_pure_and_excludes_fixed_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_trigger: bool
) -> None:
    source = Path(__file__).parents[1] / "fixtures/codex-session/rollout.jsonl"
    records = [json.loads(line) for line in source.read_bytes().splitlines()]
    if user_trigger:
        records[8]["payload"] = {
            "type": "message",
            "id": "message-fixed-current-input",
            "role": "user",
            "content": [{"type": "input_text", "text": "Fixed current input."}],
        }
    session = parse_session_bytes("\n".join(json.dumps(record) for record in records).encode())
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    before = evidence.digest, tuple(artifact.content for artifact in evidence.source_artifacts)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("native evidence discovery must not invoke runner reconstruction")

    monkeypatch.setattr("ithrl.runner.codex.materialize.materialize_codex_attempt", forbidden)
    sites = evidence.editable_text()
    assert [site.original_text for site in sites] == ["Inspect the fixture context."]
    assert not evidence.has_request
    assert (evidence.digest, tuple(a.content for a in evidence.source_artifacts)) == before
    changed_context = ContextGraph(
        nodes=tuple(
            node.model_copy(update={"protected": True}) if node.id == sites[0].context_id else node
            for node in evidence.context.nodes
        )
    )
    changed = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=changed_context,
        default_experiment=evidence.default_experiment,
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    with pytest.raises(CaseFormatError, match="does not match preserved source"):
        changed.editable_text()


def test_unadvertised_operation_and_foreign_plan_fail_before_materialization(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    old_profile = prepared.execution_plan.capability.model_copy(
        update={"supported_context_operations": ("dependency-closed-removal",)}
    )
    unsupported = prepared.execution_plan.model_copy(update={"capability": old_profile})
    with pytest.raises(ValueError, match="does not support text mutation"):
        prepare_candidate(evidence, unsupported, mutation=_mutation(evidence))
    other = prepared.execution_plan.model_copy(update={"case_digest": "f" * 64})
    with pytest.raises(ValueError, match="does not belong"):
        prepare_candidate(evidence, other, mutation=_mutation(evidence))


@pytest.mark.parametrize(
    ("content", "transformed", "expected"),
    [
        ("Raw historical text.", None, ["Raw historical text."]),
        ("Raw historical text.", "Effective historical text.", ["Effective historical text."]),
        ("Raw historical text.", {"unsupported": True}, []),
        ([{"type": "text", "text": "Unsupported block."}], None, []),
    ],
)
def test_copilot_discovery_is_pure_and_matches_effective_prior_user_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: object,
    transformed: object,
    expected: list[str],
) -> None:
    records = _source_records("final-response", context_sensitive=True)
    records[1]["data"]["content"] = content
    if transformed is not None:
        records[1]["data"]["transformedContent"] = transformed
    # Keep a single prior-user site so the immediate trigger cannot be confused
    # with the last discovered editable field.
    del records[2]
    session = parse_session_bytes("\n".join(json.dumps(record) for record in records).encode())
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    before = evidence.digest, tuple(artifact.content for artifact in evidence.source_artifacts)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Copilot text discovery must not invoke runner reconstruction")

    monkeypatch.setattr("ithrl.runner.copilot.materialize.materialize_copilot_attempt", forbidden)
    sites = evidence.editable_text()
    assert [site.original_text for site in sites] == expected
    assert all(site.content_index == 0 for site in sites)
    assert all(site.context_id != evidence.context.nodes[-1].id for site in sites)
    assert (evidence.digest, tuple(a.content for a in evidence.source_artifacts)) == before
    changed = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=ContextGraph(nodes=evidence.context.nodes[:-1]),
        default_experiment=evidence.default_experiment,
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    with pytest.raises(CaseFormatError, match="does not match preserved source"):
        changed.editable_text()


def test_append_supports_only_input_text_and_preserves_other_blocks() -> None:
    message: JsonObject = {
        "type": "message",
        "role": "user",
        "id": "original-id",
        "content": [
            {"type": "input_text", "text": "original"},
            {"type": "input_image", "image_url": "fixture-image"},
        ],
        "opaque_metadata": {"keep": True},
    }
    edit = TextAppend(
        context_id="ctx-a",
        content_index=0,
        original_text_digest=content_digest("original"),
        text="\n\nnew",
    )
    result = append_message_text(message, edit)
    assert result != message
    assert message_text(result, 0) == "original\n\nnew"
    assert message_text(message, 0) == "original"
    assert result["id"] == message["id"]
    assert result["opaque_metadata"] == message["opaque_metadata"]
    new_content, old_content = result["content"], message["content"]
    assert isinstance(new_content, list)
    assert isinstance(old_content, list)
    assert new_content[1] == old_content[1]
    invalid_messages: tuple[JsonObject, ...] = (
        {**message, "role": "developer"},
        {**message, "role": "assistant"},
        {**message, "type": "function_call_output"},
        {"role": "user", "content": None},
    )
    for invalid in invalid_messages:
        with pytest.raises(MutationError):
            append_message_text(invalid, edit)
    with pytest.raises(MutationError):
        append_message_text(message, edit.model_copy(update={"content_index": 1}))
    with pytest.raises(MutationError, match="digest"):
        append_message_text(message, edit.model_copy(update={"original_text_digest": "a" * 64}))


def test_unknown_node_and_combined_removal_do_not_modify_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    mutation = _mutation(evidence)
    with pytest.raises(MutationError, match="unchanged original"):
        evidence.materialize((evidence.context.nodes[1].id,), mutation=mutation)
    foreign = TextMutation(edits=(mutation.edits[0].model_copy(update={"context_id": "foreign"}),))
    with pytest.raises(MutationError, match="unknown context"):
        evidence.materialize(mutation=foreign)
