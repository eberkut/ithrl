from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ithrl.runner import RunAccounting, RunResult, ToolCall
from ithrl.runner.runtime import contracts as runtime_contracts
from ithrl.runner.runtime.contracts import ProcessSpec, RuntimeReport, require_process_group_support


def test_process_spec_copies_its_environment(tmp_path: Path) -> None:
    environment = {"SAFE": "value"}
    spec = ProcessSpec(
        argv=(str(Path(os.__file__).parent / "missing-python"), "argument"),
        cwd=tmp_path,
        environment=environment,
    )

    environment["SAFE"] = "changed"

    assert dict(spec.environment) == {"SAFE": "value"}
    assert "argument" not in repr(spec)
    assert str(tmp_path) not in repr(spec)
    with pytest.raises(TypeError):
        spec.environment["OTHER"] = "value"  # type: ignore[index]


def test_process_spec_rejects_standard_stream_as_inherited_descriptor(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="at least 3"):
        ProcessSpec(
            argv=("/bin/true",),
            cwd=tmp_path,
            environment={},
            inherited_fds=(2,),
        )


def test_process_spec_rejects_non_callable_stdout_stop_condition(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stop condition"):
        ProcessSpec(
            argv=("/bin/true",),
            cwd=tmp_path,
            environment={},
            stop_after_stdout_line=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("argv", "cwd", "environment", "message"),
    [
        ((), Path.cwd(), {}, "arguments must be strings"),
        (("/python", 1), Path.cwd(), {}, "arguments must be strings"),
        (("python",), Path("relative"), {}, "executable must be an absolute path"),
        (("/python", ""), Path.cwd(), {}, "arguments must be non-empty"),
        (("/python",), Path("relative"), {}, "working directory must be absolute"),
        (
            (("/python",)),
            Path("/owned/../outside"),
            {},
            "working directory must be absolute",
        ),
        (("/python",), Path.cwd(), {"BAD=NAME": "value"}, "invalid name or value"),
        (("/python",), Path.cwd(), {"NAME": "bad\0value"}, "invalid name or value"),
    ],
)
def test_process_spec_rejects_ambiguous_invocations(
    argv: tuple[str, ...],
    cwd: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessSpec(argv=argv, cwd=cwd, environment=environment)


@pytest.mark.parametrize(
    ("dimension", "message"),
    [
        ("bytes", "residual state"),
        ("entries", "residual entries"),
        ("negative", "cannot be negative"),
    ],
)
def test_runtime_report_rejects_impossible_sizes(dimension: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimeReport(
            elapsed_milliseconds=-1 if dimension == "negative" else 1,
            native_output_bytes=0,
            disposable_state_bytes=1,
            residual_disposable_state_bytes=2 if dimension == "bytes" else 0,
            disposable_state_entries=0,
            residual_disposable_state_entries=1 if dimension == "entries" else 0,
            process_outcome="not-started",
            forced_termination=False,
            stdout_stop_matched=False,
            cleanup_outcome="failed",
            source_store_effect="unchanged",
            original_workspace_effect="unchanged",
        )


def test_normalized_result_repr_omits_model_content() -> None:
    result = RunResult(
        attempt_id="attempt-0123456789abcdef0123456789abcdef",
        effective_policy_digest="0" * 64,
        tool_calls=(ToolCall(name="bash", arguments={"command": "private-command"}),),
        final_text="private-response",
        accounting=RunAccounting(),
    )

    assert "private-command" not in repr(result)
    assert "private-response" not in repr(result)


def test_process_group_support_matches_the_host() -> None:
    if os.name == "posix" and hasattr(os, "killpg"):
        require_process_group_support()
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            require_process_group_support()


@pytest.mark.parametrize("platform", ["non-posix", "missing-killpg"])
def test_process_group_support_refuses_missing_host_capabilities(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    # Replace this module's OS view without changing the process-wide os module.
    host = (
        SimpleNamespace(name="nt", killpg=object())
        if platform == "non-posix"
        else SimpleNamespace(name="posix")
    )
    monkeypatch.setattr(runtime_contracts, "os", host)
    with pytest.raises(RuntimeError, match="isolated process groups are unavailable"):
        require_process_group_support()
