"""Workspace boundary for declared outputs and checksum-bound handoffs."""

import ast
import hashlib
import os
import stat
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from local_agent.tools.base import ToolResult

from .base import AgentTask, ArtifactManifest, FileCheck, MarkdownCheck, PythonSchemaCheck, SourceManifestCheck
from .evidence import SourceManifest, markdown_evidence

MAX_BYTES = 50 * 1024 * 1024


class Artifacts:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def _path(self, relative: str) -> Path:
        path = self.workspace
        for component in relative.split("/"):
            if component in {"", ".", ".."} or any(char in component for char in ":\\"):
                raise ValueError("Invalid artifact path component")
            path /= component
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Artifact paths cannot traverse links")
        if not path.is_relative_to(self.workspace) or path.resolve() != path:
            raise ValueError("Artifact path escapes the workspace")
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Artifact must be a regular file with one filesystem link")
        return path

    def claim(self, run_id: str) -> None:
        path = self._path(f"artifacts/{run_id}/.reservation").parent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(exist_ok=False)

    def output(self, task: AgentTask, path: str) -> Path:
        if not isinstance(path, str) or path not in task.expected_outputs:
            raise ValueError("Only declared output paths may be written")
        return self._path(path)

    def _read(self, path: str) -> bytes:
        with self._path(path).open("rb") as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError("Artifact exceeds the size limit")
        return content

    def manifest(self, path: str) -> ArtifactManifest:
        content = self._read(path)
        return ArtifactManifest(path=path, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def python_schema(self, path: str) -> dict:
        content = self._read(path)
        if len(content) > 32_000 or not path.endswith(".py"):
            raise ValueError("Expected a Python schema file up to 32 KB")
        body = ast.parse(content).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Assign) or len(body[0].targets) != 1:
            raise ValueError("Expected one assignment to SCHEMA")
        target = body[0].targets[0]
        if not isinstance(target, ast.Name) or target.id != "SCHEMA" or not isinstance(body[0].value, ast.Dict):
            raise ValueError("Expected a literal SCHEMA dictionary")
        keys = [ast.literal_eval(key) for key in body[0].value.keys]
        schema = ast.literal_eval(body[0].value)
        if len(keys) != len(schema) or not all(isinstance(key, str) and value in ("str", "bool", "float") for key, value in schema.items()):
            raise ValueError("Schema fields must have distinct names and str, bool, or float types")
        return schema

    def verify(self, check: FileCheck) -> bool:
        if isinstance(check, PythonSchemaCheck):
            return self.python_schema(check.path) == {item.name: item.type for item in check.fields}
        content = self._read(check.path)
        if isinstance(check, SourceManifestCheck):
            manifest = SourceManifest.model_validate_json(content)
            hosts = {urlsplit(item.url).hostname for item in manifest.sources}
            return len(manifest.sources) >= check.min_sources and set(check.required_hosts) <= hosts
        if isinstance(check, MarkdownCheck):
            if len(content) > 256 * 1024 or not check.path.endswith(".md"):
                return False
            headings, links = markdown_evidence(content.decode("utf-8"))
            if not set(check.headings) <= headings:
                return False
            if check.sources_path:
                manifest = SourceManifest.model_validate_json(self._read(check.sources_path))
                return links <= {item.url for item in manifest.sources} and len(links) >= check.min_citations
            return True
        if len(content) < check.min_bytes:
            return False
        if check.contains is not None and check.contains not in content.decode("utf-8", errors="replace"):
            return False
        if check.sha256 and hashlib.sha256(content).hexdigest() != check.sha256:
            return False
        if check.png or check.width is not None or check.height is not None:
            from PIL import Image

            with Image.open(BytesIO(content)) as image:
                if image.format != "PNG" or image.width * image.height > 16_000_000:
                    return False
                if check.width not in (None, image.width) or check.height not in (None, image.height):
                    return False
                image.verify()
        return True


class TaskFiles:
    """Client interface used by the existing file tools, with task-specific authority."""

    def __init__(self, artifacts: Artifacts, task: AgentTask):
        self.artifacts = artifacts
        self.task = task
        self.inputs = {item.path: item.sha256 for item in task.inputs}

    async def read_file(self, path: str, *, max_lines: int = 200) -> ToolResult:
        if not isinstance(path, str) or (path not in self.inputs and path not in self.task.expected_outputs):
            return ToolResult(success=False, output="", error="Only declared inputs and outputs may be read")
        if type(max_lines) is not int or not 1 <= max_lines <= 1000:
            return ToolResult(success=False, output="", error="max_lines must be between 1 and 1000")
        try:
            content = self.artifacts._read(path)
            if path in self.inputs and hashlib.sha256(content).hexdigest() != self.inputs[path]:
                raise ValueError("Input does not match the verified handoff checksum")
            lines = content.decode("utf-8", errors="replace").splitlines()
            return ToolResult(success=True, output="\n".join(lines[:max_lines])[:32000])
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))

    async def write_file(self, path: str, content: str, append: bool = False) -> ToolResult:
        try:
            target = self.artifacts.output(self.task, path)
            if not isinstance(content, str):
                raise TypeError("File content must be text")
            data = content.encode("utf-8")
            if len(data) > 1024 * 1024:
                raise ValueError("One write may contain up to 1 MiB")
            if append and target.exists():
                data = self.artifacts._read(path) + data
            if len(data) > MAX_BYTES:
                raise ValueError("Artifact exceeds the size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(data)
                    stream.close()
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return ToolResult(success=True, output=f"Wrote {path}")
        except (OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
