"""Private receipt publication and offline reduction preparation."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from ithrl.case import CaseError, CaseWorkspace
from ithrl.case.filesystem import (
    atomic_write_bytes,
    prepare_target_path,
    read_safe_text,
    reject_symlink_components,
)
from ithrl.cli.native import native_resource_limits
from ithrl.cli.output import (
    ExitCode,
    OutputFormat,
    command_boundary,
    console,
    fail,
    print_plain,
    shell_command,
)
from ithrl.cli.paths import reserved_native_paths
from ithrl.execution.budget import reduction_budget_bounds, sampling_attempt_cost
from ithrl.preparation import (
    NativePreparationReceipt,
    PreparationError,
    ReductionPreparationSettings,
    prepare_reduction,
)
from ithrl.reduce import MAX_CANDIDATE_RUNS
from ithrl.workflow import PreparedExperiment

MAX_PREPARATION_BYTES = 1024 * 1024


def preparation_destination(path: Path, *, protected: tuple[Path, ...]) -> Path:
    """Validate an absent destination without creating files or following protected paths."""

    if ".." in path.parts:
        raise ValueError("unsafe preparation destination")
    reject_symlink_components(path)
    destination = _comparison_path(path)
    if os.path.lexists(destination):
        raise ValueError("preparation destination exists")
    for root in protected:
        root = _comparison_path(root)
        if destination == root or root in destination.parents or destination in root.parents:
            raise ValueError("preparation destination overlaps protected state")
    return destination


def _comparison_path(path: Path) -> Path:
    """Use one POSIX root spelling without resolving a recorded workspace."""

    absolute = path.absolute()
    if os.name == "posix" and absolute.anchor == "//":
        return Path("/", *absolute.parts[1:])
    return absolute


def _publish(path: Path, content: str, *, protected: tuple[Path, ...]) -> None:
    data = content.encode("utf-8")
    if len(data) > MAX_PREPARATION_BYTES:
        raise ValueError("preparation output exceeds size limit")
    destination = preparation_destination(path, protected=protected)
    prepare_target_path(destination)
    atomic_write_bytes(destination, data)


def save_preflight_receipt(
    workspace: CaseWorkspace,
    prepared: PreparedExperiment,
    original_workspace: Path,
    destination: Path,
    observation_store: Path | None = None,
) -> None:
    """Publish recorded native facts only after a successful explicit dry-run."""

    receipt = NativePreparationReceipt.create(
        prepared.execution_plan, prepared.experiment, original_workspace.absolute()
    )
    _publish(
        destination,
        receipt.model_dump_json(),
        protected=(workspace.root, original_workspace, *reserved_native_paths(observation_store)),
    )


@command_boundary("prepare")
def prepare(
    case: Annotated[Path, typer.Argument(help="Extracted native case directory.")],
    preflight: Annotated[
        Path, typer.Option("--preflight", help="Private native preflight receipt.")
    ],
    settings: Annotated[
        Path, typer.Option("--settings", help="Explicit reduction preparation TOML.")
    ],
    output: Annotated[
        Path, typer.Option("--output", help="New private experiment TOML; never overwritten.")
    ],
    max_candidates: Annotated[
        int,
        typer.Option(
            "--max-candidates",
            min=1,
            max=MAX_CANDIDATE_RUNS,
            help="Maximum candidate tests across coarse and fine search.",
        ),
    ],
    max_provider_calls: Annotated[
        int,
        typer.Option(
            "--max-provider-calls",
            min=1,
            help="Maximum total provider calls for calibration, search and confirmation.",
        ),
    ],
    max_wall_time: Annotated[
        int,
        typer.Option(
            "--max-wall-time",
            min=1,
            help="Total elapsed-time limit in seconds, including calibration.",
        ),
    ],
    inter_attempt_delay: Annotated[
        int,
        typer.Option(
            "--inter-attempt-delay",
            min=0,
            max=86_400,
            help="Seconds between attempts; included in admission bounds.",
        ),
    ] = 0,
    observation_store: Annotated[
        Path | None,
        typer.Option("--observation-store", help="Forward a store path without opening it."),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.HUMAN,
) -> None:
    """Prepare a native reduction offline, then print its inspection command."""

    try:
        workspace = CaseWorkspace.open(case)
        evidence = workspace.load()
        receipt = NativePreparationReceipt.from_json(
            read_safe_text(preflight, max_bytes=MAX_PREPARATION_BYTES, label="preflight receipt")
        )
        policy = ReductionPreparationSettings.from_toml(
            read_safe_text(settings, max_bytes=MAX_PREPARATION_BYTES, label="preparation settings")
        )
        limits = receipt.source_plan.resource_limits
        current_limits = native_resource_limits(
            receipt.source_plan.capability.runner_type,
            max_provider_calls=limits.max_provider_calls,
            attempt_timeout_seconds=limits.max_wall_time_seconds,
            max_input_tokens=limits.max_input_tokens,
        )
        if limits != current_limits:
            raise PreparationError("preflight-limits-mismatch")
        experiment = prepare_reduction(evidence, receipt, policy)
        if experiment.reduction is None:
            raise PreparationError("reduction-settings-required")
        bounds = reduction_budget_bounds(
            calibration=experiment.calibration,
            reduction=experiment.reduction,
            attempt_cost=sampling_attempt_cost(receipt.source_plan.capability, limits),
            max_candidate_comparisons=max_candidates,
            max_provider_calls=max_provider_calls,
            max_wall_time_seconds=max_wall_time,
            inter_attempt_delay_seconds=inter_attempt_delay,
        )
        if bounds.refusal_code is not None:
            raise PreparationError(bounds.refusal_code)
        original = Path(receipt.original_workspace)
        protected = (
            workspace.root,
            original,
            preflight,
            settings,
            *reserved_native_paths(observation_store),
        )
        destination = preparation_destination(output, protected=protected)
        command = [
            "ithrl",
            "shrink",
            str(workspace.root.absolute()),
            "--experiment",
            str(destination),
            "--original-workspace",
            str(original),
            "--max-candidates",
            str(max_candidates),
            "--max-provider-calls",
            str(max_provider_calls),
            "--max-wall-time",
            str(max_wall_time),
            "--attempt-timeout",
            str(limits.max_wall_time_seconds),
            "--max-provider-calls-per-attempt",
            str(limits.max_provider_calls),
            "--inter-attempt-delay",
            str(inter_attempt_delay),
            "--dry-run",
        ]
        if limits.max_input_tokens is not None:
            command += ["--max-input-tokens", str(limits.max_input_tokens)]
        if observation_store is not None:
            command += ["--observation-store", str(observation_store.absolute())]
        next_command, next_shell = shell_command(tuple(command))
        _publish(destination, experiment.to_toml(), protected=protected)
    except PreparationError as error:
        fail(
            f"Preparation refused ({error.code}). Check settings and recorded evidence; "
            "export a new receipt with shrink --dry-run --save-preflight when bindings differ.",
            ExitCode.USAGE,
            output_format,
            "prepare",
            error.code,
        )
    except (CaseError, OSError, ValueError):
        fail(
            "Cannot prepare the experiment. Check input formats, private paths and an absent "
            "output outside the case and original workspace. Use a receipt exported by "
            "shrink --dry-run --save-preflight.",
            ExitCode.USAGE,
            output_format,
            "prepare",
            "preparation-invalid",
        )
    report = {
        "schema_version": 1,
        "kind": "preparation",
        "command": "prepare",
        "status": "prepared",
        "case_digest": evidence.digest,
        "experiment_digest": experiment.digest,
        "capability_digest": receipt.source_plan.capability.digest,
        "output_path": str(destination),
        "budget": asdict(bounds),
        "provider_calls": 0,
        "actions_executed": 0,
        "next_action": "Inspect the generated configuration through native dry-run; current runtime validation is required.",
        "next_command": next_command,
        "next_command_shell": next_shell,
    }
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        print_plain(console, "Private reduction configuration prepared")
        print_plain(console, f"  output: {destination}")
        print_plain(console, f"  budget: {json.dumps(report['budget'], sort_keys=True)}")
        print_plain(console, report["next_action"])
        print_plain(console, f"  Command ({next_shell}): {next_command}", soft_wrap=True)
        print_plain(console, "No harness probes, provider calls or actions were executed.")
