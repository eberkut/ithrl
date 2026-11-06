"""Provider-free inspection of saved reduction evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ithrl.cli.output import (
    ExitCode,
    JsonAwareGroup,
    OutputFormat,
    command_boundary,
    console,
    fail,
    print_plain,
)

app = typer.Typer(
    cls=JsonAwareGroup,
    help="Inspect saved experimental evidence without running a harness.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.command("retained")
@command_boundary("inspect")
def retained(
    case: Annotated[Path, typer.Argument(help="Case containing the original evidence.")],
    result: Annotated[
        Path,
        typer.Option("--result", help="Saved deterministic or sampled reduction result directory."),
    ],
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Human-readable or JSON output.")
    ] = OutputFormat.HUMAN,
    show_text: Annotated[
        bool,
        typer.Option("--show-text", help="Include bounded retained content; keep output private."),
    ] = False,
    text_node: Annotated[
        str | None,
        typer.Option(
            "--text-node", help="Display text only for this retained unit ID; requires --show-text."
        ),
    ] = None,
    max_text_bytes: Annotated[
        int,
        typer.Option(
            "--max-text-bytes",
            min=1,
            max=65_536,
            help="Maximum total UTF-8 bytes of escaped retained content with --show-text.",
        ),
    ] = 4096,
) -> None:
    """Explain retained context and recorded removal trials without collecting observations."""

    from ithrl.inspection import inspect_retained

    if text_node is not None and not show_text:
        fail(
            "--text-node requires --show-text.",
            ExitCode.USAGE,
            output_format,
            "inspect",
            "invalid-inspection-options",
        )
    try:
        report = inspect_retained(
            case,
            result,
            show_text=show_text,
            text_node_id=text_node,
            max_text_bytes=max_text_bytes,
        )
    except (OSError, ValueError):
        fail(
            "Cannot inspect the supplied case and reduction result. Check their format, "
            "evidence identity and private-path permissions.",
            ExitCode.USAGE,
            output_format,
            "inspect",
            "inspection-invalid",
        )
    data = report.model_dump(mode="json")
    if not show_text:
        for node in data["nodes"]:
            node.pop("text", None)
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(data, ensure_ascii=True, sort_keys=True))
    else:
        _render(data)


def _render(data: dict[str, Any]) -> None:
    print_plain(console, "Retained context inspection")
    for key, value in data.items():
        if key in {
            "schema_version",
            "command",
            "kind",
            "nodes",
            "trials",
            "section_trials",
            "limitations",
            "provider_calls",
            "actions_executed",
        }:
            continue
        if key in {"active_comparison", "section_search"} and value is None:
            continue
        if (
            key in {"text_requested", "text_node_id", "text_bytes", "text_truncated"}
            and not data["text_requested"]
        ):
            continue
        label = key.replace("_", " ")
        if isinstance(value, dict):
            print_plain(console, f"  {label}")
            for name, detail in value.items():
                print_plain(console, f"    {name.replace('_', ' ')}: {_display(detail)}")
        else:
            print_plain(console, f"  {label}: {_display(value)}")
    for heading, key in (
        ("Retained units", "nodes"),
        ("Removal trials", "trials"),
        ("Text-section trials", "section_trials"),
    ):
        print_plain(console, heading)
        rows = data.get(key, [])
        if not rows:
            print_plain(console, "  none recorded")
        for row in rows:
            print_plain(
                console,
                "  "
                + "; ".join(
                    f"{name.replace('_', ' ')}: {_display(value)}"
                    for name, value in row.items()
                    if name != "text_section_fields"
                ),
                soft_wrap=True,
            )
            for section_field in row.get("text_section_fields", []):
                catalog = section_field["catalog"]
                detail = (
                    f"{len(catalog['sections'])} units; "
                    f"original UTF-8 bytes: {catalog['original_utf8_bytes']}; "
                    f"retained UTF-8 bytes: {section_field['retained_utf8_bytes']}"
                    if catalog is not None
                    else section_field["unavailable_reason"]
                )
                print_plain(
                    console,
                    f"    text block {section_field['content_index']}: "
                    f"{section_field['status']}; {detail}",
                )
                if section_field["removed_section_ids"]:
                    print_plain(
                        console,
                        "      removed sections: "
                        + ", ".join(section_field["removed_section_ids"]),
                    )
    print_plain(console, "Interpretation limits")
    for note in data.get("limitations", []):
        print_plain(console, f"  {note}")
    print_plain(console, "No harness probes, provider calls or actions were executed.")


def _display(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)
