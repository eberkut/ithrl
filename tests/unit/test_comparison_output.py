"""Comparison reports omit unsafe identities and expose distinct time ceilings."""

from io import StringIO

import pytest
from rich.console import Console

from ithrl.cli.comparison_output import (
    ComparisonOutput,
    render_comparison,
    safe_display_identifier,
)
from ithrl.cli.output import OutputFormat
from ithrl.comparison.contracts import ComparisonLimits


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("provider/model-1.0:default", "provider/model-1.0:default"),
        ("local model", None),
        ("model\nprivate", None),
        ("", None),
        ("m" * 257, None),
    ],
)
def test_display_identifier_omits_unsafe_values(value: str | None, expected: str | None) -> None:
    assert safe_display_identifier(value) == expected


def test_human_report_distinguishes_planned_time_from_configured_allowance() -> None:
    output = StringIO()
    report = ComparisonOutput(
        status="ready",
        planned_wall_time_seconds=44,
        limits=ComparisonLimits(
            max_provider_calls=0,
            max_wall_time_seconds=120,
            attempt_timeout_seconds=1,
        ),
    )
    render_comparison(report, OutputFormat.HUMAN, target=Console(file=output, width=120))
    assert "planned wall time        44s" in output.getvalue()
    assert "wall-time ceiling        120s" in output.getvalue()
