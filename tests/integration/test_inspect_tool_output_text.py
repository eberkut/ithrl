"""Provider-free tool output text discovery and span authoring through the normal CLI."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseFormatError, CaseWorkspace, SourceArtifact
from ithrl.cli import app
from ithrl.cli.comparison_output import result_report
from ithrl.comparison.results import write_comparison_result
from ithrl.extraction import extract_decision
from ithrl.inspection import inspect_tool_output_text
from ithrl.observations import ObservationStore
from ithrl.text_sections import MAX_TEXT_SECTION_BYTES
from ithrl.tool_output_text import ToolOutputTextField
from native_support.codex import _tool_output_text_records as codex_tool_output_text_records
from native_support.comparison import _TOOL_OUTPUT_TEXT, _tool_output_text_fixture
from native_support.copilot import _tool_output_text_records as copilot_tool_output_text_records

_PRIVATE = "PRIVATE_TOOL_SENTINEL"
_TEXT = f"pré {_PRIVATE} é🙂\r\n[tail]\n\\u000a"


@pytest.fixture(params=["codex", "copilot"])
def tool_case(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, CaseEvidence]:
    harness = request.param
    filename = "rollout.jsonl" if harness == "codex" else "events.jsonl"
    source = tmp_path / filename
    rows = (
        codex_tool_output_text_records(_TEXT)
        if harness == "codex"
        else copilot_tool_output_text_records(_TEXT, "final-response")
    )
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    session = parse_session(source)
    decision = enumerate_decisions(session)[-1]
    target = next(target for target in decision.targets if target.type == "final-response")
    case = tmp_path / "case"
    extract_decision(
        session, decision_id=decision.decision_id, target_id=target.target_id, case_path=case
    )
    return case, CaseWorkspace.open(case).load()


def test_tool_output_text_discovery_is_private_offline_and_leaves_source_unchanged(
    tool_case: tuple[Path, CaseEvidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, evidence = tool_case
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inspection attempted process, network or observation store access")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(ObservationStore, "__init__", forbidden)
    for output_format in ("json", "human"):
        result = CliRunner().invoke(app, ["inspect", "tool", str(case), "--format", output_format])
        assert result.exit_code == 0, result.output
        assert _PRIVATE not in result.output
        if output_format == "json":
            report = json.loads(result.stdout)
            assert report["kind"] == "tool-output-text"
            assert report["case_digest"] == evidence.digest
            assert report["fields"] == [
                field.model_dump(mode="json") for field in evidence.tool_output_text_fields()
            ]
            assert report["provider_calls"] == report["actions_executed"] == 0
            assert report["selection"] is None
            assert report["omitted_field_count"] == 0
    assert _PRIVATE not in repr(inspect_tool_output_text(evidence))
    assert before == {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}


def test_tool_output_text_span_authors_bound_mutation_and_shows_only_retained_text(
    tool_case: tuple[Path, CaseEvidence],
) -> None:
    case, evidence = tool_case
    (field,) = evidence.tool_output_text_fields()
    start, end = _TEXT.index(_PRIVATE), _TEXT.index(_PRIVATE) + len(_PRIVATE)
    args = [
        "inspect",
        "tool",
        str(case),
        "--field",
        field.field_id,
        "--start",
        str(start),
        "--end",
        str(end),
        "--format",
        "json",
    ]
    metadata = CliRunner().invoke(app, args)
    assert metadata.exit_code == 0, metadata.output
    report = json.loads(metadata.stdout)
    edit = evidence.tool_output_text_removal(field.field_id, start=start, end=end)
    assert report["mutation"]["edits"] == [edit.model_dump(mode="json")]
    detail = report["selection"]
    assert "text" not in detail
    assert detail["removed_codepoints"] == len(_PRIVATE)
    assert detail["removed_utf8_bytes"] == len(_PRIVATE.encode())
    assert detail["retained_codepoints"] == len(_TEXT) - len(_PRIVATE)
    assert detail["retained_utf8_bytes"] == len(_TEXT.encode()) - len(_PRIVATE.encode())
    human = CliRunner().invoke(app, args[:-2])
    assert human.exit_code == 0, human.output
    assert "selection:" in human.stdout
    assert "mutation:" in human.stdout
    assert _PRIVATE not in human.stdout
    private = CliRunner().invoke(app, [*args, "--show-text", "--max-text-bytes", "65536"])
    assert private.exit_code == 0, private.output
    selected = json.loads(private.stdout)["selection"]
    assert selected["text"] == "pré  é🙂\\u000d\\u000a\\u005btail\\u005d\\u000a\\\\u000a"
    assert selected["text_scope"] == "retained-tool-output-text-field"
    assert not selected["text_truncated"]
    assert _PRIVATE not in private.stdout
    assert _PRIVATE not in repr(
        inspect_tool_output_text(evidence, field_id=field.field_id, show_text=True)
    )


@pytest.mark.parametrize("allowance", [0, 1, 5, 12])
def test_tool_output_text_display_obeys_escaped_utf8_budget(
    tool_case: tuple[Path, CaseEvidence], allowance: int
) -> None:
    case, evidence = tool_case
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "tool",
            str(case),
            "--field",
            evidence.tool_output_text_fields()[0].field_id,
            "--show-text",
            "--max-text-bytes",
            str(allowance),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    selected = json.loads(result.stdout)["selection"]
    assert len(selected["text"].encode()) == selected["text_bytes"] <= allowance
    assert selected["text_truncated"]


@pytest.mark.parametrize(
    "options",
    [
        ["--start", "0"],
        ["--end", "1"],
        ["--start", "0", "--end", "1"],
        ["--show-text"],
        ["--field", "private-foreign-id"],
        ["--field", "FIELD", "--start", "2", "--end", "2"],
        ["--field", "FIELD", "--start", "100", "--end", "101"],
    ],
)
def test_tool_output_text_rejects_partial_options_foreign_fields_and_invalid_spans(
    tool_case: tuple[Path, CaseEvidence], options: list[str]
) -> None:
    case, evidence = tool_case
    options = [
        evidence.tool_output_text_fields()[0].field_id if arg == "FIELD" else arg for arg in options
    ]
    result = CliRunner().invoke(app, ["inspect", "tool", str(case), *options, "--format", "json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error_code"] in {
        "inspection-invalid",
        "invalid-inspection-options",
    }
    assert "private-foreign-id" not in result.output
    assert _PRIVATE not in result.output


def test_tool_output_text_reports_inspection_and_field_policy_limits(
    tool_case: tuple[Path, CaseEvidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl import inspection
    from ithrl.tool_output_text import EditableToolOutputText

    _, evidence = tool_case
    (site,) = evidence.editable_tool_output_text()
    # Keep genuine case bindings and native locations; increase the discovered catalog only.
    sites = (
        site,
        EditableToolOutputText(site.context_id, "/fixture/1", "a"),
        EditableToolOutputText(site.context_id, "/fixture/2", "a" * (MAX_TEXT_SECTION_BYTES + 1)),
    )

    def discovered_sites(_evidence: CaseEvidence) -> tuple[EditableToolOutputText, ...]:
        return sites

    monkeypatch.setattr(CaseEvidence, "editable_tool_output_text", discovered_sites)
    described_paths: list[str] = []
    describe = CaseEvidence._tool_output_text_field

    def counted_describe(owner: CaseEvidence, site: EditableToolOutputText) -> ToolOutputTextField:
        described_paths.append(site.field_path)
        return describe(owner, site)

    monkeypatch.setattr(CaseEvidence, "_tool_output_text_field", counted_describe)
    monkeypatch.setattr(inspection, "MAX_INSPECTION_TOOL_OUTPUT_TEXT_FIELDS", 2)
    limited = inspect_tool_output_text(evidence)
    assert len(limited.fields) == 2
    assert limited.omitted_field_count == 1
    assert described_paths == [site.field_path, "/fixture/1"]
    described_paths.clear()
    monkeypatch.setattr(inspection, "MAX_INSPECTION_TOOL_OUTPUT_TEXT_FIELDS", 3)
    policy = inspect_tool_output_text(evidence)
    assert policy.unavailable_fields[0].reason == "tool-output-text-policy-limit"
    assert policy.omitted_field_count == 0
    assert described_paths == [site.field_path, "/fixture/1", "/fixture/2"]
    assert _PRIVATE not in policy.model_dump_json()


def test_tool_output_text_human_output_reports_unavailable_field(
    tool_case: tuple[Path, CaseEvidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, _evidence = tool_case
    monkeypatch.setattr("ithrl.tool_output_text.MAX_TEXT_SECTION_BYTES", 1)

    result = CliRunner().invoke(app, ["inspect", "tool", str(case)])
    assert result.exit_code == 0, result.output
    assert "unavailable:" in result.stdout
    assert "tool-output-text-policy-limit" in result.stdout
    assert _PRIVATE not in result.stdout


def test_tool_output_text_can_select_beyond_visible_prefix_without_rehashing_catalog(
    tool_case: tuple[Path, CaseEvidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl import inspection
    from ithrl.tool_output_text import EditableToolOutputText

    _, evidence = tool_case
    (site,) = evidence.editable_tool_output_text()
    sites = (
        site,
        EditableToolOutputText(site.context_id, "/fixture/1", "second"),
        EditableToolOutputText(site.context_id, "/fixture/2", "third"),
    )

    def discovered_sites(_evidence: CaseEvidence) -> tuple[EditableToolOutputText, ...]:
        return sites

    monkeypatch.setattr(CaseEvidence, "editable_tool_output_text", discovered_sites)
    target = evidence.tool_output_text_fields()[-1]
    described_paths: list[str] = []
    describe = CaseEvidence._tool_output_text_field

    def counted_describe(owner: CaseEvidence, item: EditableToolOutputText) -> ToolOutputTextField:
        described_paths.append(item.field_path)
        return describe(owner, item)

    monkeypatch.setattr(CaseEvidence, "_tool_output_text_field", counted_describe)
    monkeypatch.setattr(inspection, "MAX_INSPECTION_TOOL_OUTPUT_TEXT_FIELDS", 1)
    selected = inspect_tool_output_text(
        evidence, field_id=target.field_id, start=1, end=3, show_text=True
    )
    assert selected.fields == (target,)
    assert selected.selection is not None
    assert selected.selection.text == "trd"
    assert selected.omitted_field_count == 2
    assert described_paths == [site.field_path, "/fixture/1", "/fixture/2"]


@pytest.mark.parametrize("changed", ["manifest", "context", "decision", "source"])
def test_tool_output_text_selected_field_cache_is_owner_bound_and_refuses_rebound_evidence(
    tool_case: tuple[Path, CaseEvidence], monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from ithrl.tool_output_text import EditableToolOutputText

    _, evidence = tool_case
    field = evidence.tool_output_text_fields()[0]
    described_owners: list[CaseEvidence] = []
    describe = CaseEvidence._tool_output_text_field

    def counted_describe(owner: CaseEvidence, site: EditableToolOutputText) -> ToolOutputTextField:
        described_owners.append(owner)
        return describe(owner, site)

    monkeypatch.setattr(CaseEvidence, "_tool_output_text_field", counted_describe)
    edit = evidence.tool_output_text_removal(field.field_id, start=1, end=3)
    bound_field, bound_site = evidence.validate_tool_output_text_span(edit)
    for _ in range(3):
        repeated_field, repeated_site = evidence.validate_tool_output_text_span(edit)
        assert repeated_field is bound_field
        assert repeated_site is bound_site
    assert described_owners == [evidence]
    for clone in (copy.copy(evidence), copy.deepcopy(evidence)):
        clone_field, clone_site = clone.validate_tool_output_text_span(edit)
        assert clone_field == bound_field
        assert clone_field is not bound_field
        assert clone_site == bound_site
        assert clone_site is not bound_site
        assert described_owners[-1] is clone
    assert len(described_owners) == 3
    if changed == "manifest":
        evidence._manifest = evidence.manifest.model_copy(update={"adapter_version": "forged"})
    elif changed == "context":
        first, *rest = evidence.context.nodes
        evidence._context = evidence.context.model_copy(
            update={"nodes": (first.model_copy(update={"protected": not first.protected}), *rest)}
        )
    elif changed == "decision":
        assert evidence.decision is not None
        evidence._decision = evidence.decision.model_copy(update={"observed_at": "forged"})
    else:
        source = evidence.source_artifacts[0]
        evidence._source_artifacts = (
            SourceArtifact(path=source.path, media_type=source.media_type, content=b"{}"),
        )
    for _ in range(2):
        with pytest.raises(CaseFormatError):
            evidence.validate_tool_output_text_span(edit)


def test_tool_output_text_unicode_span_counts_and_empty_retained_field(
    tool_case: tuple[Path, CaseEvidence],
) -> None:
    _, evidence = tool_case
    field = evidence.tool_output_text_fields()[0]
    unicode_span = inspect_tool_output_text(evidence, field_id=field.field_id, start=1, end=3)
    assert unicode_span.selection is not None
    assert unicode_span.selection.removed_codepoints == 2
    assert unicode_span.selection.removed_utf8_bytes == 3
    empty = inspect_tool_output_text(
        evidence,
        field_id=field.field_id,
        start=0,
        end=len(_TEXT),
        show_text=True,
        max_text_bytes=0,
    )
    assert empty.selection is not None
    assert empty.selection.retained_codepoints == empty.selection.retained_utf8_bytes == 0
    assert empty.selection.text == ""
    assert not empty.selection.text_truncated


def test_comparison_text_requires_source_case_before_opening_saved_result() -> None:
    result = CliRunner().invoke(
        app, ["inspect", "comparison", "private-result", "--show-text", "--format", "json"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error_code"] == "invalid-inspection-options"
    assert "private-result" not in result.stdout


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_comparison_inspection_validates_tool_span_and_bounds_retained_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Literal["codex", "copilot"]
) -> None:
    fixture = _tool_output_text_fixture(tmp_path, harness=harness, common_removal=True)
    saved = fixture.run()
    path = write_comparison_result(tmp_path / "result.json", saved)
    case = tmp_path / "case"
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("comparison inspection attempted execution or store access")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(ObservationStore, "__init__", forbidden)
    args = ["inspect", "comparison", str(path), "--format", "json"]
    runner = CliRunner()
    metadata = runner.invoke(app, args)
    assert metadata.exit_code == 0, metadata.output
    candidate = json.loads(metadata.stdout)["candidate_context"]
    assert candidate["text_operation"] == "remove-tool-output-text-span"
    assert candidate["text_content_index"] is None
    assert candidate["removed_section_ids"] == []
    assert (
        candidate["tool_output_text_edit"]["field_id"]
        == fixture.evidence.tool_output_text_fields()[0].field_id
    )
    assert candidate["tool_output_text_field"] is None
    assert "DELETE-ME" not in metadata.output

    verified = runner.invoke(app, [*args, "--case", str(case)])
    assert verified.exit_code == 0, verified.output
    report = json.loads(verified.stdout)
    detail = report["candidate_context"]["tool_output_text_field"]
    assert report["source_validation"] == "verified"
    assert "text" not in detail
    assert detail["field"]["original_codepoints"] == len(_TOOL_OUTPUT_TEXT)
    assert detail["field"]["original_utf8_bytes"] == len(_TOOL_OUTPUT_TEXT.encode())
    assert detail["removed_codepoints"] == len("DELETE-ME")
    assert detail["removed_utf8_bytes"] == len(b"DELETE-ME")
    assert detail["retained_utf8_bytes"] > detail["retained_codepoints"]

    for allowance in (0, 11, 4096):
        inspected = runner.invoke(
            app, [*args, "--case", str(case), "--show-text", "--max-text-bytes", str(allowance)]
        )
        assert inspected.exit_code == 0, inspected.output
        detail = json.loads(inspected.stdout)["candidate_context"]["tool_output_text_field"]
        assert detail["text_scope"] == "retained-tool-output-text-field"
        assert len(detail["text"].encode()) == detail["text_bytes"] <= allowance
        assert detail["text_truncated"] == (allowance < 4096)
        assert "DELETE-ME" not in detail["text"]
        if allowance == 4096:
            assert '"recipe":"\\\\nexact"' in detail["text"]
            assert "\\u000d\\u000a" in detail["text"]

    human = runner.invoke(app, ["inspect", "comparison", str(path), "--case", str(case)])
    assert human.exit_code == 0, human.output
    assert "source codepoints:" in human.stdout
    assert "UTF-8 bytes" in human.stdout
    assert "removed sections" not in human.stdout
    private = result_report(saved, case_evidence=fixture.evidence, show_text=True)
    assert "Retain diagnostics" not in repr(private)
    assert before == {p: (p.read_bytes(), p.stat().st_mode) for p in case.rglob("*") if p.is_file()}
