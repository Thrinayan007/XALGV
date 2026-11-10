"""Office automation tools — create, inspect, and convert Word and Excel documents.

Supports Phase 3 requirements from the project specification:
- Structured creation of Word documents (.docx) with headings, tables, and styling.
- Structured creation of Excel spreadsheets (.xlsx) with formulas and auto-styling.
- Headless LibreOffice conversion to PDF for verification and presentation.
- Inspection and verification of generated office files.
"""

from __future__ import annotations

from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class OfficeCreateDocumentTool(Tool):
    """Create a styled Word document (.docx) in the sandbox workspace."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "office_create_document"

    @property
    def description(self) -> str:
        return (
            "Create a professionally formatted Word document (.docx) in the workspace. "
            "Supports a document title, subtitle, author, and structured sections with "
            "headings, body text, bullet lists, and data tables. Automatically formats "
            "margins and includes page numbers."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute workspace path for the .docx file (e.g. 'report.docx' or '/workspace/poet_report/report.docx').",
                },
                "title": {
                    "type": "string",
                    "description": "Document title heading.",
                },
                "subtitle": {
                    "type": "string",
                    "description": "Optional subtitle appearing below the main title.",
                },
                "author": {
                    "type": "string",
                    "description": "Optional author name or credit.",
                },
                "sections": {
                    "type": "array",
                    "description": "Ordered list of document sections.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {
                                "type": "string",
                                "description": "Section heading text.",
                            },
                            "level": {
                                "type": "integer",
                                "description": "Heading level: 1 (main section), 2 (subsection), or 3 (sub-subsection). Default: 1.",
                                "default": 1,
                                "minimum": 1,
                                "maximum": 3,
                            },
                            "content": {
                                "type": "string",
                                "description": "Body paragraph text for this section.",
                            },
                            "bullets": {
                                "type": "array",
                                "description": "List of bullet points.",
                                "items": {"type": "string"},
                            },
                            "table": {
                                "type": "object",
                                "description": "Optional table to embed in this section.",
                                "properties": {
                                    "headers": {
                                        "type": "array",
                                        "description": "Table column header names.",
                                        "items": {"type": "string"},
                                    },
                                    "rows": {
                                        "type": "array",
                                        "description": "List of data rows (each row is a list of cell values).",
                                        "items": {
                                            "type": "array",
                                            "items": {"type": ["string", "number", "boolean"]},
                                        },
                                    },
                                },
                                "required": ["headers", "rows"],
                            },
                        },
                    },
                },
                "include_page_numbers": {
                    "type": "boolean",
                    "description": "Whether to include running page numbers in the footer (default: true).",
                    "default": True,
                },
            },
            "required": ["path", "title"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.office_create_document(
            path=params["path"],
            title=params["title"],
            subtitle=params.get("subtitle"),
            author=params.get("author"),
            sections=params.get("sections", []),
            include_page_numbers=params.get("include_page_numbers", True),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.artifacts:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Word document created and verified: {result.artifacts[0]}",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=result.error or "Failed to create Word document.",
        )


class OfficeCreateSpreadsheetTool(Tool):
    """Create a structured Excel workbook (.xlsx) in the sandbox workspace."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "office_create_spreadsheet"

    @property
    def description(self) -> str:
        return (
            "Create a structured Excel workbook (.xlsx) with one or more sheets, "
            "formatted headers, data rows, auto-fitted columns, and calculation formulas "
            "(such as =SUM(B2:B10) or =AVERAGE(C2:C10))."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute workspace path for the .xlsx file (e.g. 'budget.xlsx').",
                },
                "sheets": {
                    "type": "array",
                    "description": "List of sheets to create in the workbook.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Tab name for this sheet (max 31 chars). Default: 'Sheet1'.",
                                "default": "Sheet1",
                            },
                            "headers": {
                                "type": "array",
                                "description": "List of column header labels for row 1.",
                                "items": {"type": "string"},
                            },
                            "rows": {
                                "type": "array",
                                "description": "List of data rows (numbers, dates, or strings).",
                                "items": {
                                    "type": "array",
                                    "items": {"type": ["string", "number", "boolean"]},
                                },
                            },
                            "formulas": {
                                "type": "array",
                                "description": "Formulas to inject into specific cells.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "cell": {
                                            "type": "string",
                                            "description": "Target cell coordinate, e.g. 'B10' or 'D5'.",
                                        },
                                        "formula": {
                                            "type": "string",
                                            "description": "Excel formula string starting with '=', e.g. '=SUM(B2:B9)'.",
                                        },
                                        "label": {
                                            "type": "string",
                                            "description": "Optional label placed in the cell immediately to the left, e.g. 'Total'.",
                                        },
                                    },
                                    "required": ["cell", "formula"],
                                },
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["path", "sheets"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.office_create_spreadsheet(
            path=params["path"],
            sheets=params["sheets"],
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.artifacts:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Spreadsheet created and verified: {result.artifacts[0]}",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=result.error or "Failed to create Excel spreadsheet.",
        )


class OfficeConvertToPdfTool(Tool):
    """Convert an office document to PDF using headless LibreOffice."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "office_convert_to_pdf"

    @property
    def description(self) -> str:
        return (
            "Convert any document (.docx, .xlsx, .pptx, or .html) to a PDF using "
            "headless LibreOffice inside the sandbox. Returns the converted PDF path."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path to the source document to convert.",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Target directory for the PDF output (default: '/workspace').",
                    "default": "/workspace",
                },
            },
            "required": ["source_path"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.office_convert_to_pdf(
            source_path=params["source_path"],
            output_dir=params.get("output_dir", "/workspace"),
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.artifacts:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"PDF conversion completed: {result.artifacts[0]}",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=result.error or "Failed to convert document to PDF.",
        )


class OfficeInspectTool(Tool):
    """Inspect and verify the contents and structure of an Office file (.docx, .xlsx, or .pdf)."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "office_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect the structure and contents of a generated Office file (.docx, .xlsx, or .pdf). "
            "Reports word count, headings, table counts, sheet dimensions, and page counts. "
            "Use this to verify deliverables before concluding a task."
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
                    "description": "Path to the .docx, .xlsx, or .pdf file to inspect.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.office_inspect(path=params["path"])

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Office file inspected and verified.",
                evidence=result.metadata,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=result.error or "Failed to inspect office file.",
        )


def office_tools(sandbox_client: Any = None) -> list[Tool]:
    """Return all Phase 3 office automation tools."""
    return [
        OfficeCreateDocumentTool(sandbox_client),
        OfficeCreateSpreadsheetTool(sandbox_client),
        OfficeConvertToPdfTool(sandbox_client),
        OfficeInspectTool(sandbox_client),
    ]

