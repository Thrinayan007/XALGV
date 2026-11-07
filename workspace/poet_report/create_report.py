#!/usr/bin/env python3
"""
Script to generate a professionally formatted DOCX research report on Rabindranath Tagore.
Uses python-docx to create the document with proper formatting, headers, footers, and references.
"""

from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
import re
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# Read the research.md file
with open('/workspace/poet_report/research.md', 'r') as f:
    content = f.read()

# Create a new Document
doc = Document()

# Title and subtitle
title = doc.add_heading('Rabindranath Tagore: A Research Profile', 0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

subtitle = doc.add_paragraph('A Comprehensive Academic Report')
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
subtitle.runs[0].font.size = Pt(14)
subtitle.runs[0].font.bold = False

# Access date paragraph
access_date = doc.add_paragraph()
p = access_date.add_run('Access Date: 7 November 2026')
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.font.size = Pt(10)
p.font.italic = True

doc.add_paragraph()  # Empty paragraph for spacing

# Function to convert markdown headings to docx paragraphs
def add_heading(text, level):
    """Add a heading based on level (1-3)."""
    if level == 1:
        heading = doc.add_heading(text, 0)
    elif level == 2:
        heading = doc.add_heading(text, level=1)
    elif level == 3:
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.font.size = Pt(14)
        run.font.bold = True
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        return p
    else:
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.font.size = Pt(12)
        run.font.bold = True
        return p

# Function to convert markdown paragraphs
def add_paragraph(text, style='Normal'):
    """Add a justified paragraph with consistent spacing."""
    p = doc.add_paragraph(style=style)
    if text.strip():
        p.add_run(text.strip())
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.space_after = Pt(8)
    return p

# Function to parse and format the markdown content
def parse_markdown(content):
    """Parse markdown content and convert to docx paragraphs."""
    lines = content.split('\n')
    result = []
    
    for line in lines:
        if line.startswith('# '):
            # Level 1 heading
            text = line[2:].strip()
            add_heading(text, level=1)
            result.append(('heading', text))
        elif line.startswith('## '):
            # Level 2 heading
            text = line[3:].strip()
            add_heading(text, level=2)
            result.append(('heading', text))
        elif line.startswith('### '):
            # Level 3 heading
            text = line[4:].strip()
            add_heading(text, level=3)
            result.append(('heading', text))
        elif line.startswith('---'):
            # Horizontal rule - skip or add separator
            continue
        elif line.startswith('*Note:'):
            # Note section
            note_text = line.replace('*', '').strip()
            p = doc.add_paragraph(note_text)
            p.runs[0].font.italic = True
            result.append(('note', note_text))
        elif line.strip().startswith('- '):
            # Bullet point
            text = line.strip()[2:].strip()
            p = doc.add_paragraph(style='List Bullet')
            p.text = text
            result.append(('bullet', text))
        elif line.startswith('**'):
            # Bold text (like Access Date)
            text = line.replace('**', '').strip()
            p = add_paragraph(text, style='Normal')
            p.runs[0].font.bold = True
            result.append(('bold', text))
        else:
            # Preserve inline citations in the original paragraph.
            text = line.strip()
            if text:
                add_paragraph(text, style='Normal')
                result.append(('paragraph', text))

    return result

# Parse the content
parsed_content = parse_markdown(content)


# Add a running header and automatic page numbers.
for section in doc.sections:
    header = section.header
    hp = header.paragraphs[0]
    hp.text = "RABINDRANATH TAGORE | RESEARCH PROFILE"
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in hp.runs:
        run.font.size = Pt(8)
        run.font.bold = True

    footer = section.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp.add_run("Page ")

    page_field = OxmlElement("w:fldSimple")
    page_field.set(qn("w:instr"), "PAGE")
    fp._p.append(page_field)

# Save the document
output_path = '/workspace/poet_report/Tagore_Research_Report.docx'
doc.save(output_path)

print(f"Document saved to: {output_path}")
print("Verification: DOCX file created successfully.")
