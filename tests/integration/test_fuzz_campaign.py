"""Whole-campaign crash recovery through real private state and SQLite, without providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fuzz_support import FuzzRunner, setup
from ithrl.calibration import run_sampled_calibration
from ithrl.case import CaseEvidence
from ithrl.fuzz.campaign import initialize_fuzz_campaign, run_fuzz_campaign
from ithrl.fuzz.contracts import FuzzCampaignConfig, FuzzOutcome
from ithrl.observations import ObservationStore
from ithrl.run_state import (
    RunStateBusyError,
    RunStateError,
    SampledRunState,
    SampledRunStateFile,
    SamplingDispatchReservation,
)
from ithrl.runner import PreparedAttempt, RunResult
from ithrl.runner.contracts import CandidateInvalidError
from ithrl.workflow import PreparedExperiment

pytest.importorskip("scipy")


type CampaignParts = tuple[
    CaseEvidence,
    PreparedExperiment,
    FuzzCampaignConfig,
    FuzzRunner,
    SampledRunStateFile,
    ObservationStore,
]


def campaign(tmp_path: Path, *, limit: int = 384) -> CampaignParts:
    evidence, prepared, config, runner, state, store, _ = setup(
        tmp_path, screen_valid=2, screen_attempts=3, limit=limit
    )
    calibration = run_sampled_calibration(evidence=evidence, prepared=prepared, store=store)
    assert calibration.outcome == "measurable"
    with state.acquire_lock(store):
        state.record_calibration(store, tuple(o.observation_id for o in calibration.observations))
    initialize_fuzz_campaign(
        evidence=evidence,
        prepared=prepared,
        campaign=config,
        stale_text="irrelevant\n\nSTALE",
        state_file=state,
        store=store,
    )
    return evidence, prepared, config, runner, state, store


def run(parts: CampaignParts, *, clock: Callable[[], datetime] | None = None):
    evidence, prepared, _, _, state, store = parts
    return asyncio.run(
        run_fuzz_campaign(
            evidence=evidence,
            prepared=prepared,
            state_file=state,
            store=store,
            clock=clock or (lambda: datetime.now(UTC)),
        )
    )


def test_completed_campaign_is_private_fresh_and_never_reexecuted(tmp_path: Path) -> None:
    parts = campaign(tmp_path)
    evidence, _, _, runner, state, store = parts
    original = evidence.request
    protected = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (tmp_path / "case").rglob("*")
        if p.is_file() and ".runs" not in p.parts
    }
    result = run(parts)
    assert result.outcome is FuzzOutcome.CONFIRMED_CHANGE
    assert result.confirmation is not None
    assert result.confirmation.lower_bound is not None
    assert result.confirmation.lower_bound > 0.1
    assert result.usage.actual_provider_calls == len(runner.calls)
    assert evidence.request == original
    assert all(
        hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in protected.items()
    )
    calls = len(runner.calls)
    repeated = run(parts)
    assert len(runner.calls) == calls
    assert repeated.selected_mutation == result.selected_mutation
    assert repeated.confirmation == result.confirmation
    with state.acquire_lock(store):
        saved = state.load(store)
        blocks = state.evidence(store)
        assert saved.selection is not None
        assert blocks[-1].plan == saved.selection.plan
        assert len({o for b in blocks for o in b.observation_ids}) == sum(
            len(b.observation_ids) for b in blocks
        )
        assert saved.fuzz_progress is not None
        assert saved.fuzz_progress.terminal is FuzzOutcome.CONFIRMED_CHANGE
    assert state.path.stat().st_mode & 0o777 == 0o600
    assert state.path.parent.stat().st_mode & 0o777 == 0o700
    assert "STALE" not in repr(result)


def test_no_candidate_is_bounded_and_has_no_confirmation(tmp_path: Path) -> None:
    parts = campaign(tmp_path)
    parts[3].mutant_matches = True
    result = run(parts)
    assert result.outcome is FuzzOutcome.NO_CANDIDATE_FOUND
    assert result.confirmation is None
    assert result.selected_mutation is None
    assert all(a.phase != "confirmation" for a in parts[3].calls)


def test_error_stops_campaign_without_becoming_unchanged_or_redrawn(tmp_path: Path) -> None:
    parts = campaign(tmp_path)
    parts[3].fail = True
    result = run(parts)
    assert result.outcome is FuzzOutcome.INCOMPLETE
    calls = len(parts[3].calls)
    assert run(parts).outcome is FuzzOutcome.INCOMPLETE
    assert len(parts[3].calls) == calls


def test_frozen_generation_tampering_refuses_before_dispatch(tmp_path: Path) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    saved = json.loads(state.path.read_bytes())
    saved["fuzz_progress"]["stale_text"] = "changed private payload"
    state.path.write_text(json.dumps(saved))
    before = len(parts[3].calls)
    with pytest.raises(RunStateError, match="generation"):
        run(parts)
    assert len(parts[3].calls) == before


def test_confirmation_inconclusive_ends_without_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = campaign(tmp_path)
    runner = parts[3]
    execute = runner.run

    async def changed_holdout(attempt: PreparedAttempt) -> RunResult:
        runner.mutant_matches = attempt.phase == "confirmation"
        return await execute(attempt)

    monkeypatch.setattr(runner, "run", changed_holdout)
    result = run(parts)
    assert result.outcome is FuzzOutcome.INCONCLUSIVE
    calls = len(runner.calls)
    assert run(parts).outcome is FuzzOutcome.INCONCLUSIVE
    assert len(runner.calls) == calls


def test_confirmation_reserve_refuses_exploration_before_calls(tmp_path: Path) -> None:
    parts = campaign(tmp_path, limit=72)
    before = len(parts[3].calls)
    result = run(parts)
    assert result.outcome is FuzzOutcome.INCOMPLETE
    assert result.stop_reason == "provider-call-budget"
    assert len(parts[3].calls) == before


def test_candidate_local_refusals_consume_work_without_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = campaign(tmp_path)

    def admit(attempt: PreparedAttempt) -> None:
        if not attempt.candidate.mutation.is_empty:
            raise CandidateInvalidError("synthetic candidate-local refusal")

    monkeypatch.setattr(parts[3], "validate_candidate", admit, raising=False)
    before = len(parts[3].calls)
    assert run(parts).outcome is FuzzOutcome.NO_CANDIDATE_FOUND
    assert len(parts[3].calls) == before
    with parts[4].acquire_lock(parts[5]):
        progress = parts[4].load(parts[5]).fuzz_progress
        assert progress is not None
        assert len(progress.trials) == len(progress.generated)
        assert all(trial.plan is None for trial in progress.trials)
    monkeypatch.delattr(parts[3], "validate_candidate")
    with pytest.raises(RunStateError, match="admission"):
        run(parts)


def test_concurrent_campaign_cannot_start_another_attempt(tmp_path: Path) -> None:
    parts = campaign(tmp_path)
    before = len(parts[3].calls)
    with parts[4].acquire_lock(parts[5]), pytest.raises(RunStateBusyError):
        run((*parts[:4], SampledRunStateFile.open(parts[4].path), parts[5]))
    assert len(parts[3].calls) == before


@pytest.mark.parametrize("target", ["state", "lock"])
@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_unsafe_campaign_paths_preserve_external_target(
    tmp_path: Path,
    target: str,
    link: str,
) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    victim = tmp_path / "private-target"
    victim.write_bytes(b"preserve these private bytes")
    victim.chmod(0o600)
    path = state.path if target == "state" else state.path.with_name("run.lock")
    path.unlink()
    if link == "hard":
        path.hardlink_to(victim)
    else:
        path.symlink_to(victim)
    before = len(parts[3].calls)
    with pytest.raises(RunStateError):
        run(parts)
    assert victim.read_bytes() == b"preserve these private bytes"
    assert len(parts[3].calls) == before


@pytest.mark.parametrize(
    ("field", "value"), [("implementation_digest", "0" * 64), ("terminal", "confirmed-change")]
)
def test_forged_implementation_or_verdict_refuses_before_dispatch(
    tmp_path: Path, field: str, value: str
) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    saved = json.loads(state.path.read_bytes())
    saved["fuzz_progress"][field] = value
    if field == "terminal":
        saved["fuzz_progress"]["stop_reason"] = "confirmation-finished"
    state.path.write_text(json.dumps(saved))
    before = len(parts[3].calls)
    with pytest.raises(RunStateError):
        run(parts)
    assert len(parts[3].calls) == before


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary", ["trial", "observation", "classified", "selection", "confirmation", "result"]
)
@pytest.mark.parametrize("after_write", [False, True])
def test_crash_recovers_exact_campaign_without_duplicate_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    after_write: bool,
) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    write = state._write
    crashed = False

    def interrupt(updated: SampledRunState) -> None:
        nonlocal crashed
        progress = updated.fuzz_progress
        selected = updated.selection
        last = updated.blocks[-1] if updated.blocks else None
        hit = (
            (boundary == "trial" and progress and len(progress.trials) == 1 and not last)
            or (
                boundary == "observation"
                and last
                and last.observations
                and not updated.pending
                and not selected
            )
            or (boundary == "classified" and last and last.archived)
            or (
                boundary == "selection"
                and selected
                and (not last or last.plan.role != "confirmation")
            )
            or (
                boundary == "confirmation"
                and last
                and last.plan.role == "confirmation"
                and len(last.observations) == 2
                and not updated.pending
            )
            or (boundary == "result" and progress and progress.terminal)
        )
        if hit and not crashed:
            crashed = True
            if after_write:
                write(updated)
            raise Crash()
        write(updated)

    monkeypatch.setattr(state, "_write", interrupt)
    with pytest.raises(Crash):
        run(parts)
    assert crashed
    reopened = (*parts[:4], SampledRunStateFile.open(state.path), parts[5])
    result = run(reopened)
    assert result.outcome is FuzzOutcome.CONFIRMED_CHANGE
    attempts = [a.attempt_id for a in parts[3].calls]
    assert len(attempts) == len(set(attempts))
    assert result.usage.actual_provider_calls == len(attempts)
    before = len(attempts)
    assert run(reopened).outcome is FuzzOutcome.CONFIRMED_CHANGE
    assert len(parts[3].calls) == before


def test_unresolved_reservation_retains_charge_and_never_redispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    reserve = state.reserve_dispatch

    def interrupt(store: ObservationStore, reservation: SamplingDispatchReservation) -> None:
        reserve(store, reservation)
        raise Crash()

    monkeypatch.setattr(state, "reserve_dispatch", interrupt)
    with pytest.raises(Crash):
        run(parts)
    before = len(parts[3].calls)
    reopened = (*parts[:4], SampledRunStateFile.open(state.path), parts[5])
    result = run(reopened)
    assert result.outcome is FuzzOutcome.INCOMPLETE
    assert result.stop_reason == "unresolved-dispatch"
    assert result.usage.pending_provider_calls == 1
    assert result.usage.charged_provider_calls == before + 1
    assert len(parts[3].calls) == before


def test_stale_unfinished_plan_cannot_be_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = campaign(tmp_path)
    state = parts[4]
    write = state._write

    def interrupt(updated: SampledRunState) -> None:
        write(updated)
        if updated.fuzz_progress and updated.fuzz_progress.trials:
            raise Crash()

    monkeypatch.setattr(state, "_write", interrupt)
    with pytest.raises(Crash):
        run(parts)
    reopened = (*parts[:4], SampledRunStateFile.open(state.path), parts[5])
    before = len(parts[3].calls)
    result = run(reopened, clock=lambda: datetime.now(UTC) + timedelta(days=2))
    assert result.outcome is FuzzOutcome.INCOMPLETE
    assert len(parts[3].calls) == before


@pytest.mark.parametrize("after_write", [False, True])
def test_completed_holdout_recovers_after_expiry_without_another_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_write: bool,
) -> None:
    parts = campaign(tmp_path)
    state, store = parts[4:]
    write = state._write
    interrupted = False

    def interrupt(updated: SampledRunState) -> None:
        nonlocal interrupted
        if (
            not interrupted
            and updated.blocks
            and not updated.pending
            and updated.blocks[-1].plan.role == "confirmation"
            and len(updated.blocks[-1].observations) == 40
        ):
            interrupted = True
            if after_write:
                write(updated)
            raise Crash()
        write(updated)

    monkeypatch.setattr(state, "_write", interrupt)
    with pytest.raises(Crash):
        run(parts)
    before = len(parts[3].calls)
    reopened = (*parts[:4], SampledRunStateFile.open(state.path), store)
    result = run(reopened, clock=lambda: datetime.now(UTC) + timedelta(days=2))
    assert result.outcome is FuzzOutcome.CONFIRMED_CHANGE
    assert result.usage.actual_provider_calls == before
    assert result.usage.pending_provider_calls == 0
    assert len(parts[3].calls) == before
    assert result.confirmation is not None
    assert result.confirmation.outcome is FuzzOutcome.CONFIRMED_CHANGE
