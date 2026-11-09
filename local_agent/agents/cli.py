"""Opt-in command for trusted workflow contracts."""

import asyncio
import logging
from pathlib import Path

import httpx

from local_agent.config import AgentConfig
from local_agent.models import ModelManager
from local_agent.sandbox.client import SandboxClient

from .definitions import default_registry
from .inference_scheduler import InferenceLimitError
from .orchestrator import Orchestrator
from .resources import ResourceUnavailable
from .runtime import SpecialistRuntime
from .task_graph import WorkflowRequest
from .workflow_store import WorkflowStore

logger = logging.getLogger(__name__)


def _read_contract(path: Path):
    with path.open("rb") as stream:
        payload = stream.read(256 * 1024 + 1)
    if len(payload) > 256 * 1024:
        raise ValueError("Workflow contract exceeds 256 KiB")
    return WorkflowRequest.model_validate_json(payload)


def _write_result(path: Path, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(output + "\n")


async def run_workflow(args) -> int:
    config = AgentConfig.from_env()
    model = ModelManager(config)
    sandbox = SandboxClient(config.sandbox_url, admin_token="")
    try:
        runtime = SpecialistRuntime(config, model, sandbox, default_registry())
        orchestrator = Orchestrator(model, runtime)
        if args.command == "multi-resume":
            result = await orchestrator.resume(args.run_id, confirm=tuple(args.confirm_outcome))
        else:
            request = await asyncio.to_thread(_read_contract, Path(args.contract))
            result = await orchestrator.run(args.goal, request)
            path = config.data_dir / "agent-runs" / request.run_id / "workflow.json"
            await asyncio.to_thread(_write_result, path, result.model_dump_json(indent=2))
        output = result.model_dump_json(indent=2)
        print(output)
        return 0 if result.completed else 1
    except (OSError, ValueError, httpx.HTTPError, TimeoutError, InferenceLimitError, ResourceUnavailable) as exc:
        logger.error("Workflow could not complete: %s", exc)
        return 1
    finally:
        await sandbox.close()
        await model.close()


def inspect_workflow(args) -> int:
    import json

    path = AgentConfig.from_env().data_dir / "agent-runs" / "workflows.sqlite3"
    if not path.is_file():
        logger.error("No workflow journal exists at %s", path)
        return 1
    try:
        print(json.dumps(WorkflowStore(path, read_only=True).inspect(args.run_id), indent=2))
        return 0
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
