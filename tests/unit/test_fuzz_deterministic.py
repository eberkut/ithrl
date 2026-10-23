"""Credential-free discovery, controls and mutation-only simplification."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, override

import pytest

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence
from ithrl.context import content_digest
from ithrl.experiment import ExperimentDefinition, FakeRunnerConfig
from ithrl.fuzz import FuzzCampaignConfig
from ithrl.fuzz.deterministic import DeterministicMutationEvaluator
from ithrl.fuzz.generation import generate_mutations
from ithrl.fuzz.simplify import MutationDecision, simplify_mutation
from ithrl.mutation import EditableText, MutationError, TextAppend, TextMutation
from ithrl.runner import (
    Candidate,
    FakeRunner,
    PreparedAttempt,
    RunAccounting,
    RunnerRegistry,
    RunResult,
    prepare_candidate,
)
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment


def _mutation(text: str) -> TextMutation:
    return TextMutation(
        edits=(
            TextAppend(
                context_id="ctx-a",
                content_index=0,
                original_text_digest=content_digest("original"),
                text=text,
            ),
        )
    )


def _config(original: Candidate, **changes: Any) -> FuzzCampaignConfig:
    return FuzzCampaignConfig.model_validate(
        {
            "original": original,
            "seed": 3,
            "mutators": ("append-stale-text",),
            "max_candidates": 4,
            "max_simplification_trials": 10,
            "calibration": {"required_valid": 3, "minimum_matches": 2, "max_attempts": 7},
            "screening": {
                "original": {"required_valid": 8, "max_attempts": 18},
                "mutant": {"required_valid": 8, "max_attempts": 18},
                "minimum_decrease": 0.4,
            },
            "confirmation": {
                "original": {"required_valid": 20, "max_attempts": 36},
                "mutant": {"required_valid": 20, "max_attempts": 36},
                "alpha": 0.05,
            },
            "minimum_decrease": 0.1,
            "eligibility_policy": "independent-safe-preaccept-only",
            "freshness_seconds": 86400,
            **changes,
        }
    )


def _evidence(tmp_path: Path) -> CaseEvidence:
    return extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()


def test_fake_runner_discovers_and_simplifies_a_change_with_source_unchanged(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        evidence = _evidence(tmp_path)
        before, digest = evidence.request, evidence.digest
        prepared = prepare_experiment(evidence)
        original = prepare_candidate(evidence, prepared.execution_plan)
        evaluator = DeterministicMutationEvaluator(evidence, prepared)
        batch = generate_mutations(
            evidence.editable_text()[:4],
            _config(original),
            stale_text="noise\n\nstale\n\nother\n\nmore",
        )
        assert batch.proposals_examined == 4
        assert (
            generate_mutations(
                evidence.editable_text()[:4],
                _config(original),
                stale_text="noise\n\nstale\n\nother\n\nmore",
            )
            == batch
        )
        selected = None
        for mutation in batch.mutations:
            if await evaluator.evaluate(mutation) is MutationDecision.CHANGED:
                selected = mutation
                break
        assert selected is not None
        result = await simplify_mutation(selected, evaluator.evaluate, max_trials=10)
        assert result.complete
        assert len(result.mutation.edits[0].text) < len(selected.edits[0].text) / 2
        assert await evaluator.evaluate(result.mutation) is MutationDecision.CHANGED
        replay = DeterministicMutationEvaluator(evidence, prepared)
        assert await replay.evaluate(result.mutation) is MutationDecision.CHANGED
        assert replay.attempts == 2
        attempts = evaluator.attempts
        await evaluator.evaluate(result.mutation)
        assert evaluator.attempts == attempts
        assert await evaluator.evaluate(TextMutation()) is MutationDecision.UNCHANGED
        assert evidence.request == before
        assert evidence.digest == digest

    asyncio.run(run())


def test_identical_shrink_proposals_reuse_decisions_and_invalid_trials_are_not_accepted() -> None:
    async def run() -> None:
        initial = _mutation("\n\nONE\n\nONE")
        calls: list[str] = []

        async def evaluate(mutation: TextMutation) -> MutationDecision:
            assert mutation.digest not in calls
            calls.append(mutation.digest)
            return MutationDecision.CHANGED if mutation == initial else MutationDecision.INVALID

        result = await simplify_mutation(initial, evaluate, max_trials=10)
        assert result.complete
        assert result.mutation == initial
        assert result.proposals == 2
        assert result.evaluations == 1
        assert len(calls) == 2

    asyncio.run(run())


def test_generation_caps_work_and_deduplicates_equivalent_edits(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    original = prepare_candidate(evidence, prepare_experiment(evidence).execution_plan)
    site = evidence.editable_text()[0]
    config = _config(original, mutators=("duplicate-text", "append-stale-text"), max_candidates=3)
    batch = generate_mutations((site, site, site), config, stale_text=site.original_text)
    assert batch.proposals_examined == 3
    assert len(batch.mutations) == 1
    empty = EditableText(site.context_id, 0, "")
    assert generate_mutations((empty,), _config(original), stale_text="").mutations == ()
    oversized = generate_mutations((site,), _config(original), stale_text="x" * (64 * 1024))
    assert oversized.proposals_examined == 1
    assert not oversized.mutations
    with pytest.raises(MutationError, match="original candidate"):
        generate_mutations((EditableText("foreign", 0, "text"),), config, stale_text="stale")


@pytest.mark.parametrize("required", [("ONE",), ("ONE", "TWO")])
@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n"])
def test_simplification_keeps_joint_effect_and_never_redraws_candidate(
    required: tuple[str, ...],
    separator: str,
) -> None:
    async def run() -> None:
        visited: list[str] = []

        async def evaluate(mutation: TextMutation) -> MutationDecision:
            assert mutation.digest not in visited
            visited.append(mutation.digest)
            text = mutation.edits[0].text
            return (
                MutationDecision.CHANGED
                if all(token in text for token in required)
                else MutationDecision.UNCHANGED
            )

        initial = _mutation(separator + separator.join(("noise", "ONE", "TWO", "other")))
        result = await simplify_mutation(initial, evaluate, max_trials=10)
        assert result.complete
        assert result.mutation.edits[0].text == "".join(separator + token for token in required)
        assert result.evaluations <= 10
        assert len(visited) == result.evaluations + 1

    asyncio.run(run())


def test_invalid_first_mutation_is_refused_without_running_the_baseline(tmp_path: Path) -> None:
    async def run() -> None:
        evidence = _evidence(tmp_path)
        prepared = prepare_experiment(evidence)
        evaluator = DeterministicMutationEvaluator(evidence, prepared)
        site = evidence.editable_text()[0]
        valid = TextAppend(
            context_id=site.context_id,
            content_index=site.content_index,
            original_text_digest=content_digest(site.original_text),
            text="append",
        )
        for changes in ({"context_id": "unknown"}, {"original_text_digest": "f" * 64}):
            invalid = TextMutation(edits=(valid.model_copy(update=changes),))
            assert await evaluator.evaluate(invalid) is MutationDecision.INVALID
            assert evaluator.attempts == 0
        capability = prepared.execution_plan.capability.model_copy(
            update={"supported_context_operations": ("dependency-closed-removal",)}
        )
        unsupported = DeterministicMutationEvaluator(
            evidence,
            replace(
                prepared,
                execution_plan=prepared.execution_plan.model_copy(
                    update={"capability": capability}
                ),
            ),
        )
        with pytest.raises(ValueError, match="does not support text mutation"):
            await unsupported.evaluate(TextMutation(edits=(valid,)))
        assert unsupported.attempts == 0

    asyncio.run(run())


def test_simplification_refuses_invalid_start_and_retains_best_at_work_limit() -> None:
    async def run() -> None:
        async def invalid(_mutation: TextMutation) -> MutationDecision:
            return MutationDecision.INVALID

        with pytest.raises(ValueError, match="observed changed"):
            await simplify_mutation(_mutation("ONE"), invalid, max_trials=10)

        async def changed(_mutation: TextMutation) -> MutationDecision:
            return MutationDecision.CHANGED

        initial = _mutation("\n\nONE\n\nTWO\n\nTHREE")
        result = await simplify_mutation(initial, changed, max_trials=1)
        assert not result.complete
        assert result.proposals == 1
        assert result.mutation.edits[0].text == "\n\nTWO\n\nTHREE"
        with pytest.raises(ValueError, match="finite positive"):
            await simplify_mutation(initial, changed, max_trials=True)

    asyncio.run(run())


class _InteractionRunner(FakeRunner):
    """Only the private test payload's ordered tokens cause a final-answer flip."""

    def __init__(self, *, failing: bool = False) -> None:
        super().__init__(required_markers=frozenset({"A"}), final_text="BLUE")
        self.failing = failing

    @property
    @override
    def fingerprint(self) -> str:
        return f"fixture-ordered-interaction:{self.failing}:{super().fingerprint}"

    @override
    async def run(self, attempt: PreparedAttempt) -> RunResult:
        if self.failing and not attempt.candidate.mutation.is_empty:
            raise RuntimeError("synthetic runner failure")
        request = attempt.evidence.materialize(mutation=attempt.candidate.mutation)
        text = str(request["input"])
        changed = "ONE" in text and "TWO" in text and text.index("ONE") < text.index("TWO")
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            final_text="RED" if changed else "BLUE",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider="fake",
            accounting=RunAccounting(),
        )


def test_ordering_interaction_runner_end_to_end_and_errors_do_not_count_as_changes(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        evidence = _evidence(tmp_path)
        experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
        experiment = ExperimentDefinition.model_validate(
            {
                **experiment.model_dump(),
                "target": FinalResponseTarget(text_equals="BLUE"),
                "runner": FakeRunnerConfig(required_markers=("A",), final_text="BLUE"),
            }
        )
        runner = _InteractionRunner()
        prepared = prepare_experiment(
            evidence,
            experiment=experiment,
            registry=RunnerRegistry({"fake": lambda _c, _e: runner}),
        )
        evaluator = DeterministicMutationEvaluator(evidence, prepared)
        site = evidence.editable_text()[0]
        initial = TextMutation(
            edits=(
                TextAppend(
                    context_id=site.context_id,
                    content_index=0,
                    original_text_digest=content_digest(site.original_text),
                    text="\n\nnoise\n\nONE\n\nTWO\n\nother",
                ),
            )
        )
        result = await simplify_mutation(initial, evaluator.evaluate, max_trials=10)
        assert result.complete
        assert result.mutation.edits[0].text == "\n\nONE\n\nTWO"
        reverse = TextMutation(
            edits=(initial.edits[0].model_copy(update={"text": "\n\nTWO\n\nONE"}),)
        )
        assert await evaluator.evaluate(reverse) is MutationDecision.UNCHANGED
        failing = _InteractionRunner(failing=True)
        failed_prepared = prepare_experiment(
            evidence,
            experiment=experiment,
            registry=RunnerRegistry({"fake": lambda _c, _e: failing}),
        )
        failed = DeterministicMutationEvaluator(evidence, failed_prepared)
        with pytest.raises(RuntimeError, match="synthetic runner failure"):
            await failed.evaluate(initial)
        invalid = TextMutation(
            edits=(initial.edits[0].model_copy(update={"original_text_digest": "f" * 64}),)
        )
        assert await evaluator.evaluate(invalid) is MutationDecision.INVALID

    asyncio.run(run())
