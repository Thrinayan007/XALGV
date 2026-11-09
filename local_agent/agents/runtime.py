"""Isolated specialist attempts using the existing single-agent executor."""

import re
import time
from dataclasses import replace
from pathlib import Path

from local_agent.core.loop import AgentLoop
from local_agent.memory.store import MemoryStore, RunStatus
from local_agent.models import ModelManager
from local_agent.security.permissions import PermissionPolicy, PermissionTier
from local_agent.tools.base import VerificationResult, VerificationStatus
from local_agent.tools.browser import browser_tools
from local_agent.tools.file_ops import ReadFileTool, WriteFileTool
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.web_search import WebSearchTool

from .artifacts import Artifacts, TaskFiles
from .base import (
    AgentResult,
    AgentRole,
    AgentTask,
    AttemptOutcome,
    CheckOutcome,
    MarkdownCheck,
    PageCheck,
    Usage,
    VerificationReport,
)
from .context import specialist_context
from .inference_scheduler import InferenceScheduler
from .resources import TaskResources
from .tools import BrowserExportTool, ResearchExportTool, ScopedBrowserTool, ValidatePythonSchemaTool


class TaskVerifier:
    """Independent checks, shared by the attempt completion gate and the coordinator."""

    def __init__(self, artifacts: Artifacts, sandbox):
        self.artifacts = artifacts
        self.sandbox = sandbox

    async def check(self, task, attempt: int) -> VerificationReport:
        pages = [check.model_dump(mode="json") for check in task.acceptance_checks if isinstance(check, PageCheck)]
        page_outcomes = []
        if pages:
            result = await self.sandbox.browser_verify(pages)
            page_outcomes = result.metadata.get("data", {}).get("checks", [])
            if (not isinstance(page_outcomes, list) or len(page_outcomes) != len(pages)
                    or any(not isinstance(item, dict) or item.get("check") != check for item, check in zip(page_outcomes, pages, strict=False))):
                page_outcomes = [{"passed": False}] * len(pages)
        outcomes = []
        for index, check in enumerate(task.acceptance_checks):
            if isinstance(check, PageCheck):
                passed = page_outcomes.pop(0).get("passed") is True
            else:
                passed = self.artifact_passes(check)
                if passed and isinstance(check, MarkdownCheck) and check.sources_path:
                    try:
                        manifest = self.artifacts.manifest(check.sources_path)
                        passed = any(item.path == manifest.path and item.sha256 == manifest.sha256 for item in task.inputs)
                    except (ValueError, OSError):
                        passed = False
            outcomes.append(CheckOutcome(check_index=index, passed=passed))
        return VerificationReport(run_id=task.run_id, task_id=task.task_id, attempt=attempt,
                                  criteria_sha256=task.criteria_sha256, outcomes=tuple(outcomes))

    def artifact_passes(self, check) -> bool:
        try:
            return self.artifacts.verify(check)
        except (OSError, ValueError, SyntaxError, TypeError, RecursionError, ImportError):
            return False


class _MilestoneVerifier:
    def __init__(self, verifier, task, attempt):
        self.verifier = verifier
        self.task = task
        self.attempt = attempt

    async def verify(self, checks):
        report = await self.verifier.check(self.task, self.attempt)
        return VerificationResult(
            status=VerificationStatus.PASSED if report.passed else VerificationStatus.FAILED,
            message="Specialist checks passed." if report.passed else "Specialist checks failed.",
            evidence={"checks": [
                {**criterion, "passed": outcome.passed}
                for criterion, outcome in zip(checks, report.outcomes, strict=True)
            ]},
        )


class _Meter:
    def __init__(self, model):
        self.model = model
        self.calls = self.prompt = self.generated = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        response = await self.model.chat(*args, **kwargs)
        self.prompt += response.prompt_eval_count
        self.generated += response.eval_count
        return response


class SpecialistRuntime:
    def __init__(self, config, model, sandbox, registry, *, allowed_tools=None, policy=None, search_factory=WebSearchTool):
        self.config = config
        self.model = model
        self.sandbox = sandbox
        self.registry = registry
        self._search_factory = search_factory
        self.artifacts = Artifacts(config.workspace_dir)
        self.verifier = TaskVerifier(self.artifacts, sandbox)
        self.resources = TaskResources(config, sandbox)
        self._inference = None if isinstance(model, ModelManager) else InferenceScheduler(
            config.data_dir / "resource-locks", config.ollama_base_url,
        )
        self._allowed = None if allowed_tools is None else frozenset(allowed_tools)
        source = policy or PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK)
        self._policy = PermissionPolicy(source.auto_approve_up_to, frozenset(source.require_approval))

    async def chat(self, role, messages, **kwargs):
        if self._inference is None:
            return await self.model.chat(role, messages, **kwargs)
        context = self.config.model_roles[role].context_length
        limit = min(kwargs.get("max_tokens") or 2048, context)
        return await self._inference.submit(lambda: self.model.chat(role, messages, **kwargs),
                                            context_tokens=context, output_tokens=limit, label=role)

    def effective_tools(self, role: AgentRole) -> frozenset[str]:
        definition = self.registry.require(role)
        if role == AgentRole.BROWSER:
            candidates = [*browser_tools(self.sandbox), BrowserExportTool(None, None), ReadFileTool()] if self.sandbox else []
        elif role == AgentRole.CODING:
            candidates = [ReadFileTool(), WriteFileTool(), ValidatePythonSchemaTool(None)]
        elif role == AgentRole.RESEARCH:
            candidates = [ResearchExportTool(None, None), ReadFileTool()]
        else:
            candidates = [ReadFileTool(), WriteFileTool()]
        return frozenset(tool.name for tool in candidates if (
            tool.name in definition.allowed_tools
            and (self._allowed is None or tool.name in self._allowed)
            and self._policy.is_allowed(tool.permission_tier)
        ))

    def tool_registry(self, task, meter, *, tool_grant=None, trace_dir=None) -> ToolRegistry:
        allowed = self.effective_tools(task.agent)
        if tool_grant is not None:
            allowed = allowed.intersection(tool_grant)
        files = TaskFiles(self.artifacts, task)
        tools = ToolRegistry(self._policy)
        if task.agent == AgentRole.BROWSER:
            candidates = [
                *(ScopedBrowserTool(tool, task, self.artifacts, allowed) for tool in browser_tools(self.sandbox, vision_model=meter)),
                BrowserExportTool(self.sandbox, files),
                ReadFileTool(files),
            ]
        elif task.agent == AgentRole.CODING:
            candidates = [ReadFileTool(files), WriteFileTool(files), ValidatePythonSchemaTool(files)]
        elif task.agent == AgentRole.RESEARCH:
            candidates = [ReadFileTool(files)]
            if "research_export" in allowed:
                search = self._search_factory(base_url=self.config.searxng_base_url,
                                              trace_dir=str(trace_dir or self.config.trace_dir), task_id=task.task_id)
                candidates.append(ResearchExportTool(search, files))
        else:
            candidates = [ReadFileTool(files), WriteFileTool(files)]
        tools.register_many(*(tool for tool in candidates if tool.name in allowed))
        return tools

    def validate_task(self, task) -> None:
        definition = self.registry.require(task.agent)
        if task.max_steps > definition.max_steps or not self.effective_tools(task.agent):
            raise ValueError("Specialist task exceeds its grant")
        roles = ("planner", "executor", "vision") if task.agent == AgentRole.BROWSER else ("planner", "executor")
        if any(self.config.model_roles[role].context_length > definition.max_context_tokens
               for role in roles if role in self.config.model_roles):
            raise ValueError(f"Configured model context exceeds {task.agent}'s specialist ceiling")

    async def execute(
        self, task: AgentTask, attempt: int, *, seconds: int | None = None, tool_grant: frozenset[str] | None = None,
    ) -> AgentResult:
        task = AgentTask.model_validate(task)
        self.validate_task(task)
        if type(attempt) is not int or not 1 <= attempt <= task.max_attempts:
            raise ValueError("Attempt is outside the task grant")
        if tool_grant is not None and (not tool_grant or not tool_grant <= self.effective_tools(task.agent)):
            raise ValueError("Task tool grant exceeds the effective specialist permissions")
        if self.resources.holds(task):
            return await self._execute(task, attempt, seconds, tool_grant)
        async with self.resources.acquire(task, seconds=seconds or task.max_wall_time_seconds):
            return await self._execute(task, attempt, seconds, tool_grant)

    async def _execute(self, task, attempt, seconds, tool_grant):
        context = specialist_context(task, self.config.model_roles["executor"].context_length)
        started = time.monotonic()
        root = Path(self.config.data_dir) / "agent-runs" / task.run_id / task.task_id / str(attempt)
        root.mkdir(parents=True, exist_ok=False)
        config = replace(self.config, db_path=root / "memory.db", trace_dir=root / "traces",
                         max_steps=task.max_steps, max_wall_time_seconds=min(
                             task.max_wall_time_seconds, seconds if seconds is not None else task.max_wall_time_seconds,
                         ))
        meter = _Meter(self)
        definition = self.registry.require(task.agent)
        loop = AgentLoop(
            config, meter, self.tool_registry(task, meter, tool_grant=tool_grant, trace_dir=config.trace_dir), MemoryStore(config.db_path),
            task_verifier=_MilestoneVerifier(self.verifier, task, attempt),
            specialist_prompt=definition.system_prompt + "\nTrusted artifact scope: " + context,
        )
        record = await loop.run(task.objective, milestones=[{
            "objective": task.objective,
            "checks": [check.model_dump(mode="json") for check in task.acceptance_checks],
        }])
        tool_steps = 0
        uncertain = False
        for step in record.steps:
            if step.action_type != "tool_call":
                continue
            data = (step.result_metadata or {}).get("data", {})
            uncertain |= (step.result_metadata or {}).get("browser_mutation_uncertain") is True
            if step.tool_name == "browser_run":
                planned = (step.tool_params or {}).get("steps", [])
                attempted = data.get("attempted_actions")
                tool_steps += attempted if type(attempted) is int else max(1, len(planned)) if isinstance(planned, list) else 1
            else:
                tool_steps += 1
        outcome = (
            AttemptOutcome.OUTCOME_UNKNOWN if uncertain else {
                RunStatus.COMPLETED: AttemptOutcome.REPORTED_SUCCESS,
                RunStatus.AWAITING_HUMAN: AttemptOutcome.BLOCKED,
                RunStatus.KILLED: AttemptOutcome.CANCELLED,
            }.get(record.status, AttemptOutcome.FAILED)
        )
        manifests = []
        for path in task.expected_outputs:
            try:
                manifests.append(self.artifacts.manifest(path))
            except (OSError, ValueError):
                if outcome == AttemptOutcome.REPORTED_SUCCESS:
                    outcome = AttemptOutcome.FAILED
        error_code = {
            AttemptOutcome.OUTCOME_UNKNOWN: "OUTCOME_UNKNOWN",
            AttemptOutcome.BLOCKED: "REVIEW_REQUIRED",
            AttemptOutcome.CANCELLED: "CANCELLED",
        }.get(outcome)
        if outcome == AttemptOutcome.FAILED:
            error_code = "TASK_FAILED"
            for step in reversed(record.steps):
                candidate = (step.result_metadata or {}).get("error_code")
                if step.result_success is False and isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate):
                    error_code = candidate
                    break
        return AgentResult(
            run_id=task.run_id, task_id=task.task_id, agent=task.agent, attempt=attempt,
            outcome=outcome, summary=(record.summary or outcome.value)[:2000],
            artifacts=tuple(manifests), error_code=error_code, usage=Usage(tool_steps=tool_steps, model_calls=meter.calls,
                                                  prompt_tokens=meter.prompt, generated_tokens=meter.generated,
                                                  wall_time_ms=int((time.monotonic() - started) * 1000)),
        )
