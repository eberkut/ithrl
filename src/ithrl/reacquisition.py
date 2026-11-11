"""Exact, bounded comparison of recorded operations around native compaction."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ithrl.adapters.contracts import ParsingLimits
from ithrl.adapters.reacquisition import Boundary, CapturedResult, Operation, PassiveProjection
from ithrl.context import JsonValue, canonical_json_bytes, content_digest

MAX_REPORT_GROUPS = 1000
MAX_TEXT_BYTES = 65_536
_LIMITATIONS = (
    "recorded-operations-do-not-establish-resource-identity",
    "captured-results-do-not-establish-historical-resource-state",
    "captured-completeness-does-not-establish-full-tool-output",
    "repetition-does-not-establish-necessity-causality-or-savings",
    "normalized-and-heuristic-matching-not-assessed",
    "active-compaction-reconstruction-not-assessed",
)


def _encoded(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _operation_key(operation: Operation) -> str:
    return content_digest(
        {
            "tool": operation.tool,
            "arguments": operation.arguments,
            "workspace": operation.workspace_digest,
        }
    )


def _substantive(value: JsonValue) -> bool:
    """Only nonempty captured text supports a positive repeated-content comparison."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_substantive(item) for item in value)
    if isinstance(value, dict):
        # Native text containers only; arbitrary status/metadata objects are not content.
        return any(_substantive(value[key]) for key in ("text", "content") if key in value)
    return False


def _usable_results(operation: Operation) -> tuple[CapturedResult, ...]:
    return tuple(
        result
        for result in operation.results
        if result.completeness == "captured" and result.has_content and _substantive(result.value)
    )


def _eligible(operation: Operation) -> bool:
    return (
        operation.status == "completed"
        and operation.completion is not None
        and bool(_usable_results(operation))
    )


def _summary(operation: Operation) -> dict[str, Any]:
    return {
        "call": asdict(operation.call),
        "completion": None if operation.completion is None else asdict(operation.completion),
        "operation_digest": _operation_key(operation),
        "category": operation.category,
        "workspace_digest": operation.workspace_digest,
        "results": [
            {
                "scope": result.scope,
                "digest": content_digest(result.value),
                "canonical_utf8_bytes": len(canonical_json_bytes(result.value)),
                "completeness": result.completeness,
                "substantive_text": result.has_content and _substantive(result.value),
            }
            for result in operation.results
        ],
    }


def _result_relations(before: Operation, after: Operation) -> list[dict[str, str]]:
    preceding = {result.scope: result for result in before.results}
    following = {result.scope: result for result in after.results}
    relations = []
    for scope in sorted(preceding.keys() | following.keys()):
        left, right = preceding.get(scope), following.get(scope)
        relation = "unavailable"
        if (
            left is not None
            and right is not None
            and left.completeness == right.completeness == "captured"
            and left.has_content
            and right.has_content
            and _substantive(left.value)
            and _substantive(right.value)
        ):
            relation = (
                "equal"
                if content_digest(left.value) == content_digest(right.value)
                else "different"
            )
        relations.append({"scope": scope, "relation": relation})
    return relations


def compare_reacquisition(
    projection: PassiveProjection,
    *,
    boundary_id: str | None = None,
    max_groups: int = 100,
    show_text: bool = False,
    max_text_bytes: int = 4096,
) -> dict[str, Any]:
    """Compare adjacent source epochs without opening resources named by recorded tools."""
    if type(max_groups) is not int or not 1 <= max_groups <= MAX_REPORT_GROUPS:
        raise ValueError("invalid report group limit")
    if type(max_text_bytes) is not int or not 1 <= max_text_bytes <= MAX_TEXT_BYTES:
        raise ValueError("invalid report text limit")
    report: dict[str, Any] = {
        "schema_version": 1,
        "command": "inspect",
        "kind": "reacquisition",
        "harness": projection.harness,
        "profile": f"{projection.harness}-passive-compaction-v1",
        "source": projection.source.model_dump(mode="json"),
        "record_count": projection.record_count,
        "boundaries": [asdict(boundary) for boundary in projection.boundaries[:max_groups]],
        "boundary": None,
        "groups": [],
        "coverage": {
            "unknown_records": projection.unknown_records,
            "boundary_count": len(projection.boundaries),
            "boundaries_omitted": max(0, len(projection.boundaries) - max_groups),
        },
        "limitations": list(dict.fromkeys((*_LIMITATIONS, *projection.limitations))),
        "usage": {"status": "unknown", "savings": "not-estimated"},
        "text_requested": show_text,
        "text_bytes": 0,
        "text_truncated": False,
        "provider_calls": 0,
        "actions_executed": 0,
    }
    if boundary_id is None:
        return report
    boundary = next((b for b in projection.boundaries if b.id == boundary_id), None)
    if boundary is None:
        raise ValueError("selected compaction boundary unavailable")
    before, after, coverage = _adjacent_operations(projection, boundary)
    report["boundary"] = asdict(boundary)
    report["coverage"].update(coverage)
    groups, text_bytes, text_truncated, coverage = _match_groups(
        before, after, max_groups=max_groups, show_text=show_text, max_text_bytes=max_text_bytes
    )
    report.update(groups=groups, text_bytes=text_bytes, text_truncated=text_truncated)
    report["coverage"].update(coverage)
    return report


def _adjacent_operations(
    projection: PassiveProjection, boundary: Boundary
) -> tuple[list[Operation], list[Operation], dict[str, int]]:
    markers = [anchor.line for anchor in projection.barriers]
    markers += [b.end.line for b in projection.boundaries if b.end.line < boundary.start.line]
    left = max((line for line in markers if line < boundary.start.line), default=0)
    markers = [anchor.line for anchor in projection.barriers]
    markers += [b.start.line for b in projection.boundaries if b.start.line > boundary.end.line]
    right = min(
        (line for line in markers if line > boundary.end.line), default=projection.record_count + 1
    )
    before: list[Operation] = []
    after: list[Operation] = []
    excluded = 0
    for operation in projection.operations:
        completion = operation.completion
        if not _eligible(operation) or completion is None:
            excluded += 1
            continue
        if left < operation.call.line <= completion.line < boundary.start.line:
            before.append(operation)
        elif boundary.end.line < operation.call.line <= completion.line < right:
            after.append(operation)
    coverage = {
        "epoch_start_line": left + 1,
        "epoch_end_line": right - 1,
        "projected_operations": len(projection.operations),
        "eligible_before": len(before),
        "eligible_after": len(after),
        "ineligible_operations": excluded,
        "outside_adjacent_epochs_or_straddling": len(projection.operations)
        - excluded
        - len(before)
        - len(after),
    }
    return before, after, coverage


def _match_groups(
    before: list[Operation],
    after: list[Operation],
    *,
    max_groups: int,
    show_text: bool,
    max_text_bytes: int,
) -> tuple[list[dict[str, Any]], int, bool, dict[str, int | bool]]:
    operations: dict[str, Operation] = {}
    results: dict[tuple[str, str], Operation] = {}
    for operation in before:
        operations[_operation_key(operation)] = operation
        for result in _usable_results(operation):
            results[(result.scope, content_digest(result.value))] = operation
    groups: dict[str, dict[str, Any]] = {}
    seen_after: set[str] = set()
    omitted_operations: set[str] = set()
    omitted = 0
    text_remaining = max_text_bytes
    text_truncated = False
    for operation in after:
        key = _operation_key(operation)
        if key in groups:
            groups[key]["subsequent_after_occurrences"] += 1
            continue
        if key in seen_after:
            omitted += int(key in omitted_operations)
            continue
        seen_after.add(key)
        preceding = operations.get(key)
        exact = preceding is not None
        if preceding is None:
            candidates = [
                results[(result.scope, content_digest(result.value))]
                for result in _usable_results(operation)
                if (result.scope, content_digest(result.value)) in results
            ]
            preceding = max(candidates, key=lambda op: op.call.line, default=None)
        if preceding is None:
            continue
        if len(groups) == max_groups:
            omitted += 1
            omitted_operations.add(key)
            continue
        group: dict[str, Any] = {
            "before": _summary(preceding),
            "after": _summary(operation),
            "operation_relation": "exact-recorded-operation"
            if exact
            else "different-recorded-operation",
            "result_relations": _result_relations(preceding, operation),
            "resource_state": "unknown",
            "subsequent_after_occurrences": 0,
        }
        if show_text:
            details = []
            for item in (preceding, operation):
                value = _encoded(
                    {
                        "tool": item.tool,
                        "arguments": item.arguments,
                        "results": [
                            {"scope": result.scope, "value": result.value}
                            for result in item.results
                        ],
                    }
                )
                # ensure_ascii makes byte slicing safe and escapes terminal controls.
                text = value[:text_remaining]
                text_remaining -= len(text)
                text_truncated |= len(text) < len(value)
                details.append(text)
            group["escaped_detail"] = details
        groups[key] = group
    return (
        list(groups.values()),
        max_text_bytes - text_remaining,
        text_truncated,
        {
            "reported_groups": len(groups),
            "omitted_after_occurrences": omitted,
            "report_truncated": omitted > 0,
        },
    )


def inspect_reacquisition(
    source: Path,
    *,
    boundary_id: str | None = None,
    max_groups: int = 100,
    show_text: bool = False,
    max_text_bytes: int = 4096,
    limits: ParsingLimits | None = None,
) -> dict[str, Any]:
    """Read one immutable bounded source and inspect its native passive projection."""
    from ithrl.adapters.registry import project_reacquisition

    return compare_reacquisition(
        project_reacquisition(source, limits=limits),
        boundary_id=boundary_id,
        max_groups=max_groups,
        show_text=show_text,
        max_text_bytes=max_text_bytes,
    )
