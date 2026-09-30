"""Select one exact native runner and resolve its sampled experiment.

The local selection convention is the executable on PATH, the current Python
executable, and CODEX_HOME or COPILOT_HOME when set (otherwise ~/.codex or
~/.copilot). No missing provider identity or authentication source is inferred
from case content. Detection uses local structural probes only; provider calls
belong to the separately acknowledged calibration command.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.case.filesystem import reject_symlink_components
from ithrl.cli.paths import private_data_root
from ithrl.execution import PreflightIssue, ResourceLimits, SafetyPolicy, calibration_budget_bounds
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    UnavailableRunnerConfig,
    parse_experiment_toml,
)
from ithrl.runner import RunnerRegistry
from ithrl.runner.codex import (
    CodexRunner,
    CodexRuntime,
    detect_codex_capability,
    resolve_codex_configuration,
)
from ithrl.runner.codex.capability import CodexCapabilityError
from ithrl.runner.codex.configuration import CodexConfigurationError
from ithrl.runner.codex.isolation import CodexIsolationError
from ithrl.runner.contracts import Runner
from ithrl.runner.copilot import (
    CopilotRunner,
    CopilotRuntime,
    detect_copilot_capability,
    resolve_copilot_configuration,
)
from ithrl.runner.copilot.capability import CopilotCapabilityError
from ithrl.runner.copilot.configuration import CopilotConfigurationError
from ithrl.runner.copilot.isolation import CopilotIsolationError
from ithrl.runner.copilot.policy import CopilotTarget
from ithrl.runner.runtime import NativeOutputStoreError, ProtectedPath

_MAX_WALL_TIME_SECONDS = 300
_MAX_NATIVE_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_DISPOSABLE_STATE_BYTES = 64 * 1024 * 1024
_MAX_DISPOSABLE_STATE_ENTRIES = 10_000


class NativeSelectionError(RuntimeError):
    """Content-safe refusal before a native runner attempt begins."""

    def __init__(self, code: str, message: str, *, harness_started: bool | None = False) -> None:
        super().__init__(message)
        self.code = code
        self.harness_started = harness_started


@dataclass(frozen=True, slots=True)
class NativeSelection:
    """One exact registry entry and the bounds used to resolve its plan."""

    registry: RunnerRegistry
    safety_policy: SafetyPolicy
    resource_limits: ResourceLimits
    experiment: SampledExperimentDefinition = field(repr=False)


def native_selection(
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    *,
    original_workspace: Path | None,
    calibration: SampledCalibrationPhase | None = None,
    max_provider_calls: int | None = None,
    attempt_timeout_seconds: int | None = None,
    max_input_tokens: int | None = None,
) -> NativeSelection:
    """Resolve explicit samples against an exact current or persisted profile."""

    persisted = parse_experiment_toml(evidence.default_experiment)
    if isinstance(persisted, SampledExperimentDefinition):
        harness = persisted.runner.type
        required_digest = persisted.runner.required_capability_digest
        phase = calibration or persisted.calibration
    elif isinstance(persisted.runner, UnavailableRunnerConfig):
        harness = persisted.runner.harness
        required_digest = None
        if calibration is None:
            raise NativeSelectionError(
                "calibration-settings-required", "explicit calibration sample settings are required"
            )
        phase = calibration
    else:
        raise NativeSelectionError(
            "sampled-experiment-required", "native selection requires a sampled experiment"
        )
    if harness not in ("codex", "copilot"):
        raise NativeSelectionError("unsupported-harness", "native harness is unsupported")
    native_type: Literal["codex", "copilot"] = "codex" if harness == "codex" else "copilot"
    if evidence.manifest.harness != harness:
        raise NativeSelectionError(
            "runner-evidence-mismatch", "native runner does not match the captured harness"
        )
    if not evidence.source_artifacts:
        raise NativeSelectionError(
            "source-evidence-unavailable", "native selection requires saved source evidence"
        )

    protected_workspace = _original_workspace(original_workspace)
    sources = (ProtectedPath("case-evidence", workspace.root),)
    workspaces = (ProtectedPath("original-workspace", protected_workspace),)
    state_parent, output_root = _storage_paths(workspace, protected_workspace)

    if harness == "codex":
        runner = _codex_runner(
            required_digest,
            evidence,
            persisted.target.type,
            state_parent=state_parent,
            output_root=output_root,
            sources=sources,
            workspaces=workspaces,
        )
    else:
        runner = _copilot_runner(
            required_digest,
            persisted.target.type,
            state_parent=state_parent,
            output_root=output_root,
            sources=sources,
            workspaces=workspaces,
        )

    capability = runner.capability_profile
    config = NativeRunnerConfig(
        type=native_type,
        required_capability_digest=capability.digest,
    )
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=persisted.target,
        oracle=persisted.oracle,
        runner=config,
        calibration=phase,
    )
    if capability.provider is None or capability.endpoint is None:
        raise NativeSelectionError(
            "provider-destination-unavailable",
            "native provider destination is unavailable",
            harness_started=True,
        )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider=capability.provider,
        allowed_endpoint=capability.endpoint,
    )
    limits = ResourceLimits(
        max_provider_calls=capability.provider_calls_per_attempt,
        max_input_tokens=max_input_tokens,
        max_wall_time_seconds=(
            attempt_timeout_seconds
            if attempt_timeout_seconds is not None
            else _MAX_WALL_TIME_SECONDS
        ),
        max_native_output_bytes=_MAX_NATIVE_OUTPUT_BYTES,
        max_disposable_state_bytes=_MAX_DISPOSABLE_STATE_BYTES,
        max_disposable_state_entries=_MAX_DISPOSABLE_STATE_ENTRIES,
    )
    if max_input_tokens is not None and capability.input_token_limit_enforcement != "pre-dispatch":
        raise NativeSelectionError(
            "input-token-limit-unenforceable",
            "runner cannot enforce the requested input token cap",
            harness_started=True,
        )
    bounds = calibration_budget_bounds(phase, capability, limits)
    if max_provider_calls is not None and bounds.provider_calls > max_provider_calls:
        raise NativeSelectionError(
            "provider-call-limit-too-small",
            "calibration exceeds the requested provider call cap",
            harness_started=True,
        )

    def exact_runner(selected: object, selected_evidence: CaseEvidence) -> Runner | PreflightIssue:
        if selected != config or selected_evidence.digest != evidence.digest:
            return PreflightIssue(
                code="runner-evidence-mismatch",
                message="native runner does not match the selected case and experiment",
            )
        return runner

    return NativeSelection(
        registry=RunnerRegistry({harness: exact_runner}),
        safety_policy=policy,
        resource_limits=limits,
        experiment=experiment,
    )


def _codex_runner(
    required_capability_digest: str | None,
    evidence: CaseEvidence,
    target_type: str,
    *,
    state_parent: Path,
    output_root: Path,
    sources: tuple[ProtectedPath, ...],
    workspaces: tuple[ProtectedPath, ...],
) -> CodexRunner:
    if target_type != "final-response":
        raise NativeSelectionError(
            "unsupported-target", "Codex runner does not support the selected target"
        )
    provider_id = evidence.manifest.provider
    if provider_id is None:
        raise NativeSelectionError(
            "provider-unavailable", "captured Codex provider identity is unavailable"
        )
    codex = _executable("codex")
    bubblewrap = _executable("bwrap")
    python = Path(sys.executable)
    if not sys.executable:
        raise NativeSelectionError("runtime-unavailable", "native Python runtime is unavailable")
    try:
        runtime = CodexRuntime(codex=codex, bubblewrap=bubblewrap, python=python)
        home = _harness_home("CODEX_HOME", ".codex")
        configuration = resolve_codex_configuration(
            provider_id=provider_id,
            config_file=_optional_file(home / "config.toml"),
            auth_file=_optional_file(home / "auth.json"),
            environment=os.environ,
        )
    except CodexConfigurationError as error:
        raise NativeSelectionError(
            f"codex-{error.code.value}", "local Codex configuration cannot be reproduced"
        ) from error
    except (CodexIsolationError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "runtime-unavailable", "local Codex runtime is unavailable"
        ) from error

    try:
        profile = detect_codex_capability(runtime, configuration)
    except CodexCapabilityError as error:
        raise NativeSelectionError(
            f"codex-{error.code.value}",
            "local Codex capability cannot be established",
            harness_started=None,
        ) from error
    except (CodexIsolationError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "codex-probe-failed",
            "local Codex capability cannot be established",
            harness_started=None,
        ) from error
    if required_capability_digest is not None and profile.digest != required_capability_digest:
        raise NativeSelectionError(
            "capability-profile-mismatch",
            "local capability does not match the experiment",
            harness_started=True,
        )
    try:
        return CodexRunner(
            capability_profile=profile,
            runtime=runtime,
            configuration=configuration,
            state_parent=state_parent,
            output_root=output_root,
            source_paths=sources,
            workspace_paths=workspaces,
        )
    except (CodexIsolationError, NativeOutputStoreError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "runner-unavailable", "Codex runner cannot be prepared", harness_started=True
        ) from error


def _copilot_runner(
    required_capability_digest: str | None,
    target: str,
    *,
    state_parent: Path,
    output_root: Path,
    sources: tuple[ProtectedPath, ...],
    workspaces: tuple[ProtectedPath, ...],
) -> CopilotRunner:
    if target not in ("final-response", "shell-action"):
        raise NativeSelectionError(
            "unsupported-target", "Copilot runner does not support the selected target"
        )
    copilot_target: CopilotTarget = "shell-action" if target == "shell-action" else "final-response"
    copilot = _executable("copilot")
    bubblewrap = _executable("bwrap")
    try:
        runtime = CopilotRuntime(copilot=copilot, bubblewrap=bubblewrap)
        home = _harness_home("COPILOT_HOME", ".copilot")
        configuration = resolve_copilot_configuration(
            environment=os.environ,
            config_file=_optional_file(home / "config.json"),
        )
    except CopilotConfigurationError as error:
        raise NativeSelectionError(
            f"copilot-{error.code.value}", "local Copilot configuration cannot be reproduced"
        ) from error
    except (CopilotIsolationError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "runtime-unavailable", "local Copilot runtime is unavailable"
        ) from error

    try:
        detected = detect_copilot_capability(runtime, configuration, target=copilot_target)
    except CopilotCapabilityError as error:
        raise NativeSelectionError(
            f"copilot-{error.code.value}",
            "local Copilot capability cannot be established",
            harness_started=None,
        ) from error
    except (CopilotIsolationError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "copilot-probe-failed",
            "local Copilot capability cannot be established",
            harness_started=None,
        ) from error
    if (
        required_capability_digest is not None
        and detected.profile.digest != required_capability_digest
    ):
        raise NativeSelectionError(
            "capability-profile-mismatch",
            "local capability does not match the experiment",
            harness_started=True,
        )
    try:
        return CopilotRunner(
            detected_capability=detected,
            state_parent=state_parent,
            output_root=output_root,
            source_paths=sources,
            workspace_paths=workspaces,
        )
    except (CopilotIsolationError, NativeOutputStoreError, OSError, ValueError) as error:
        raise NativeSelectionError(
            "runner-unavailable", "Copilot runner cannot be prepared", harness_started=True
        ) from error


def _original_workspace(path: Path | None) -> Path:
    if path is None:
        raise NativeSelectionError(
            "original-workspace-required", "native execution requires --original-workspace"
        )
    try:
        if ".." in path.parts:
            raise ValueError
        reject_symlink_components(path)
        original = path.resolve(strict=True)
        if not original.is_dir():
            raise ValueError
    except (OSError, ValueError) as error:
        raise NativeSelectionError(
            "original-workspace-unavailable", "original workspace is unavailable or unsafe"
        ) from error
    return original


def _storage_paths(workspace: CaseWorkspace, original_workspace: Path) -> tuple[Path, Path]:
    root = user_data_path("ithrl", ensure_exists=False).absolute().resolve(strict=False)
    protected = (workspace.root.resolve(strict=False), original_workspace)
    if any(_paths_overlap(root, path) for path in protected):
        raise NativeSelectionError(
            "storage-overlap", "private runner storage overlaps protected state"
        )
    try:
        root = private_data_root()
    except (OSError, ValueError) as error:
        raise NativeSelectionError(
            "storage-unavailable", "private runner storage is unavailable"
        ) from error
    return root / "native-attempts", root / "native-output"


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _executable(name: str) -> Path:
    selected = shutil.which(name)
    if selected is None:
        raise NativeSelectionError(
            "runtime-unavailable", "native runtime executable is unavailable"
        )
    return Path(selected)


def _harness_home(environment_name: str, default_name: str) -> Path:
    selected = os.environ.get(environment_name)
    if selected is not None and (not selected or not Path(selected).is_absolute()):
        raise NativeSelectionError(
            "configuration-home-invalid", "local harness configuration home is invalid"
        )
    home = Path(selected) if selected is not None else Path.home() / default_name
    try:
        reject_symlink_components(home)
    except ValueError as error:
        raise NativeSelectionError(
            "configuration-home-invalid", "local harness configuration home is invalid"
        ) from error
    return home


def _optional_file(path: Path) -> Path | None:
    return path if os.path.lexists(path) else None


__all__ = ["NativeSelection", "NativeSelectionError", "native_selection"]
