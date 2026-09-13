"""Compile selected native decisions into immutable case workspaces."""

from __future__ import annotations

import os
from pathlib import Path

from ithrl.adapters import (
    AdapterError,
    AdapterErrorCode,
    DecisionSummary,
    DiscoveredSession,
    DiscoveryLimits,
    DiscoveryResult,
    HarnessName,
    NativeSession,
    discover_sessions,
    enumerate_decisions,
    parse_session,
    reopen_discovered_session,
    resolve_session,
    select_decision,
)
from ithrl.adapters.registry import parse_session_bytes
from ithrl.behavior import BehaviorTarget
from ithrl.case import (
    CaptureFacts,
    CaseEvidence,
    CaseFormatError,
    CaseManifest,
    CaseWorkspace,
    ProvenanceFacts,
    ReplayFacts,
    SourceArtifact,
)
from ithrl.decisions import SelectedDecision
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    StructuralOracleConfig,
    UnavailableRunnerConfig,
)


def extract_decision(
    session: NativeSession,
    *,
    decision_id: str,
    target_id: str,
    case_path: str | os.PathLike[str],
) -> CaseWorkspace:
    """Compile one selected native decision without running the source harness."""

    selected = select_decision(
        session,
        decision_id=decision_id,
        target_id=target_id,
    )
    selector = selected.evidence.selector
    manifest = _case_manifest(session, selected)
    experiment = _default_experiment(selected.evidence.selected_target, selector.harness)
    source = SourceArtifact(
        path=selector.source,
        media_type=selected.case.source_media_type,
        content=session.native_bytes,
    )
    return CaseWorkspace.create(
        Path(case_path),
        manifest=manifest,
        request=None,
        nodes=selected.context.nodes,
        default_experiment=experiment.to_toml(),
        source_artifacts=(source,),
        decision=selected.evidence,
    )


def discover_native_sessions(
    *,
    harness: HarnessName | None = None,
    limits: DiscoveryLimits | None = None,
) -> DiscoveryResult:
    """Return supported saved sessions without exposing their captured content."""

    return discover_sessions(harness=harness, limits=limits)


def open_native_session(
    *,
    source: Path | None = None,
    harness: HarnessName | None = None,
    session_id: str | None = None,
    discovered: DiscoveredSession | None = None,
) -> NativeSession:
    """Open one explicit, discovered, or stable-ID native session."""

    modes = sum(value is not None for value in (source, session_id, discovered))
    if modes != 1:
        raise ValueError("select exactly one native session source")
    if source is not None:
        session = parse_session(source)
    elif discovered is not None:
        session = reopen_discovered_session(discovered)
    elif session_id is not None and harness is not None:
        session = resolve_session(harness=harness, session_id=session_id)
    else:
        raise AdapterError(
            AdapterErrorCode.SESSION_NOT_FOUND,
            "--harness is required with --session",
        )
    if harness is not None and session.metadata.format.harness != harness:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_FORMAT,
            "session source does not match --harness",
        )
    return session


def list_native_decisions(session: NativeSession) -> tuple[DecisionSummary, ...]:
    """Return completed decisions for selection without exposing their content."""

    return enumerate_decisions(session)


def validate_extracted_case(evidence: CaseEvidence) -> None:
    """Re-derive a logical-session case from its preserved native source."""

    decision = evidence.decision
    if decision is None:
        return
    source = next(
        (
            artifact
            for artifact in evidence.source_artifacts
            if artifact.path == decision.selector.source
        ),
        None,
    )
    if source is None:
        raise CaseFormatError("logical-session case has no selected source evidence")
    try:
        session = parse_session_bytes(source.content)
        selected = select_decision(
            session,
            decision_id=decision.decision_id,
            target_id=decision.selected_target_id,
        )
    except AdapterError:
        raise CaseFormatError(
            "logical-session case cannot be re-derived from preserved source"
        ) from None
    if selected.evidence != decision or selected.context != evidence.context:
        raise CaseFormatError("logical-session case does not match preserved source evidence")
    if source.media_type != selected.case.source_media_type:
        raise CaseFormatError("logical-session case source type does not match the adapter")
    if _case_manifest(session, selected) != evidence.manifest:
        raise CaseFormatError("logical-session case metadata does not match preserved source")


def load_extracted_case(workspace: CaseWorkspace) -> CaseEvidence:
    """Load a case and re-derive native evidence before application use."""

    evidence = workspace.load()
    validate_extracted_case(evidence)
    return evidence


def _case_manifest(session: NativeSession, selected: SelectedDecision) -> CaseManifest:
    selector = selected.evidence.selector
    facts = selected.case
    return CaseManifest(
        adapter=facts.adapter,
        adapter_version=facts.adapter_version,
        captured_at=session.metadata.captured_at,
        harness=session.metadata.format.harness,
        harness_version=session.metadata.format.producer_version,
        source_schema=facts.source_schema,
        provider=facts.provider,
        endpoint=facts.endpoint,
        model_requested=facts.model_requested,
        model_returned=facts.model_returned,
        capture=CaptureFacts(
            request=facts.capture_type,
            completeness=facts.completeness,
        ),
        replay=ReplayFacts(
            state=facts.replay_state,
            environment=facts.environment,
        ),
        provenance=ProvenanceFacts(
            source=selector.source,
            inference_call_id=None,
            trace_schema=facts.source_schema,
        ),
    )


def _default_experiment(target: BehaviorTarget, harness: str) -> ExperimentDefinition:
    return ExperimentDefinition(
        target=target,
        oracle=StructuralOracleConfig(),
        runner=UnavailableRunnerConfig(harness=harness),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )
