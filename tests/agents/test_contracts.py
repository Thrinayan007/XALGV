"""Phase 0 invariants at the worker, coordinator, and verifier boundaries."""

import json

import pytest
from pydantic import ValidationError

from local_agent.agents.base import AgentResult, AgentTask, TaskState, VerificationReport
from sandbox.xbrowser.schemas import VerificationRequest

OUTPUT = "artifacts/run_1/task_2/report.txt"


def task_payload(**changes):
    return {
        "run_id": "run_1",
        "task_id": "task_2",
        "agent": "coding",
        "objective": "Write a report",
        "dependencies": ["task_1"],
        "inputs": [{
            "name": "evidence", "producer_task_id": "task_1",
            "path": "artifacts/run_1/task_1/evidence.json", "sha256": "a" * 64,
        }],
        "expected_outputs": [OUTPUT],
        "acceptance_checks": [{"kind": "artifact", "path": OUTPUT, "contains": "Findings"}],
        "max_steps": 12,
        "max_wall_time_seconds": 120,
        **changes,
    }


def parse_task(**changes):
    return AgentTask.model_validate_json(json.dumps(task_payload(**changes)))


def test_task_round_trips_and_checks_match_existing_verifier():
    task = parse_task()
    assert AgentTask.model_validate_json(task.model_dump_json()) == task
    packet = {"checks": [check.model_dump(mode="json") for check in task.acceptance_checks]}
    assert VerificationRequest.model_validate(packet).checks[0].path == OUTPUT
    with pytest.raises(ValidationError):
        task.max_steps = 500


@pytest.mark.parametrize("path", [
    "../report.txt", "/artifacts/run_1/task_2/report.txt",
    "C:/artifacts/run_1/task_2/report.txt", "artifacts/run_1/task_2/../report.txt",
    "artifacts/run_1/task_2/COM1.txt", "artifacts/run_1/task_2/report.txt:secret",
    "artifacts/run_1/task_2/report.txt.", "artifacts/run_9/task_2/report.txt",
    "artifacts/run_1/task_1/report.txt", "artifacts\\run_1\\task_2\\report.txt",
])
def test_rejects_invalid_or_unowned_output(path):
    with pytest.raises(ValidationError):
        parse_task(expected_outputs=[path], acceptance_checks=[{"kind": "artifact", "path": path}])


@pytest.mark.parametrize("changes", [
    {"max_steps": "12"}, {"max_attempts": 4}, {"schema_version": 2}, {"allowed_tools": ["shell_exec"]},
    {"dependencies": ["task_2"]}, {"dependencies": ["task_1", "task_1"]}, {"dependencies": []},
    {"expected_outputs": [OUTPUT, OUTPUT.upper()]}, {"expected_outputs": [OUTPUT, OUTPUT + "/child"]},
    {"expected_outputs": [OUTPUT, "artifacts/run_1/task_2/unchecked.txt"]},
    {"acceptance_checks": []}, {"expected_outputs": [], "acceptance_checks": [{"kind": "text", "expected": "Other tab"}]},
])
def test_rejects_authority_expansion_or_invalid_task_shape(changes):
    with pytest.raises(ValidationError):
        parse_task(**changes)


def test_worker_success_is_separate_from_verified_completion():
    task = parse_task()
    result = {
        "run_id": task.run_id, "task_id": task.task_id, "attempt": 1, "agent": "coding",
        "outcome": "reported_success", "summary": "Report generated",
        "artifacts": [{"path": OUTPUT, "size_bytes": 80, "sha256": "b" * 64}],
        "usage": {"tool_steps": 3, "model_calls": 2, "prompt_tokens": 900, "generated_tokens": 100, "wall_time_ms": 5000},
    }
    assert AgentResult.model_validate_json(json.dumps(result)).matches(task, attempt=1)
    result["outcome"] = "completed"
    with pytest.raises(ValidationError):
        AgentResult.model_validate_json(json.dumps(result))
    assert not TaskState.RUNNING.allows(TaskState.COMPLETED)
    assert not TaskState.OUTCOME_UNKNOWN.allows(TaskState.READY)
    assert TaskState.OUTCOME_UNKNOWN.allows(TaskState.VERIFYING)
    report = VerificationReport.model_validate_json(json.dumps({
        "run_id": task.run_id, "task_id": task.task_id, "attempt": 1,
        "criteria_sha256": task.criteria_sha256, "outcomes": [{"check_index": 0, "passed": True}],
    }))
    assert report.accepts(task, attempt=1)
    assert not report.accepts(task, attempt=2)
    assert not report.accepts(parse_task(acceptance_checks=[{"kind": "artifact", "path": OUTPUT}]), attempt=1)
