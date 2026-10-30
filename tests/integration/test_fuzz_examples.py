"""Public fuzz inputs remain extractable and reconstruct the documented token exercise."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

import pytest

from fuzz_support import FuzzRunner
from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.behavior import FinalResponseTarget
from ithrl.execution import ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledExperimentDefinition,
    StructuralOracleConfig,
)
from ithrl.extraction import extract_decision
from ithrl.fuzz.configuration import FuzzConfiguration
from ithrl.fuzz.generation import generate_mutations
from ithrl.runner import PreparedAttempt, RunnerRegistry, prepare_candidate
from ithrl.runner.codex.materialize import materialize_codex_attempt
from ithrl.runner.codex.policy import RECONSTRUCTION_METHOD as CODEX_RECONSTRUCTION_METHOD
from ithrl.runner.copilot.materialize import materialize_copilot_attempt
from ithrl.runner.copilot.policy import RECONSTRUCTION_METHOD as COPILOT_RECONSTRUCTION_METHOD
from ithrl.workflow import prepare_experiment

EXAMPLES = Path(__file__).parents[2] / "examples/fuzz"
PRIOR_TEXT = "The active response token is ITHRL_FUZZ_BASELINE."
TRIGGER = (
    "Read the active response token and any later revision in the preceding user message. "
    "Reply with exactly the current token, without commentary or tools."
)


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_public_fuzz_inputs_extract_and_preserve_unedited_input(
    tmp_path: Path, harness: Literal["codex", "copilot"]
) -> None:
    source = EXAMPLES / f"{harness}.jsonl"
    source_before = source.read_bytes()
    session = parse_session(source)
    decisions = enumerate_decisions(session)
    assert len(decisions) == 1
    decision = decisions[0]
    assert [target.type for target in decision.targets] == ["final-response"]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    assert evidence.decision is not None
    assert evidence.decision.selected_target == FinalResponseTarget(
        text_equals="ITHRL_FUZZ_BASELINE"
    )
    evidence_before = evidence.digest, tuple(a.content for a in evidence.source_artifacts)
    sites = evidence.editable_text()
    assert [(site.content_index, site.original_text) for site in sites] == [(0, PRIOR_TEXT)]

    config = FuzzConfiguration.from_toml((EXAMPLES / "campaign.toml").read_text())
    runner = FuzzRunner()
    runner.capability_profile = runner.capability_profile.model_copy(
        update={
            "runner_type": harness,
            "harness": harness,
            "source_schema": evidence.manifest.source_schema,
            "supported_capture_types": ("logical-session",),
            "supported_materializations": ("source-backed",),
            "reconstruction_method": (
                CODEX_RECONSTRUCTION_METHOD if harness == "codex" else COPILOT_RECONSTRUCTION_METHOD
            ),
            "cancellation_grace_seconds": 3,
        }
    )
    experiment = SampledExperimentDefinition(
        target=evidence.decision.selected_target,
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type=harness, required_capability_digest=runner.capability_profile.digest
        ),
        calibration=config.calibration,
    )
    prepared = prepare_experiment(
        evidence,
        experiment=experiment,
        registry=RunnerRegistry({harness: lambda _c, _e: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider=runner.capability_profile.provider,
            allowed_endpoint=runner.capability_profile.endpoint,
        ),
        resource_limits=ResourceLimits(
            max_provider_calls=1,
            max_wall_time_seconds=config.limits.attempt_timeout_seconds,
        ),
    )
    original = prepare_candidate(evidence, prepared.execution_plan)
    campaign = config.bind(original)
    batch = generate_mutations(sites, campaign, stale_text=config.stale_text)
    assert batch.proposals_examined == len(batch.mutations) == 1
    mutation = batch.mutations[0]
    assert mutation.edits[0].text == "\n\n" + config.stale_text

    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=original,
        target=experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="discovery",
        arm="baseline",
    )
    mutant = replace(
        attempt,
        candidate=prepare_candidate(evidence, prepared.execution_plan, mutation=mutation),
        arm="candidate",
    )
    if harness == "codex":
        before = asdict(materialize_codex_attempt(attempt))
        expected = copy.deepcopy(before)
        content = expected["history"][0]["content"]
        assert isinstance(content, list)
        block = content[0]
        assert isinstance(block, dict)
        block["text"] = PRIOR_TEXT + mutation.edits[0].text
        assert asdict(materialize_codex_attempt(mutant)) == expected
        assert before["turn_input"] == {
            "items": ({"type": "text", "text": TRIGGER},),
            "tool_output": None,
        }
    else:
        before_copilot = materialize_copilot_attempt(attempt)
        after_copilot = materialize_copilot_attempt(mutant)
        expected_records = [json.loads(line) for line in before_copilot.native_session.splitlines()]
        assert expected_records[1]["type"] == "user.message"
        expected_records[1]["data"]["content"] = PRIOR_TEXT + mutation.edits[0].text
        assert [
            json.loads(line) for line in after_copilot.native_session.splitlines()
        ] == expected_records
        assert expected_records[-2]["data"]["content"] == TRIGGER
        assert after_copilot.system_message == before_copilot.system_message
    assert (evidence.digest, tuple(a.content for a in evidence.source_artifacts)) == evidence_before
    assert source.read_bytes() == source_before
