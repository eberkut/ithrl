"""Case extraction command behavior and guided selection."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Literal
from uuid import uuid4

import typer

from ithrl.adapters import (
    AdapterError,
    AdapterErrorCode,
    DecisionSummary,
    DiscoveredSession,
    HarnessName,
    NativeSession,
)
from ithrl.case import CaseWorkspace
from ithrl.cli.output import CommandOutput, OutputFormat, console, print_plain, shell_command
from ithrl.cli.paths import private_data_root
from ithrl.extraction import (
    discover_native_sessions,
    extract_decision,
    list_native_decisions,
    load_extracted_case,
    open_native_session,
)
from ithrl.synthetic import SyntheticTraceError, extract_inference, list_inferences

type ExtractionMode = Literal["native", "synthetic"]


class ExtractionSelectionError(ValueError):
    """Command options do not identify one extraction target."""

    __slots__ = ("code",)

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def create_case_report(
    *,
    source: Path | None,
    inference: str | None,
    harness: str | None,
    session_id: str | None,
    decision_id: str | None,
    target: str | None,
    output: Path | None,
    output_format: OutputFormat,
) -> CommandOutput:
    """Select evidence, create one case, and return content-safe output."""

    selected_harness = _parse_harness(harness)
    mode, opened_session = _select_mode(
        source=source,
        inference=inference,
        harness=selected_harness,
        session_id=session_id,
        decision_id=decision_id,
        target=target,
    )
    if mode == "synthetic":
        if source is None:
            raise ExtractionSelectionError(
                "conflicting-options",
                "a synthetic extraction requires a source path",
            )
        return _extract_synthetic(
            source=source,
            inference=inference,
            output=output,
            output_format=output_format,
        )
    return _extract_native(
        source=source,
        opened_session=opened_session,
        harness=selected_harness,
        session_id=session_id,
        decision_id=decision_id,
        target=target,
        output=output,
        output_format=output_format,
    )


def _select_mode(
    *,
    source: Path | None,
    inference: str | None,
    harness: HarnessName | None,
    session_id: str | None,
    decision_id: str | None,
    target: str | None,
) -> tuple[ExtractionMode, NativeSession | None]:
    native_options = any(value is not None for value in (harness, session_id, decision_id, target))
    if inference is not None:
        if native_options:
            raise ExtractionSelectionError(
                "conflicting-options",
                "--inference cannot be combined with native session options",
            )
        return "synthetic", None
    if source is None or native_options:
        return "native", None
    try:
        session = open_native_session(source=source)
    except AdapterError as native_error:
        if native_error.code not in {
            AdapterErrorCode.UNSUPPORTED_FORMAT,
            AdapterErrorCode.INVALID_JSONL,
        }:
            raise
        try:
            list_inferences(source)
        except SyntheticTraceError:
            if native_error.code is AdapterErrorCode.INVALID_JSONL:
                raise native_error from None
            raise AdapterError(
                native_error.code,
                "source is not a supported native session or synthetic trace",
            ) from None
        return "synthetic", None
    return "native", session


def _extract_synthetic(
    *,
    source: Path,
    inference: str | None,
    output: Path | None,
    output_format: OutputFormat,
) -> CommandOutput:
    selected_inference = select_inference(source, inference, output_format)
    destination = _case_destination(output, selected_inference)
    workspace = extract_inference(
        source,
        inference_call_id=selected_inference,
        case_path=destination,
    )
    evidence = load_extracted_case(workspace)
    next_command, next_command_shell = shell_command(
        ("ithrl", "shrink", str(workspace.root.absolute()))
    )
    return CommandOutput(
        command="extract",
        status="case-created",
        case_path=str(workspace.root.absolute()),
        case_digest=evidence.digest,
        capture_type=evidence.manifest.capture.request,
        capture_completeness=evidence.manifest.capture.completeness,
        replay_state=evidence.manifest.replay.state,
        replay_environment=evidence.manifest.replay.environment,
        harness=evidence.manifest.harness,
        harness_version=evidence.manifest.harness_version,
        original_context_count=len(evidence.context.nodes),
        retained_context_count=len(evidence.context.nodes),
        next_action="Run shrink to calibrate and reduce this case.",
        next_command=next_command,
        next_command_shell=next_command_shell,
    )


def _extract_native(
    *,
    source: Path | None,
    opened_session: NativeSession | None,
    harness: HarnessName | None,
    session_id: str | None,
    decision_id: str | None,
    target: str | None,
    output: Path | None,
    output_format: OutputFormat,
) -> CommandOutput:
    session = opened_session or _select_session(
        source=source,
        harness=harness,
        session_id=session_id,
        output_format=output_format,
    )
    if session_id is not None and session.metadata.session_id != session_id:
        raise AdapterError(
            AdapterErrorCode.SESSION_NOT_FOUND,
            "session source does not match --session",
        )
    summaries = list_native_decisions(session)
    if not summaries:
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session contains no supported completed decisions",
        )
    decision = _select_decision(summaries, decision_id, output_format)
    target_id = _select_target(decision, target, output_format)
    destination = _case_destination(output, decision.decision_id)
    workspace = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=target_id,
        case_path=destination,
    )
    evidence = load_extracted_case(workspace)
    selected_target = next(item for item in decision.targets if item.target_id == target_id)
    return CommandOutput(
        command="extract",
        status="case-created",
        case_path=str(workspace.root.absolute()),
        case_digest=evidence.digest,
        capture_type=evidence.manifest.capture.request,
        capture_completeness=evidence.manifest.capture.completeness,
        replay_state=evidence.manifest.replay.state,
        replay_environment=evidence.manifest.replay.environment,
        harness=session.metadata.format.harness,
        harness_version=session.metadata.format.producer_version,
        session_id=session.metadata.session_id,
        decision_id=decision.decision_id,
        target_id=target_id,
        target_type=selected_target.type,
        evidence_support=selected_target.evidence_support,
        experimental_support=selected_target.experimental_support,
        limitations=decision.limitations,
        runner_type="unavailable",
        original_context_count=len(evidence.context.nodes),
        retained_context_count=len(evidence.context.nodes),
    )


def _select_session(
    *,
    source: Path | None,
    harness: HarnessName | None,
    session_id: str | None,
    output_format: OutputFormat,
) -> NativeSession:
    if source is not None:
        return open_native_session(source=source, harness=harness)
    if session_id is not None:
        if harness is None:
            raise ExtractionSelectionError(
                "selection-required",
                "--harness is required with --session",
            )
        return open_native_session(harness=harness, session_id=session_id)

    interactive = _is_interactive(output_format)
    progress = console.status("Finding supported sessions...") if interactive else nullcontext()
    with progress:
        result = discover_native_sessions(harness=harness)
    if not result.sessions:
        reasons = ", ".join(reason.value for reason in result.rejection_codes)
        reason_suffix = f"; rejected candidates: {reasons}" if reasons else ""
        raise AdapterError(
            AdapterErrorCode.NO_SESSIONS,
            "no supported completed sessions were found in the selected local stores"
            + reason_suffix,
        )
    if not interactive:
        available = ", ".join(
            f"{item.summary.harness}:{item.summary.session_id}" for item in result.sessions
        )
        raise ExtractionSelectionError(
            "selection-required",
            "--harness and --session are required in non-interactive mode; "
            f"available sessions: {available}",
        )
    discovered = _prompt_for_session(result.sessions, truncated=result.truncated)
    return open_native_session(discovered=discovered)


def _prompt_for_session(
    sessions: tuple[DiscoveredSession, ...],
    *,
    truncated: bool,
) -> DiscoveredSession:
    print_plain(console, "Recent sessions")
    if truncated:
        print_plain(console, f"Showing the {len(sessions)} most recent supported sessions.")
    for index, discovered in enumerate(sessions, start=1):
        summary = discovered.summary
        targets = ", ".join(summary.target_types)
        decision_label = "decision" if summary.decision_count == 1 else "decisions"
        limitations = ", ".join(
            {
                "experimental-support-unvalidated": "experiment not validated",
                "opaque-context-frozen": "opaque context kept",
            }[limitation]
            for limitation in summary.limitations
        )
        limitation_suffix = f"  {limitations}" if limitations else ""
        print_plain(
            console,
            f"  {index}. {summary.harness} {summary.producer_version}  "
            f"{summary.session_id}  {summary.captured_at}  "
            f"{summary.decision_count} {decision_label}  {targets}{limitation_suffix}",
            soft_wrap=True,
        )
    choice = typer.prompt("Select a session", type=int, default=1)
    return _numbered_choice(sessions, choice, label="session")


def _select_decision(
    decisions: tuple[DecisionSummary, ...],
    selected: str | None,
    output_format: OutputFormat,
) -> DecisionSummary:
    if selected is not None:
        return _decision_by_id(decisions, selected)
    return _prompt_for_decision(decisions, output_format)


def _decision_by_id(
    decisions: tuple[DecisionSummary, ...],
    decision_id: str,
) -> DecisionSummary:
    matches = tuple(item for item in decisions if item.decision_id == decision_id)
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(item.decision_id for item in decisions)
    raise AdapterError(
        AdapterErrorCode.DECISION_NOT_FOUND,
        f"completed decision was not found; available decisions: {available}",
    )


def _prompt_for_decision(
    decisions: tuple[DecisionSummary, ...],
    output_format: OutputFormat,
) -> DecisionSummary:
    if not _is_interactive(output_format):
        available = ", ".join(item.decision_id for item in decisions)
        raise ExtractionSelectionError(
            "selection-required",
            f"--decision is required in non-interactive mode; available decisions: {available}",
        )
    if len(decisions) == 1:
        return decisions[0]
    print_plain(console, "Completed decisions")
    for index, decision in enumerate(decisions, start=1):
        targets = ", ".join(target.type for target in decision.targets)
        print_plain(
            console,
            f"  {index}. {decision.decision_id}  {decision.observed_at}  "
            f"{decision.kind}  {targets}",
            soft_wrap=True,
        )
    choice = typer.prompt("Select a decision", type=int, default=1)
    return _numbered_choice(decisions, choice, label="decision")


def _select_target(
    decision: DecisionSummary,
    selected: str | None,
    output_format: OutputFormat,
) -> str:
    if selected is not None:
        return _target_by_id_or_type(decision, selected)
    return _prompt_for_target(decision, output_format)


def _target_by_id_or_type(decision: DecisionSummary, selected: str) -> str:
    matches = tuple(
        item for item in decision.targets if item.target_id == selected or item.type == selected
    )
    if len(matches) == 1:
        return matches[0].target_id
    available = _available_targets(decision)
    raise AdapterError(
        AdapterErrorCode.TARGET_NOT_FOUND,
        f"behavior target was not found or is ambiguous; available targets: {available}",
    )


def _prompt_for_target(
    decision: DecisionSummary,
    output_format: OutputFormat,
) -> str:
    if not _is_interactive(output_format):
        raise ExtractionSelectionError(
            "selection-required",
            "--target is required in non-interactive mode; available targets: "
            + _available_targets(decision),
        )
    if len(decision.targets) == 1:
        return decision.targets[0].target_id
    print_plain(console, "Behavior targets")
    for index, item in enumerate(decision.targets, start=1):
        print_plain(
            console,
            f"  {index}. {item.target_id}  {item.type}  {item.experimental_support}",
        )
    choice = typer.prompt("Select a target", type=int, default=1)
    return _numbered_choice(decision.targets, choice, label="target").target_id


def _available_targets(decision: DecisionSummary) -> str:
    return ", ".join(f"{item.target_id} ({item.type})" for item in decision.targets)


def select_inference(
    trace: Path,
    selected: str | None,
    output_format: OutputFormat,
) -> str:
    """Select a synthetic inference explicitly or through a TTY prompt."""

    if selected is not None:
        return selected
    summaries = list_inferences(trace)
    available = ", ".join(summary.inference_call_id for summary in summaries)
    if not _is_interactive(output_format):
        raise ExtractionSelectionError(
            "selection-required",
            "--inference is required in non-interactive mode; available IDs: " + available,
        )
    print_plain(console, "Available inferences")
    for index, summary in enumerate(summaries, start=1):
        print_plain(
            console,
            f"  {index}. {summary.inference_call_id}  {summary.captured_at}  "
            f"{summary.model_requested}",
        )
    choice = typer.prompt("Select an inference", type=int, default=1)
    return _numbered_choice(summaries, choice, label="inference").inference_call_id


def _numbered_choice[T](items: tuple[T, ...], choice: int, *, label: str) -> T:
    if choice < 1 or choice > len(items):
        raise ExtractionSelectionError(
            "invalid-selection",
            f"{label} selection must be between 1 and {len(items)}",
        )
    return items[choice - 1]


def _parse_harness(value: str | None) -> HarnessName | None:
    if value in (None, "codex", "copilot"):
        return value
    raise ExtractionSelectionError(
        "unsupported-harness",
        "--harness must be codex or copilot",
    )


def _default_case_path(identity: str) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in identity
    )
    safe = safe.strip("-")[:48] or "case"
    return private_data_root() / "cases" / f"{safe}-{uuid4().hex[:8]}"


def _case_destination(output: Path | None, identity: str) -> Path:
    destination = output or _default_case_path(identity)
    return CaseWorkspace.prepare_destination(destination)


def _is_interactive(output_format: OutputFormat) -> bool:
    return output_format is OutputFormat.HUMAN and sys.stdin.isatty()
