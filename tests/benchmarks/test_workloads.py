"""Bounded, sanitized workloads; fixture construction is outside measured calls.

The sampled-state and fuzz configuration helpers deliberately reuse the unit suite's
validated contract builders. No saved native session or provider is accessed.
"""

from __future__ import annotations

import asyncio
import copy
import json
import tracemalloc
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ithrl.adapters import enumerate_decisions
from ithrl.adapters.copilot.reacquisition import project_reacquisition
from ithrl.adapters.registry import parse_session_bytes
from ithrl.case import CaseEvidence
from ithrl.context import ContextGraph, ContextNode, canonical_json_bytes, content_digest
from ithrl.extraction import extract_decision
from ithrl.fuzz.generation import generate_mutations
from ithrl.mutation import EditableText
from ithrl.observations import Observation
from ithrl.reacquisition import compare_reacquisition
from ithrl.reduce import SampledCandidateTrial, SampledComparison, SampledReductionCheckpoint
from ithrl.reduce.contracts import trial_key
from ithrl.reduce.search import _Search
from ithrl.run_state import SampledRunState, SamplingBlockProgress, SamplingObservationReference
from ithrl.runner import prepare_candidate
from ithrl.workflow import prepare_experiment
from native_support.copilot import _source_records
from unit.test_copilot_reacquisition import _document, _records
from unit.test_fuzz_deterministic import _config, _evidence
from unit.test_sampled_run_state import (
    _TIME,
    _approval,
    _create,
    _observation,
    _plan,
    _reservation,
)

pytestmark = pytest.mark.benchmark
_FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize("size", [64, 512, 2048], ids=["small", "medium", "large"])
def test_repeated_sparse_closure(benchmark: Any, size: int) -> None:
    # Independent pairs keep the closure sparse even at the largest admitted size.
    graph = ContextGraph(
        nodes=tuple(
            ContextNode(
                id=f"node-{index}",
                kind="message",
                path=f"/input/{index}",
                mutable=True,
                depends_on=(f"node-{index - 1}",) if index % 2 else (),
                source="source/request.json",
                digest="0" * 64,
            )
            for index in range(size)
        )
    )

    def repeated() -> list[frozenset[str]]:
        return [graph.dependency_closure({f"node-{index}"}) for index in range(0, 64, 2)]

    result = benchmark.pedantic(repeated, rounds=20, iterations=5, warmup_rounds=2)
    assert result[0] == frozenset({"node-0", "node-1"})
    assert len(result) == 32


def test_cold_graph_construction_and_closure(benchmark: Any) -> None:
    nodes = tuple(
        ContextNode(
            id=f"node-{index}",
            kind="message",
            path=f"/input/{index}",
            mutable=True,
            depends_on=(f"node-{index - 1}",) if index % 2 else (),
            source="source/request.json",
            digest="0" * 64,
        )
        for index in range(2048)
    )

    def construct() -> ContextGraph:
        graph = ContextGraph(nodes=nodes)
        assert graph.dependency_closure({"node-0"}) == frozenset({"node-0", "node-1"})
        return graph

    tracemalloc.start()
    try:
        retained_graph = construct()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    benchmark.extra_info.update(retained_bytes=retained, peak_bytes=peak)
    assert len(retained_graph.nodes) == 2048
    result = benchmark.pedantic(construct, rounds=5, iterations=1)
    assert len(result.nodes) == 2048


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_cold_source_editable_discovery(benchmark: Any, tmp_path: Path, harness: str) -> None:
    if harness == "codex":
        content = (_FIXTURES / "codex-session/rollout.jsonl").read_bytes()
    else:
        records = _source_records("final-response", context_sensitive=True)
        content = "\n".join(json.dumps(record) for record in records).encode()
    session = parse_session_bytes(content)
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    expected = evidence.editable_text()
    fresh = copy.copy(evidence)
    tracemalloc.start()
    try:
        retained_sites = fresh.editable_text()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    benchmark.extra_info.update(retained_bytes=retained, peak_bytes=peak)
    assert retained_sites == expected

    def setup() -> tuple[tuple[CaseEvidence], dict[str, object]]:
        # A copied owner must derive its own verified view even if its input has one.
        return (copy.copy(evidence),), {}

    result = benchmark.pedantic(CaseEvidence.editable_text, setup=setup, rounds=5)
    assert result == expected


def test_source_editable_discovery(benchmark: Any, tmp_path: Path) -> None:
    session = parse_session_bytes((_FIXTURES / "codex-session/rollout.jsonl").read_bytes())
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    # Automatic calibration handles both cold reconstruction and sub-microsecond
    # repeated access when the evidence implementation retains a verified view.
    result = benchmark(evidence.editable_text)
    assert len(result) == 1
    assert result[0].original_text == "Inspect the fixture context."


def test_candidate_preparation(benchmark: Any, tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    plan = prepare_experiment(evidence).execution_plan
    result = benchmark.pedantic(
        prepare_candidate, args=(evidence, plan), rounds=20, iterations=5, warmup_rounds=2
    )
    assert result.retained_context_ids == tuple(node.id for node in evidence.context.nodes)


@pytest.mark.parametrize("block_count", [1, 32], ids=["small", "history"])
def test_sampled_state_fresh_replay(benchmark: Any, tmp_path: Path, block_count: int) -> None:
    file, store, _ = _create(tmp_path, admit_block=False)
    blocks: list[SamplingBlockProgress] = []
    observations: list[Observation] = []
    for index in range(block_count):
        plan = _plan(
            block_id=f"block-{index}",
            candidate_digest=f"{index + 100:064x}",
            frozen_at=_TIME + timedelta(seconds=index * 10),
        )
        references = []
        counts = {"baseline": 0, "candidate": 0}
        for position, arm in enumerate(plan.schedule):
            if counts[arm] == plan.targets_for(arm).required_valid:
                continue
            counts[arm] += 1
            reservation = _reservation(plan, position, counts[arm]).model_copy(
                update={
                    "attempt_id": f"attempt-{index * 10 + position:032x}",
                    "attempt_digest": f"{index * 10 + position:064x}",
                }
            )
            observation = _observation(plan, reservation).model_copy(
                update={
                    "observation_id": f"observation-{index}-{position}",
                    "attempt": index * 2 + counts[arm],
                }
            )
            observations.append(observation)
            references.append(
                SamplingObservationReference(
                    observation_id=observation.observation_id,
                    schedule_position=position,
                    attempt_id=reservation.attempt_id,
                    attempt_digest=reservation.attempt_digest,
                )
            )
        blocks.append(
            SamplingBlockProgress(
                plan=plan,
                observations=tuple(references),
                archived=True,
            )
        )
    store.append_many(observations)
    with file.acquire_lock(store):
        # Publish a validated synthetic history once, outside the measured replay.
        base = file.load(store)
        state = SampledRunState.model_validate(
            {
                **base.model_dump(),
                "blocks": tuple(blocks),
                "approvals": (_approval(max_provider_calls=1000),),
            }
        )
        file._write(state)
        assert file.load(store) == state
        hydrated = file.evidence(store)
        trials: list[SampledCandidateTrial] = []
        removed: tuple[str, ...] = ()
        for index, block in enumerate(hydrated):
            requested = (f"context-{index}",)
            effective = tuple(sorted((*removed, *requested)))
            trial = SampledCandidateTrial(
                trial_key=trial_key("chunk", removed, requested),
                base_removals=removed,
                requested_removals=requested,
                effective_removals=effective,
                phase="chunk",
                comparison=SampledComparison.from_evidence(block),
            )
            assert trial.accepts_removal
            trials.append(trial)
            removed = effective
        checkpoint = SampledReductionCheckpoint(
            reduction=hydrated[0].plan.preservation,
            original_identity=state.original_identity,
            original_candidate_digest=state.original_candidate_digest,
            best_candidate_digest=hydrated[-1].plan.candidate_digest,
            removed_context_ids=removed,
            trials=tuple(trials),
        )
        file.save_checkpoint(store, checkpoint)
        result = benchmark.pedantic(file.load, args=(store,), rounds=5, iterations=1)
        assert result.checkpoint == checkpoint
        assert len(result.blocks) == block_count
        assert sum(len(block.observations) for block in result.blocks) == block_count * 4


def test_observation_read(benchmark: Any, tmp_path: Path) -> None:
    _, store, plan = _create(tmp_path)
    observation = _observation(plan, _reservation(plan))
    store.append(observation)
    result = benchmark.pedantic(
        store.get, args=(observation.observation_id,), rounds=20, iterations=5, warmup_rounds=2
    )
    assert result == observation


@pytest.mark.parametrize("site_count", [1000, 10000])
def test_fuzz_generation(benchmark: Any, tmp_path: Path, site_count: int) -> None:
    evidence = _evidence(tmp_path)
    original = prepare_candidate(evidence, prepare_experiment(evidence).execution_plan)
    sites = tuple(EditableText(f"site-{index}", 0, "synthetic text") for index in range(site_count))
    original = original.model_copy(
        update={"retained_context_ids": tuple(s.context_id for s in sites)}
    )
    config = _config(original, max_candidates=site_count)
    result = benchmark.pedantic(
        generate_mutations,
        args=(sites, config),
        kwargs={"stale_text": "synthetic stale instruction"},
        rounds=5,
        iterations=1,
    )
    assert result.proposals_examined == site_count
    assert len(result.mutations) == site_count


@dataclass(frozen=True)
class _CheapTrial:
    trial_key: str
    effective_removals: tuple[str, ...] = ()
    accepts_removal: bool = False
    allows_search_continuation: bool = True


def test_cheap_search_trial_deduplication(benchmark: Any, tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    keys = tuple(trial_key("chunk", frozenset(), (str(index),)) for index in range(1000))

    async def classify(**kwargs: Any) -> _CheapTrial:
        return _CheapTrial(
            trial_key(kwargs["phase"], kwargs["base_removals"], kwargs["requested_removals"])
        )

    async def traverse() -> list[_CheapTrial]:
        search = _Search(evidence, [], classify, deque(), False, lambda *_args: None, [])
        for index in range(1000):
            await search._run_trial(frozenset(), (str(index),), "chunk")
        # Duplicate decisions remain frozen and preserve the authoritative list order.
        for index in (0, 500, 999):
            assert (
                await search._run_trial(frozenset(), (str(index),), "chunk") is search.trials[index]
            )
        return search.trials

    def run() -> list[_CheapTrial]:
        return asyncio.run(traverse())

    result = benchmark.pedantic(run, rounds=5, iterations=1)
    assert tuple(trial.trial_key for trial in result) == keys


def test_passive_report_summary(benchmark: Any) -> None:
    records = _records()
    payloads = {
        "content": "synthetic-content-" + "x" * 16384,
        "detailedContent": "synthetic-detail-" + "y" * 16384,
    }
    for record in records:
        if record["type"] == "tool.execution_complete":
            record["data"]["result"] = dict(payloads)
    projection = project_reacquisition(_document(records))
    boundary_id = projection.boundaries[0].id

    def report() -> dict[str, Any]:
        return compare_reacquisition(projection, boundary_id=boundary_id)

    tracemalloc.start()
    try:
        retained_report = report()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    benchmark.extra_info.update(retained_bytes=retained, peak_bytes=peak)
    result = benchmark.pedantic(report, rounds=10, iterations=1)
    assert result == retained_report
    assert len(result["groups"]) == 1
    for side in ("before", "after"):
        for summary in result["groups"][0][side]["results"]:
            value = payloads[summary["scope"]]
            assert summary["digest"] == content_digest(value)
            assert summary["canonical_utf8_bytes"] == len(canonical_json_bytes(value))
    assert result["text_requested"] is False
    assert result["text_bytes"] == 0
    assert result["provider_calls"] == result["actions_executed"] == 0
    serialized = json.dumps(result)
    assert all(value not in serialized for value in payloads.values())
