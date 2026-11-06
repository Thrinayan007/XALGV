"""Browser automation tools — Playwright-based browser control inside the sandbox.

The browser module implements the docx's "structure first, pixels second"
principle: it uses the accessibility tree and DOM structure for navigation
and interaction, falling back to screenshots only when needed.

Tools:
* ``browser_navigate`` — Load a URL in the browser.
* ``browser_get_content`` — Get the page's accessible content (a11y tree or text).
* ``browser_click`` — Click an element by selector or accessible name.
* ``browser_type`` — Type text into an input element.
* ``browser_screenshot`` — Take a screenshot of the current page (fallback).
"""

from __future__ import annotations

from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class BrowserNavigateTool(Tool):
    """Navigate to a URL in the sandbox browser."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "browser_navigate"

    @property
    def description(self) -> str:
        return (
            "Navigate to a URL in the sandbox browser.  Returns the page title "
            "and a summary of the page's accessible content.  Use this before "
            "interacting with page elements."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to.",
                },
                "wait_for": {
                    "type": "string",
                    "description": "Wait condition: 'load', 'domcontentloaded', or 'networkidle' (default: 'load').",
                    "default": "load",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.browser_navigate(
            url=params["url"],
            wait_for=params.get("wait_for", "load"),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Navigation successful.",
                evidence={"url": params["url"]},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Navigation failed: {result.error}",
        )


class BrowserGetContentTool(Tool):
    """Get the accessible content of the current page.

    Returns the page's accessibility tree (structure-first approach) or
    simplified text content.  This is preferred over screenshots for
    understanding page structure.
    """

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "browser_get_content"

    @property
    def description(self) -> str:
        return (
            "Get the current page's content as structured text.  Returns "
            "the accessibility tree with element roles, names, and values.  "
            "Use this to understand page structure before clicking or typing."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Content mode: 'a11y' for accessibility tree, 'text' for text content, 'html' for simplified HTML (default: 'a11y').",
                    "default": "a11y",
                    "enum": ["a11y", "text", "html"],
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to scope the content to a specific element.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.browser_get_content(
            mode=params.get("mode", "a11y"),
            selector=params.get("selector"),
        )


class BrowserClickTool(Tool):
    """Click an element on the page by CSS selector or accessible name."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return (
            "Click an element on the page.  Identify the element by CSS selector "
            "or by its accessible name/role from the accessibility tree.  "
            "Prefer accessible names over selectors when possible."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the element to click.",
                },
                "text": {
                    "type": "string",
                    "description": "Visible text or accessible name of the element to click (alternative to selector).",
                },
                "role": {
                    "type": "string",
                    "description": "ARIA role to match when using text-based selection (e.g. 'button', 'link').",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.browser_click(
            selector=params.get("selector"),
            text=params.get("text"),
            role=params.get("role"),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Click action completed.",
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Click failed: {result.error}",
        )


class BrowserTypeTool(Tool):
    """Type text into an input element on the page."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return (
            "Type text into an input or text field on the page.  "
            "Identify the element by CSS selector or accessible name.  "
            "Optionally clear the field first."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the input element.",
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the element.",
                },
                "clear_first": {
                    "type": "boolean",
                    "description": "Clear the field before typing (default: true).",
                    "default": True,
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Press Enter after typing (default: false).",
                    "default": False,
                },
            },
            "required": ["selector", "text"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.browser_type(
            selector=params["selector"],
            text=params["text"],
            clear_first=params.get("clear_first", True),
            press_enter=params.get("press_enter", False),
        )


class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current page (vision fallback).

    Per the docx's design principle: "Structure first, pixels second."
    Use ``browser_get_content`` first.  Only use this tool when
    the accessibility tree is insufficient.
    """

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return (
            "Take a screenshot of the current browser page.  Returns the "
            "screenshot file path.  Use this only as a fallback when the "
            "accessibility tree is not enough to understand the page."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page (default: false).",
                    "default": False,
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to capture only a specific element.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.browser_screenshot(
            full_page=params.get("full_page", False),
            selector=params.get("selector"),
        )

