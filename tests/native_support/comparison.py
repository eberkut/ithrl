"""Provider-free comparison fixtures usable with installed optional dependencies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, override

from ithrl.adapters import enumerate_decisions
from ithrl.adapters.registry import parse_session_bytes
from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.case import CaseEvidence
from ithrl.comparison.contracts import (
    ComparisonCalibration,
    ComparisonConfiguration,
    ComparisonConfirmation,
    ComparisonLimits,
    ComparisonProgress,
)
from ithrl.comparison.engine import (
    PreparedComparison,
    comparison_budget_bounds,
    prepare_comparison,
    run_comparison,
)
from ithrl.comparison.results import (
    ComparisonEvidence,
)
from ithrl.context import content_digest
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.experiment import (
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    SamplingArmTarget,
    StructuralOracleConfig,
)
from ithrl.extraction import extract_decision
from ithrl.mutation import ContextSelection, TextAppend, TextMutation
from ithrl.observations import ObservationStore, SamplingBlockEvidence, SamplingBudgetAllowance
from ithrl.run_state import SampledRunStateFile, SamplingBudgetApproval
from ithrl.runner import (
    Candidate,
    PreparedAttempt,
    RunAccounting,
    RunnerRegistry,
    RunResult,
    ToolCall,
)
from ithrl.runner.contracts import RunnerError, RunnerErrorCode
from ithrl.runner.fake import FakeRunner
from ithrl.synthetic import extract_inference
from ithrl.tool_output_text import ToolOutputTextSpanRemoval, remove_tool_output_text_span
from ithrl.workflow import PreparedExperiment, prepare_experiment
from native_support.codex import _tool_output_text_records as codex_tool_output_text_records
from native_support.copilot import _tool_output_text_records as copilot_tool_output_text_records

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


class CountingRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__(required_markers=frozenset({"A", "C"}), final_text="BLUE")
        self.attempts: list[PreparedAttempt] = []
        self.fail = False
        self.power_loss = False
        self.lose_effect = False
        self.cancel = False
        self.model_drift = False
        self.admissions: list[Candidate] = []

    def validate_candidate(self, attempt: PreparedAttempt) -> None:
        self.admissions.append(attempt.candidate)

    @override
    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        self.attempts.append(attempt)
        if self.cancel:
            raise asyncio.CancelledError
        if self.power_loss:
            raise SystemExit("simulated process loss")
        if self.fail:
            raise RunnerError(RunnerErrorCode.CONTRACT, "sanitized test refusal")
        result = await super().run(attempt, runtime_deadline=runtime_deadline)
        if self.model_drift:
            return result.model_copy(update={"model_returned": "different-model"})
        return (
            result.model_copy(update={"final_text": "BLUE"})
            if self.lose_effect and attempt.phase == "confirmation"
            else result
        )


@dataclass
class ComparisonFixture:
    evidence: CaseEvidence
    prepared: PreparedExperiment
    configuration: ComparisonConfiguration
    state: SampledRunStateFile
    store: ObservationStore
    runner: CountingRunner
    admission: PreparedComparison

    def run(
        self,
        *,
        on_progress: Callable[[SamplingBlockEvidence], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        calibration_only: bool = False,
    ) -> ComparisonEvidence:
        return asyncio.run(
            run_comparison(
                evidence=self.evidence,
                prepared=self.prepared,
                configuration=self.configuration,
                state_file=self.state,
                store=self.store,
                admission=self.admission,
                on_progress=on_progress,
                clock=clock,
                calibration_only=calibration_only,
            )
        )


def _fixture(
    tmp_path: Path,
    *,
    objective: Literal["repair", "degradation"] = "repair",
    both_nonfull: bool = False,
    indistinguishable: bool = False,
    append: bool = False,
    wall_seconds: int = 1000,
    freshness_seconds: int = 600,
    pacing_seconds: int = 0,
    runner: CountingRunner | None = None,
    evidence: CaseEvidence | None = None,
    selection_pair: tuple[ContextSelection, ContextSelection] | None = None,
    target: FinalResponseTarget | ShellActionTarget | None = None,
) -> ComparisonFixture:
    evidence = (
        evidence
        or extract_inference(
            FIXTURE, inference_call_id="synthetic-ac-001", case_path=tmp_path / "case"
        ).load()
    )
    runner = runner or CountingRunner()
    profile = runner.capability_profile
    provider_calls = profile.provider_calls_per_attempt
    experiment = SampledExperimentDefinition(
        target=target or FinalResponseTarget(text_equals="BLUE"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type=cast(Literal["codex", "copilot"], profile.runner_type),
            required_capability_digest=profile.digest,
        )
        if provider_calls
        else FakeRunnerConfig(required_markers=("A", "C"), final_text="BLUE"),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
    )
    prepared = prepare_experiment(
        evidence,
        experiment=experiment,
        registry=RunnerRegistry({profile.runner_type: lambda _config, _evidence: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider=profile.provider,
            allowed_endpoint=profile.endpoint,
        )
        if provider_calls
        else None,
        resource_limits=ResourceLimits(max_provider_calls=provider_calls, max_wall_time_seconds=1),
    )
    nodes = {node.path: node.id for node in evidence.context.nodes}
    if selection_pair is not None:
        reference_selection, candidate_selection = selection_pair
    else:
        reference_selection, candidate_selection = _selections(
            nodes,
            objective=objective,
            both_nonfull=both_nonfull,
            indistinguishable=indistinguishable,
            append=append,
        )
    one = SamplingArmTarget(required_valid=1, max_attempts=1)
    seven = SamplingArmTarget(required_valid=7, max_attempts=7)
    configuration = ComparisonConfiguration(
        objective=objective,
        reference=reference_selection,
        candidate=candidate_selection,
        calibration=ComparisonCalibration(
            reference=one, candidate=one, positive_minimum_matches=1, negative_maximum_matches=0
        ),
        confirmation=ComparisonConfirmation(reference=seven, candidate=seven, alpha=0.05),
        minimum_effect=0.1,
        seed=7,
        eligibility_policy="safe-provider-overload"
        if provider_calls
        else "independent-safe-preaccept-only",
        freshness_seconds=freshness_seconds,
        limits=ComparisonLimits(
            max_provider_calls=100 * provider_calls,
            max_wall_time_seconds=wall_seconds,
            attempt_timeout_seconds=1,
            inter_attempt_delay_seconds=pacing_seconds,
        ),
    )
    admission = prepare_comparison(evidence, prepared, configuration)
    prepared, binding = admission.prepared, admission.binding
    bounds = comparison_budget_bounds(
        configuration, sampling_attempt_cost(runner.capability_profile, prepared.resource_limits)
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    started = datetime.now(UTC)
    state = SampledRunStateFile.create(
        case_root=tmp_path / "case",
        run_id="direct-comparison",
        store=store,
        started_at=started,
        original_identity=prepared.identity,
        original_candidate_digest=binding.reference.digest,
        provider_calls_per_attempt=provider_calls,
        comparison_progress=ComparisonProgress(binding=binding),
        approval=SamplingBudgetApproval(
            approval_id="provider-free-test",
            approved_at=started,
            allowance=SamplingBudgetAllowance(
                run_id="direct-comparison",
                max_provider_calls=100 * provider_calls,
                max_wall_time_seconds=wall_seconds,
                max_candidate_comparisons=1,
                confirmation_provider_calls=bounds.confirmation_provider_calls,
                confirmation_runner_seconds=bounds.confirmation_runner_seconds,
                inter_attempt_delay_seconds=pacing_seconds,
            ),
        ),
    )
    return ComparisonFixture(evidence, prepared, configuration, state, store, runner, admission)


def _selections(
    nodes: dict[str | None, str],
    *,
    objective: Literal["repair", "degradation"],
    both_nonfull: bool,
    indistinguishable: bool,
    append: bool,
) -> tuple[ContextSelection, ContextSelection]:
    absent = (nodes["/input/0"],) + ((nodes["/input/1"],) if both_nonfull else ())
    present = (nodes["/input/1"],) if both_nonfull or indistinguishable else ()
    if indistinguishable:
        absent = ()
    reference, candidate = (absent, present) if objective == "repair" else (present, absent)
    reference_selection = ContextSelection(removed_context_ids=reference)
    candidate_selection = ContextSelection(removed_context_ids=candidate)
    if append:
        reference_selection = ContextSelection()
        candidate_selection = ContextSelection(
            mutation=TextMutation(
                edits=(
                    TextAppend(
                        context_id=nodes["/input/0"],
                        content_index=0,
                        original_text_digest=content_digest("A"),
                        text="\nAdditional context.",
                    ),
                )
            )
        )
    return reference_selection, candidate_selection


_TOOL_OUTPUT_TEXT = 'Keep é🙂.\r\n{"recipe":"DELETE-ME\\nexact"}\r\nRetain diagnostics.'


class ToolOutputTextCountingRunner(CountingRunner):
    """Observe a validated source string without invoking a native materializer."""

    def __init__(self, harness: Literal["codex", "copilot"]) -> None:
        super().__init__()
        self.harness = harness

    @property
    @override
    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile.model_validate(
            {
                **super().capability_profile.model_dump(),
                "harness": self.harness,
                "source_schema": "codex-rollout-jsonl"
                if self.harness == "codex"
                else "copilot-events-jsonl",
                "supported_capture_types": ("logical-session",),
                "supported_materializations": ("source-backed",),
                "supported_context_operations": (
                    "dependency-closed-removal",
                    "remove-tool-output-text-span",
                ),
            }
        )

    @override
    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        self.attempts.append(attempt)
        text = attempt.evidence.editable_tool_output_text()[0].original_text
        for edit in attempt.candidate.mutation.edits:
            assert isinstance(edit, ToolOutputTextSpanRemoval)
            descriptor, original = attempt.evidence.validate_tool_output_text_span(edit)
            text = remove_tool_output_text_span(original.original_text, descriptor, edit)
        matched = "DELETE-ME" in text
        calls = (
            (ToolCall(name="bash", arguments={"command": "pwd"}),)
            if (matched and self.harness == "copilot")
            else ()
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            final_text="BLUE" if matched and self.harness == "codex" else "No match.",
            tool_calls=calls,
            provider="fake",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            accounting=RunAccounting(actions_requested=len(calls), actions_denied=len(calls)),
        )


def _tool_output_text_fixture(
    tmp_path: Path,
    *,
    harness: Literal["codex", "copilot"] = "codex",
    common_removal: bool = False,
) -> ComparisonFixture:
    """Sanitized native evidence with one prior tool field and a fixed later input."""

    if harness == "codex":
        records = codex_tool_output_text_records(_TOOL_OUTPUT_TEXT)
        records[-1]["payload"]["last_agent_message"] = "BLUE"
    else:
        records = copilot_tool_output_text_records(_TOOL_OUTPUT_TEXT, "shell-action")
        records[5]["data"]["detailedContent"] = "Display-only unchanged."
    session = parse_session_bytes("\n".join(json.dumps(r) for r in records).encode())
    decision = enumerate_decisions(session)[-1]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    field = evidence.tool_output_text_fields()[0]
    start = _TOOL_OUTPUT_TEXT.index("DELETE-ME")
    edit = evidence.tool_output_text_removal(
        field.field_id, start=start, end=start + len("DELETE-ME")
    )
    removed = ()
    if common_removal:
        removable = evidence.editable_text()[0]
        # The common omission must leave the edited call/result bundle retained.
        removed = (removable.context_id,)
    return _fixture(
        tmp_path,
        objective="degradation",
        evidence=evidence,
        runner=ToolOutputTextCountingRunner(harness),
        selection_pair=(
            ContextSelection(removed_context_ids=removed),
            ContextSelection(removed_context_ids=removed, mutation=TextMutation(edits=(edit,))),
        ),
        target=FinalResponseTarget(text_equals="BLUE")
        if harness == "codex"
        else ShellActionTarget(tool_name_equals="bash", command_equals="pwd"),
    )
