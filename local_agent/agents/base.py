"""Versioned, immutable data contracts. Construction does not grant authority."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Summary = Annotated[str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)]
_DEVICES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(10)
}


def _validate_artifact_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 4 or parts[0] != "artifacts":
        raise ValueError("Expected artifacts/<run_id>/<task_id>/<file>")
    if any(
        part in {"", ".", ".."} or len(part) > 255 or part.endswith(".") or part.split(".")[0].lower() in _DEVICES
        for part in parts
    ):
        raise ValueError("Artifact path contains an invalid or reserved component")
    return path


ArtifactPath = Annotated[
    str,
    StringConstraints(max_length=512, pattern=r"^[a-zA-Z0-9_.\-/]+$"),
    AfterValidator(_validate_artifact_path),
]


def _owned(path: str, run_id: str, task_id: str) -> bool:
    return path.split("/")[1:3] == [run_id, task_id]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, revalidate_instances="always")


class Envelope(Contract):
    schema_version: Literal[1] = 1


class AgentRole(StrEnum):
    BROWSER = "browser"
    CODING = "coding"
    RESEARCH = "research"
    DOCUMENT = "document"


class AgentDefinition(Envelope):
    """Trusted configuration, never accepted from a worker or model-generated plan."""

    name: AgentRole
    system_prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
    allowed_tools: frozenset[ToolName] = Field(max_length=64)
    max_steps: int = Field(ge=1, le=1000)
    max_context_tokens: int = Field(ge=1024, le=262144)


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"

    def allows(self, destination: TaskState) -> bool:
        return destination in _TRANSITIONS.get(self, ())


_TRANSITIONS = {
    TaskState.PENDING: (TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED),
    TaskState.READY: (TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED),
    TaskState.RUNNING: (TaskState.VERIFYING, TaskState.READY, TaskState.BLOCKED,
                        TaskState.FAILED, TaskState.CANCELLED, TaskState.OUTCOME_UNKNOWN),
    TaskState.VERIFYING: (TaskState.COMPLETED, TaskState.READY, TaskState.BLOCKED,
                          TaskState.FAILED, TaskState.CANCELLED, TaskState.OUTCOME_UNKNOWN),
    TaskState.BLOCKED: (TaskState.READY, TaskState.FAILED, TaskState.CANCELLED),
    TaskState.OUTCOME_UNKNOWN: (TaskState.VERIFYING, TaskState.FAILED),
}


class ArtifactCheck(Contract):
    kind: Literal["artifact"]
    path: ArtifactPath
    min_bytes: int = Field(default=1, ge=1, le=50 * 1024 * 1024)
    contains: str | None = Field(default=None, min_length=1, max_length=10000)
    sha256: Digest | None = None
    png: bool = False
    width: int | None = Field(default=None, ge=1, le=16384)
    height: int | None = Field(default=None, ge=1, le=16384)


class PageCheck(Contract):
    kind: Literal["url", "url_host", "url_contains", "text", "tab_count"]
    expected: str = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_tab_count(self):
        if self.kind == "tab_count" and self.expected not in {str(number) for number in range(1, 13)}:
            raise ValueError("Expected a tab count between 1 and 12")
        return self


class SchemaField(Contract):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    type: Literal["str", "bool", "float"]


class PythonSchemaCheck(Contract):
    kind: Literal["python_schema"]
    path: ArtifactPath
    fields: tuple[SchemaField, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("Python schema field names must be unique")
        return self


class SourceManifestCheck(Contract):
    kind: Literal["source_manifest"]
    path: ArtifactPath
    min_sources: int = Field(default=1, ge=1, le=10)
    required_hosts: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]{1,253}$")], ...] = Field(
        default=(), max_length=10,
    )


class MarkdownCheck(Contract):
    kind: Literal["markdown"]
    path: ArtifactPath
    headings: tuple[Summary, ...] = Field(min_length=1, max_length=12)
    sources_path: ArtifactPath | None = None
    min_citations: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def citation_scope(self):
        if self.min_citations and self.sources_path is None:
            raise ValueError("Citation checks require a source manifest")
        return self


FileCheck = Annotated[ArtifactCheck | PythonSchemaCheck | SourceManifestCheck | MarkdownCheck, Field(discriminator="kind")]
AcceptanceCheck = Annotated[
    ArtifactCheck | PageCheck | PythonSchemaCheck | SourceManifestCheck | MarkdownCheck, Field(discriminator="kind"),
]


class ArtifactInput(Contract):
    name: Identifier
    producer_task_id: Identifier
    path: ArtifactPath
    sha256: Digest


class AgentTask(Envelope):
    run_id: Identifier
    task_id: Identifier
    agent: AgentRole
    objective: Summary
    dependencies: tuple[Identifier, ...] = Field(default=(), max_length=31)
    inputs: tuple[ArtifactInput, ...] = Field(default=(), max_length=32)
    expected_outputs: tuple[ArtifactPath, ...] = Field(default=(), max_length=32)
    max_steps: int = Field(ge=1, le=1000)
    max_wall_time_seconds: int = Field(ge=1, le=3600)
    max_attempts: int = Field(default=1, ge=1, le=3)
    max_model_tokens: int = Field(default=131072, ge=1, le=5_000_000)
    acceptance_checks: tuple[AcceptanceCheck, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_boundaries(self):
        if self.task_id in self.dependencies or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Dependencies must be distinct and cannot include this task")
        if len({item.name for item in self.inputs}) != len(self.inputs):
            raise ValueError("Input names must be unique")
        for item in self.inputs:
            if item.producer_task_id not in self.dependencies or not _owned(item.path, self.run_id, item.producer_task_id):
                raise ValueError("Inputs must belong to a declared dependency in the same run")
        outputs = {path.casefold() for path in self.expected_outputs}
        if len(outputs) != len(self.expected_outputs):
            raise ValueError("Output paths must be unique even on case-insensitive filesystems")
        if any(other.startswith(path + "/") for path in outputs for other in outputs):
            raise ValueError("An output file cannot also be an output directory")
        if any(not _owned(path, self.run_id, self.task_id) for path in self.expected_outputs):
            raise ValueError("Outputs must belong to this run and task")
        checked_outputs = {check.path for check in self.acceptance_checks if not isinstance(check, PageCheck)}
        if checked_outputs != set(self.expected_outputs):
            raise ValueError("Artifact checks must cover exactly the declared outputs")
        for check in self.acceptance_checks:
            if isinstance(check, PageCheck) and self.agent != AgentRole.BROWSER:
                raise ValueError("Live page checks require the browser task's session lease")
        return self

    @property
    def criteria_sha256(self) -> str:
        checks = [check.model_dump(mode="json") for check in self.acceptance_checks]
        canonical = json.dumps(checks, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class AttemptRef(Envelope):
    run_id: Identifier
    task_id: Identifier
    attempt: int = Field(ge=1, le=3)


class ArtifactManifest(Contract):
    path: ArtifactPath
    size_bytes: int = Field(ge=0, le=50 * 1024 * 1024)
    sha256: Digest


class Usage(Contract):
    tool_steps: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    generated_tokens: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)


class AttemptOutcome(StrEnum):
    REPORTED_SUCCESS = "reported_success"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AgentResult(AttemptRef):
    agent: AgentRole
    outcome: AttemptOutcome
    summary: Summary
    artifacts: tuple[ArtifactManifest, ...] = Field(default=(), max_length=32)
    usage: Usage
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_artifacts(self):
        if any(not _owned(item.path, self.run_id, self.task_id) for item in self.artifacts):
            raise ValueError("Result artifacts must belong to the producing task")
        if len({item.path.casefold() for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Result artifact paths must be unique")
        if self.outcome == AttemptOutcome.REPORTED_SUCCESS and self.error_code is not None:
            raise ValueError("Reported success cannot carry an error code")
        return self

    def matches(self, task: AgentTask, *, attempt: int) -> bool:
        paths = {artifact.path for artifact in self.artifacts}
        expected = set(task.expected_outputs)
        return (
            (self.run_id, self.task_id, self.agent, self.attempt) == (task.run_id, task.task_id, task.agent, attempt)
            and 1 <= attempt <= task.max_attempts
            and paths <= expected
            and (self.outcome != AttemptOutcome.REPORTED_SUCCESS or paths == expected)
        )


class CheckOutcome(Contract):
    check_index: int = Field(ge=0, lt=50)
    passed: bool
    detail: Summary | None = None


class VerificationReport(AttemptRef):
    """Verifier output. The coordinator binds it to the exact trusted criteria."""

    criteria_sha256: Digest
    outcomes: tuple[CheckOutcome, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_indices(self):
        if tuple(item.check_index for item in self.outcomes) != tuple(range(len(self.outcomes))):
            raise ValueError("Verification outcomes must be ordered and contiguous")
        return self

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.outcomes)

    def accepts(self, task: AgentTask, *, attempt: int) -> bool:
        """Bind an authenticated verifier result to one attempt and its full criteria."""
        return (
            (self.run_id, self.task_id, self.attempt) == (task.run_id, task.task_id, attempt)
            and 1 <= attempt <= task.max_attempts
            and self.criteria_sha256 == task.criteria_sha256
            and len(self.outcomes) == len(task.acceptance_checks)
            and self.passed
        )
