"""Shared section edits preserve source, dependency closure and native envelopes."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.adapters import enumerate_decisions
from ithrl.adapters.registry import parse_session_bytes
from ithrl.case import CaseEvidence
from ithrl.context import ContextGraph, ContextNode, JsonValue, content_digest
from ithrl.experiment import StructuralOracleConfig, parse_experiment_toml
from ithrl.extraction import extract_decision
from ithrl.mutation import MutationError, TextMutation, replace_message_text
from ithrl.runner import Candidate, PreparedAttempt, prepare_candidate
from ithrl.synthetic import extract_inference
from ithrl.text_sections import TextSectionRemoval, remove_text_sections
from ithrl.workflow import prepare_experiment
from native_support.copilot import _source_records

_MIXED = "Keep the debugging constraint.\r\n\r\nArchived background: é🙂.\r\n"


def _evidence(tmp_path: Path, *, text: str = _MIXED) -> CaseEvidence:
    original = extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    request = original.request
    inputs = request["input"]
    assert isinstance(inputs, list)
    inputs[2] = {
        "role": "user",
        "id": "message-prior",
        "opaque": {"preserve": True},
        "content": [
            {"type": "input_text", "text": text},
            {"type": "input_image", "image_url": "fixture-image"},
        ],
    }
    return CaseEvidence(
        manifest=original.manifest,
        request=request,
        context=ContextGraph(
            nodes=tuple(
                node.model_copy(update={"digest": content_digest(inputs[2])})
                if node.path == "/input/2"
                else node
                for node in original.context.nodes
            )
        ),
        default_experiment=original.default_experiment,
    )


def _edit(evidence: CaseEvidence) -> TextSectionRemoval:
    catalog = next(
        c for c in evidence.text_sections() if c.original_utf8_bytes == len(_MIXED.encode())
    )
    return catalog.removal((catalog.sections[1].id,))


def test_cataloguing_many_fields_builds_the_context_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _evidence(tmp_path)
    request = original.request
    inputs: list[JsonValue] = [
        {"role": "user", "content": f"Keep instruction {index}.\n\nArchived background."}
        for index in range(200)
    ]
    inputs.append({"role": "user", "content": "Fixed current input."})
    request["input"] = inputs
    evidence = CaseEvidence(
        manifest=original.manifest,
        request=request,
        context=ContextGraph(
            nodes=tuple(
                ContextNode(
                    id=f"ctx-{index}",
                    kind="message",
                    path=f"/input/{index}",
                    mutable=True,
                    source="request.json",
                    digest=content_digest(message),
                )
                for index, message in enumerate(inputs)
            )
        ),
        default_experiment=original.default_experiment,
    )
    expected = evidence.text_sections()
    index_reads = 0
    by_id = ContextGraph.by_id.fget
    assert by_id is not None

    def counted_index(graph: ContextGraph) -> Mapping[str, ContextNode]:
        nonlocal index_reads
        index_reads += 1
        return by_id(graph)

    monkeypatch.setattr(ContextGraph, "by_id", property(counted_index))
    assert evidence.text_sections() == expected
    assert len(expected) == 200
    assert index_reads == 1
    index_reads = 0
    selected = expected[-1]
    assert (
        evidence.validate_text_sections(selected.removal((selected.sections[-1].id,))) == selected
    )
    assert index_reads == 1


def test_section_and_whole_record_removal_use_original_indices_and_preserve_envelopes(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    before, digest = evidence.request, evidence.digest
    edit = _edit(evidence)
    mutation = TextMutation(edits=(edit,))
    removed = (evidence.context.nodes[0].id,)
    actual = evidence.materialize(removed, mutation=mutation)
    expected = copy.deepcopy(before)
    inputs = expected["input"]
    assert isinstance(inputs, list)
    assert isinstance(inputs[2], dict)
    inputs[2] = replace_message_text(inputs[2], 0, "Keep the debugging constraint.\r\n\r\n")
    del inputs[0]
    assert actual == expected
    assert evidence.request == before
    assert evidence.digest == digest
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan, removed, mutation=mutation)
    assert candidate.removed_context_ids == removed
    assert Candidate.model_validate_json(candidate.model_dump_json()) == candidate
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="search",
        arm="candidate",
    )
    assert attempt.candidate == candidate
    data = candidate.model_dump()
    data["mutation"]["edits"][0]["section_ids"] = ("section-" + "f" * 64,)
    tampered = Candidate.model_validate(data)
    with pytest.raises(MutationError, match="known units"):
        PreparedAttempt(
            evidence=evidence,
            candidate=tampered,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="search",
            arm="candidate",
        )


def test_overlap_including_dependency_closure_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    edit = _edit(evidence)
    parent = evidence.context.by_id[edit.context_id]
    earlier = evidence.context.nodes[0]
    evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=ContextGraph(
            nodes=tuple(
                node.model_copy(update={"depends_on": (earlier.id,)})
                if node.id == parent.id
                else node
                for node in evidence.context.nodes
            )
        ),
        default_experiment=evidence.default_experiment,
    )
    # Rebind to the changed immutable graph before exercising removal closure.
    edit = _edit(evidence)
    mutation = TextMutation(edits=(edit,))
    prepared = prepare_experiment(evidence)
    for remove_ids in ((parent.id,), (earlier.id,)):
        with pytest.raises(MutationError, match="overlaps"):
            prepare_candidate(evidence, prepared.execution_plan, remove_ids, mutation=mutation)
        with pytest.raises(MutationError, match="overlaps"):
            evidence.materialize(remove_ids, mutation=mutation)
    with pytest.raises(ValidationError, match="retained context"):
        Candidate(
            case_digest=evidence.digest,
            execution_plan_digest=prepared.execution_plan.digest,
            retained_context_ids=(),
            removed_context_ids=(parent.id,),
            materialization="exact-request",
            mutation=mutation,
        )


@pytest.mark.parametrize("change", [{"protected": True}, {"mutable": False}])
def test_fixed_frozen_unknown_and_opaque_fields_are_not_catalogued(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    evidence = _evidence(tmp_path)
    edit = _edit(evidence)
    nodes = tuple(
        node.model_copy(update=change) if node.id == edit.context_id else node
        for node in evidence.context.nodes
    )
    protected = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=ContextGraph(nodes=nodes),
        default_experiment=evidence.default_experiment,
    )
    assert edit.context_id not in {c.context_id for c in protected.text_sections()}
    with pytest.raises(MutationError, match="editable prior-user"):
        protected.validate_text_sections(edit)
    for changes in (
        {"context_id": "unknown"},
        {"context_id": evidence.context.nodes[-1].id},
        {"content_index": 1},
    ):
        with pytest.raises(MutationError, match="editable prior-user"):
            evidence.validate_text_sections(edit.model_copy(update=changes))


def test_all_sections_removed_keeps_empty_text_field_and_other_blocks(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    catalog = evidence.validate_text_sections(_edit(evidence))
    edit = catalog.removal(tuple(s.id for s in catalog.sections))
    actual = evidence.materialize(mutation=TextMutation(edits=(edit,)))
    expected = evidence.request
    inputs = expected["input"]
    assert isinstance(inputs, list)
    assert isinstance(inputs[2], dict)
    inputs[2] = replace_message_text(inputs[2], 0, "")
    assert actual == expected


@pytest.mark.parametrize("descendant", [False, True])
@pytest.mark.parametrize("dependent", [False, True])
def test_combined_removal_protects_fixed_input_despite_permissive_graph_flags(
    tmp_path: Path, descendant: bool, dependent: bool
) -> None:
    original = _evidence(tmp_path)
    inputs: list[dict[str, Any]] = [
        {"role": "user", "content": "Earlier context."},
        {"role": "user", "content": _MIXED},
        {"role": "user", "content": "Fixed latest input."},
    ]
    nodes = tuple(
        ContextNode(
            id=name,
            kind="message",
            path=f"/input/{index}" + ("/content" if name == "fixed" and descendant else ""),
            mutable=True,
            protected=False,
            depends_on=("earlier",) if name == "fixed" and dependent else (),
            source="fixture",
            digest=content_digest(item["content"] if name == "fixed" and descendant else item),
        )
        for index, (name, item) in enumerate(
            zip(("earlier", "edited", "fixed"), inputs, strict=True)
        )
    )
    request: dict[str, Any] = {"input": inputs}
    evidence = CaseEvidence(
        manifest=original.manifest,
        request=request,
        context=ContextGraph(nodes=nodes),
        default_experiment=original.default_experiment,
    )
    before = evidence.request, evidence.digest
    mutation = TextMutation(edits=(_edit(evidence),))
    removed = ("earlier",) if dependent else ("fixed",)
    experiment = parse_experiment_toml(evidence.default_experiment).model_copy(
        update={"oracle": StructuralOracleConfig()}
    )
    prepared = prepare_experiment(evidence, experiment=experiment)
    # Coarse-only behavior remains governed by the supplied graph.
    evidence.materialize(removed)
    with pytest.raises(MutationError, match="current decision input"):
        evidence.materialize(removed, mutation=mutation)
    with pytest.raises(MutationError, match="current decision input"):
        prepare_candidate(evidence, prepared.execution_plan, removed, mutation=mutation)
    closed = evidence.context.dependency_closure(removed)
    forged = Candidate(
        case_digest=evidence.digest,
        execution_plan_digest=prepared.execution_plan.digest,
        retained_context_ids=tuple(n.id for n in nodes if n.id not in closed),
        removed_context_ids=tuple(n.id for n in nodes if n.id in closed),
        materialization="exact-request",
        mutation=mutation,
    )
    with pytest.raises(MutationError, match="current decision input"):
        PreparedAttempt(
            evidence=evidence,
            candidate=forged,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="search",
            arm="candidate",
        )
    assert (evidence.request, evidence.digest) == before


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_native_mixed_text_contract_preserves_sanitized_envelopes_without_runner_integration(
    tmp_path: Path, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if harness == "codex":
        source = Path(__file__).parents[1] / "fixtures/codex-session/rollout.jsonl"
        records = [json.loads(line) for line in source.read_bytes().splitlines()]
        envelope = records[3]["payload"]
        envelope["content"][0]["text"] = _MIXED
        envelope["opaque"] = {"keep": True}
        message = envelope
    else:
        records = _source_records("final-response", context_sensitive=True)
        envelope = records[1]["data"]
        envelope["content"] = "Opaque raw content stays unchanged."
        envelope["transformedContent"] = _MIXED
        envelope["opaque"] = {"keep": True}
        message = {"role": "user", "content": envelope["transformedContent"]}
    session = parse_session_bytes("\n".join(json.dumps(r) for r in records).encode())
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    before = (
        evidence.digest,
        tuple(a.content for a in evidence.source_artifacts),
        copy.deepcopy(records),
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the shared section contract must not invoke native materialization")

    monkeypatch.setattr(
        f"ithrl.runner.{harness}.materialize.materialize_{harness}_attempt", forbidden
    )
    catalog = next(
        c for c in evidence.text_sections() if c.original_utf8_bytes == len(_MIXED.encode())
    )
    edit = catalog.removal((catalog.sections[1].id,))
    assert evidence.validate_text_sections(edit) == catalog
    kept = remove_text_sections(_MIXED, catalog, edit)
    updated = replace_message_text(message, 0, kept)
    assert kept == "Keep the debugging constraint.\r\n\r\n"
    if harness == "codex":
        expected = copy.deepcopy(envelope)
        expected["content"][0]["text"] = kept
        assert updated == expected
    else:
        copied = copy.deepcopy(envelope)
        copied["transformedContent"] = updated["content"]
        expected = {**envelope, "transformedContent": kept}
        assert copied == expected
        assert copied["content"] == "Opaque raw content stays unchanged."
    assert (evidence.digest, tuple(a.content for a in evidence.source_artifacts), records) == before
