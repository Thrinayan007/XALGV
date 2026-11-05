"""Tests for the tool system: base, registry, and permission gating."""

import asyncio
import unittest
from typing import Any

from local_agent.security.permissions import (
    PermissionDeniedError,
    PermissionPolicy,
    PermissionTier,
)
from local_agent.tools.base import (
    Tool,
    ToolResult,
    VerificationResult,
    VerificationStatus,
)
from local_agent.tools.registry import ToolRegistry


# ── Test tool implementations ────────────────────────────────────────────

class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the input message."

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, output=params.get("message", ""))

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.output == params.get("message"):
            return VerificationResult(status=VerificationStatus.PASSED, message="Echo matched.")
        return VerificationResult(status=VerificationStatus.FAILED, message="Echo did not match.")


class DangerousTool(Tool):
    @property
    def name(self) -> str:
        return "danger"

    @property
    def description(self) -> str:
        return "A dangerous tool."

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.DESTRUCTIVE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, output="boom")


# ── Tests ────────────────────────────────────────────────────────────────

class TestToolBase(unittest.TestCase):
    def test_tool_openai_format(self):
        tool = EchoTool()
        defn = tool.to_openai_tool()
        self.assertEqual(defn["type"], "function")
        self.assertEqual(defn["function"]["name"], "echo")
        self.assertIn("parameters", defn["function"])

    def test_tool_result_truncation(self):
        result = ToolResult(success=True, output="x" * 10000)
        truncated = result.truncated(max_chars=100)
        self.assertLessEqual(len(truncated), 200)  # rough bound with "... truncated ..."
        self.assertIn("truncated", truncated)


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry(PermissionPolicy())
        self.registry.register(EchoTool())
        self.registry.register(DangerousTool())

    def test_register_and_lookup(self):
        self.assertIsNotNone(self.registry.get("echo"))
        self.assertIsNone(self.registry.get("nonexistent"))

    def test_duplicate_registration_raises(self):
        with self.assertRaises(ValueError):
            self.registry.register(EchoTool())

    def test_list_tools_filters_by_tier(self):
        read_only = self.registry.list_tools(max_tier=PermissionTier.READ_ONLY)
        self.assertEqual(len(read_only), 1)
        self.assertEqual(read_only[0].name, "echo")

        all_tools = self.registry.list_tools()
        self.assertEqual(len(all_tools), 2)

    def test_get_tool_definitions(self):
        defs = self.registry.get_tool_definitions(max_tier=PermissionTier.READ_ONLY)
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["function"]["name"], "echo")

    def test_execute_allowed_tool(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.registry.execute("echo", {"message": "hello"})
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output, "hello")

    def test_execute_blocked_tool(self):
        with self.assertRaises(PermissionDeniedError):
            asyncio.get_event_loop().run_until_complete(
                self.registry.execute("danger", {})
            )

    def test_execute_blocked_tool_with_approval(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.registry.execute("danger", {}, human_approved=True)
        )
        self.assertTrue(result.success)

    def test_execute_unknown_tool(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.registry.execute("unknown", {})
        )
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.error)

    def test_execute_and_verify(self):
        result, verification = asyncio.get_event_loop().run_until_complete(
            self.registry.execute_and_verify("echo", {"message": "test"})
        )
        self.assertTrue(result.success)
        self.assertEqual(verification.status, VerificationStatus.PASSED)


class TestPermissions(unittest.TestCase):
    def test_default_policy_allows_read(self):
        policy = PermissionPolicy()
        self.assertTrue(policy.is_allowed(PermissionTier.READ_ONLY))
        self.assertTrue(policy.is_allowed(PermissionTier.WORKSPACE_WRITE))

    def test_default_policy_blocks_destructive(self):
        policy = PermissionPolicy()
        self.assertFalse(policy.is_allowed(PermissionTier.DESTRUCTIVE))
        self.assertTrue(policy.is_allowed(PermissionTier.DESTRUCTIVE, human_approved=True))

    def test_needs_approval(self):
        policy = PermissionPolicy()
        self.assertFalse(policy.needs_approval(PermissionTier.READ_ONLY))
        self.assertTrue(policy.needs_approval(PermissionTier.DESTRUCTIVE))


if __name__ == "__main__":
    unittest.main()
