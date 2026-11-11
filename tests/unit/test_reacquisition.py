"""Passive reports preserve native evidence relations without inferring causality."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from ithrl.adapters.contracts import SourceFacts
from ithrl.adapters.reacquisition import (
    Anchor,
    Boundary,
    CapturedResult,
    Operation,
    PassiveProjection,
)
from ithrl.context import JsonValue, content_digest
from ithrl.reacquisition import compare_reacquisition


def _anchor(line: int) -> Anchor:
    return Anchor(line=line, digest=content_digest({"line": line}))


def _boundary(start: int, end: int | None = None) -> Boundary:
    anchor = _anchor(start)
    return Boundary(id=f"boundary-{anchor.digest}", start=anchor, end=_anchor(end or start))


def _operation(
    line: int,
    *,
    argument: str = "private/source.txt",
    result: JsonValue = "captured text",
    completion: int | None = None,
) -> Operation:
    return Operation(
        call=_anchor(line),
        completion=_anchor(completion if completion is not None else line + 1),
        tool="view",
        arguments={"path": argument},
        results=(CapturedResult(scope="content", value=result),),
        category="file-read",
    )


def _projection(
    *operations: Operation,
    boundaries: tuple[Boundary, ...] | None = None,
    barriers: tuple[Anchor, ...] = (),
) -> PassiveProjection:
    return PassiveProjection(
        source=SourceFacts(sha256="a" * 64, size_bytes=4096),
        harness="copilot",
        producer_version="test-producer",
        record_count=40,
        boundaries=boundaries if boundaries is not None else (_boundary(10),),
        operations=operations,
        barriers=barriers,
    )


def _report(projection: PassiveProjection, **options: Any) -> dict[str, Any]:
    return compare_reacquisition(projection, boundary_id=projection.boundaries[0].id, **options)


def test_boundary_listing_needs_no_operation_selection() -> None:
    projection = _projection(_operation(2), boundaries=(_boundary(10, 12), _boundary(30)))
    report = compare_reacquisition(projection)
    assert [boundary["start"]["line"] for boundary in report["boundaries"]] == [10, 30]
    assert report["boundary"] is None
    assert report["groups"] == []
    assert report["source"]["sha256"] == "a" * 64
    assert report["provider_calls"] == report["actions_executed"] == 0
    with pytest.raises(ValueError, match="boundary unavailable"):
        compare_reacquisition(projection, boundary_id="not-a-native-boundary")


def test_boundary_listing_honors_limit_and_reports_omitted_boundaries() -> None:
    projection = _projection(boundaries=(_boundary(10), _boundary(20), _boundary(30)))
    report = compare_reacquisition(projection, max_groups=1)
    assert len(report["boundaries"]) == 1
    assert report["coverage"]["boundary_count"] == 3
    assert report["coverage"]["boundaries_omitted"] == 2
    # A known source-bound selection remains selectable even outside the listing window.
    selected = compare_reacquisition(
        projection, boundary_id=projection.boundaries[2].id, max_groups=1
    )
    assert selected["boundary"]["start"]["line"] == 30


def test_pairs_first_after_with_nearest_predecessor_and_counts_later_repeats() -> None:
    report = _report(
        _projection(
            _operation(1, result="old value"),
            _operation(6, result="new value"),
            _operation(12, result="new value"),
            _operation(16, result="changed again"),
        )
    )
    (group,) = report["groups"]
    assert group["before"]["call"]["line"] == 6
    assert group["after"]["call"]["line"] == 12
    assert group["operation_relation"] == "exact-recorded-operation"
    assert group["result_relations"] == [{"scope": "content", "relation": "equal"}]
    assert group["subsequent_after_occurrences"] == 1
    assert group["resource_state"] == "unknown"


def test_same_operation_with_changed_capture_is_not_reported_as_redundant() -> None:
    report = _report(_projection(_operation(2, result="before"), _operation(12, result="after")))
    (group,) = report["groups"]
    assert group["operation_relation"] == "exact-recorded-operation"
    assert group["result_relations"] == [{"scope": "content", "relation": "different"}]
    assert group["resource_state"] == "unknown"
    assert report["usage"] == {"status": "unknown", "savings": "not-estimated"}


def test_captured_json_types_are_not_coerced_by_python_equality() -> None:
    before = _operation(2, result={"text": "same", "flag": True})
    after = _operation(12, result={"text": "same", "flag": 1})
    (group,) = _report(_projection(before, after))["groups"]
    assert group["result_relations"] == [{"scope": "content", "relation": "different"}]


def test_equal_capture_under_different_arguments_keeps_known_operation_difference() -> None:
    report = _report(
        _projection(
            _operation(2, argument="private/first.txt"),
            _operation(12, argument="private/second.txt"),
        )
    )
    (group,) = report["groups"]
    assert group["operation_relation"] == "different-recorded-operation"
    assert group["result_relations"] == [{"scope": "content", "relation": "equal"}]


def test_native_content_and_display_scopes_are_compared_independently() -> None:
    before = replace(
        _operation(2),
        results=(
            CapturedResult("content", "same capture"),
            CapturedResult("detailedContent", "old display"),
            CapturedResult("additional", "only before"),
        ),
    )
    after = replace(
        _operation(12),
        results=(
            CapturedResult("content", "same capture"),
            CapturedResult("detailedContent", "new display"),
        ),
    )
    report = _report(_projection(before, after))
    assert report["groups"][0]["result_relations"] == [
        {"scope": "additional", "relation": "unavailable"},
        {"scope": "content", "relation": "equal"},
        {"scope": "detailedContent", "relation": "different"},
    ]


def test_different_native_scopes_cannot_supply_equal_result_fallback() -> None:
    before = _operation(2)
    after = replace(
        _operation(12, argument="other"),
        results=(CapturedResult("detailedContent", "captured text"),),
    )
    assert _report(_projection(before, after))["groups"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        replace(_operation(2), status="failed"),
        replace(_operation(2), status="incomplete"),
        replace(_operation(2), completion=None),
        _operation(2, result=""),
        _operation(2, result=" \n\t"),
        _operation(2, result={"status": "success", "exitCode": 0}),
        _operation(
            2,
            result="Chunk ID: fixture\nWall time: 0.1 seconds\nProcess exited with code 0\nFinal output:\n",
        ),
        replace(_operation(2), results=(CapturedResult("content", "text", "truncated"),)),
        replace(_operation(2), results=(CapturedResult("content", "text", "unknown"),)),
    ],
)
def test_failed_missing_empty_status_only_and_incomplete_outputs_do_not_match(
    invalid: Operation,
) -> None:
    report = _report(_projection(invalid, _operation(12)))
    assert report["groups"] == []
    assert report["coverage"]["ineligible_operations"] == 1


def test_one_truncated_scope_does_not_hide_usable_independent_capture() -> None:
    before = replace(
        _operation(2),
        results=(
            CapturedResult("content", "full captured field"),
            CapturedResult("detailedContent", "partial", "truncated"),
        ),
    )
    after = replace(
        _operation(12),
        results=(
            CapturedResult("content", "full captured field"),
            CapturedResult("detailedContent", "partial", "truncated"),
        ),
    )
    report = _report(_projection(before, after))
    assert report["groups"][0]["result_relations"] == [
        {"scope": "content", "relation": "equal"},
        {"scope": "detailedContent", "relation": "unavailable"},
    ]


def test_only_complete_operations_in_immediately_adjacent_epochs_are_compared() -> None:
    selected = _boundary(10, 12)
    projection = _projection(
        _operation(1),  # Ends before previous boundary.
        _operation(4, completion=6),  # Straddles previous boundary.
        _operation(8, completion=13),  # Straddles selected compaction.
        _operation(14, completion=15),
        _operation(18, completion=22),  # Straddles next boundary.
        _operation(24),
        boundaries=(_boundary(5), selected, _boundary(20)),
    )
    report = compare_reacquisition(projection, boundary_id=selected.id)
    assert report["groups"] == []
    assert report["coverage"]["epoch_start_line"] == 6
    assert report["coverage"]["epoch_end_line"] == 19
    assert report["coverage"]["eligible_before"] == 0
    assert report["coverage"]["eligible_after"] == 1
    assert report["coverage"]["outside_adjacent_epochs_or_straddling"] == 5


def test_unverified_compaction_barriers_limit_both_adjacent_epochs() -> None:
    report = _report(
        _projection(
            _operation(2),
            _operation(7),
            _operation(12),
            _operation(18),
            barriers=(_anchor(6), _anchor(17)),
        )
    )
    (group,) = report["groups"]
    assert group["before"]["call"]["line"] == 7
    assert group["after"]["call"]["line"] == 12
    assert report["coverage"]["epoch_start_line"] == 7
    assert report["coverage"]["epoch_end_line"] == 16


def test_workspace_identity_is_part_of_exact_operation_matching() -> None:
    before = replace(_operation(2, result="old"), workspace_digest="b" * 64)
    after = replace(_operation(12, result="new"), workspace_digest="c" * 64)
    assert _report(_projection(before, after))["groups"] == []


def test_default_output_and_reprs_exclude_private_recorded_values() -> None:
    operation = replace(
        _operation(2, argument="private-argument", result="private-captured-content"),
        tool="private-tool-name",
    )
    projection = _projection(
        operation, replace(operation, call=_anchor(12), completion=_anchor(13))
    )
    report = _report(projection)
    rendered = json.dumps(report) + repr(projection)
    for private in (
        "private-argument",
        "private-captured-content",
        "private-tool-name",
        "test-producer",
    ):
        assert private not in rendered
    assert "escaped_detail" not in report["groups"][0]
    assert report["text_bytes"] == 0
    assert report["text_requested"] is False


def test_opt_in_detail_is_globally_bounded_and_terminal_controls_are_escaped() -> None:
    projection = _projection(
        _operation(2, result="private\x1b[31m\nβ"),
        _operation(12, result="private\x1b[31m\nβ"),
    )
    full = _report(projection, show_text=True, max_text_bytes=4096)
    (group,) = full["groups"]
    text = "".join(group["escaped_detail"])
    assert "\\u001b" in text
    assert "\\n" in text
    assert "\\u03b2" in text
    assert "\x1b" not in text
    assert "\n" not in text
    assert full["text_bytes"] == len(text.encode("utf-8"))
    limited = _report(projection, show_text=True, max_text_bytes=23)
    assert sum(len(part.encode("utf-8")) for part in limited["groups"][0]["escaped_detail"]) <= 23
    assert limited["text_bytes"] == 23
    assert limited["text_truncated"] is True


def test_group_limit_reports_omitted_matches_without_claiming_complete_inventory() -> None:
    report = _report(
        _projection(
            _operation(1, argument="first"),
            _operation(3, argument="second"),
            _operation(12, argument="first"),
            _operation(14, argument="second"),
        ),
        max_groups=1,
    )
    assert len(report["groups"]) == 1
    assert report["coverage"]["reported_groups"] == 1
    assert report["coverage"]["omitted_after_occurrences"] == 1
    assert report["coverage"]["report_truncated"] is True


@pytest.mark.parametrize(
    "options",
    [
        {"max_groups": 0},
        {"max_groups": 1001},
        {"max_groups": True},
        {"max_text_bytes": 0},
        {"max_text_bytes": 65_537},
        {"max_text_bytes": True},
    ],
)
def test_invalid_output_limits_refuse(options: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="invalid report"):
        _report(_projection(), **options)


def test_later_result_equality_cannot_substitute_for_first_after_occurrence() -> None:
    report = _report(
        _projection(
            _operation(2, argument="before", result="earlier capture"),
            _operation(12, argument="after", result="different first capture"),
            _operation(16, argument="after", result="earlier capture"),
        )
    )
    assert report["groups"] == []
