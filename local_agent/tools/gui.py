"""Desktop GUI automation tools — control the X11 virtual desktop inside the sandbox.

Supports Phase 3 requirements from the project specification:
- Coordinate-validated screen clicks (0 <= x <= 1280, 0 <= y <= 720)
- Keystroke typing into active windows with keystroke delays
- Sanitized keyboard shortcuts (hotkeys) with strict validation
- Desktop screenshot capture for visual observation and feedback
- Window listing to detect and inspect open application windows
- Application launching from an authorized whitelist (terminal, libreoffice, feh)
"""

from __future__ import annotations

import re
from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus

DEFAULT_MAX_WIDTH = 1280
DEFAULT_MAX_HEIGHT = 720

# Regex for validating safe keyboard shortcuts
VALID_HOTKEY_REGEX = re.compile(
    r"^(?:(?:ctrl|control|alt|shift|super|meta)\+)*(?:[a-zA-Z0-9]|F[1-9]|F1[0-2]|Return|Enter|BackSpace|Tab|Escape|Delete|Insert|Home|End|Page_Up|Page_Down|Prior|Next|Up|Down|Left|Right|space|plus|minus|slash|period|comma)$",
    re.IGNORECASE,
)

ALLOWED_APPS = {
    "terminal", "xterm", "feh", "image_viewer",
    "libreoffice", "soffice", "calc", "writer", "impress",
}


class GuiClickTool(Tool):
    """Click screen coordinates on the virtual desktop."""

    def __init__(self, sandbox_client: Any = None, max_width: int = DEFAULT_MAX_WIDTH, max_height: int = DEFAULT_MAX_HEIGHT) -> None:
        self._sandbox = sandbox_client
        self._max_width = max_width
        self._max_height = max_height

    @property
    def name(self) -> str:
        return "gui_click"

    @property
    def description(self) -> str:
        return (
            "Click screen coordinates (x, y) on the virtual desktop. Supports mouse buttons "
            "(1=left, 2=middle, 3=right) and double-clicking. Coordinates are validated to "
            "ensure they reside within the screen boundaries. Automatically captures a "
            "post-click screenshot to observe the effect."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {
                    "type": "integer",
                    "description": f"Horizontal pixel coordinate (0 to {self._max_width}).",
                    "minimum": 0,
                    "maximum": self._max_width,
                },
                "y": {
                    "type": "integer",
                    "description": f"Vertical pixel coordinate (0 to {self._max_height}).",
                    "minimum": 0,
                    "maximum": self._max_height,
                },
                "button": {
                    "type": "integer",
                    "description": "Mouse button: 1=left (default), 2=middle, 3=right.",
                    "enum": [1, 2, 3],
                    "default": 1,
                },
                "double": {
                    "type": "boolean",
                    "description": "Whether to perform a double click (default: false).",
                    "default": False,
                },
                "capture_after": {
                    "type": "boolean",
                    "description": "Whether to capture a screenshot after the click for observation (default: true).",
                    "default": True,
                },
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return ToolResult(success=False, output="", error="Both 'x' and 'y' coordinates are required.")

        # Coordinate bounds validation
        if not (0 <= x <= self._max_width):
            return ToolResult(
                success=False,
                output="",
                error=f"Coordinate x={x} is out of bounds [0, {self._max_width}].",
            )
        if not (0 <= y <= self._max_height):
            return ToolResult(
                success=False,
                output="",
                error=f"Coordinate y={y} is out of bounds [0, {self._max_height}].",
            )

        return await self._sandbox.gui_click(
            x=x,
            y=y,
            button=params.get("button", 1),
            double=params.get("double", False),
            capture_after=params.get("capture_after", True),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            evidence: dict[str, Any] = {"x": params.get("x"), "y": params.get("y")}
            if result.artifacts:
                evidence["screenshot"] = result.artifacts[0]
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Successfully clicked at ({params.get('x')}, {params.get('y')}).",
                evidence=evidence,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"GUI click failed: {result.error}",
        )


class GuiTypeTool(Tool):
    """Type text into the active window on the virtual desktop."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "gui_type"

    @property
    def description(self) -> str:
        return (
            "Type text into the currently focused window on the virtual desktop. "
            "Supports configurable keystroke delay. Automatically captures a screenshot "
            "after typing for visual feedback."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The exact text string to type into the focused window.",
                },
                "delay_ms": {
                    "type": "integer",
                    "description": "Keystroke delay in milliseconds (default: 12ms).",
                    "minimum": 0,
                    "maximum": 1000,
                    "default": 12,
                },
                "capture_after": {
                    "type": "boolean",
                    "description": "Capture a screenshot after typing for visual observation (default: true).",
                    "default": True,
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        text = params.get("text", "")
        if not text:
            return ToolResult(success=False, output="", error="Text parameter cannot be empty.")

        return await self._sandbox.gui_type(
            text=text,
            delay_ms=params.get("delay_ms", 12),
            capture_after=params.get("capture_after", True),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Successfully typed {len(params.get('text', ''))} characters.",
                evidence={"char_count": len(params.get("text", "")), "artifacts": result.artifacts},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"GUI type failed: {result.error}",
        )


class GuiHotkeyTool(Tool):
    """Execute keyboard shortcuts on the virtual desktop."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "gui_hotkey"

    @property
    def description(self) -> str:
        return (
            "Execute a keyboard shortcut or key on the virtual desktop (e.g. 'ctrl+c', 'Alt+F4', "
            "'Return', 'BackSpace', 'ctrl+alt+t', 'Escape'). Validates keys to ensure safety "
            "and automatically captures a screenshot after execution."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Key shortcut, e.g. 'ctrl+c', 'Alt+F4', 'Return', 'Tab', 'BackSpace'.",
                },
                "capture_after": {
                    "type": "boolean",
                    "description": "Capture screenshot after shortcut execution (default: true).",
                    "default": True,
                },
            },
            "required": ["keys"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        keys = params.get("keys", "").strip()
        if not keys:
            return ToolResult(success=False, output="", error="Keys parameter cannot be empty.")

        # Input safety validation
        if not VALID_HOTKEY_REGEX.match(keys):
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Invalid or unsafe hotkey '{keys}'. Only standard key combinations are allowed "
                    "(e.g. 'ctrl+c', 'Return', 'Alt+F4', 'ctrl+alt+t', 'BackSpace')."
                ),
            )

        return await self._sandbox.gui_hotkey(
            keys=keys,
            capture_after=params.get("capture_after", True),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Executed keyboard shortcut '{params.get('keys')}'.",
                evidence={"keys": params.get("keys"), "artifacts": result.artifacts},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"GUI hotkey failed: {result.error}",
        )


class GuiScreenshotTool(Tool):
    """Capture a screenshot of the virtual desktop."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "gui_screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture a full-resolution screenshot of the virtual desktop (Xvfb DISPLAY=:99). "
            "Returns the saved image file path and metadata for visual observation and state verification."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional destination path in /workspace (e.g. 'desktop.png').",
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        return await self._sandbox.gui_screenshot(path=params.get("path"))

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.artifacts:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Desktop screenshot captured: {result.artifacts[0]}",
                evidence={"path": result.artifacts[0]},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Screenshot failed: {result.error}",
        )


class GuiListWindowsTool(Tool):
    """List open application windows on the virtual desktop."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "gui_list_windows"

    @property
    def description(self) -> str:
        return (
            "List all open application windows on the virtual desktop, including their window IDs, "
            "desktop indices, dimensions, positions, and window titles."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "all_desktops": {
                    "type": "boolean",
                    "description": "List windows across all desktops (default: true).",
                    "default": True,
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        return await self._sandbox.gui_list_windows(all_desktops=params.get("all_desktops", True))

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            count = result.metadata.get("count", 0)
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Successfully queried windows (found {count}).",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Failed to query windows: {result.error}",
        )


class GuiLaunchAppTool(Tool):
    """Launch an authorized desktop application on the virtual desktop."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "gui_launch_app"

    @property
    def description(self) -> str:
        return (
            "Launch an authorized desktop application (such as 'terminal', 'xterm', 'feh', "
            "or 'libreoffice') on the virtual desktop. Waits for the application window to "
            "initialize and returns screenshot and window observations."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Authorized application name ('terminal', 'xterm', 'feh', 'libreoffice', 'calc', 'writer', 'impress').",
                    "enum": sorted(list(ALLOWED_APPS)),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional command line arguments or file path to open in the app.",
                },
                "capture_after": {
                    "type": "boolean",
                    "description": "Capture screenshot after launching (default: true).",
                    "default": True,
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Seconds to wait for window initialization (default: 1.0s).",
                    "default": 1.0,
                    "minimum": 0.1,
                    "maximum": 10.0,
                },
            },
            "required": ["app"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        app = params.get("app", "").strip().lower()
        if app not in ALLOWED_APPS:
            return ToolResult(
                success=False,
                output="",
                error=f"Application '{app}' is not authorized. Allowed applications: {sorted(list(ALLOWED_APPS))}",
            )

        return await self._sandbox.gui_launch_app(
            app=app,
            args=params.get("args"),
            capture_after=params.get("capture_after", True),
            wait_seconds=params.get("wait_seconds", 1.0),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Application '{params.get('app')}' launched successfully.",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"App launch failed: {result.error}",
        )


def gui_tools(sandbox_client: Any = None) -> list[Tool]:
    """Return all Phase 3 desktop GUI automation tools configured with sandbox client."""
    return [
        GuiClickTool(sandbox_client),
        GuiTypeTool(sandbox_client),
        GuiHotkeyTool(sandbox_client),
        GuiScreenshotTool(sandbox_client),
        GuiListWindowsTool(sandbox_client),
        GuiLaunchAppTool(sandbox_client),
    ]
