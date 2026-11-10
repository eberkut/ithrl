from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ithrl.reduce import ReductionCheckpoint
from ithrl.run_state import RunStateBusyError, RunStateError, RunStateFile


def _checkpoint() -> ReductionCheckpoint:
    return ReductionCheckpoint(
        case_digest="case",
        experiment_digest="experiment",
        runner_fingerprint="fake-v2",
        execution_plan_digest="plan",
        target_digest="target",
        removed_context_ids=(),
        trials=(),
        candidate_runs_used=0,
    )


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("missing", "unavailable"),
        ("directory", "regular file"),
        ("file-mode", "0600"),
        ("parent-mode", "0700"),
        ("utf8", "unavailable"),
        ("json", "invalid run state"),
        ("active-result", "only completed"),
        ("completed-without-result", "only completed"),
    ],
)
def test_deterministic_resume_refuses_unavailable_or_contradictory_state(
    tmp_path: Path, corruption: str, message: str
) -> None:
    case = tmp_path / "case"
    case.mkdir()
    file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )
    if corruption == "missing":
        file.path.unlink()
    elif corruption == "directory":
        file.path.unlink()
        file.path.mkdir()
    elif corruption == "file-mode":
        if os.name != "posix":
            pytest.skip("POSIX permission contract")
        file.path.chmod(0o644)
    elif corruption == "parent-mode":
        if os.name != "posix":
            pytest.skip("POSIX permission contract")
        file.path.parent.chmod(0o755)
    elif corruption in {"utf8", "json"}:
        file.path.write_bytes(b"\xff" if corruption == "utf8" else b"{invalid")
    else:
        payload = json.loads(file.path.read_text())
        if corruption == "active-result":
            payload["result_path"] = "unexpected-result"
        else:
            payload["status"] = "completed"
        file.path.write_text(json.dumps(payload))
    before = file.path.read_bytes() if file.path.is_file() else None
    with pytest.raises(RunStateError, match=message):
        file.load()
    if before is not None:
        assert file.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_failed_state_checkpoint_publication_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case = tmp_path / "case"
    case.mkdir()
    file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )
    before = file.path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("injected checkpoint publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(os if operation == "fsync" else Path, operation, fail)
        with pytest.raises(PermissionError, match="checkpoint publication failure"):
            file.save_checkpoint(_checkpoint())
    assert file.path.read_bytes() == before
    assert not file.path.with_name(".state.json.tmp").exists()
    assert file.load().checkpoint == _checkpoint()


def test_run_state_round_trip_and_completion(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )

    active = state_file.load()
    assert active.status == "active"
    assert active.observation_store == str((tmp_path / "observations.sqlite").absolute())
    assert active.checkpoint == _checkpoint()

    result = case / "results" / "result-001"
    state_file.complete(result)

    completed = state_file.load()
    assert completed.status == "completed"
    assert completed.result_path == str(result.absolute())
    with pytest.raises(RunStateError, match="already completed"):
        state_file.save_checkpoint(_checkpoint())


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_run_state_is_private(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()

    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )

    assert state_file.path.parent.parent.stat().st_mode & 0o777 == 0o700
    assert state_file.path.parent.stat().st_mode & 0o777 == 0o700
    assert state_file.path.stat().st_mode & 0o777 == 0o600


def test_run_state_refuses_symbolic_link_components(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(RunStateError, match="symbolic link"):
        RunStateFile.open(linked / "state.json").load()


def test_run_state_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )
    source = json.loads(state_file.path.read_text(encoding="utf-8"))
    rendered_checkpoint = json.dumps(source["checkpoint"])
    state_file.path.write_text(
        "{"
        '"schema_version": 1,'
        '"run_id": "run-001",'
        '"run_id": "other",'
        f'"source_case": {json.dumps(source["source_case"])},'
        f'"observation_store": {json.dumps(source["observation_store"])},'
        f'"checkpoint": {rendered_checkpoint},'
        '"status": "active",'
        '"result_path": null'
        "}\n",
        encoding="utf-8",
    )

    with pytest.raises(RunStateError, match="duplicate key 'run_id'"):
        state_file.load()


def test_run_state_rejects_oversized_file_before_reading(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )
    with state_file.path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(RunStateError, match=r"exceeds the .*byte limit"):
        state_file.load()


def test_run_state_lock_refuses_a_concurrent_owner(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )

    with (
        state_file.acquire_lock(),
        pytest.raises(RunStateBusyError, match="another process"),
        RunStateFile.open(state_file.path.parent).acquire_lock(),
    ):
        pytest.fail("concurrent lock acquisition succeeded")


def test_run_state_lock_rejects_hard_link_without_modifying_state(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    state_file = RunStateFile.create(
        case_root=case,
        run_id="run-001",
        observation_store=tmp_path / "observations.sqlite",
        checkpoint=_checkpoint(),
    )
    original = state_file.path.read_bytes()
    lock_path = state_file.path.with_name("run.lock")
    os.link(state_file.path, lock_path)

    with pytest.raises(RunStateError, match="run lock file is unsafe"), state_file.acquire_lock():
        pytest.fail("hard-linked state was accepted as a lock")

    assert state_file.path.read_bytes() == lock_path.read_bytes() == original
    assert state_file.load().checkpoint == _checkpoint()
