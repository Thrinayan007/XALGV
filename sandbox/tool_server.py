
"""FastAPI tool server — runs inside the Docker sandbox.

This server exposes shell, file, and screenshot tools over HTTP.
The agent core (on the host) calls these endpoints to execute actions
inside the isolated environment.
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="XALGV Sandbox Tool Server", version="0.1.0")

# ── Register browser automation routes ───────────────────────────────────
try:
    from browser_server import router as browser_router
    app.include_router(browser_router)
except ImportError:
    pass

WORKSPACE = Path("/workspace")


# ── Request / Response models ────────────────────────────────────────────

class ToolResponse(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    artifacts: list[str] = []


class ShellRequest(BaseModel):
    command: str
    working_dir: str = "/workspace"
    timeout: int = 30


class ReadFileRequest(BaseModel):
    path: str
    max_lines: int = 200


class WriteFileRequest(BaseModel):
    path: str
    content: str
    append: bool = False


class ListFilesRequest(BaseModel):
    path: str = "/workspace"


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Shell execution ─────────────────────────────────────────────────────

@app.post("/tools/shell", response_model=ToolResponse)
async def shell_exec(req: ShellRequest) -> ToolResponse:
    """Execute a shell command and return stdout/stderr."""
    try:
        result = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                req.command,
                cwd=req.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "TERM": "dumb"},
            ),
            timeout=req.timeout + 5,
        )
        stdout, stderr = await asyncio.wait_for(
            result.communicate(),
            timeout=req.timeout,
        )
        output = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        combined = output
        if err_text:
            combined += f"\n--- stderr ---\n{err_text}"
        return ToolResponse(
            success=result.returncode == 0,
            output=combined[:50_000],  # Cap output size.
            error=err_text[:5000] if result.returncode != 0 and err_text else None,
            exit_code=result.returncode,
        )
    except asyncio.TimeoutError:
        return ToolResponse(success=False, output="", error=f"Command timed out after {req.timeout}s.")
    except Exception as exc:
        return ToolResponse(success=False, output="", error=f"Execution error: {exc}")


# ── File operations ─────────────────────────────────────────────────────

@app.post("/tools/read_file", response_model=ToolResponse)
async def read_file(req: ReadFileRequest) -> ToolResponse:
    """Read a file and return its contents."""
    path = Path(req.path)
    if not path.is_absolute():
        path = WORKSPACE / path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > req.max_lines:
            text = "\n".join(lines[: req.max_lines])
            text += f"\n\n... [{len(lines) - req.max_lines} more lines truncated]"
        return ToolResponse(success=True, output=text)
    except FileNotFoundError:
        return ToolResponse(success=False, output="", error=f"File not found: {req.path}")
    except Exception as exc:
        return ToolResponse(success=False, output="", error=f"Read error: {exc}")


@app.post("/tools/write_file", response_model=ToolResponse)
async def write_file(req: WriteFileRequest) -> ToolResponse:
    """Write content to a file, creating parent directories."""
    path = Path(req.path)
    if not path.is_absolute():
        path = WORKSPACE / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if req.append:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(req.content)
        else:
            path.write_text(req.content, encoding="utf-8")
        verb = "Appended" if req.append else "Wrote"
        return ToolResponse(
            success=True,
            output=f"{verb} {len(req.content)} chars to {path} (file size now {path.stat().st_size} bytes)",
        )
    except Exception as exc:
        return ToolResponse(success=False, output="", error=f"Write error: {exc}")


@app.post("/tools/list_files", response_model=ToolResponse)
async def list_files(req: ListFilesRequest) -> ToolResponse:
    """List directory contents."""
    path = Path(req.path)
    if not path.is_absolute():
        path = WORKSPACE / path
    try:
        if not path.is_dir():
            return ToolResponse(success=False, output="", error=f"Not a directory: {req.path}")
        entries: list[str] = []
        for entry in sorted(path.iterdir()):
            kind = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else 0
            entries.append(f"{kind}\t{size}\t{entry.name}")
        return ToolResponse(success=True, output="\n".join(entries))
    except Exception as exc:
        return ToolResponse(success=False, output="", error=f"List error: {exc}")


# ── Screenshot ───────────────────────────────────────────────────────────

@app.post("/tools/screenshot", response_model=ToolResponse)
async def screenshot() -> ToolResponse:
    """Take a screenshot using scrot and return the file path."""
    screenshot_path = "/tmp/screenshot.png"
    try:
        result = subprocess.run(
            ["scrot", screenshot_path],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
        )
        if result.returncode != 0:
            return ToolResponse(success=False, output="", error=f"scrot failed: {result.stderr}")
        return ToolResponse(
            success=True,
            output=f"Screenshot saved to {screenshot_path}",
            artifacts=[screenshot_path],
        )
    except Exception as exc:
        return ToolResponse(success=False, output="", error=f"Screenshot error: {exc}")

