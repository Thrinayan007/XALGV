"""Admission for trusted workflow requirements and model-proposed ordering."""

from graphlib import CycleError, TopologicalSorter

from pydantic import Field, model_validator

from .base import (
    AgentRole,
    AgentTask,
    ArtifactInput,
    ArtifactManifest,
    ArtifactPath,
    Contract,
    Envelope,
    FileCheck,
    Identifier,
    MarkdownCheck,
    SourceManifestCheck,
    Summary,
    ToolName,
)


class InputBinding(Contract):
    name: Identifier
    producer_task_id: Identifier
    path: ArtifactPath


class TaskRequirement(Contract):
    task: AgentTask
    bindings: tuple[InputBinding, ...] = Field(default=(), max_length=32)
    required_tools: frozenset[ToolName] = Field(default_factory=frozenset, max_length=64)
    tool_allowlist: frozenset[ToolName] | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def unresolved_inputs(self):
        if self.task.inputs:
            raise ValueError("Workflow input digests are bound after verification; use bindings")
        if len({binding.name for binding in self.bindings}) != len(self.bindings):
            raise ValueError("Input binding names must be unique")
        return self


class WorkflowRequest(Envelope):
    run_id: Identifier
    tasks: tuple[TaskRequirement, ...] = Field(min_length=1, max_length=32)
    max_steps: int = Field(ge=1, le=1000)
    max_wall_time_seconds: int = Field(ge=1, le=3600)
    max_model_tokens: int = Field(default=1_000_000, ge=1, le=20_000_000)
    final_checks: tuple[FileCheck, ...] = Field(
        default=(), max_length=50,
    )


class PlannedTask(Contract):
    task_id: Identifier
    approach: Summary
    dependencies: tuple[Identifier, ...] = Field(default=(), max_length=31)


class Plan(Envelope):
    tasks: tuple[PlannedTask, ...] = Field(min_length=1, max_length=32)


class TaskGraph:
    def __init__(self, request, registry, effective_tools, plan=None, *, checkpoint=None):
        self.request = request
        requirements = {item.task.task_id: item for item in request.tasks}
        if len(requirements) != len(request.tasks):
            raise ValueError("Duplicate task ID")
        if checkpoint is not None and checkpoint.keys() != requirements.keys():
            raise ValueError("Checkpoint must contain exactly the trusted tasks")
        outputs = {path for item in request.tasks for path in item.task.expected_outputs}
        source_outputs = {check.path for item in request.tasks for check in item.task.acceptance_checks
                          if isinstance(check, SourceManifestCheck)}
        if any(check.path not in outputs or (
            isinstance(check, MarkdownCheck) and check.sources_path and check.sources_path not in source_outputs
        ) for check in request.final_checks):
            raise ValueError("Final checks must refer to declared workflow outputs")
        proposals = {item.task_id: item for item in plan.tasks} if plan else {}
        if plan and (len(proposals) != len(plan.tasks) or proposals.keys() != requirements.keys()):
            raise ValueError("The plan must contain each trusted task exactly once")
        self._tasks = {}
        self._bindings = {}
        self._tool_grants = {}
        reserved_steps = reserved_seconds = 0
        for task_id, requirement in requirements.items():
            task = requirement.task
            if task.run_id != request.run_id:
                raise ValueError(f"Cross-run task: {task_id}")
            definition = registry.require(task.agent)
            available = effective_tools(task.agent)
            grant = available if requirement.tool_allowlist is None else requirement.tool_allowlist
            if not grant or not requirement.required_tools <= grant <= available:
                raise ValueError(f"Unavailable capabilities for task {task_id}")
            if any(isinstance(check, SourceManifestCheck) for check in task.acceptance_checks) and (
                task.agent != AgentRole.RESEARCH or "research_export" not in grant
            ):
                raise ValueError("Source manifests require the Research specialist and its export tool")
            self._tool_grants[task_id] = grant
            if task.max_steps > definition.max_steps:
                raise ValueError(f"Specialist step ceiling exceeded: {task_id}")
            reserved_steps += task.max_steps * task.max_attempts
            reserved_seconds += task.max_wall_time_seconds * task.max_attempts
            dependencies = list(task.dependencies)
            input_paths = {binding.path for binding in requirement.bindings}
            if any(isinstance(check, MarkdownCheck) and check.sources_path
                   and (check.sources_path not in input_paths or check.sources_path not in source_outputs)
                   for check in task.acceptance_checks):
                raise ValueError("Markdown citations must bind a verified source manifest")
            for binding in requirement.bindings:
                producer = requirements.get(binding.producer_task_id)
                if producer is None or binding.path not in producer.task.expected_outputs:
                    raise ValueError(f"Input must reference a declared dependency output: {binding.path}")
                dependencies.append(binding.producer_task_id)
            if task_id in proposals:
                proposal = proposals[task_id]
                if len(set(proposal.dependencies)) != len(proposal.dependencies):
                    raise ValueError(f"Duplicate proposed dependency: {task_id}")
                dependencies.extend(proposal.dependencies)
            if any(dependency not in requirements for dependency in dependencies):
                raise ValueError(f"Unknown dependency for task {task_id}")
            payload = task.model_dump()
            payload["dependencies"] = tuple(dict.fromkeys(dependencies))
            if task_id in proposals:
                payload["objective"] = (task.objective + "\nSuggested approach: " + proposals[task_id].approach)[:2000]
            if checkpoint is not None:
                saved = checkpoint[task_id].model_dump()
                if any(saved[key] != value for key, value in payload.items() if key not in {"objective", "dependencies"}):
                    raise ValueError("Checkpoint does not match trusted task requirements")
                if not set(payload["dependencies"]) <= set(saved["dependencies"]) <= requirements.keys():
                    raise ValueError("Checkpoint dependency is invalid")
                if not saved["objective"].startswith(task.objective):
                    raise ValueError("Checkpoint must preserve the trusted objective")
                payload = saved
            self._tasks[task_id] = AgentTask.model_validate(payload)
            self._bindings[task_id] = requirement.bindings
        if reserved_steps > request.max_steps or reserved_seconds > request.max_wall_time_seconds:
            raise ValueError("Reserved task attempts exceed the workflow budget")
        try:
            self.order = tuple(TopologicalSorter({key: task.dependencies for key, task in self._tasks.items()}).static_order())
        except CycleError as exc:
            raise ValueError("Workflow dependencies contain a cycle") from exc

    def task(self, task_id: str) -> AgentTask:
        return self._tasks[task_id]

    def tool_grant(self, task_id: str) -> frozenset[str]:
        return self._tool_grants[task_id]

    def bind(self, task_id: str, manifests: dict[str, dict[str, ArtifactManifest]]) -> AgentTask:
        task = self.task(task_id)
        inputs = []
        for binding in self._bindings[task_id]:
            manifest = manifests.get(binding.producer_task_id, {}).get(binding.path)
            if manifest is None:
                raise ValueError(f"Verified artifact is unavailable: {binding.path}")
            inputs.append(ArtifactInput(**binding.model_dump(), sha256=manifest.sha256))
        return AgentTask.model_validate({**task.model_dump(), "inputs": tuple(inputs)})
