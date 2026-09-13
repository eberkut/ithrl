from __future__ import annotations

from pathlib import Path

import pytest

from ithrl.adapters import enumerate_decisions, parse_session, select_decision

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    ("fixture", "harness"),
    [
        ("codex-session/rollout.jsonl", "codex"),
        ("copilot-session/events.jsonl", "copilot"),
    ],
)
def test_native_decision_adapter_contract(fixture: str, harness: str) -> None:
    session = parse_session(FIXTURES / fixture)

    first = enumerate_decisions(session)
    second = enumerate_decisions(session)

    assert first
    assert first == second
    assert len({summary.decision_id for summary in first}) == len(first)
    for summary in first:
        assert summary.completeness == "complete"
        assert summary.harness == harness
        assert summary.targets
        assert len({target.target_id for target in summary.targets}) == len(summary.targets)
        assert all(target.evidence_support == "supported" for target in summary.targets)

        selected = select_decision(
            session,
            decision_id=summary.decision_id,
            target_id=summary.targets[0].target_id,
        )
        assert selected.evidence.decision_id == summary.decision_id
        assert selected.evidence.selected_target_id == summary.targets[0].target_id
        assert selected.context.nodes
        source_lines = tuple(
            node.selector.line for node in selected.context.nodes if node.selector is not None
        )
        assert source_lines == tuple(sorted(source_lines))
        assert len(source_lines) == len(set(source_lines))
        assert all(line < selected.evidence.selector.context_before_line for line in source_lines)
        assert all(
            node.source == selected.evidence.selector.source for node in selected.context.nodes
        )
