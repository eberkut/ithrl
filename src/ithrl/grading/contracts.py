"""Identity and result contracts for isolated repair grading."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.context import content_digest

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GRADING_CLEANUP_SECONDS = 2


def _implementation_files() -> dict[str, bytes]:
    root = Path(__file__).parent
    return {
        name: (root / name).read_bytes()
        for name in ("contracts.py", "bundle.py", "runtime.py", "driver.py", "patch_driver.py")
    }


def current_implementation_digest() -> str:
    """Bind the actual grading implementation, including its isolated test driver."""

    return content_digest(
        {name: hashlib.sha256(data).hexdigest() for name, data in _implementation_files().items()}
    )


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class PatchGradingConfig(_Contract):
    """An immutable task bundle and Python environment for grading cooperative repairs."""

    type: Literal["python-patch-tests"] = "python-patch-tests"
    bundle_path: str = Field(min_length=1, max_length=4096, repr=False)
    bundle_digest: Digest
    environment_path: str = Field(min_length=1, max_length=4096, repr=False)
    environment_digest: Digest
    implementation_digest: Digest = Field(default_factory=current_implementation_digest)
    timeout_seconds: int = Field(default=10, ge=1, le=30)

    @field_validator("bundle_path", "environment_path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
            raise ValueError("grading paths must be absolute without parent traversal")
        return value

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class TestSuite(_Contract):
    """One fixed suite; selectors are data, never executable command strings."""

    __test__ = False
    kind: Literal["pytest", "unittest"]
    selector: str = Field(min_length=1, max_length=512)
    expected_count: int = Field(ge=1, le=10_000)

    @field_validator("selector")
    @classmethod
    def safe_selector(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or any(c.isspace() for c in value):
            raise ValueError("test selector must be a relative path or module")
        if value.startswith("-") or "\x00" in value:
            raise ValueError("test selector is not a command option")
        return value


class BundleManifest(_Contract):
    """The source and verifier files included in a task's complete content identity."""

    schema_version: Literal[1] = 1
    files: dict[str, Digest]
    editable_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    python_path: str = Field(min_length=1, max_length=512)
    source_suite: TestSuite
    independent_suite: TestSuite

    @field_validator("editable_paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def safe_paths(self) -> BundleManifest:
        for value in (*self.files, *self.editable_paths, self.python_path):
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\x00" in value
                or path.as_posix() != value
            ):
                raise ValueError("bundle paths must remain relative")
        if len(set(self.editable_paths)) != len(self.editable_paths):
            raise ValueError("editable paths must be unique")
        if not self.files or any(
            not name.startswith(("source/", "checks/")) for name in self.files
        ):
            raise ValueError("bundle files must belong to source or checks")
        if any(f"source/{name}" not in self.files for name in self.editable_paths):
            raise ValueError("editable files must exist in source")
        if not any(name.startswith("checks/") for name in self.files):
            raise ValueError("independent verifier files are required")
        if self.independent_suite.kind != "unittest":
            raise ValueError("independent controls use unittest discovery")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class GradeEvidence(_Contract):
    """Content-safe grade retained with the native attempt that supplied its repair."""

    config_digest: Digest
    candidate_digest: Digest
    response_digest: Digest
    attempt_id: Annotated[str, Field(pattern=r"^attempt-[0-9a-f]{32}$")]
    status: Literal["pass", "fail", "error"]
    reason_code: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")] = None
    elapsed_milliseconds: int = Field(ge=0)
    suites: tuple[SuiteEvidence, ...] = ()
    report_digest: Digest | None = None

    @field_validator("suites", mode="before")
    @classmethod
    def normalize_suites(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def error_reason(self) -> GradeEvidence:
        if (self.status == "error") != (self.reason_code is not None):
            raise ValueError("only grading errors require a reason code")
        if self.status == "pass" and (
            {row.role for row in self.suites} != {"source", "independent"}
            or len(self.suites) != 2
            or any(
                row.collected != row.expected
                or row.passed != row.expected
                or row.failed
                or row.errors
                for row in self.suites
            )
        ):
            raise ValueError("passing grades require both complete successful suites")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class SuiteEvidence(_Contract):
    """Counts from one completed verifier, without test names or captured source."""

    role: Literal["source", "independent"]
    expected: int = Field(ge=1)
    collected: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)


GradeEvidence.model_rebuild()
