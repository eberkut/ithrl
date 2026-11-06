from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from ithrl.adapters import parse_session
from ithrl.adapters.contracts import NativeSession
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import SourceRecordSelector
from ithrl.decisions import DecisionSelector

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture(params=["codex", "copilot"])
def selected(request: pytest.FixtureRequest) -> tuple[ModuleType, NativeSession, DecisionSelector]:
    harness = request.param
    source = "codex-session/rollout.jsonl" if harness == "codex" else "copilot-session/events.jsonl"
    session = parse_session(FIXTURES / source)
    context = import_module(f"ithrl.adapters.{harness}.context")
    decisions = import_module(f"ithrl.adapters.{harness}.decisions")
    return context, session, decisions.enumerate_decisions(session)[0].selector


@pytest.mark.parametrize(
    "mismatch", ["harness", "session", "source", "line", "native-identity", "digest"]
)
def test_context_selector_refuses_each_identity_mismatch_without_changing_source(
    selected: tuple[ModuleType, NativeSession, DecisionSelector], mismatch: str
) -> None:
    context, session, selector = selected
    before = (session.native_bytes, session.records)
    if mismatch == "harness":
        changed = selector.model_copy(update={"harness": "other"})
    elif mismatch == "session":
        changed = selector.model_copy(update={"session_id": "other"})
    elif mismatch == "source":
        changed = selector.model_copy(update={"source": "source/other.jsonl"})
    elif mismatch == "line":
        changed = selector.model_copy(
            update={"record": SourceRecordSelector(line=session.record_count + 1)}
        )
    elif mismatch == "native-identity":
        changed = selector.model_copy(
            update={"record": SourceRecordSelector(line=selector.record.line)}
        )
    else:
        changed = selector.model_copy(update={"record_digest": "0" * 64})

    with pytest.raises(AdapterError) as captured:
        context.compile_context(session, changed)
    assert captured.value.code == AdapterErrorCode.DECISION_NOT_FOUND
    assert str(captured.value) == "decision selector does not match the selected session"
    assert (session.native_bytes, session.records) == before


def test_context_selector_checks_session_binding_before_native_record_selection(
    selected: tuple[ModuleType, NativeSession, DecisionSelector], monkeypatch: pytest.MonkeyPatch
) -> None:
    context, session, selector = selected

    def forbidden(*_args: object) -> None:
        raise AssertionError("native selector was evaluated before session binding")

    monkeypatch.setattr(context, "source_selector", forbidden)
    changed = selector.model_copy(update={"harness": "other"})
    with pytest.raises(AdapterError) as captured:
        context.compile_context(session, changed)
    assert captured.value.code == AdapterErrorCode.DECISION_NOT_FOUND
