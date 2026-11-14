from __future__ import annotations

import pytest

from ithrl.cli.shrink import _validate_observation_claim
from ithrl.observations import Observation, ObservationArm, ObservationOutcome, ObservationRole
from ithrl.reduce import ResumeMismatchError
from ithrl.results import ResultStatus
from ithrl.runner import AttemptEvidence, ProviderAcceptanceState, RunAccounting


@pytest.mark.parametrize(
    ("claim", "status", "outcome", "supported"),
    [
        ("observed", "calibrated", ObservationOutcome.MATCH, True),
        ("observed", "calibrated", ObservationOutcome.NO_MATCH, False),
        ("not-observed", "not-observed", ObservationOutcome.NO_MATCH, True),
        ("not-observed", "not-observed", ObservationOutcome.MATCH, False),
        ("failed", "failed", ObservationOutcome.RUNNER_ERROR, True),
        ("failed", "failed", ObservationOutcome.NO_MATCH, False),
        ("failed", "confirmation-failed", ObservationOutcome.NO_MATCH, True),
        ("failed", "confirmation-failed", ObservationOutcome.MATCH, False),
        ("not-run", "stopped", ObservationOutcome.MATCH, True),
    ],
)
def test_resume_claims_must_be_supported_by_the_recorded_outcome(
    claim: str, status: ResultStatus, outcome: ObservationOutcome, supported: bool
) -> None:
    failed = outcome is ObservationOutcome.RUNNER_ERROR
    observation = Observation(
        case_digest="case",
        candidate_digest="candidate",
        experiment_digest="experiment",
        runner_fingerprint="fixture-runner",
        execution_plan_digest="plan",
        target_digest="target",
        algorithm_digest="algorithm",
        role=ObservationRole.CONFIRMATION,
        arm=ObservationArm.SELECTED,
        attempt=1,
        outcome=outcome,
        error_category="transport" if failed else None,
        attempt_evidence=AttemptEvidence(
            attempt_id="attempt-" + "a" * 32,
            attempt_digest="b" * 64,
            termination_reason="failed" if failed else "completed",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            effective_policy_digest="c" * 64,
            accounting=RunAccounting(),
        ),
    )
    if supported:
        _validate_observation_claim(
            observation.observation_id, observation, result=claim, status=status
        )
    else:
        with pytest.raises(ResumeMismatchError, match="does not support its"):
            _validate_observation_claim(
                observation.observation_id, observation, result=claim, status=status
            )
