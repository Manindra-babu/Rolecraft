from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.schemas.resume import ParsedResumeStructure
from app.services.resume.section_parser import parse_lines_to_structure

PDF_FORMAT_NOTICE = (
    "Exact-format editing requires a DOCX file. ResumeIQ has extracted your PDF "
    "for analysis and can generate a clean, ATS-friendly DOCX approximating your content."
)


def parse_pdf_resume(
    file_path: Path,
) -> tuple[ParsedResumeStructure, dict[str, Any], str, int]:
    """
    Extract text and structure from a PDF resume for analysis.
    Returns (parsed_structure, element_map, raw_text, page_count).
    """
    doc = fitz.open(str(file_path))
    page_count = len(doc)
    lines: list[str] = []
    for page in doc:
        page_text = page.get_text("text") or ""
        for line in page_text.splitlines():
            cleaned = line.strip()
            if cleaned:
                lines.append(cleaned)
    doc.close()

    elements: list[dict[str, Any]] = []
    for idx, text in enumerate(lines):
        elements.append(
            {
                "id": f"pdf_{idx}",
                "text": text,
                "kind": "pdf_line",
                "paragraph_index": idx,
                "style_name": "",
                "is_bullet": text.startswith(("•", "-", "–", "*")),
                "section": "other",
            }
        )

    parsed = parse_lines_to_structure(elements)
    raw_text = "\n".join(lines)
    element_map = {
        "layout_type": "pdf",
        "elements": {el["id"]: el for el in elements},
        "order": [el["id"] for el in elements],
    }
    return parsed, element_map, raw_text, page_count
