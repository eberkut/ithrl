from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from ithrl.adapters import AdapterError, AdapterErrorCode, NativeSession, SourceFacts, parse_session
from ithrl.adapters._jsonl import JsonlDocument, read_jsonl
from ithrl.adapters.contracts import ParsingLimits
from ithrl.adapters.registry import NativeSessionParser, _parse_document

FIXTURES = Path(__file__).parents[1] / "fixtures"
CASES = (
    (
        FIXTURES / "codex-session" / "rollout.jsonl",
        "codex",
        "codex-rollout-jsonl",
        "0.153.4",
        None,
        13,
        "fixture.unknown_record",
    ),
    (
        FIXTURES / "copilot-session" / "events.jsonl",
        "copilot",
        "copilot-events-jsonl",
        "1.0.82",
        1,
        27,
        "fixture.unknown_event",
    ),
)


@pytest.mark.parametrize(
    (
        "fixture",
        "harness",
        "format_name",
        "producer_version",
        "record_schema_version",
        "record_count",
        "unknown_type",
    ),
    CASES,
)
def test_builtin_adapter_contract(
    fixture: Path,
    harness: str,
    format_name: str,
    producer_version: str,
    record_schema_version: int | None,
    record_count: int,
    unknown_type: str,
) -> None:
    before = fixture.stat()
    expected_bytes = fixture.read_bytes()

    session = parse_session(fixture)

    after = fixture.stat()
    assert session.metadata.format.harness == harness
    assert session.metadata.format.name == format_name
    assert session.metadata.format.producer_version == producer_version
    assert session.metadata.format.record_schema_version == record_schema_version
    assert session.record_count == record_count
    assert session.native_bytes == expected_bytes
    assert session.source.sha256 == hashlib.sha256(expected_bytes).hexdigest()
    assert any(record["type"] == unknown_type for record in session.records)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_native_session_returns_defensive_record_copies() -> None:
    session = parse_session(CASES[0][0])
    records = session.records
    records[0]["type"] = "changed"

    assert session.records[0]["type"] == "session_meta"


@pytest.mark.parametrize(("sha256", "size_delta"), [("0" * 64, 0), (None, 1)])
def test_native_session_requires_source_facts_to_match_bytes(
    sha256: str | None,
    size_delta: int,
) -> None:
    parsed = parse_session(CASES[0][0])
    source = SourceFacts(
        sha256=sha256 or parsed.source.sha256,
        size_bytes=parsed.source.size_bytes + size_delta,
    )

    with pytest.raises(ValueError, match="native session"):
        NativeSession(
            metadata=parsed.metadata,
            source=source,
            native_bytes=parsed.native_bytes,
            records=parsed.records,
        )


def test_registry_refuses_unknown_and_ambiguous_formats(tmp_path: Path) -> None:
    source = tmp_path / "unknown.jsonl"
    source.write_text('{"type":"fixture.unknown"}\n', encoding="utf-8")

    with pytest.raises(AdapterError) as unsupported:
        parse_session(source)
    assert unsupported.value.code is AdapterErrorCode.UNSUPPORTED_FORMAT
    assert source.name not in str(unsupported.value)

    document = read_jsonl(source, limits=ParsingLimits())
    parsers: tuple[NativeSessionParser, ...] = (_AlwaysMatches(), _AlwaysMatches())
    with pytest.raises(AdapterError) as ambiguous:
        _parse_document(document, parsers)
    assert ambiguous.value.code is AdapterErrorCode.AMBIGUOUS_FORMAT


def test_adapter_layers_do_not_depend_on_execution_or_network_modules() -> None:
    package = Path(__file__).parents[2] / "src" / "ithrl" / "adapters"
    forbidden = (
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "ithrl.case",
        "ithrl.cli",
        "ithrl.execution",
        "ithrl.observations",
        "ithrl.reduce",
        "ithrl.runner",
        "ithrl.workflow",
    )

    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert not {
            module
            for module in imported
            if any(module == blocked or module.startswith(f"{blocked}.") for blocked in forbidden)
        }, source


def test_case_layer_does_not_depend_on_harness_or_execution_modules() -> None:
    package = Path(__file__).parents[2] / "src" / "ithrl" / "case"
    forbidden = (
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "ithrl.adapters",
        "ithrl.cli",
        "ithrl.execution",
        "ithrl.observations",
        "ithrl.reduce",
        "ithrl.runner",
        "ithrl.workflow",
    )

    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert not {
            module
            for module in imported
            if any(module == blocked or module.startswith(f"{blocked}.") for blocked in forbidden)
        }, source


def test_shared_domain_layers_do_not_depend_on_concrete_harnesses() -> None:
    root = Path(__file__).parents[2] / "src" / "ithrl"
    shared = (
        root / "behavior.py",
        root / "context.py",
        root / "decisions.py",
        root / "experiment.py",
        root / "measurement.py",
        root / "observations.py",
        root / "workflow.py",
        *sorted((root / "execution").glob("*.py")),
        *sorted((root / "reduce").glob("*.py")),
        *sorted((root / "results").glob("*.py")),
        *sorted((root / "runner").glob("*.py")),
    )

    for source in shared:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert not {
            module
            for module in imported
            if module.startswith(("ithrl.adapters.codex", "ithrl.adapters.copilot"))
        }, source


def test_runner_core_does_not_depend_on_adapters_cli_or_workflow() -> None:
    package = Path(__file__).parents[2] / "src" / "ithrl" / "runner"
    forbidden = ("ithrl.adapters", "ithrl.cli", "ithrl.workflow")

    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert not {
            module
            for module in imported
            if any(module == blocked or module.startswith(f"{blocked}.") for blocked in forbidden)
        }, source


def test_workflow_has_no_concrete_runner_selection_branch() -> None:
    source = Path(__file__).parents[2] / "src" / "ithrl" / "workflow.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = _imported_modules(tree)

    assert "ithrl.runner.fake" not in imported
    assert "ithrl.adapters.codex" not in imported
    assert "ithrl.adapters.copilot" not in imported
    assert not any(
        isinstance(node, ast.Name) and node.id in {"FakeRunner", "FakeRunnerConfig"}
        for node in ast.walk(tree)
    )


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


class _AlwaysMatches:
    format_name = "fixture"

    def matches(self, document: JsonlDocument) -> bool:
        del document
        return True

    def parse(self, document: JsonlDocument) -> NativeSession:
        del document
        raise AssertionError("ambiguous formats must be refused before parsing")
