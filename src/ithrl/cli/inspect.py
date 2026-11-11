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


@app.command("reacquire")
@command_boundary("inspect")
def reacquire(
    session: Annotated[Path, typer.Argument(help="Explicit saved Codex or Copilot JSONL source.")],
    boundary: Annotated[
        str | None, typer.Option("--boundary", help="Boundary ID; omit to list compactions.")
    ] = None,
    max_groups: Annotated[
        int,
        typer.Option(
            "--max-groups", min=1, max=1000, help="Maximum reported groups or boundaries."
        ),
    ] = 100,
    show_text: Annotated[
        bool,
        typer.Option("--show-text", help="Include bounded escaped detail; keep output private."),
    ] = False,
    max_text_bytes: Annotated[
        int,
        typer.Option(
            "--max-text-bytes", min=1, max=65_536, help="Total escaped detail byte limit."
        ),
    ] = 4096,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Human-readable or JSON output.")
    ] = OutputFormat.HUMAN,
) -> None:
    """Inspect recorded operations and results across compaction without model access."""
    from ithrl.adapters.errors import AdapterError
    from ithrl.reacquisition import inspect_reacquisition

    try:
        data = inspect_reacquisition(
            session,
            boundary_id=boundary,
            max_groups=max_groups,
            show_text=show_text,
            max_text_bytes=max_text_bytes,
        )
    except AdapterError as error:
        fail(
            "Cannot inspect this saved source under the passive profile.",
            ExitCode.USAGE,
            output_format,
            "inspect",
            error.code.value,
        )
    except (OSError, ValueError):
        fail(
            "Cannot inspect the source or selected compaction boundary.",
            ExitCode.USAGE,
            output_format,
            "inspect",
            "reacquisition-invalid",
        )
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(data, ensure_ascii=True, sort_keys=True))
    else:
        _render_reacquisition(data)


def _render_reacquisition(data: dict[str, Any]) -> None:
    print_plain(console, "Passive compaction inspection (qualification pending)")
    print_plain(
        console, f"  harness: {data['harness']}; source SHA-256: {data['source']['sha256']}"
    )
    if data["boundary"] is None:
        for boundary in data["boundaries"]:
            print_plain(
                console,
                f"  {boundary['id']}: lines {boundary['start']['line']}-{boundary['end']['line']}",
            )
        print_plain(console, "Select a boundary with --boundary ID to compare adjacent epochs.")
    else:
        print_plain(console, f"  boundary: {data['boundary']['id']}")
        for group in data["groups"]:
            print_plain(
                console,
                f"  call lines {group['before']['call']['line']} → {group['after']['call']['line']}: {group['operation_relation']}",
            )
            for result in group["result_relations"]:
                print_plain(console, f"    {result['scope']}: {result['relation']}")
            print_plain(
                console,
                f"    later occurrences: {group['subsequent_after_occurrences']}; resource state: unknown",
            )
            for detail in group.get("escaped_detail", ()):
                print_plain(console, f"    private detail: {detail}", soft_wrap=True)
        if not data["groups"]:
            print_plain(console, "  No eligible matching groups in the selected adjacent epochs.")
    print_plain(console, f"  coverage: {json.dumps(data['coverage'], sort_keys=True)}")
    if data["text_requested"]:
        print_plain(
            console, f"  text bytes: {data['text_bytes']}; truncated: {data['text_truncated']}"
        )
    print_plain(console, "  usage: unknown; savings: not estimated")
    for limitation in data["limitations"]:
        print_plain(console, f"  limitation: {limitation}")


@app.command("sections")
@command_boundary("inspect")
def sections(
    case: Annotated[Path, typer.Argument(help="Case containing the original evidence.")],
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Human-readable or JSON output.")
    ] = OutputFormat.HUMAN,
) -> None:
    """List eligible section IDs and half-open Unicode codepoint offsets without text.

    Section sizes are UTF-8 byte counts.
    """

    from ithrl.case import CaseWorkspace
    from ithrl.extraction import load_extracted_case

    try:
        evidence = load_extracted_case(CaseWorkspace.open(case))
        catalogs = evidence.text_sections()
        available = {(catalog.context_id, catalog.content_index) for catalog in catalogs}
        unavailable = [
            {
                "context_id": site.context_id,
                "content_index": site.content_index,
                "reason": "section-policy-limit",
            }
            for site in evidence.editable_text()
            if (site.context_id, site.content_index) not in available
        ]
        data = {
            "schema_version": 1,
            "command": "inspect",
            "kind": "sections",
            "case_digest": evidence.digest,
            "fields": [catalog.model_dump(mode="json") for catalog in catalogs],
            "unavailable_fields": unavailable,
            "provider_calls": 0,
            "actions_executed": 0,
        }
    except (OSError, ValueError):
        fail(
            "Cannot inspect case section catalogs. Check the case and its private paths.",
            ExitCode.USAGE,
            output_format,
            "inspect",
            "inspection-invalid",
        )
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(data, ensure_ascii=True, sort_keys=True))
    else:
        print_plain(console, "Case text sections (untested)")
        for catalog in catalogs:
            print_plain(console, f"  {catalog.context_id}, content index {catalog.content_index}")
            for section in catalog.sections:
                print_plain(console, f"    {section.id}: {section.model_dump_json()}")
        for field in unavailable:
            print_plain(console, f"  unavailable: {json.dumps(field, ensure_ascii=True)}")
        print_plain(console, "No harness probes, provider calls or actions were executed.")


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
