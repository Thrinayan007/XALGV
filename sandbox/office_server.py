"""Office document automation endpoints for the sandbox tool server.

Provides FastAPI endpoints to generate and inspect Word documents (.docx),
Excel spreadsheets (.xlsx), and convert documents to PDF via headless LibreOffice.
Follows the project's API-first principle for office documents.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools/office", tags=["office"])

WORKSPACE = Path("/workspace")


def _resolve_workspace_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = WORKSPACE / p
    # Keep strictly within workspace
    resolved = p.resolve()
    if not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ValueError(f"Path must remain inside /workspace: {rel_or_abs}")
    return resolved


# ── Request / Response models ────────────────────────────────────────────

class OfficeResponse(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    artifacts: list[str] = []
    metadata: dict[str, Any] = {}


class TableData(BaseModel):
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class DocumentSection(BaseModel):
    heading: str | None = None
    level: int = 1  # 1, 2, or 3
    content: str | None = None
    bullets: list[str] = Field(default_factory=list)
    table: TableData | None = None


class CreateDocumentRequest(BaseModel):
    path: str
    title: str
    subtitle: str | None = None
    author: str | None = None
    sections: list[DocumentSection] = Field(default_factory=list)
    include_page_numbers: bool = True


class SpreadsheetFormula(BaseModel):
    cell: str  # e.g. "B10"
    formula: str  # e.g. "=SUM(B2:B9)"
    label: str | None = None  # optional label e.g. "Total" in adjacent cell


class SpreadsheetSheet(BaseModel):
    name: str = "Sheet1"
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    formulas: list[SpreadsheetFormula] = Field(default_factory=list)


class CreateSpreadsheetRequest(BaseModel):
    path: str
    sheets: list[SpreadsheetSheet] = Field(default_factory=list)


class ConvertToPdfRequest(BaseModel):
    source_path: str
    output_dir: str = "/workspace"


class InspectRequest(BaseModel):
    path: str


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/create_document", response_model=OfficeResponse)
async def create_document(req: CreateDocumentRequest) -> OfficeResponse:
    """Create a styled Word document (.docx)."""
    try:
        from docx import Document
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor

        dest = _resolve_workspace_path(req.path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        doc = Document()

        # Set standard margins (1 inch)
        for section in doc.sections:
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin = Inches(1)
            section.right_margin = Inches(1)

        # Title
        title_para = doc.add_heading(req.title, level=0)
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Subtitle
        if req.subtitle:
            sub_para = doc.add_paragraph()
            sub_run = sub_para.add_run(req.subtitle)
            sub_run.font.size = Pt(13)
            sub_run.font.italic = True
            sub_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Author / Date metadata
        if req.author:
            auth_para = doc.add_paragraph()
            auth_run = auth_para.add_run(f"Author: {req.author}")
            auth_run.font.size = Pt(10)
            auth_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph()  # Spacing

        total_paragraphs = 0
        total_tables = 0

        # Build sections
        for sec in req.sections:
            if sec.heading:
                doc.add_heading(sec.heading, level=min(max(sec.level, 1), 3))
                total_paragraphs += 1

            if sec.content:
                p = doc.add_paragraph(sec.content)
                p.paragraph_format.space_after = Pt(6)
                p.paragraph_format.line_spacing = 1.15
                total_paragraphs += 1

            if sec.bullets:
                for bullet in sec.bullets:
                    bp = doc.add_paragraph(bullet, style="List Bullet")
                    bp.paragraph_format.space_after = Pt(3)
                    total_paragraphs += 1

            if sec.table and sec.table.headers:
                table_data = sec.table
                rows_count = len(table_data.rows) + 1  # headers + data
                cols_count = len(table_data.headers)
                t = doc.add_table(rows=rows_count, cols=cols_count)
                t.alignment = WD_TABLE_ALIGNMENT.CENTER
                t.style = "Table Grid"

                # Header row styling
                hdr_cells = t.rows[0].cells
                for i, h_text in enumerate(table_data.headers):
                    hdr_cells[i].text = str(h_text)
                    for r in hdr_cells[i].paragraphs[0].runs:
                        r.font.bold = True
                        r.font.size = Pt(10)

                # Data rows
                for r_idx, row in enumerate(table_data.rows):
                    row_cells = t.rows[r_idx + 1].cells
                    for c_idx, cell_val in enumerate(row[:cols_count]):
                        row_cells[c_idx].text = str(cell_val)
                        for r in row_cells[c_idx].paragraphs[0].runs:
                            r.font.size = Pt(9.5)

                doc.add_paragraph()  # Spacing after table
                total_tables += 1

        # Header / Footer with page numbers
        if req.include_page_numbers:
            for s in doc.sections:
                footer = s.footer
                f_p = footer.paragraphs[0]
                f_p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                f_run = f_p.add_run("Page ")
                f_run.font.size = Pt(9)
                page_elem = OxmlElement("w:fldSimple")
                page_elem.set(qn("w:instr"), "PAGE")
                f_p._p.append(page_elem)

        doc.save(str(dest))
        file_size = dest.stat().st_size

        return OfficeResponse(
            success=True,
            output=(
                f"Successfully created Word document at {dest.name} "
                f"({file_size:,} bytes, {len(req.sections)} sections, "
                f"{total_tables} table(s))."
            ),
            artifacts=[str(dest)],
            metadata={
                "path": str(dest),
                "file_size": file_size,
                "sections_count": len(req.sections),
                "tables_count": total_tables,
            },
        )
    except Exception as exc:
        logger.exception("Failed to create document")
        return OfficeResponse(success=False, error=f"Document creation error: {exc}")


@router.post("/create_spreadsheet", response_model=OfficeResponse)
async def create_spreadsheet(req: CreateSpreadsheetRequest) -> OfficeResponse:
    """Create a structured Excel workbook (.xlsx)."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        dest = _resolve_workspace_path(req.path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        # Remove default sheet
        wb.remove(wb.active)

        if not req.sheets:
            req.sheets.append(SpreadsheetSheet(name="Sheet1"))

        # Styling constants
        hdr_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        hdr_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        thin_border = Border(
            left=Side(style="thin", color="D9D9D9"),
            right=Side(style="thin", color="D9D9D9"),
            top=Side(style="thin", color="D9D9D9"),
            bottom=Side(style="thin", color="D9D9D9"),
        )

        for sheet_cfg in req.sheets:
            ws = wb.create_sheet(title=sheet_cfg.name[:31])

            # Write headers
            if sheet_cfg.headers:
                ws.append(sheet_cfg.headers)
                for col_num in range(1, len(sheet_cfg.headers) + 1):
                    cell = ws.cell(row=1, column=col_num)
                    cell.font = hdr_font
                    cell.fill = hdr_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center")

            # Write data rows
            for row in sheet_cfg.rows:
                ws.append(row)

            # Apply cell borders & zebra striping
            for row_idx, row in enumerate(ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=ws.max_column), start=2):
                fill = PatternFill(start_color="F2F5F9", end_color="F2F5F9", fill_type="solid") if row_idx % 2 == 0 else None
                for cell in row:
                    cell.border = thin_border
                    if fill:
                        cell.fill = fill
                    if isinstance(cell.value, (int, float)):
                        cell.alignment = Alignment(horizontal="right")

            # Injected formulas
            for f in sheet_cfg.formulas:
                cell = ws[f.cell]
                cell.value = f.formula
                cell.font = Font(name="Calibri", size=11, bold=True)
                # If a label was provided, put it in the cell immediately to the left if available
                if f.label:
                    col_letter = "".join(filter(str.isalpha, f.cell))
                    row_num = int("".join(filter(str.isdigit, f.cell)))
                    col_idx = openpyxl.utils.column_index_from_string(col_letter)
                    if col_idx > 1:
                        lbl_cell = ws.cell(row=row_num, column=col_idx - 1)
                        lbl_cell.value = f.label
                        lbl_cell.font = Font(name="Calibri", size=11, bold=True)

            # Auto-fit column widths
            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    val_str = str(cell.value or "")
                    if len(val_str) > max_len:
                        max_len = len(val_str)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

        wb.save(str(dest))
        file_size = dest.stat().st_size

        return OfficeResponse(
            success=True,
            output=(
                f"Successfully created Excel spreadsheet at {dest.name} "
                f"({file_size:,} bytes, {len(req.sheets)} sheet(s))."
            ),
            artifacts=[str(dest)],
            metadata={
                "path": str(dest),
                "file_size": file_size,
                "sheets_count": len(req.sheets),
            },
        )
    except Exception as exc:
        logger.exception("Failed to create spreadsheet")
        return OfficeResponse(success=False, error=f"Spreadsheet creation error: {exc}")


@router.post("/convert_to_pdf", response_model=OfficeResponse)
async def convert_to_pdf(req: ConvertToPdfRequest) -> OfficeResponse:
    """Convert an office document to PDF using headless LibreOffice."""
    try:
        src = _resolve_workspace_path(req.source_path)
        if not src.exists():
            return OfficeResponse(success=False, error=f"Source file not found: {src}")

        out_dir = _resolve_workspace_path(req.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "libreoffice",
            "--headless",
            "--convert-to", "pdf",
            str(src),
            "--outdir", str(out_dir),
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        expected_pdf = out_dir / f"{src.stem}.pdf"
        if not expected_pdf.exists():
            return OfficeResponse(
                success=False,
                error=f"Conversion failed (exit code {proc.returncode}): {proc.stderr or proc.stdout}",
            )

        file_size = expected_pdf.stat().st_size
        return OfficeResponse(
            success=True,
            output=f"Successfully converted {src.name} to {expected_pdf.name} ({file_size:,} bytes).",
            artifacts=[str(expected_pdf)],
            metadata={"path": str(expected_pdf), "file_size": file_size},
        )
    except subprocess.TimeoutExpired:
        return OfficeResponse(success=False, error="LibreOffice conversion timed out after 60s.")
    except Exception as exc:
        logger.exception("Failed to convert document to PDF")
        return OfficeResponse(success=False, error=f"PDF conversion error: {exc}")


@router.post("/inspect", response_model=OfficeResponse)
async def inspect_document(req: InspectRequest) -> OfficeResponse:
    """Inspect structure and contents of a .docx, .xlsx, or .pdf file."""
    try:
        target = _resolve_workspace_path(req.path)
        if not target.exists():
            return OfficeResponse(success=False, error=f"File not found: {target}")

        ext = target.suffix.lower()
        file_size = target.stat().st_size
        details: dict[str, Any] = {"path": str(target), "file_size": file_size, "extension": ext}

        if ext == ".docx":
            from docx import Document
            doc = Document(str(target))
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            headings = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading")]
            tables_info = []
            for idx, tbl in enumerate(doc.tables):
                headers = [cell.text.strip() for cell in tbl.rows[0].cells] if tbl.rows else []
                tables_info.append({"index": idx, "rows": len(tbl.rows), "cols": len(tbl.columns), "headers": headers})

            word_count = sum(len(p.split()) for p in paragraphs)
            details.update({
                "paragraphs_count": len(paragraphs),
                "headings": headings[:15],
                "tables_count": len(doc.tables),
                "tables": tables_info,
                "word_count": word_count,
            })
            summary = (
                f"Word Document: {target.name}\n"
                f"- Size: {file_size:,} bytes\n"
                f"- Paragraphs: {len(paragraphs)}, Words: {word_count}\n"
                f"- Headings: {', '.join(headings[:5]) or '(none)'}\n"
                f"- Tables: {len(doc.tables)}"
            )

        elif ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(target), data_only=False)
            sheets_info = []
            for sheetname in wb.sheetnames:
                ws = wb[sheetname]
                sheets_info.append({
                    "name": sheetname,
                    "max_row": ws.max_row,
                    "max_column": ws.max_column,
                })
            details.update({"sheets_count": len(wb.sheetnames), "sheets": sheets_info})
            summary = (
                f"Excel Spreadsheet: {target.name}\n"
                f"- Size: {file_size:,} bytes\n"
                f"- Sheets ({len(wb.sheetnames)}): {', '.join(wb.sheetnames)}"
            )

        elif ext == ".pdf":
            import PyPDF2
            with open(target, "rb") as fh:
                reader = PyPDF2.PdfReader(fh)
                num_pages = len(reader.pages)
                sample_text = reader.pages[0].extract_text()[:300] if num_pages > 0 else ""
            details.update({"num_pages": num_pages, "sample_text": sample_text})
            summary = (
                f"PDF Document: {target.name}\n"
                f"- Size: {file_size:,} bytes\n"
                f"- Pages: {num_pages}"
            )
        else:
            return OfficeResponse(success=False, error=f"Unsupported office format: {ext}")

        return OfficeResponse(
            success=True,
            output=summary,
            artifacts=[str(target)],
            metadata=details,
        )
    except Exception as exc:
        logger.exception("Failed to inspect file")
        return OfficeResponse(success=False, error=f"File inspection error: {exc}")
