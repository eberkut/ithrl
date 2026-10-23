"""A real fake-runner workflow, without native harnesses or provider access."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ithrl.context import content_digest
from ithrl.fuzz.deterministic import DeterministicMutationEvaluator
from ithrl.fuzz.simplify import MutationDecision, simplify_mutation
from ithrl.mutation import TextAppend, TextMutation
from ithrl.runner import FakeRunner, RunnerRegistry
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment


def test_deduplication_serializes_concurrent_requests_and_control_stays_unchanged(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        evidence = extract_inference(
            Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
            inference_call_id="synthetic-ac-001",
            case_path=tmp_path / "case",
        ).load()
        prepared = prepare_experiment(evidence)
        evaluator = DeterministicMutationEvaluator(evidence, prepared)
        site = evidence.editable_text()[0]
        mutation = TextMutation(
            edits=(
                TextAppend(
                    context_id=site.context_id,
                    content_index=0,
                    original_text_digest=content_digest(site.original_text),
                    text="\n\nsynthetic",
                ),
            )
        )
        results = await asyncio.gather(*(evaluator.evaluate(mutation) for _ in range(8)))
        assert results == [MutationDecision.CHANGED] * 8
        assert evaluator.attempts == 2
        control_site = evidence.editable_text()[1]
        control = TextMutation(
            edits=(
                TextAppend(
                    context_id=control_site.context_id,
                    content_index=0,
                    original_text_digest=content_digest(control_site.original_text),
                    text="\n\nsynthetic",
                ),
            )
        )
        assert await evaluator.evaluate(control) is MutationDecision.UNCHANGED
        assert evaluator.attempts == 3
        assert prepared.runner.capability_profile.provider_calls_per_attempt == 0
        assert prepared.safety_policy.provider_transport == "forbidden"

    asyncio.run(run())


def test_missing_baseline_target_and_callback_errors_cannot_become_findings(tmp_path: Path) -> None:
    async def run() -> None:
        evidence = extract_inference(
            Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
            inference_call_id="synthetic-ac-001",
            case_path=tmp_path / "case",
        ).load()
        missing = FakeRunner(
            required_markers=frozenset({"MISSING"}), tool_name="exec_command", command="true"
        )
        prepared = prepare_experiment(
            evidence, registry=RunnerRegistry({"fake": lambda _c, _e: missing})
        )
        evaluator = DeterministicMutationEvaluator(evidence, prepared)
        with pytest.raises(ValueError, match="unchanged original"):
            await evaluator.evaluate(TextMutation())

        async def failed(_mutation: TextMutation) -> MutationDecision:
            raise RuntimeError("synthetic callback failure")

        site = evidence.editable_text()[0]
        mutation = TextMutation(
            edits=(
                TextAppend(
                    context_id=site.context_id,
                    content_index=0,
                    original_text_digest=content_digest(site.original_text),
                    text="append",
                ),
            )
        )
        with pytest.raises(RuntimeError, match="synthetic callback failure"):
            await simplify_mutation(mutation, failed, max_trials=10)

    asyncio.run(run())
