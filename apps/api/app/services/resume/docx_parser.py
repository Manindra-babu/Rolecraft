import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from docx import Document

from app.schemas.resume import ParsedResumeStructure
from app.services.resume.section_parser import parse_lines_to_structure

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_is_bullet(paragraph) -> bool:
    pPr = paragraph._p.pPr
    if pPr is not None and pPr.find(f"{W_NS}numPr") is not None:
        return True
    style_name = (paragraph.style.name if paragraph.style else "") or ""
    if "bullet" in style_name.lower() or "list" in style_name.lower():
        return True
    text = paragraph.text.lstrip()
    return text.startswith(("•", "-", "–", "*"))


def _detect_docx_layout(doc: Document) -> tuple[str, str, str | None]:
    """Detect DOCX layout type and return (layout_type, layout_support, notice)."""
    xml_bytes = doc.element.xml
    has_two_cols = False
    for section in doc.sections:
        sectPr = section._sectPr
        cols = sectPr.find(f"{W_NS}cols")
        if cols is not None:
            num = cols.get(f"{W_NS}num")
            if num and num.isdigit() and int(num) >= 2:
                has_two_cols = True

    has_textbox = "txbxContent" in xml_bytes or "v:textbox" in xml_bytes
    has_tables = len(doc.tables) > 0

    if has_textbox:
        return (
            "textbox_based",
            "partial",
            "This DOCX uses floating text boxes. Text is extracted, but in-place edits work best on standard body or table paragraphs.",
        )
    if has_two_cols:
        return (
            "two_column",
            "partial",
            "Two-column section layout detected. Formatting is preserved in place; check the rendered PDF preview for column breaks.",
        )
    if has_tables:
        return (
            "table_based",
            "full",
            None,
        )
    return ("single_column", "full", None)


def _extract_textbox_texts(doc: Document) -> list[str]:
    texts: list[str] = []
    root = ET.fromstring(doc.element.xml.encode("utf-8"))
    for txbx in root.iter(f"{W_NS}txbxContent"):
        for p in txbx.iter(f"{W_NS}p"):
            parts = [t.text for t in p.iter(f"{W_NS}t") if t.text]
            joined = "".join(parts).strip()
            if joined:
                texts.append(joined)
    return texts


def parse_docx_resume(
    file_path: Path,
) -> tuple[ParsedResumeStructure, dict[str, Any], str, str, str, str | None]:
    """
    Parse a DOCX resume and build a coordinate map (element_map) for in-place editing.
    Returns:
      (parsed_structure, element_map, raw_text, layout_type, layout_support, notice)
    """
    doc = Document(str(file_path))
    layout_type, layout_support, notice = _detect_docx_layout(doc)

    elements: list[dict[str, Any]] = []
    element_index = 0
    R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

    def _extract_para_links(para) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for h in para._p.findall(f"{W_NS}hyperlink"):
            rid = h.get(f"{R_NS}id")
            if rid and rid in doc.part.rels:
                target = str(doc.part.rels[rid].target_ref or "").strip()
                if not target or target in seen:
                    continue
                seen.add(target)
                low = target.lower()
                if low.startswith("mailto:") or ("@" in target and not low.startswith("http")):
                    continue
                label = (
                    "GitHub"
                    if "github.com" in low
                    else ("LinkedIn" if "linkedin.com" in low else "Website")
                )
                links.append({"label": label, "url": target})
        return links

    def _is_para_bold(para) -> bool:
        runs = [r for r in para.runs if r.text and r.text.strip()]
        if not runs:
            return False
        bold_chars = sum(len(r.text.strip()) for r in runs if r.bold)
        total = sum(len(r.text.strip()) for r in runs)
        return (bold_chars / max(1, total)) >= 0.55

    def add_element(
        text: str,
        *,
        kind: str,
        paragraph_index: int | None = None,
        table_index: int | None = None,
        row_index: int | None = None,
        col_index: int | None = None,
        style_name: str = "",
        is_bullet: bool = False,
        is_bold: bool = False,
        links: list[dict[str, str]] | None = None,
    ) -> None:
        nonlocal element_index
        cleaned_text = re.sub(r"[\uE000-\uF8FF\ufffd\u25af]+", "", text or "")
        lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
        for line_idx, cleaned in enumerate(lines):
            el_id = f"el_{element_index}"
            element_index += 1
            elements.append(
                {
                    "id": el_id,
                    "text": cleaned,
                    "kind": kind,
                    "paragraph_index": paragraph_index,
                    "line_index": line_idx if len(lines) > 1 else None,
                    "table_index": table_index,
                    "row_index": row_index,
                    "col_index": col_index,
                    "style_name": style_name,
                    "is_bullet": is_bullet or cleaned.startswith(("•", "-", "–", "*")),
                    "is_bold": is_bold,
                    "links": links or [],
                    "section": "other",
                }
            )

    # Index lookup for body paragraphs & tables so elements keep exact python-docx coordinates
    body_para_by_xml = {id(p._p): (idx, p) for idx, p in enumerate(doc.paragraphs)}
    table_by_xml = {id(t._tbl): (idx, t) for idx, t in enumerate(doc.tables)}

    # Walk document body in true visual reading order (interleaving tables & paragraphs)
    for child in doc.element.body:
        cid = id(child)
        if cid in body_para_by_xml:
            p_idx, para = body_para_by_xml[cid]
            add_element(
                para.text,
                kind="body_paragraph",
                paragraph_index=p_idx,
                style_name=(para.style.name if para.style else "") or "",
                is_bullet=_paragraph_is_bullet(para),
                is_bold=_is_para_bold(para),
                links=_extract_para_links(para),
            )
        elif cid in table_by_xml:
            t_idx, table = table_by_xml[cid]
            for r_idx, row in enumerate(table.rows):
                seen_cells: set[int] = set()
                for c_idx, cell in enumerate(row.cells):
                    if id(cell._tc) in seen_cells:
                        continue
                    seen_cells.add(id(cell._tc))
                    for p_idx, para in enumerate(cell.paragraphs):
                        add_element(
                            para.text,
                            kind="table_cell_paragraph",
                            paragraph_index=p_idx,
                            table_index=t_idx,
                            row_index=r_idx,
                            col_index=c_idx,
                            style_name=(para.style.name if para.style else "") or "",
                            is_bullet=_paragraph_is_bullet(para),
                            is_bold=_is_para_bold(para),
                            links=_extract_para_links(para),
                        )

    # Textbox paragraphs (if present)
    if layout_type == "textbox_based":
        for tb_idx, tb_text in enumerate(_extract_textbox_texts(doc)):
            add_element(
                tb_text,
                kind="textbox_paragraph",
                paragraph_index=tb_idx,
                style_name="TextBox",
                is_bullet=tb_text.lstrip().startswith(("•", "-", "–", "*")),
            )

    parsed = parse_lines_to_structure(elements)
    raw_text = "\n".join(el["text"] for el in elements)
    element_map = {
        "layout_type": layout_type,
        "elements": {el["id"]: el for el in elements},
        "order": [el["id"] for el in elements],
    }
    return parsed, element_map, raw_text, layout_type, layout_support, notice
