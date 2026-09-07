from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence
from ithrl.context import JsonObject
from ithrl.experiment import ExperimentDefinition
from ithrl.measurement import observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationRole,
    ObservationStore,
)
from ithrl.runner import RunResult
from ithrl.synthetic import extract_inference
from ithrl.workflow import OraclePrerequisiteError, calibrate, confirm, prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"
INFERENCE_ID = "synthetic-ac-001"


def _evidence(tmp_path: Path) -> CaseEvidence:
    return extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "case",
    ).load()


def test_prepare_calibrate_and_confirm_use_separate_observation_roles(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    store = ObservationStore(tmp_path / "observations.sqlite")

    assert asyncio.run(calibrate(evidence=evidence, prepared=prepared, store=store)).target_observed
    assert asyncio.run(calibrate(evidence=evidence, prepared=prepared, store=store)).target_observed
    assert asyncio.run(
        confirm(request=evidence.request, prepared=prepared, store=store)
    ).target_observed
    assert asyncio.run(
        confirm(request=evidence.request, prepared=prepared, store=store)
    ).target_observed

    observations = store.list_for_experiment(prepared.experiment.digest)
    assert [item.role for item in observations] == [
        ObservationRole.CALIBRATION,
        ObservationRole.CONFIRMATION,
        ObservationRole.CONFIRMATION,
    ]
    assert prepared.execution_plan.network_calls_planned == 0
    assert prepared.execution_plan.provider_calls_planned == 0
    assert prepared.execution_plan.action_execution == "denied"


def test_calibration_records_when_the_target_is_absent(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    changed = experiment.model_copy(
        update={
            "runner": experiment.runner.model_copy(update={"required_markers": ("missing-marker",)})
        }
    )
    changed_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=evidence.context,
        default_experiment=changed.to_toml(),
    )
    prepared = prepare_experiment(changed_evidence)

    observation = asyncio.run(
        calibrate(
            evidence=changed_evidence,
            prepared=prepared,
            store=ObservationStore(tmp_path / "observations.sqlite"),
        )
    )

    assert observation.target_observed is False


def test_concurrent_measurements_keep_both_completed_runner_results(tmp_path: Path) -> None:
    class ConcurrentRunner:
        fingerprint = "concurrent-v1"

        def __init__(self) -> None:
            self.calls = 0
            self._both_started = asyncio.Event()

        async def run(self, request: Mapping[str, Any]) -> RunResult:
            del request
            self.calls += 1
            if self.calls == 2:
                self._both_started.set()
            await self._both_started.wait()
            return RunResult(provider="fake", final_text="target")

    async def measure_twice() -> tuple[Observation, Observation]:
        runner = ConcurrentRunner()
        store = ObservationStore(tmp_path / "observations.sqlite")
        identity = ObservationIdentity(
            case_digest="case",
            experiment_digest="experiment",
            runner_fingerprint=runner.fingerprint,
            target_digest="target",
            algorithm_digest="algorithm",
        )
        target = FinalResponseTarget(text_equals="target")
        request: JsonObject = {"model": "fake", "input": []}
        first, second = await asyncio.gather(
            observe_once(
                runner=runner,
                target=target,
                identity=identity,
                store=store,
                request=request,
                role=ObservationRole.CONFIRMATION,
                arm=ObservationArm.SELECTED,
            ),
            observe_once(
                runner=runner,
                target=target,
                identity=identity,
                store=store,
                request=request,
                role=ObservationRole.CONFIRMATION,
                arm=ObservationArm.SELECTED,
            ),
        )
        assert runner.calls == 2
        assert len(store.list_for_experiment("experiment")) == 2
        return first, second

    first, second = asyncio.run(measure_twice())

    assert {first.attempt, second.attempt} == {1, 2}


def test_preflight_requires_oracle_dependencies_to_be_protected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    mutable_id = next(node.id for node in evidence.context.nodes if node.mutable)
    changed = experiment.model_copy(
        update={
            "oracle": experiment.oracle.model_copy(update={"protected_context_ids": (mutable_id,)})
        }
    )
    changed_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=evidence.context,
        default_experiment=changed.to_toml(),
    )

    with pytest.raises(OraclePrerequisiteError, match="reducible"):
        prepare_experiment(changed_evidence)
