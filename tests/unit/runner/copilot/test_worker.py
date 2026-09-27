from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ithrl.runner.copilot import worker


def _write_request(
    tmp_path: Path,
    *,
    flags: list[str] | None = None,
    trigger: str = "private trigger",
) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    semantic_session = tmp_path / "session.jsonl"
    semantic_session.write_text('{"type":"session"}\n', encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "commandFlags": flags or ["--output-format=json", "--stream=off"],
                "model": "fixture-model",
                "semanticSession": os.fspath(semantic_session),
                "target": "final-response",
                "trigger": trigger,
                "workspace": os.fspath(workspace),
            }
        ),
        encoding="utf-8",
    )
    request.chmod(0o600)
    return request, semantic_session, workspace


def _fake_copilot(tmp_path: Path) -> Path:
    executable = tmp_path / "copilot"
    executable.write_text(
        """#!/usr/bin/python3
import json
import os
import sys
import time

record_path = os.environ["ITHRL_WORKER_RECORD"]
arguments = sys.argv[1:]
if arguments[:3] == ["--no-auto-update", "sessions", "import"]:
    behavior = os.environ.get("ITHRL_IMPORT_BEHAVIOR", "success")
    if behavior == "timeout":
        time.sleep(10)
    if behavior == "failure":
        sys.stderr.write("private import failure detail\\n")
        raise SystemExit(7)
    if behavior == "oversized":
        sys.stdout.write("x" * 1024)
        raise SystemExit(0)
    if behavior in ("stdout-stream-overflow", "stderr-stream-overflow"):
        os.write(1 if behavior.startswith("stdout") else 2, b"x" * 1024)
        time.sleep(10)
    if behavior == "malformed":
        sys.stdout.write('{"private":"import output"}')
        raise SystemExit(0)
    if behavior == "duplicate":
        sys.stdout.write(
            '{"ok":true,"code":"session_imported",'
            '"sessionId":"session-123","sessionId":"private replacement"}'
        )
        raise SystemExit(0)
    document = {
        "ok": True,
        "code": os.environ.get("ITHRL_IMPORT_CODE", "session_imported"),
        "sessionId": os.environ.get("ITHRL_SESSION_ID", "session-123"),
    }
    stdin = None
else:
    behavior = "resume"
    document = None
    stdin = sys.stdin.read()

with open(record_path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": arguments, "stdin": stdin}) + "\\n")
if document is not None:
    print(json.dumps(document))
    raise SystemExit(0)
raise SystemExit(int(os.environ.get("ITHRL_RESUME_RETURN_CODE", "0")))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_main_imports_then_resumes_with_fixed_direct_arguments_and_private_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = ["--output-format=json", "--stream=off", "--available-tools=none"]
    request, semantic_session, workspace = _write_request(tmp_path, flags=flags)
    executable = _fake_copilot(tmp_path)
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("ITHRL_WORKER_RECORD", os.fspath(record))

    result = worker.main([os.fspath(request), os.fspath(executable)])

    assert result == 0
    assert not request.exists()
    assert _records(record) == [
        {
            "argv": [
                "--no-auto-update",
                "sessions",
                "import",
                "--output",
                "json",
                "--working-directory",
                os.fspath(workspace),
                os.fspath(semantic_session),
            ],
            "stdin": None,
        },
        {
            "argv": [*flags, "--model", "fixture-model", "--resume", "session-123"],
            "stdin": "private trigger\n",
        },
    ]
    assert "private trigger" not in json.dumps(_records(record)[1]["argv"])


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("extra", "unsupported", "unsupported shape"),
        ("commandFlags", ["not-a-flag"], "invalid fields"),
        ("model", "model\0private", "invalid fields"),
        ("target", "unknown", "invalid fields"),
        ("workspace", "../private", "invalid fields"),
    ],
)
def test_main_rejects_noncanonical_requests_without_exposing_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    replacement: object,
    message: str,
) -> None:
    request, _, _ = _write_request(tmp_path)
    value = json.loads(request.read_bytes())
    value[field] = replacement
    request.write_text(json.dumps(value), encoding="utf-8")
    request.chmod(0o600)

    result = worker.main([os.fspath(request), os.fspath(tmp_path / "missing")])

    assert result == 70
    error = capsys.readouterr().err
    assert message in error
    assert "private" not in error
    assert os.fspath(request) not in error


def test_main_rejects_oversized_and_non_private_request_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized, _, _ = _write_request(tmp_path)
    monkeypatch.setattr(worker, "MAX_REQUEST_BYTES", oversized.stat().st_size - 1)

    assert worker.main([os.fspath(oversized), os.fspath(tmp_path / "missing")]) == 70
    assert "request is unsafe" in capsys.readouterr().err

    unsafe_mode, _, _ = _write_request(tmp_path)
    unsafe_mode.chmod(0o644)

    assert worker.main([os.fspath(unsafe_mode), os.fspath(tmp_path / "missing")]) == 70
    assert "request is unsafe" in capsys.readouterr().err


def test_main_rejects_duplicate_request_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, _, _ = _write_request(tmp_path)
    content = request.read_text(encoding="utf-8").replace(
        '"model": "fixture-model"',
        '"model": "fixture-model", "model": "private replacement"',
    )
    request.write_text(content, encoding="utf-8")
    request.chmod(0o600)

    assert worker.main([os.fspath(request), os.fspath(tmp_path / "missing")]) == 70
    error = capsys.readouterr().err
    assert "request contains invalid JSON" in error
    assert "private replacement" not in error


@pytest.mark.parametrize("code", ["wrong_code", ""])
def test_main_requires_the_documented_session_import_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    request, _, _ = _write_request(tmp_path)
    executable = _fake_copilot(tmp_path)
    monkeypatch.setenv("ITHRL_WORKER_RECORD", os.fspath(tmp_path / "record.jsonl"))
    monkeypatch.setenv("ITHRL_IMPORT_CODE", code)

    result = worker.main([os.fspath(request), os.fspath(executable)])

    assert result == 70
    assert "import returned invalid output" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("behavior", "message"),
    [
        ("failure", "import failed"),
        ("malformed", "import returned invalid output"),
        ("duplicate", "import returned invalid output"),
        ("oversized", "import exceeded its output limit"),
        ("timeout", "import timed out"),
    ],
)
def test_import_failures_are_bounded_and_content_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    message: str,
) -> None:
    request, _, _ = _write_request(tmp_path)
    executable = _fake_copilot(tmp_path)
    monkeypatch.setenv("ITHRL_WORKER_RECORD", os.fspath(tmp_path / "record.jsonl"))
    monkeypatch.setenv("ITHRL_IMPORT_BEHAVIOR", behavior)
    if behavior == "oversized":
        monkeypatch.setattr(worker, "MAX_IMPORT_OUTPUT_BYTES", 32)
    if behavior == "timeout":
        monkeypatch.setattr(worker, "_IMPORT_TIMEOUT_SECONDS", 0.01)

    result = worker.main([os.fspath(request), os.fspath(executable)])

    assert result == 70
    error = capsys.readouterr().err
    assert message in error
    assert "private" not in error


@pytest.mark.parametrize("behavior", ["stdout-stream-overflow", "stderr-stream-overflow"])
def test_import_output_limit_stops_a_still_running_process(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
) -> None:
    request, _, _ = _write_request(tmp_path)
    executable = _fake_copilot(tmp_path)
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("ITHRL_WORKER_RECORD", os.fspath(record))
    monkeypatch.setenv("ITHRL_IMPORT_BEHAVIOR", behavior)
    monkeypatch.setattr(worker, "MAX_IMPORT_OUTPUT_BYTES", 32)
    started = time.monotonic()

    result = worker.main([os.fspath(request), os.fspath(executable)])

    assert result == 70
    assert time.monotonic() - started < 5
    assert "import exceeded its output limit" in capsys.readouterr().err
    assert not record.exists()


def test_resume_exit_status_is_returned_without_interpreting_native_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _write_request(tmp_path)
    executable = _fake_copilot(tmp_path)
    monkeypatch.setenv("ITHRL_WORKER_RECORD", os.fspath(tmp_path / "record.jsonl"))
    monkeypatch.setenv("ITHRL_RESUME_RETURN_CODE", "23")

    assert worker.main([os.fspath(request), os.fspath(executable)]) == 23
