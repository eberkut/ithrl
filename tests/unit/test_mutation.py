"""Mutation identities, canonical no-ops and bounded private payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from ithrl.context import content_digest
from ithrl.mutation import MAX_MUTATION_TEXT_BYTES, TextAppend, TextMutation
from ithrl.runner import Candidate, PreparedAttempt, prepare_candidate
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment


def _append(**changes: Any) -> TextAppend:
    return TextAppend.model_validate(
        {
            "context_id": "ctx-a",
            "content_index": 0,
            "original_text_digest": content_digest("original text"),
            "text": "\n\nSynthetic archived note.",
            **changes,
        }
    )


def _candidate(**changes: Any) -> Candidate:
    return Candidate.model_validate(
        {
            "case_digest": "a" * 64,
            "execution_plan_digest": "b" * 64,
            "source_artifact_digests": ("c" * 64,),
            "retained_context_ids": ("ctx-a", "ctx-b"),
            "removed_context_ids": (),
            "materialization": "source-backed",
            **changes,
        }
    )


def test_empty_append_normalizes_to_original_candidate_identity() -> None:
    baseline = _candidate()
    noop = TextMutation(edits=(_append(text=""),))

    assert noop.is_empty
    assert noop == TextMutation()
    assert noop.digest == TextMutation().digest
    assert _candidate(mutation=noop).digest == baseline.digest
    assert TextMutation.model_validate_json(noop.model_dump_json()) == noop


@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=1000))
def test_round_trip_preserves_exact_unicode_and_candidate_binding(text: str) -> None:
    mutation = TextMutation(edits=(_append(text=text),))
    candidate = _candidate(mutation=mutation)

    assert TextMutation.model_validate_json(mutation.model_dump_json()) == mutation
    assert Candidate.model_validate_json(candidate.model_dump_json()) == candidate
    assert (candidate.digest != _candidate().digest) == bool(text)
    if text:
        assert mutation.edits[0].text == text


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "different text"},
        {"text": "\r\nSynthetic archived note."},
        {"content_index": 1},
        {"context_id": "ctx-b"},
        {"original_text_digest": "d" * 64},
    ],
)
def test_edit_payload_and_original_selector_change_candidate_identity(
    changes: dict[str, Any],
) -> None:
    original = _candidate(mutation=TextMutation(edits=(_append(),)))
    changed = _candidate(mutation=TextMutation(edits=(_append(**changes),)))

    assert original.digest != changed.digest


def test_original_source_and_execution_identity_remain_in_mutant_digest() -> None:
    mutation = TextMutation(edits=(_append(),))
    original = _candidate(mutation=mutation)
    for changes in (
        {"case_digest": "d" * 64},
        {"execution_plan_digest": "e" * 64},
        {"source_artifact_digests": ("f" * 64,)},
    ):
        assert _candidate(mutation=mutation, **changes).digest != original.digest


def test_mutation_does_not_allow_a_reduced_baseline_or_unknown_node() -> None:
    mutation = TextMutation(edits=(_append(),))
    with pytest.raises(ValidationError, match="unchanged original"):
        _candidate(
            mutation=mutation, retained_context_ids=("ctx-a",), removed_context_ids=("ctx-b",)
        )
    with pytest.raises(ValidationError, match="retained context"):
        _candidate(mutation=TextMutation(edits=(_append(context_id="ctx-unknown"),)))


def test_payload_limit_counts_utf8_bytes_and_allows_exact_boundary() -> None:
    boundary = "é" * (MAX_MUTATION_TEXT_BYTES // 2)
    assert _append(text=boundary).text == boundary
    with pytest.raises(ValidationError, match="UTF-8 byte limit"):
        _append(text=boundary + "é")
    with pytest.raises(ValidationError, match="unicode"):
        _append(text="\ud800")


@pytest.mark.parametrize(
    "changes",
    [
        {"context_id": ""},
        {"content_index": -1},
        {"content_index": True},
        {"original_text_digest": "not-a-digest"},
        {"operation": "replace-text"},
        {"text": ["text"]},
        {"undeclared": "value"},
    ],
)
def test_unknown_operations_and_malformed_edits_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _append(**changes)
    with pytest.raises(ValidationError):
        TextMutation(edits=(_append(), _append()))


def test_private_mutation_content_stays_out_of_reprs_and_validation_messages() -> None:
    secret = "synthetic-private-payload"
    mutation = TextMutation(edits=(_append(text=secret),))
    assert secret not in repr(mutation)
    assert secret not in repr(_candidate(mutation=mutation))
    for constructor in (
        lambda: _append(text=secret, content_index=-1),
        lambda: TextMutation.model_validate(
            {"edits": [{**_append(text=secret).model_dump(), "bad": 1}]}
        ),
        lambda: _candidate(mutation=mutation, retained_context_ids=()),
    ):
        with pytest.raises(ValidationError) as caught:
            constructor()
        assert secret not in str(caught.value)


def test_wrong_original_text_cannot_bypass_runner_preparation(tmp_path: Path) -> None:
    evidence = extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    prepared = prepare_experiment(evidence)
    original = prepare_candidate(evidence, prepared.execution_plan)
    node = next(node for node in evidence.context.nodes if node.mutable and not node.protected)
    data = original.model_dump()
    data["mutation"] = TextMutation(edits=(_append(context_id=node.id),))
    mutant = Candidate.model_validate(data)

    with pytest.raises(ValueError, match="original text digest"):
        PreparedAttempt(
            evidence=evidence,
            candidate=mutant,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="search",
            arm="candidate",
        )


@pytest.mark.parametrize(("field", "value"), [("text", "replacement"), ("edits", ())])
def test_contracts_are_immutable_and_cannot_alias_mutation_payloads(field: str, value: Any) -> None:
    contract = _append() if field == "text" else TextMutation(edits=(_append(),))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(contract, field, value)
