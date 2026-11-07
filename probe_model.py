#!/usr/bin/env python3
"""Probe why the executor model returns no tool call.

Sends the agent's REAL system prompt + REAL tool schemas to Ollama in a few variants
(context size x thinking on/off x short/long task) and prints what comes back.

Run from the project root (Ollama running, no sandbox needed):
    python probe_model.py                       # uses the executor model from config.py
    python probe_model.py --model qwen3:8b      # try another model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_agent.config import AgentConfig  # noqa: E402
from local_agent.core.loop import EXECUTOR_SYSTEM_PROMPT  # noqa: E402
from local_agent.security.permissions import PermissionPolicy  # noqa: E402
from local_agent.tools.browser import (  # noqa: E402
    BrowserClickTool, BrowserGetContentTool, BrowserNavigateTool, BrowserScreenshotTool, BrowserTypeTool,
)
from local_agent.tools.file_ops import ListFilesTool, ReadFileTool, WriteFileTool  # noqa: E402
from local_agent.tools.python_exec import PythonExecTool  # noqa: E402
from local_agent.tools.rag import IngestDocumentTool, QueryDocumentsTool  # noqa: E402
from local_agent.tools.registry import ToolRegistry  # noqa: E402
from local_agent.tools.shell import ShellExecTool  # noqa: E402
from local_agent.tools.web_search import WebSearchTool  # noqa: E402

LONG_GOAL = (
    "Create a complete LAN Network Inventory Dashboard as ONE file /workspace/network_dashboard/index.html "
    "with dark UI, sidebar, three summary cards, a device table of six devices, search, All/Online/Offline "
    "filters, a clear-search button, responsive layout, embedded CSS and JavaScript. Use write_file."
)
SHORT_GOAL = "Create the file /workspace/hello.txt containing the text Hello World. Use write_file."


def build_registry(all_tools: bool) -> ToolRegistry:
    reg = ToolRegistry(PermissionPolicy())
    if all_tools:
        reg.register_many(
            ShellExecTool(), ReadFileTool(), WriteFileTool(), ListFilesTool(), PythonExecTool(),
            WebSearchTool(), BrowserNavigateTool(), BrowserGetContentTool(), BrowserClickTool(),
            BrowserTypeTool(), BrowserScreenshotTool(), IngestDocumentTool(), QueryDocumentsTool(),
        )
    else:
        reg.register(WriteFileTool())
    return reg


async def probe(client, model, goal, ctx, think, all_tools, num_predict):
    reg = build_registry(all_tools)
    system = EXECUTOR_SYSTEM_PROMPT.format(
        tool_names=", ".join(reg.names),
        state_summary=f"Goal: {goal}\nStatus: executing\nStep: 1/2\nCurrent task: {goal}",
    )
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"Execute this step: {goal}"}],
        "tools": reg.get_tool_definitions(),
        "options": {"num_ctx": ctx, "temperature": 0.3, "num_predict": num_predict},
    }
    if think is not None:
        payload["think"] = think
    t0 = time.time()
    r = await client.post("/api/chat", json=payload)
    dt = time.time() - t0
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}", "secs": dt}
    d = r.json()
    m = d.get("message", {})
    return {
        "prompt_tok": d.get("prompt_eval_count"),
        "gen_tok": d.get("eval_count"),
        "done_reason": d.get("done_reason"),
        "content_chars": len(m.get("content", "") or ""),
        "thinking_chars": len(m.get("thinking", "") or ""),
        "tool_calls": [tc.get("function", {}).get("name") for tc in m.get("tool_calls", []) or []],
        "content_head": (m.get("content", "") or "")[:120].replace("\n", " "),
        "secs": dt,
    }


async def main() -> None:
    cfg = AgentConfig.from_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=cfg.model_roles["executor"].model)
    ap.add_argument("--num-predict", type=int, default=3000)
    args = ap.parse_args()

    variants = [
        # label, goal, ctx, think, all_tools
        ("short task, 1 tool, ctx 8192, think off", SHORT_GOAL, 8192, False, False),
        ("short task, all tools, ctx 8192, think off", SHORT_GOAL, 8192, False, True),
        ("LONG task, all tools, ctx 4096, think default", LONG_GOAL, 4096, None, True),
        ("LONG task, all tools, ctx 4096, think off", LONG_GOAL, 4096, False, True),
        ("LONG task, all tools, ctx 8192, think default", LONG_GOAL, 8192, None, True),
        ("LONG task, all tools, ctx 8192, think off", LONG_GOAL, 8192, False, True),
    ]
    print(f"model = {args.model}   (num_predict cap {args.num_predict})\n")
    async with httpx.AsyncClient(base_url=cfg.ollama_base_url, timeout=600) as client:
        for label, goal, ctx, think, all_tools in variants:
            print(f"--- {label}")
            res = await probe(client, args.model, goal, ctx, think, all_tools, args.num_predict)
            print("   ", json.dumps(res))
    print(
        "\nHow to read this:\n"
        " * prompt_tok close to num_ctx  -> input is being truncated: raise context_length.\n"
        " * done_reason=length + no tool_calls -> ran out of tokens (thinking or a long file body).\n"
        " * thinking_chars large with think default -> thinking is eating the budget: send think=false.\n"
        " * short task fails too -> the model/template isn't producing tool_calls: try another model tag.\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
