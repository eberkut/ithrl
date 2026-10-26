from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


@pytest.mark.parametrize(
    ("classification", "full", "succeeds"),
    [
        ("success", "true", True),
        ("success", "false", True),
        ("success", "", False),
        ("success", "unexpected", False),
        ("failure", "false", False),
        ("cancelled", "true", False),
        ("skipped", "false", False),
    ],
)
def test_required_workflows_gate_refuses_failed_or_missing_classification(
    classification: str,
    full: str,
    succeeds: bool,
) -> None:
    job = CI["jobs"]["workflows"]
    assert job["needs"] == "changes"
    assert job["if"] == "always()"
    result = subprocess.run(  # noqa: S603 - execute the checked-in gate without credentials.
        ["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", job["steps"][0]["run"]],
        env={
            "PATH": os.defpath,
            "CLASSIFICATION_RESULT": classification,
            "FULL_VALIDATION": full,
        },
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is succeeds


def test_documentation_and_distribution_checks_cannot_be_filtered_out() -> None:
    for name in ("docs", "package", "package-test"):
        assert "if" not in CI["jobs"][name]
    assert CI["jobs"]["package-test"]["needs"] == "package"
    # No workflow-level path filter can leave required checks permanently pending.
    assert CI["on"]["pull_request"] is None
    assert CI["on"]["push"] == {"branches": ["main"]}


def test_pr_title_workflow_reads_metadata_without_executing_pr_code() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-title.yml").read_text())
    assert workflow["permissions"] == {"pull-requests": "read"}
    assert set(workflow["on"]) == {"pull_request_target"}
    assert {"opened", "edited", "synchronize"} <= set(
        workflow["on"]["pull_request_target"]["types"]
    )
    steps = workflow["jobs"]["title"]["steps"]
    assert len(steps) == 1
    assert steps[0]["uses"].startswith("amannn/action-semantic-pull-request@")
    assert "run" not in steps[0]
