"""Explicit live-run gates and harmless, source-backed smoke cases."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseWorkspace
from ithrl.case.filesystem import reject_symlink_components
from ithrl.extraction import extract_decision
from native_support import codex, copilot

type Profile = Literal["codex-final", "copilot-final", "copilot-shell"]
PROFILES: tuple[Profile, ...] = ("codex-final", "copilot-final", "copilot-shell")
RESPONSE = "ITHRL_SMOKE_OK"
FINAL_PROMPT = "Reply with exactly ITHRL_SMOKE_OK. Do not use tools."
SHELL_PROMPT = (
    "Request the bash tool exactly once with command pwd. Do not run any other tool or command."
)


def protected_tree(
    root: Path, *, allow_case_results: bool = False
) -> dict[Path, tuple[bytes | None, int]]:
    """Snapshot protected bytes/modes; only the case's own results may change."""
    return {
        path.relative_to(root): (
            path.read_bytes() if path.is_file() else None,
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in (root, *root.rglob("*"))
        if not (allow_case_results and path.relative_to(root).parts[:1] == ("results",))
    }


@dataclass(frozen=True)
class SmokeOptions:
    mode: Literal["preflight", "run"]
    profile: Profile
    model: str
    provider: str
    root: Path
    capability_digest: str | None = None

    def validate(self, *, ci: bool) -> None:
        if ci:
            raise ValueError("live native smoke modes are forbidden in CI")
        if self.profile not in PROFILES or self.mode not in ("preflight", "run"):
            raise ValueError("select one explicit native profile and smoke mode")
        if not re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", self.model):
            raise ValueError("an explicit bounded model identifier is required")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", self.provider):
            raise ValueError("an explicit bounded provider identifier is required")
        if self.profile != "codex-final" and self.provider != "github-copilot":
            raise ValueError("Copilot requires provider github-copilot")
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("smoke evidence root must be absolute without traversal")
        reject_symlink_components(self.root)
        checkout = Path(__file__).resolve().parents[2]
        if self.root.resolve().is_relative_to(checkout):
            raise ValueError("smoke evidence parent must be outside the checkout")
        if not self.root.is_dir():
            raise ValueError("smoke evidence parent must already exist")
        if self.mode == "run" and (
            self.capability_digest is None
            or re.fullmatch(r"[0-9a-f]{64}", self.capability_digest) is None
        ):
            raise ValueError(
                "provider execution requires the separately approved capability digest"
            )

    @property
    def provider_call_bound(self) -> int:
        return 4 if self.profile == "copilot-shell" else 2


def synthetic_case(root: Path, options: SmokeOptions) -> tuple[Path, CaseWorkspace, Path]:
    """Generate only synthetic evidence; never inspect a user's saved sessions."""
    original = root / "original"
    original.mkdir(mode=0o700)
    source = root / ("rollout.jsonl" if options.profile == "codex-final" else "events.jsonl")
    if options.profile == "codex-final":
        records = [
            json.loads(line)
            for line in (codex.FIXTURES / "codex-session/rollout.jsonl").read_text().splitlines()
        ]
        records = [records[index] for index in (0, 1, 2, 3, 12)]
        records[0]["payload"]["model_provider"] = options.provider
        records[0]["payload"]["cwd"] = str(original)
        records[0]["payload"]["base_instructions"] = {
            "text": "Follow the harmless smoke-test request."
        }
        records[0]["payload"].pop("git", None)
        records[1]["payload"]["model"] = options.model
        records[1]["payload"]["cwd"] = str(original)
        records[1]["payload"]["workspace_roots"] = [str(original)]
        records[3]["payload"]["content"] = [{"type": "input_text", "text": FINAL_PROMPT}]
        records[4]["payload"]["last_agent_message"] = RESPONSE
    else:
        target = "shell-action" if options.profile == "copilot-shell" else "final-response"
        records = copilot._source_records(target)
        records[0]["data"]["selectedModel"] = options.model
        records[1]["data"]["content"] = SHELL_PROMPT if target == "shell-action" else FINAL_PROMPT
        records[3]["data"]["model"] = options.model
        if target == "final-response":
            records[3]["data"]["content"] = RESPONSE
    source.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    source.chmod(0o600)
    session = parse_session(source)
    decision = enumerate_decisions(session)[0]
    expected = "shell-action" if options.profile == "copilot-shell" else "final-response"
    target_id = next(target.target_id for target in decision.targets if target.type == expected)
    case = extract_decision(
        session, decision_id=decision.decision_id, target_id=target_id, case_path=root / "case"
    )
    return source, case, original
