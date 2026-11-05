"""Evaluation harness — runs task suites and produces reports.

Each task has:
* A ``goal`` string (what the agent is asked to do).
* A list of ``checkers`` — programmatic assertions that verify the outcome.
* Optional ``setup`` and ``teardown`` shell commands.

The harness runs each task, collects results, and produces a JSON report.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskDefinition:
    """A single evaluation task."""

    task_id: str
    goal: str
    category: str = "general"
    checkers: list[dict[str, Any]] = field(default_factory=list)
    setup_commands: list[str] = field(default_factory=list)
    teardown_commands: list[str] = field(default_factory=list)
    max_steps: int = 20
    timeout_seconds: int = 300
    tags: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    """Result of running a single evaluation task."""

    task_id: str
    goal: str
    category: str
    passed: bool
    checker_results: list[dict[str, Any]] = field(default_factory=list)
    steps_taken: int = 0
    wall_time_seconds: float = 0.0
    error: str | None = None
    agent_summary: str = ""


@dataclass
class EvalReport:
    """Summary report for an evaluation run."""

    total_tasks: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    results: list[TaskResult] = field(default_factory=list)
    wall_time_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.passed / self.total_tasks if self.total_tasks > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "success_rate": round(self.success_rate, 3),
            "wall_time_seconds": round(self.wall_time_seconds, 1),
            "results": [asdict(r) for r in self.results],
        }


class EvalHarness:
    """Runs evaluation tasks against the agent and collects results."""

    def __init__(self, tasks_dir: Path | str | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else Path(__file__).parent / "tasks"

    def load_tasks(self, category: str | None = None) -> list[TaskDefinition]:
        """Load task definitions from JSON files in the tasks directory."""
        tasks: list[TaskDefinition] = []
        if not self._tasks_dir.exists():
            logger.warning("Tasks directory not found: %s", self._tasks_dir)
            return tasks

        for path in sorted(self._tasks_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                file_tasks = data if isinstance(data, list) else data.get("tasks", [data])
                for item in file_tasks:
                    task = TaskDefinition(**{k: v for k, v in item.items() if k in TaskDefinition.__dataclass_fields__})
                    if category is None or task.category == category:
                        tasks.append(task)
            except Exception as exc:
                logger.warning("Failed to load %s: %s", path, exc)

        return tasks

    async def run_task(self, task: TaskDefinition, agent_loop: Any) -> TaskResult:
        """Run a single task and return the result."""
        start = time.monotonic()
        try:
            run_record = await agent_loop.run(task.goal)
            elapsed = time.monotonic() - start

            # Run checkers.
            checker_results = []
            all_passed = True
            for checker_def in task.checkers:
                checker_result = await self._run_checker(checker_def, agent_loop)
                checker_results.append(checker_result)
                if not checker_result.get("passed", False):
                    all_passed = False

            # If no checkers, consider it passed if the agent completed.
            if not task.checkers:
                all_passed = run_record.status.value == "completed"

            return TaskResult(
                task_id=task.task_id,
                goal=task.goal,
                category=task.category,
                passed=all_passed,
                checker_results=checker_results,
                steps_taken=len(run_record.steps),
                wall_time_seconds=elapsed,
                agent_summary=run_record.summary,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return TaskResult(
                task_id=task.task_id,
                goal=task.goal,
                category=task.category,
                passed=False,
                wall_time_seconds=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def run_suite(self, agent_loop: Any, category: str | None = None) -> EvalReport:
        """Run all tasks and produce a report."""
        tasks = self.load_tasks(category)
        report = EvalReport(total_tasks=len(tasks))
        suite_start = time.monotonic()

        for task in tasks:
            logger.info("Running task: %s — %s", task.task_id, task.goal)
            result = await self.run_task(task, agent_loop)
            report.results.append(result)
            if result.error:
                report.errors += 1
            elif result.passed:
                report.passed += 1
            else:
                report.failed += 1

        report.wall_time_seconds = time.monotonic() - suite_start
        return report

    async def _run_checker(self, checker_def: dict[str, Any], agent_loop: Any) -> dict[str, Any]:
        """Run a single checker.  Checkers are shell commands that exit 0 on pass."""
        checker_type = checker_def.get("type", "shell")
        if checker_type == "shell":
            command = checker_def.get("command", "")
            description = checker_def.get("description", command[:80])
            # Execute the check command via the sandbox.
            try:
                from local_agent.tools.base import ToolResult
                result = await agent_loop._tools.execute("shell_exec", {"command": command})
                passed = result.success and (result.exit_code == 0)
                return {"type": "shell", "description": description, "passed": passed, "output": result.output[:500]}
            except Exception as exc:
                return {"type": "shell", "description": description, "passed": False, "error": str(exc)}
        elif checker_type == "file_exists":
            path = checker_def.get("path", "")
            try:
                result = await agent_loop._tools.execute("shell_exec", {"command": f"test -f {path} && echo EXISTS"})
                return {"type": "file_exists", "path": path, "passed": result.success}
            except Exception as exc:
                return {"type": "file_exists", "path": path, "passed": False, "error": str(exc)}

        return {"type": checker_type, "passed": False, "error": "Unknown checker type."}

    def save_report(self, report: EvalReport, output_path: Path | str) -> Path:
        """Save the report as JSON."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        logger.info("Report saved to %s", path)
        return path
