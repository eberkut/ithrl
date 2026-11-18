"""Prepare frozen public-task bundles and replay repairs through ithrl's patch grader."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from reproduce import ROOT, apply_changes, digest, environment, prepare_task

from ithrl.context import content_digest
from ithrl.grading import grade_response
from ithrl.grading.bundle import bind_environment, write_bundle
from ithrl.grading.contracts import (
    GradeEvidence,
    PatchGradingConfig,
    TestSuite,
    current_implementation_digest,
)
from ithrl.path_safety import validate_directory_chain


def repair_patch(
    original: Path, repaired: Path, editable: tuple[str, ...], omitted_metadata: tuple[str, ...]
) -> str:
    """Render exact source changes; reviewed fixtures must not alter verifier files."""

    before = {
        path.relative_to(original).as_posix(): path
        for path in original.rglob("*")
        if path.is_file()
    }
    after = {
        path.relative_to(repaired).as_posix(): path
        for path in repaired.rglob("*")
        if path.is_file()
    }
    if before.keys() != after.keys():
        raise ValueError("reviewed repairs must preserve the task's file set")
    differences = []
    for name in sorted(before):
        previous = before[name].read_bytes()
        current = after[name].read_bytes()
        if previous == current:
            continue
        if name in omitted_metadata:
            continue
        if name not in editable:
            raise ValueError("reviewed repair changed a file outside the frozen edit domain")
        for line in difflib.unified_diff(
            io.StringIO(previous.decode("utf-8"), newline="\n").readlines(),
            io.StringIO(current.decode("utf-8"), newline="\n").readlines(),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        ):
            differences.append(line)
            if not line.endswith("\n"):
                differences.append("\n\\ No newline at end of file\n")
    return "".join(differences)


def prepare_bundle(
    task: dict[str, Any],
    source: Path,
    checks: Path,
    destination: Path,
    python: Path,
    environment_digest: str,
) -> PatchGradingConfig:
    command = task["official_command"]
    if (
        len(command) == 8
        and command[:6] == ["-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider"]
        and command[-1] == "-q"
    ):
        source_suite = TestSuite(
            kind="pytest",
            selector=command[6],
            expected_count=task["official_test_count"],
        )
    elif len(command) == 4 and command[:2] == ["-m", "unittest"] and command[-1] == "-q":
        source_suite = TestSuite(
            kind="unittest", selector=command[2], expected_count=task["official_test_count"]
        )
    else:
        raise ValueError("task source suite has no reviewed fixed invocation")
    bundle_digest = write_bundle(
        source=source,
        checks=checks,
        destination=destination,
        editable_paths=tuple(task["context_files"]),
        python_path=task["python_path"],
        source_suite=source_suite,
        independent_suite=TestSuite(
            kind="unittest",
            selector=Path(task["independent_file"]).name,
            expected_count=task["independent_test_count"],
        ),
    )
    config = PatchGradingConfig(
        bundle_path=str(destination.absolute()),
        bundle_digest=bundle_digest,
        environment_path=str(python.parent.parent),
        environment_digest=environment_digest,
        timeout_seconds=10,
    )
    destination.with_suffix(".json").write_text(config.model_dump_json(indent=2) + "\n")
    return config


async def replay(
    *,
    config: PatchGradingConfig,
    patch: str,
    task: str,
    variant: str,
) -> GradeEvidence:
    response_digest = hashlib.sha256(patch.encode()).hexdigest()
    identity = content_digest(
        {
            "task": task,
            "variant": variant,
            "bundle": config.bundle_digest,
            "response": response_digest,
        }
    )
    return await grade_response(
        config,
        patch,
        candidate_digest=identity,
        attempt_id=f"attempt-{identity[:32]}",
    )


def prepare(cache: Path, python: Path, output: Path) -> dict[str, Any]:
    validate_directory_chain(output)
    if not output.parent.is_dir():
        raise ValueError("grading output requires an existing parent directory")
    task_bytes = (ROOT / "tasks.json").read_bytes()
    data = json.loads(task_bytes)
    environment_digest = bind_environment(python.parent.parent)
    _, actual_environment = environment(python, data["environment"])
    output.mkdir(mode=0o700)
    (output / "patches").mkdir(mode=0o700)
    receipt: dict[str, Any] = {
        "tasks_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "preparer_sha256": digest(Path(__file__)),
        "environment": actual_environment,
        "environment_digest": environment_digest,
        "grader_implementation_digest": current_implementation_digest(),
        "source_archives_sha256": {},
        "fixtures_sha256": {},
        "bundles": {},
        "results": [],
        "native_agents_started": 0,
        "provider_inference_requests": 0,
    }

    def check(
        task: dict[str, Any],
        variant: str,
        config: PatchGradingConfig,
        original: Path,
        repaired: Path,
        expected: dict[str, bool],
        omitted_metadata: tuple[str, ...] = (),
    ) -> None:
        if config.implementation_digest != receipt["grader_implementation_digest"]:
            raise ValueError("grading implementation changed during task preparation")
        expected_grade = "pass" if all(expected.values()) else "fail"
        patch = repair_patch(original, repaired, tuple(task["context_files"]), omitted_metadata)
        if not patch and variant != "buggy":
            raise ValueError("only the buggy no-repair control may have an empty patch")
        name = f"{task['key']}-{variant}.patch"
        (output / "patches" / name).write_text(patch)
        grade = asyncio.run(replay(config=config, patch=patch, task=task["key"], variant=variant))
        receipt["results"].append(
            {
                "task": task["key"],
                "variant": variant,
                "patch_sha256": digest(output / "patches" / name),
                "omitted_noncode_changes": list(omitted_metadata),
                "expected": expected_grade,
                "grade": grade.model_dump(mode="json"),
            }
        )
        (output / "grading-verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if grade.status != expected_grade:
            raise ValueError(
                f"unexpected product grade for {task['key']}/{variant}: {grade.status}"
            )

        if patch and (
            len(grade.suites) != 2
            or any(row.collected != row.expected or row.errors for row in grade.suites)
            or {row.role: row.failed == 0 for row in grade.suites}
            != {"source": expected["official"], "independent": expected["independent"]}
        ):
            raise ValueError(f"unexpected verifier outcomes for {task['key']}/{variant}")

    for task in data["tasks"]:
        with tempfile.TemporaryDirectory(prefix="ithrl-grader-preparation-") as temporary:
            tree = Path(temporary)
            original, independent = prepare_task(
                cache, task, tree, receipt["source_archives_sha256"], receipt["fixtures_sha256"]
            )
            checks = independent.parent
            config = prepare_bundle(
                task, original, checks, output / task["key"], python, environment_digest
            )
            receipt["bundles"][task["key"]] = config.bundle_digest
            for variant in task["variants"]:
                repaired = tree / variant["name"]
                shutil.copytree(original, repaired)
                apply_changes(repaired, variant, receipt["fixtures_sha256"])
                check(
                    task,
                    variant["name"],
                    config,
                    original,
                    repaired,
                    variant["expected"],
                    ("CHANGELOG.md",)
                    if task["key"] == "migration-linter" and variant["name"] == "upstream-reference"
                    else (),
                )
            changed = tree / "changed-defect"
            changed_key = f"{task['key']}-changed-defect"
            changed_config = prepare_bundle(
                task, changed, checks, output / changed_key, python, environment_digest
            )
            receipt["bundles"][changed_key] = changed_config.bundle_digest
            check(
                task,
                "changed-source-reference",
                changed_config,
                changed,
                tree / "changed-reference",
                {"official": True, "independent": True},
            )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt = prepare(args.cache.absolute(), args.python.absolute(), args.output.absolute())
    sys.stdout.write(
        json.dumps(
            {
                "status": "verified",
                "repair_variants": len(receipt["results"]),
                "task_bundles": len(receipt["bundles"]),
                "native_agents_started": 0,
                "provider_inference_requests": 0,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
