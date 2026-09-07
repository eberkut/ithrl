"""Structured command output and safe terminal display."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum, StrEnum
from functools import wraps
from typing import Any, Literal, Never, override

import typer
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.text import Text
from typer._click import ClickException
from typer._click.core import Exit
from typer.core import TyperGroup


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    UNSUPPORTED = 3
    INCONCLUSIVE = 4
    PROVIDER = 5
    BUDGET = 6
    INTERNAL = 70


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


class CommandOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command: Literal["extract", "shrink"]
    status: Literal[
        "case-created",
        "ready",
        "calibrated",
        "reduced",
        "stopped",
        "interrupted",
        "confirmation-failed",
        "not-observed",
        "failed",
    ]
    case_path: str
    case_digest: str
    result_path: str | None = None
    original_context_count: int | None = None
    retained_context_count: int | None = None
    calibration_result: str | None = None
    search_result: str | None = None
    confirmation_result: str | None = None
    candidate_runs_used: int = 0
    max_candidate_runs: int | None = None
    observations_recorded: int = 0
    execution_plan_digest: str | None = None
    runner_type: str | None = None
    runner_version: str | None = None
    target_type: str | None = None
    protected_context_ids: tuple[str, ...] = ()
    run_state_path: str | None = None
    network_calls: Literal[0] = 0
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0
    next_action: str | None = None
    next_command: str | None = None
    next_command_shell: Literal["posix", "powershell"] | None = None


class ErrorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command: Literal["ithrl", "extract", "shrink"]
    status: Literal["error"] = "error"
    exit_code: int
    error_code: str
    message: str
    network_calls: Literal[0] = 0
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0


console = Console()
error_console = Console(stderr=True)

_SHRINK_HEADINGS = {
    "ready": "Execution plan is valid",
    "calibrated": "Original context tested",
    "reduced": "Reduced and confirmed",
    "stopped": "Search stopped before confirmation",
    "interrupted": "Search interrupted before confirmation",
    "confirmation-failed": "Reduced context did not pass confirmation",
    "not-observed": "Original context did not produce the target behavior",
    "failed": "Experiment failed before confirmation",
}
_CALIBRATION_LABELS = {
    "observed": "target observed",
    "not-observed": "target not observed",
    "failed": "failed",
    "not-run": "not run",
}
_SEARCH_LABELS = {
    "search-1-minimal": "1-minimal; all valid single removals tested",
    "search-incomplete": "incomplete",
    "not-run": "not run",
}
_CONFIRMATION_LABELS = {
    "confirmed-preserving": "target preserved",
    "failed": "failed",
    "not-run": "not run",
}


class JsonAwareGroup(TyperGroup):
    """Keep command-line parser errors inside the JSON output contract."""

    @override
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        if not _requests_json(arguments):
            return super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        try:
            result = super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except ClickException as error:
            command: Literal["ithrl", "extract", "shrink"] = "ithrl"
            if "extract" in arguments:
                command = "extract"
            elif "shrink" in arguments:
                command = "shrink"
            report = ErrorOutput(
                command=command,
                exit_code=error.exit_code,
                error_code="invalid-command-line",
                message=str(error),
            )
            typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
            if standalone_mode:
                raise SystemExit(error.exit_code) from None
            raise Exit(error.exit_code) from None
        if standalone_mode and isinstance(result, int) and result != 0:
            raise SystemExit(result)
        return result


def _requests_json(arguments: Sequence[str]) -> bool:
    return any(
        argument == "--format=json"
        or (
            argument == "--format" and index + 1 < len(arguments) and arguments[index + 1] == "json"
        )
        for index, argument in enumerate(arguments)
    )


def command_boundary(
    command: Literal["extract", "shrink"],
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Convert uncaught command failures to the public error format."""

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> None:
            output_format = kwargs.get("output_format", OutputFormat.HUMAN)
            if not isinstance(output_format, OutputFormat):
                output_format = OutputFormat.HUMAN
            try:
                function(*args, **kwargs)
            except typer.Exit:
                raise
            except OSError as error:
                fail(
                    f"filesystem operation failed: {error}",
                    ExitCode.INTERNAL,
                    output_format,
                    command,
                    "filesystem-error",
                )
            except Exception as error:
                fail(
                    f"unexpected internal error ({type(error).__name__})",
                    ExitCode.INTERNAL,
                    output_format,
                    command,
                    "internal-error",
                )

        return guarded

    return decorate


def render(report: CommandOutput, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return
    if report.command == "extract":
        _render_extract(report)
    else:
        _render_shrink(report)


def _render_extract(report: CommandOutput) -> None:
    print_plain(console, "Case created")
    print_plain(console, f"  path            {report.case_path}", soft_wrap=True)
    print_plain(console, f"  case digest     {report.case_digest[:12]}")
    print_plain(console, f"  context items   {report.original_context_count}")
    print_plain(console, "  No network requests or provider calls. No actions executed.")
    console.print()
    print_plain(console, "Next step")
    print_plain(console, f"  {report.next_action}")
    print_plain(
        console,
        f"  Command ({report.next_command_shell}): {report.next_command}",
        soft_wrap=True,
    )


def _render_shrink(report: CommandOutput) -> None:
    print_plain(console, _SHRINK_HEADINGS.get(report.status, report.status))
    print_plain(console, f"  case            {report.case_path}", soft_wrap=True)
    print_plain(console, f"  case digest     {report.case_digest[:12]}")
    _render_execution_details(report)
    _render_phase_results(report)
    print_plain(console, f"  candidates      {report.candidate_runs_used}")
    if report.max_candidate_runs is not None:
        print_plain(console, f"  candidate limit {report.max_candidate_runs}")
    print_plain(console, f"  observations    {report.observations_recorded}")
    print_plain(console, "  No network requests or provider calls. No actions executed.")
    if report.result_path is not None:
        print_plain(console, f"  result          {report.result_path}", soft_wrap=True)
    if report.run_state_path is not None:
        print_plain(console, f"  run state       {report.run_state_path}", soft_wrap=True)
    _render_next_step(report)


def _render_execution_details(report: CommandOutput) -> None:
    if report.runner_type is not None:
        print_plain(console, f"  runner          {report.runner_type} {report.runner_version}")
    if report.target_type is not None:
        print_plain(console, f"  target          {report.target_type}")
    if report.execution_plan_digest is not None:
        print_plain(console, f"  plan digest     {report.execution_plan_digest[:12]}")
    protected_count = len(report.protected_context_ids)
    protected_label = "context item" if protected_count == 1 else "context items"
    print_plain(console, f"  protected       {protected_count} {protected_label}")
    if report.original_context_count is not None:
        print_plain(
            console,
            f"  context items   {report.original_context_count} -> {report.retained_context_count}",
        )


def _render_phase_results(report: CommandOutput) -> None:
    phases = (
        ("original test", report.calibration_result, _CALIBRATION_LABELS),
        ("search", report.search_result, _SEARCH_LABELS),
        ("confirmation", report.confirmation_result, _CONFIRMATION_LABELS),
    )
    for name, result, labels in phases:
        if result is None:
            continue
        print_plain(
            console,
            f"  {name:<15} {labels.get(result, result)}",
        )


def _render_next_step(report: CommandOutput) -> None:
    if report.next_action is not None or report.next_command is not None:
        console.print()
        print_plain(console, "Next step")
        if report.next_action is not None:
            print_plain(console, f"  {report.next_action}")
        if report.next_command is not None:
            print_plain(
                console,
                f"  Command ({report.next_command_shell}): {report.next_command}",
                soft_wrap=True,
            )


def shell_command(arguments: tuple[str, ...]) -> tuple[str, Literal["posix", "powershell"]]:
    if os.name == "nt":
        quoted = " ".join(f"'{argument.replace("'", "''")}'" for argument in arguments)
        return f"& {quoted}", "powershell"
    return shlex.join(arguments), "posix"


def fail(
    message: str,
    code: ExitCode,
    output_format: OutputFormat,
    command: Literal["extract", "shrink"],
    error_code: str,
) -> Never:
    if output_format is OutputFormat.JSON:
        report = ErrorOutput(
            command=command,
            exit_code=code,
            error_code=error_code,
            message=message,
        )
        typer.echo(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    else:
        print_plain(error_console, message)
        print_plain(
            error_console,
            "No network requests or provider calls. No actions executed.",
        )
    raise typer.Exit(code)


def print_plain(target: Console, value: object, *, soft_wrap: bool = False) -> None:
    """Print data without Rich markup or terminal control characters."""

    safe = "".join(
        character if character.isprintable() else f"\\u{ord(character):04x}"
        for character in str(value)
    )
    target.print(Text(safe), soft_wrap=soft_wrap)
