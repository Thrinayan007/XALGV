"""Tests for Phase 3 office automation tools — schemas, verification, and document creation."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import ToolResult, VerificationStatus
from local_agent.tools.office import (
    OfficeConvertToPdfTool,
    OfficeCreateDocumentTool,
    OfficeCreateSpreadsheetTool,
    OfficeInspectTool,
    office_tools,
)


class TestOfficeToolSchemas:
    """Verify tool schemas, metadata, and OpenAI serialization."""

    TOOLS = [
        ("office_create_document", OfficeCreateDocumentTool, PermissionTier.WORKSPACE_WRITE),
        ("office_create_spreadsheet", OfficeCreateSpreadsheetTool, PermissionTier.WORKSPACE_WRITE),
        ("office_convert_to_pdf", OfficeConvertToPdfTool, PermissionTier.WORKSPACE_WRITE),
        ("office_inspect", OfficeInspectTool, PermissionTier.READ_ONLY),
    ]

    @pytest.mark.parametrize("expected_name,tool_cls,expected_tier", TOOLS)
    def test_tool_metadata(self, expected_name, tool_cls, expected_tier):
        tool = tool_cls()
        assert tool.name == expected_name
        assert len(tool.description) > 20
        assert tool.permission_tier == expected_tier

    @pytest.mark.parametrize("expected_name,tool_cls,_", TOOLS)
    def test_openai_format(self, expected_name, tool_cls, _):
        tool = tool_cls()
        defn = tool.to_openai_tool()
        assert defn["type"] == "function"
        assert defn["function"]["name"] == expected_name
        assert "parameters" in defn["function"]
        assert defn["function"]["parameters"]["type"] == "object"

    def test_create_document_schema(self):
        tool = OfficeCreateDocumentTool()
        schema = tool.parameters_schema
        assert "path" in schema["required"]
        assert "title" in schema["required"]
        assert "sections" in schema["properties"]

    def test_create_spreadsheet_schema(self):
        tool = OfficeCreateSpreadsheetTool()
        schema = tool.parameters_schema
        assert "path" in schema["required"]
        assert "sheets" in schema["required"]

    def test_convert_to_pdf_schema(self):
        tool = OfficeConvertToPdfTool()
        schema = tool.parameters_schema
        assert "source_path" in schema["required"]

    def test_office_tools_helper(self):
        tools = office_tools()
        assert len(tools) == 4
        names = {t.name for t in tools}
        assert names == {
            "office_create_document",
            "office_create_spreadsheet",
            "office_convert_to_pdf",
            "office_inspect",
        }


class TestOfficeToolExecution:
    """Verify execution behavior with and without sandbox."""

    @pytest.mark.asyncio
    async def test_tools_without_sandbox(self):
        for tool_cls in [
            OfficeCreateDocumentTool,
            OfficeCreateSpreadsheetTool,
            OfficeConvertToPdfTool,
            OfficeInspectTool,
        ]:
            tool = tool_cls(sandbox_client=None)
            res = await tool.execute({"path": "test", "title": "test", "sheets": [], "source_path": "x"})
            assert not res.success
            assert "Sandbox not configured" in res.error

    @pytest.mark.asyncio
    async def test_document_delegates_to_sandbox(self):
        sb = AsyncMock()
        sb.office_create_document.return_value = ToolResult(
            success=True, output="Created docx", artifacts=["/workspace/report.docx"]
        )
        tool = OfficeCreateDocumentTool(sb)
        res = await tool.execute({"path": "report.docx", "title": "My Report"})
        assert res.success
        assert "/workspace/report.docx" in res.artifacts
        sb.office_create_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spreadsheet_delegates_to_sandbox(self):
        sb = AsyncMock()
        sb.office_create_spreadsheet.return_value = ToolResult(
            success=True, output="Created xlsx", artifacts=["/workspace/data.xlsx"]
        )
        tool = OfficeCreateSpreadsheetTool(sb)
        res = await tool.execute({"path": "data.xlsx", "sheets": [{"name": "Sales"}]})
        assert res.success
        sb.office_create_spreadsheet.assert_awaited_once()


class TestOfficeVerification:
    """Verify post-action validation logic."""

    @pytest.mark.asyncio
    async def test_create_document_verification(self):
        tool = OfficeCreateDocumentTool()
        success_res = ToolResult(success=True, output="Created docx", artifacts=["/workspace/report.docx"], metadata={"file_size": 12000})
        ver = await tool.verify({"path": "report.docx"}, success_res)
        assert ver.status == VerificationStatus.PASSED

        fail_res = ToolResult(success=False, output="", error="Write failure")
        ver = await tool.verify({"path": "report.docx"}, fail_res)
        assert ver.status == VerificationStatus.FAILED

    @pytest.mark.asyncio
    async def test_create_spreadsheet_verification(self):
        tool = OfficeCreateSpreadsheetTool()
        success_res = ToolResult(success=True, output="Created xlsx", artifacts=["/workspace/sheet.xlsx"], metadata={"file_size": 8000})
        ver = await tool.verify({"path": "sheet.xlsx"}, success_res)
        assert ver.status == VerificationStatus.PASSED


class TestOfficeServerEndpoints:
    """Functional tests for the underlying office generation endpoints using local packages."""

    @pytest.mark.asyncio
    async def test_functional_create_document_and_inspect(self):
        from sandbox.office_server import (
            CreateDocumentRequest,
            DocumentSection,
            InspectRequest,
            TableData,
            create_document,
            inspect_document,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            from unittest.mock import patch
            with patch("sandbox.office_server.WORKSPACE", Path(tmp_dir)):
                req = CreateDocumentRequest(
                    path="quarterly_report.docx",
                    title="Q3 Performance Summary",
                    subtitle="Executive Overview",
                    author="Agent XALGV",
                    sections=[
                        DocumentSection(
                            heading="1. Financial Highlights",
                            level=1,
                            content="Revenue grew by 24% year-over-year.",
                            bullets=["Strong enterprise adoption", "New platform features released"],
                            table=TableData(
                                headers=["Quarter", "Revenue (M$)", "Growth"],
                                rows=[
                                    ["Q1", 12.5, "+15%"],
                                    ["Q2", 14.8, "+18%"],
                                    ["Q3", 18.2, "+24%"],
                                ],
                            ),
                        ),
                    ],
                )
                res = await create_document(req)
                assert res.success
                assert len(res.artifacts) == 1
                created_file = Path(res.artifacts[0])
                assert created_file.exists()
                assert created_file.stat().st_size > 0

                # Test inspection of the created document
                inspect_req = InspectRequest(path="quarterly_report.docx")
                inspect_res = await inspect_document(inspect_req)
                assert inspect_res.success
                assert inspect_res.metadata["tables_count"] == 1
                assert inspect_res.metadata["word_count"] > 10

    @pytest.mark.asyncio
    async def test_functional_create_spreadsheet_and_inspect(self):
        from sandbox.office_server import (
            CreateSpreadsheetRequest,
            InspectRequest,
            SpreadsheetFormula,
            SpreadsheetSheet,
            create_spreadsheet,
            inspect_document,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            from unittest.mock import patch
            with patch("sandbox.office_server.WORKSPACE", Path(tmp_dir)):
                req = CreateSpreadsheetRequest(
                    path="annual_budget.xlsx",
                    sheets=[
                        SpreadsheetSheet(
                            name="Expenses",
                            headers=["Category", "Q1", "Q2", "Q3", "Q4"],
                            rows=[
                                ["Engineering", 50000, 52000, 55000, 58000],
                                ["Marketing", 20000, 25000, 22000, 30000],
                                ["Operations", 15000, 16000, 15500, 17000],
                            ],
                            formulas=[
                                SpreadsheetFormula(cell="B5", formula="=SUM(B2:B4)", label="Total"),
                            ],
                        ),
                    ],
                )
                res = await create_spreadsheet(req)
                assert res.success
                assert len(res.artifacts) == 1
                created_file = Path(res.artifacts[0])
                assert created_file.exists()
                assert created_file.stat().st_size > 0

                # Test inspection
                inspect_req = InspectRequest(path="annual_budget.xlsx")
                inspect_res = await inspect_document(inspect_req)
                assert inspect_res.success
                assert inspect_res.metadata["sheets_count"] == 1
                assert inspect_res.metadata["sheets"][0]["name"] == "Expenses"
