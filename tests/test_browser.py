"""Tests for browser automation tools — schema validation and sandbox-absent fallback."""

import pytest

from local_agent.security.permissions import PermissionTier
from local_agent.tools.browser import (
    BrowserClickTool,
    BrowserGetContentTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
)


# ── Schema and metadata tests (sync, no sandbox needed) ─────────────────

class TestBrowserToolSchemas:
    """Verify all browser tools have correct schemas and metadata."""

    TOOLS = [
        ("browser_navigate", BrowserNavigateTool),
        ("browser_get_content", BrowserGetContentTool),
        ("browser_click", BrowserClickTool),
        ("browser_type", BrowserTypeTool),
        ("browser_screenshot", BrowserScreenshotTool),
    ]

    @pytest.mark.parametrize("expected_name,tool_cls", TOOLS)
    def test_tool_name(self, expected_name, tool_cls):
        tool = tool_cls()
        assert tool.name == expected_name

    @pytest.mark.parametrize("expected_name,tool_cls", TOOLS)
    def test_tool_description_non_empty(self, expected_name, tool_cls):
        tool = tool_cls()
        assert len(tool.description) > 10

    @pytest.mark.parametrize("expected_name,tool_cls", TOOLS)
    def test_tool_permission_tier(self, expected_name, tool_cls):
        tool = tool_cls()
        assert tool.permission_tier == PermissionTier.NETWORK

    @pytest.mark.parametrize("expected_name,tool_cls", TOOLS)
    def test_openai_format(self, expected_name, tool_cls):
        tool = tool_cls()
        defn = tool.to_openai_tool()
        assert defn["type"] == "function"
        assert defn["function"]["name"] == expected_name
        assert "parameters" in defn["function"]
        assert defn["function"]["parameters"]["type"] == "object"

    def test_navigate_requires_url(self):
        tool = BrowserNavigateTool()
        schema = tool.parameters_schema
        assert "url" in schema["properties"]
        assert "url" in schema["required"]

    def test_type_requires_selector_and_text(self):
        tool = BrowserTypeTool()
        schema = tool.parameters_schema
        assert "selector" in schema["properties"]
        assert "text" in schema["properties"]
        assert "selector" in schema["required"]
        assert "text" in schema["required"]

    def test_get_content_has_mode_enum(self):
        tool = BrowserGetContentTool()
        schema = tool.parameters_schema
        assert "mode" in schema["properties"]
        assert set(schema["properties"]["mode"]["enum"]) == {"a11y", "text", "html"}


# ── Execution tests (async, no sandbox → graceful error) ────────────────

class TestBrowserToolExecution:
    """Tools return a clear error when no sandbox is configured."""

    @pytest.mark.asyncio
    async def test_navigate_without_sandbox(self):
        tool = BrowserNavigateTool(sandbox_client=None)
        result = await tool.execute({"url": "https://example.com"})
        assert not result.success
        assert "Sandbox not configured" in result.error

    @pytest.mark.asyncio
    async def test_get_content_without_sandbox(self):
        tool = BrowserGetContentTool(sandbox_client=None)
        result = await tool.execute({"mode": "a11y"})
        assert not result.success
        assert "Sandbox not configured" in result.error

    @pytest.mark.asyncio
    async def test_click_without_sandbox(self):
        tool = BrowserClickTool(sandbox_client=None)
        result = await tool.execute({"text": "Submit"})
        assert not result.success
        assert "Sandbox not configured" in result.error

    @pytest.mark.asyncio
    async def test_type_without_sandbox(self):
        tool = BrowserTypeTool(sandbox_client=None)
        result = await tool.execute({"selector": "#email", "text": "test@test.com"})
        assert not result.success
        assert "Sandbox not configured" in result.error

    @pytest.mark.asyncio
    async def test_screenshot_without_sandbox(self):
        tool = BrowserScreenshotTool(sandbox_client=None)
        result = await tool.execute({"full_page": True})
        assert not result.success
        assert "Sandbox not configured" in result.error


# ── Verification tests ──────────────────────────────────────────────────

class TestBrowserVerification:
    @pytest.mark.asyncio
    async def test_navigate_verify_success(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = BrowserNavigateTool()
        result = ToolResult(success=True, output="Page loaded")
        verification = await tool.verify({"url": "https://example.com"}, result)
        assert verification.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_navigate_verify_failure(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = BrowserNavigateTool()
        result = ToolResult(success=False, output="", error="Connection refused")
        verification = await tool.verify({"url": "https://example.com"}, result)
        assert verification.status == VerificationStatus.FAILED

    @pytest.mark.asyncio
    async def test_click_verify_success(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = BrowserClickTool()
        result = ToolResult(success=True, output="Clicked")
        verification = await tool.verify({"text": "Submit"}, result)
        assert verification.status == VerificationStatus.PASSED

