"""Section descriptors remain outside native execution and persisted sampling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from ithrl.context import content_digest
from ithrl.fuzz.deterministic import DeterministicMutationEvaluator
from ithrl.fuzz.sampling import fuzz_block_plan
from ithrl.fuzz.simplify import simplification_steps
from ithrl.mutation import MutationError, TextMutation
from ithrl.runner import PreparedAttempt
from ithrl.runner.codex.materialize import (
    CodexMaterializationError,
    CodexMaterializationErrorCode,
    materialize_codex_attempt,
)
from ithrl.runner.copilot.materialize import (
    CopilotMaterializationError,
    CopilotMaterializationErrorCode,
    materialize_copilot_attempt,
)
from ithrl.sampling import _collect_sampling_block_owned, collect_sampling_block
from ithrl.synthetic import extract_inference
from ithrl.text_sections import TextSectionRemoval
from ithrl.workflow import prepare_experiment


def _removal() -> TextMutation:
    return TextMutation(
        edits=(
            TextSectionRemoval(
                context_id="ctx-a",
                content_index=0,
                original_text_digest=content_digest("ordinary\n\nbackground"),
                policy_digest="a" * 64,
                section_ids=("section-" + "b" * 64,),
            ),
        )
    )


def test_native_materializers_refuse_sections_before_reading_evidence() -> None:
    # Even a stale/forged capability must not reach native source reconstruction.
    attempt = cast(PreparedAttempt, SimpleNamespace(candidate=SimpleNamespace(mutation=_removal())))
    with pytest.raises(CodexMaterializationError, match="only append-text") as codex:
        materialize_codex_attempt(attempt)
    assert codex.value.code is CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT
    with pytest.raises(CopilotMaterializationError, match="only append-text") as copilot:
        materialize_copilot_attempt(attempt)
    assert copilot.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT


def test_fuzz_simplification_refuses_sections_before_first_evaluation() -> None:
    with pytest.raises(MutationError, match="only append-text"):
        next(simplification_steps(_removal(), max_trials=3))


def test_deterministic_fuzz_refuses_sections_before_baseline(tmp_path: Path) -> None:
    evidence = extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    evaluator = DeterministicMutationEvaluator(evidence, prepare_experiment(evidence))
    with pytest.raises(MutationError, match="only append-text"):
        asyncio.run(evaluator.evaluate(_removal()))
    assert evaluator.attempts == 0


def test_fuzz_block_refuses_sections_before_campaign_preparation() -> None:
    # Required objects are deliberately absent: operation admission comes first.
    arguments: dict[str, Any] = {
        "evidence": None,
        "prepared": None,
        "campaign": None,
        "mutation": _removal(),
        "role": "discovery",
        "block_id": "discovery-1",
        "frozen_at": datetime.now(UTC),
        "schedule_seed": 1,
    }
    with pytest.raises(MutationError, match="only append-text"):
        fuzz_block_plan(**arguments)


@pytest.mark.parametrize("owned", [False, True])
def test_sampling_refuses_sections_before_lock_or_state_access(owned: bool) -> None:
    state = Mock()
    arguments: dict[str, Any] = {
        "evidence": None,
        "original": None,
        "candidate": None,
        "removed_context_ids": (),
        "plan": None,
        "state_file": state,
        "store": None,
        "mutation": _removal(),
    }
    collect = _collect_sampling_block_owned if owned else collect_sampling_block
    with pytest.raises(MutationError, match="does not yet support text-section removal"):
        asyncio.run(collect(**arguments))
    assert state.mock_calls == []
