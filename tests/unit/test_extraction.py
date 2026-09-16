from __future__ import annotations

import hashlib
import json
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from ithrl.adapters import (
    AdapterError,
    AdapterErrorCode,
    enumerate_decisions,
    parse_session,
    select_decision,
)
from ithrl.adapters.codex import context as codex_context
from ithrl.adapters.codex import decisions as codex_decisions
from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence, CaseFormatError
from ithrl.experiment import (
    ExperimentDefinition,
    FakeRunnerConfig,
    UnavailableRunnerConfig,
)
from ithrl.extraction import extract_decision, load_extracted_case, validate_extracted_case
from ithrl.workflow import PreflightRefusedError, prepare_experiment

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    ("source", "stored_source", "harness"),
    [
        ("codex-session/rollout.jsonl", "source/rollout.jsonl", "codex"),
        ("copilot-session/events.jsonl", "source/events.jsonl", "copilot"),
    ],
)
def test_extract_decision_compiles_private_source_backed_case(
    tmp_path: Path,
    source: str,
    stored_source: str,
    harness: str,
) -> None:
    source_path = FIXTURES / source
    before = source_path.read_bytes()
    before_stat = source_path.stat()
    session = parse_session(source_path)
    summary = enumerate_decisions(session)[0]
    target = summary.targets[0]

    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=target.target_id,
        case_path=tmp_path / "case",
    )
    evidence = workspace.load()
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)

    assert not evidence.has_request
    with pytest.raises(CaseFormatError, match="exact provider request"):
        _ = evidence.request
    assert evidence.decision is not None
    assert evidence.decision.decision_id == summary.decision_id
    assert evidence.decision.selected_target_id == target.target_id
    assert evidence.manifest.harness == harness
    assert evidence.manifest.harness_version == session.metadata.format.producer_version
    assert evidence.manifest.capture.request == "logical-session"
    assert evidence.manifest.capture.completeness == "complete"
    assert evidence.manifest.replay.state == "harness-local"
    assert evidence.manifest.source_schema == session.metadata.format.name
    assert evidence.manifest.provenance.inference_call_id is None
    assert all(node.path is None and node.selector is not None for node in evidence.context.nodes)
    assert (workspace.root / stored_source).read_bytes() == before
    assert not (workspace.root / "request.json").exists()
    assert (workspace.root / "decision.json").is_file()
    assert stat.S_IMODE((workspace.root / "decision.json").stat().st_mode) == 0o600
    assert isinstance(experiment.runner, UnavailableRunnerConfig)
    assert experiment.runner.harness == harness
    assert experiment.target.digest == evidence.decision.selected_target.digest
    with pytest.raises(PreflightRefusedError) as refused:
        prepare_experiment(evidence)
    assert [issue.code for issue in refused.value.report.issues] == ["runner-unavailable"]

    after_stat = source_path.stat()
    assert source_path.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == session.source.sha256
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


@pytest.mark.parametrize(
    "source",
    ["codex-session/rollout.jsonl", "copilot-session/events.jsonl"],
)
def test_extract_decision_is_deterministic(tmp_path: Path, source: str) -> None:
    session = parse_session(FIXTURES / source)
    summary = enumerate_decisions(session)[0]
    target_id = summary.targets[0].target_id

    first = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=target_id,
        case_path=tmp_path / "first",
    ).load()
    second = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=target_id,
        case_path=tmp_path / "second",
    ).load()

    assert first.digest == second.digest


def test_decision_selection_refuses_unknown_stable_ids() -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]

    with pytest.raises(AdapterError) as missing_decision:
        select_decision(
            session,
            decision_id="decision-0000000000000000",
            target_id=summary.targets[0].target_id,
        )
    assert missing_decision.value.code is AdapterErrorCode.DECISION_NOT_FOUND

    with pytest.raises(AdapterError) as missing_target:
        select_decision(
            session,
            decision_id=summary.decision_id,
            target_id="target-0000000000000000",
        )
    assert missing_target.value.code is AdapterErrorCode.TARGET_NOT_FOUND


@pytest.mark.parametrize("metadata_file", ["context.jsonl", "decision.json"])
def test_source_selector_tampering_is_refused(tmp_path: Path, metadata_file: str) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    path = workspace.root / metadata_file

    if metadata_file == "context.jsonl":
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["selector"]["identity_value"] = 999
        lines[0] = json.dumps(record, separators=(",", ":"), sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["selector"]["record_digest"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CaseFormatError, match="source record"):
        workspace.load()


def test_decision_context_boundary_tampering_is_refused(tmp_path: Path) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    path = workspace.root / "decision.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["selector"]["context_before_line"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CaseFormatError, match="context must precede"):
        workspace.load()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('harness = "codex"', 'harness = "copilot"', "decision harness"),
        (
            'source = "source/rollout.jsonl"',
            'source = "source/other.jsonl"',
            "decision source",
        ),
    ],
)
def test_decision_must_match_manifest_metadata(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    manifest = workspace.root / "case.toml"
    source = manifest.read_text(encoding="utf-8")
    assert old in source
    manifest.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(CaseFormatError, match=message):
        workspace.load()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'trace_schema = "codex-rollout-jsonl"',
            'trace_schema = "another-schema"',
            "source schema does not match provenance",
        ),
        (
            'completeness = "complete"',
            'completeness = "partial"',
            "completeness does not match decision evidence",
        ),
        (
            'source = "source/rollout.jsonl"',
            'source = "source/rollout.jsonl"\ninference_call_id = "call-1"',
            "cannot identify a provider inference call",
        ),
    ],
)
def test_logical_session_manifest_invariants_are_enforced(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    manifest = workspace.root / "case.toml"
    source = manifest.read_text(encoding="utf-8")
    assert old in source
    manifest.write_text(source.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(CaseFormatError, match=message):
        workspace.load()


@pytest.mark.parametrize(
    "tamper",
    ["capture", "context", "decision", "manifest", "request", "source-manifest"],
)
def test_logical_session_case_is_rederived_before_experiment_use(
    tmp_path: Path,
    tamper: str,
) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    if tamper == "capture":
        path = workspace.root / "case.toml"
        source = path.read_text(encoding="utf-8")
        path.write_text(
            source.replace('request = "logical-session"', 'request = "reconstructed"'),
            encoding="utf-8",
        )
    elif tamper == "context":
        path = workspace.root / "context.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    elif tamper == "decision":
        path = workspace.root / "decision.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["selector"]["kind"] = "codex-function-call"
        path.write_text(json.dumps(document), encoding="utf-8")
    elif tamper in {"manifest", "request"}:
        path = workspace.root / "case.toml"
        source = path.read_text(encoding="utf-8")
        if tamper == "manifest":
            source = source.replace('provider = "fixture-provider"', 'provider = "other-provider"')
        else:
            source = source.replace('request = "logical-session"', 'request = "provider-request"')
            request = workspace.root / "request.json"
            request.write_text("{}\n", encoding="utf-8")
            request.chmod(0o600)
        path.write_text(source, encoding="utf-8")
    else:
        path = workspace.root / "source/manifest.json"
        source = path.read_text(encoding="utf-8")
        path.write_text(
            source.replace("application/x-ndjson", "application/json"),
            encoding="utf-8",
        )

    evidence = workspace.load()
    with pytest.raises(CaseFormatError, match="logical-session case"):
        validate_extracted_case(evidence)
    with pytest.raises(CaseFormatError, match="logical-session case"):
        load_extracted_case(workspace)


def test_native_case_refuses_fake_runner_even_when_its_target_matches(
    tmp_path: Path,
) -> None:
    evidence = _native_case_evidence(tmp_path)
    assert evidence.decision is not None
    target = evidence.decision.selected_target
    assert isinstance(target, FinalResponseTarget)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment).model_copy(
        update={
            "runner": FakeRunnerConfig(
                required_markers=("fixture",),
                final_text=target.text_equals,
            )
        }
    )

    with pytest.raises(PreflightRefusedError) as refused:
        prepare_experiment(_with_experiment(evidence, experiment))

    assert [issue.code for issue in refused.value.report.issues] == [
        "unsupported-capture",
        "unsupported-materialization",
    ]


def test_native_case_refuses_experiment_target_or_harness_mismatch(
    tmp_path: Path,
) -> None:
    evidence = _native_case_evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)

    for changed in (
        experiment.model_copy(
            update={"target": FinalResponseTarget(text_equals="another response")}
        ),
        experiment.model_copy(
            update={"runner": UnavailableRunnerConfig(harness="another-harness")}
        ),
    ):
        with pytest.raises(PreflightRefusedError) as refused:
            prepare_experiment(_with_experiment(evidence, changed))
        assert [issue.code for issue in refused.value.report.issues] == ["runner-evidence-mismatch"]


def _native_case_evidence(tmp_path: Path) -> CaseEvidence:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]
    return extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()


def _with_experiment(
    evidence: CaseEvidence,
    experiment: ExperimentDefinition,
) -> CaseEvidence:
    return CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=evidence.context,
        default_experiment=experiment.to_toml(),
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )


def test_public_adapter_errors_do_not_expose_invalid_native_identifiers() -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    marker = "private-marker-" * 100
    metadata = session.metadata.model_copy(update={"session_id": marker})
    invalid = type(session)(
        metadata=metadata,
        source=session.source,
        native_bytes=session.native_bytes,
        records=session.records,
    )

    with pytest.raises(AdapterError) as refused:
        enumerate_decisions(invalid)

    assert refused.value.code is AdapterErrorCode.INVALID_SESSION
    assert marker not in str(refused.value)


def test_decision_and_context_limits_fail_before_case_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    records = list(session.records)
    records.extend(
        [
            {
                "ordinal": 13,
                "timestamp": "2026-01-01T00:00:13.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn-fixture-002", "model": "fixture-model"},
            },
            {
                "ordinal": 14,
                "timestamp": "2026-01-01T00:00:14.000Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-fixture-002"},
            },
            {
                "ordinal": 15,
                "timestamp": "2026-01-01T00:00:15.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-fixture-002",
                    "last_agent_message": "Second fixture response.",
                },
            },
        ]
    )
    source = tmp_path / "two-turns.jsonl"
    source.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_decisions, "MAX_DECISIONS", 1)

    with pytest.raises(AdapterError) as too_many:
        enumerate_decisions(parse_session(source))
    assert too_many.value.code is AdapterErrorCode.TOO_MANY_DECISIONS

    monkeypatch.setattr(codex_decisions, "MAX_DECISIONS", 4096)
    monkeypatch.setattr(codex_context, "MAX_CONTEXT_NODES", 1)
    summary = enumerate_decisions(session)[0]
    with pytest.raises(AdapterError) as too_large:
        select_decision(
            session,
            decision_id=summary.decision_id,
            target_id=summary.targets[0].target_id,
        )
    assert too_large.value.code is AdapterErrorCode.CONTEXT_TOO_LARGE


def test_local_extraction_does_not_open_sockets_or_start_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local extraction attempted external execution")

    monkeypatch.setattr(socket, "socket", unexpected_call)
    monkeypatch.setattr(subprocess, "Popen", unexpected_call)
    session = parse_session(FIXTURES / "codex-session/rollout.jsonl")
    summary = enumerate_decisions(session)[0]

    workspace = extract_decision(
        session,
        decision_id=summary.decision_id,
        target_id=summary.targets[0].target_id,
        case_path=tmp_path / "case",
    )

    assert workspace.load().decision is not None
