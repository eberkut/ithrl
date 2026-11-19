"""Generate and inspect provider-free token-cost reports."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer

from ithrl.cli.output import ExitCode, OutputFormat, command_boundary, console, fail, print_plain

if TYPE_CHECKING:
    from ithrl.cost_reporting import CostReport


@command_boundary("cost")
def cost(
    observations: Annotated[
        Path | None, typer.Argument(help="Existing observation database; omit with --scenario.")
    ] = None,
    *,
    pricing: Annotated[
        Path, typer.Option("--pricing", help="Private JSON reference-pricing specification.")
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output", help="New private cost report; existing files are not overwritten."
        ),
    ],
    case_digest: Annotated[
        str | None,
        typer.Option("--case-digest", help="Exact case digest for recorded observations."),
    ] = None,
    experiment_digest: Annotated[
        str | None,
        typer.Option(
            "--experiment-digest", help="Exact experiment digest for recorded observations."
        ),
    ] = None,
    scenario: Annotated[
        Path | None, typer.Option("--scenario", help="Explicit hypothetical token counts as JSON.")
    ] = None,
    max_observations: Annotated[
        int, typer.Option("--max-observations", min=1, max=10_000)
    ] = 10_000,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.HUMAN,
) -> None:
    """Estimate recorded or hypothetical token costs using explicit reference rates."""
    from ithrl.case.filesystem import read_safe_text
    from ithrl.cost_reporting import hypothetical_report, recorded_report, write_cost_report
    from ithrl.costs import CostUsage, PriceBook, PricingSpec
    from ithrl.observations import ObservationStore
    from ithrl.results.validation import loads_json

    if scenario is not None:
        if observations is not None or case_digest is not None or experiment_digest is not None:
            fail(
                "Use --scenario alone; omit the database, --case-digest and --experiment-digest.",
                ExitCode.USAGE,
                output_format,
                "cost",
                "cost-input-mode",
            )
    elif observations is None or case_digest is None or experiment_digest is None:
        fail(
            "Recorded costs require a database, --case-digest and --experiment-digest; "
            "use --scenario for hypothetical counts.",
            ExitCode.USAGE,
            output_format,
            "cost",
            "cost-input-mode",
        )

    try:
        source = read_safe_text(pricing, max_bytes=1_048_576, label="pricing specification")
        loads_json(source, label="pricing specification")
        spec = PricingSpec.model_validate_json(source)
        if scenario is None and spec.recorded_source is None:
            raise ValueError("recorded costs require recorded_source in the pricing specification")
        book = PriceBook(spec)
    except (OSError, ValueError, RuntimeError):
        fail(
            "Cannot resolve --pricing. Check private file permissions, JSON fields, exact "
            "provider/model identifiers, reference time and context; recorded costs also require recorded_source.",
            ExitCode.USAGE,
            output_format,
            "cost",
            "cost-pricing-invalid",
        )

    if scenario is not None:
        try:
            usage_source = read_safe_text(scenario, max_bytes=65_536, label="hypothetical usage")
            loads_json(usage_source, label="hypothetical usage")
            report = hypothetical_report(CostUsage.model_validate_json(usage_source), book)
        except (OSError, ValueError, RuntimeError):
            fail(
                "Cannot estimate --scenario. Check private file permissions, JSON token counts "
                "and cache/reasoning subsets.",
                ExitCode.USAGE,
                output_format,
                "cost",
                "cost-usage-invalid",
            )
    else:
        try:
            report = recorded_report(
                ObservationStore(cast(Path, observations)),
                case_digest=cast(str, case_digest),
                experiment_digest=cast(str, experiment_digest),
                book=book,
                limit=max_observations,
            )
        except (OSError, ValueError, RuntimeError):
            fail(
                "Cannot read recorded usage. Check the private rollback-journal database, "
                "case/experiment digests, row limit and consistency of the recorded evidence.",
                ExitCode.USAGE,
                output_format,
                "cost",
                "cost-source-invalid",
            )

    try:
        write_cost_report(output, report)
    except (OSError, ValueError, RuntimeError):
        fail(
            "Cannot save --output. Choose a new file in a writable private directory; "
            "existing files are not overwritten. The report must fit its byte limit.",
            ExitCode.USAGE,
            output_format,
            "cost",
            "cost-output-invalid",
        )
    render_cost_report(report, output_format)


def render_cost_report(report: CostReport, output_format: OutputFormat) -> None:
    digest, summary = report.digest, report.summary()
    if output_format is OutputFormat.JSON:
        data = {
            "schema_version": 1,
            "command": "cost",
            "kind": "token-cost-estimate",
            "mode": report.mode,
            "report_digest": digest,
            "pricing": report.pricing.model_dump(mode="json"),
            "summary": summary,
            "observations": [row.model_dump(mode="json") for row in report.observations[:20]],
            "observations_omitted": max(0, len(report.observations) - 20),
            "scenario": report.scenario.model_dump(mode="json")
            if report.scenario is not None
            else None,
            "provider_calls": 0,
            "actions_executed": 0,
        }
        typer.echo(json.dumps(data, ensure_ascii=True, sort_keys=True))
        return
    print_plain(console, f"Token cost estimate ({report.mode})")
    print_plain(
        console,
        f"  pricing: {report.pricing.provider}/{report.pricing.model}; {report.pricing.semantics}",
    )
    print_plain(console, f"  reference time: {report.pricing.as_of.isoformat()}; currency: USD")
    for key, value in summary.items():
        print_plain(console, f"  {key}: {'unknown' if value is None else value}")
    charges = (
        (report.scenario,)
        if report.scenario is not None
        else (row.charges for row in report.observations if row.charges is not None)
    )
    unpriced = Counter(
        (component.dimension, component.reason)
        for charge in charges
        for component in charge.components
        if component.reason is not None
    )
    if unpriced:
        print_plain(console, "  Unpriced token dimensions:")
        for (dimension, reason), count in sorted(unpriced.items()):
            occurrences = f" (observations: {count})" if report.scenario is None else ""
            print_plain(console, f"    {dimension}: {reason}{occurrences}")
    print_plain(console, f"  report SHA-256: {digest}")
