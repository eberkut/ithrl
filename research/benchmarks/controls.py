"""Bind prospective context controls to reproduced incident inputs, without native calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from ithrl.context import content_digest
from ithrl.text_sections import (
    TEXT_SECTION_POLICY_DIGEST,
    catalog_text_sections,
    remove_text_sections,
)

ROOT = Path(__file__).resolve().parent
SOURCE_ACKNOWLEDGEMENT = "ACK"


def build_controls(replay: Path) -> dict[str, object]:
    task_bytes = (ROOT / "tasks.json").read_bytes()
    tasks = json.loads(task_bytes)["tasks"]
    receipt = json.loads((replay / "verification.json").read_text())
    if hashlib.sha256(task_bytes).hexdigest() != receipt["tasks_sha256"]:
        raise ValueError("task definitions differ from the replay receipt")
    rows = []
    for task in tasks:
        name = f"inputs/{task['key']}-buggy.md"
        original = (replay / name).read_text()
        digest = hashlib.sha256(original.encode()).hexdigest()
        if digest != receipt["incident_inputs"][name]["sha256"]:
            raise ValueError("incident input differs from the replay receipt")
        catalog = catalog_text_sections(
            case_digest=content_digest({"prototype_task": task["key"]}),
            context_id="prototype-user-report",
            context_digest=content_digest(original),
            content_index=0,
            text=original,
        )
        expected_candidate = None
        if "nondiagnostic_context_file" in task:
            selector = task["nondiagnostic_context_file"]
            start = original.index(f"## {selector}\n")
            next_file = original.find("\n## ", start + 1)
            end = len(original) if next_file == -1 else next_file + 1
            expected_candidate = original[:start] + original[end:]
            removed = [section for section in catalog.sections if start <= section.start < end]
        else:
            selector = task["nondiagnostic_section_contains"]
            removed = [
                section
                for section in catalog.sections
                if selector in original[section.start : section.end]
            ]
        removed_bytes = sum(section.utf8_bytes for section in removed)
        if not removed or removed_bytes < 512:
            raise ValueError("prospective section control does not meet the frozen byte threshold")
        candidate = remove_text_sections(
            original, catalog, catalog.removal(tuple(section.id for section in removed))
        )
        if expected_candidate is not None and candidate != expected_candidate:
            raise ValueError("file removal changed another source snapshot")
        changed = f"inputs/{task['key']}-changed-defect.md"
        changed_digest = hashlib.sha256((replay / changed).read_bytes()).hexdigest()
        if changed_digest != receipt["incident_inputs"][changed]["sha256"]:
            raise ValueError("changed-defect input differs from the replay receipt")
        rows.append(
            {
                "task": task["key"],
                "original_input": name,
                "original_input_sha256": digest,
                "section_control": {
                    "selector": selector,
                    "removed_utf8_bytes": removed_bytes,
                    "source_codepoint_ranges": [
                        [section.start, section.end] for section in removed
                    ],
                    "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
                    "expected_task_grade": "preserved; model invariance is untested",
                },
                "withheld_control": "remove the whole prior-user incident, including symptoms",
                "changed_defect_input": changed,
                "changed_defect_sha256": changed_digest,
                "changed_defect_requires_new_source_generation": True,
            }
        )
    return {
        "binding_scope": "provider-free incident prototypes; not extracted native cases",
        "paragraph_policy_digest": TEXT_SECTION_POLICY_DIGEST,
        "source_acknowledgement": SOURCE_ACKNOWLEDGEMENT,
        "source_generation_first_message": (
            "Keep the following incident capture for the next request. "
            f"Reply with exactly {SOURCE_ACKNOWLEDGEMENT} and nothing else. "
            "Do not diagnose it or call tools.\n\n{incident}"
        ),
        "fixed_trigger": (
            "Diagnose the reported behavior and return a unified diff that repairs it. "
            "Preserve the documented behavior and existing tests. Work from the supplied "
            "snapshots; do not call tools. Return only the diff."
        ),
        "cell_order": [
            "iniconfig/codex",
            "iniconfig/copilot",
            "migration-linter/copilot",
            "migration-linter/codex",
        ],
        "native_rebinding_required": True,
        "native_context_dependence_observed": False,
        "search_or_confirmation_included": False,
        "controls": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--replay", required=True, type=Path)
    args = parser.parse_args()
    sys.stdout.write(json.dumps(build_controls(args.replay), indent=2) + "\n")
