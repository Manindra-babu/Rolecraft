import copy
import shutil
import subprocess
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from docx import Document
from docx.oxml import OxmlElement
from docx.shared import Pt

from app.core.logging import logger
from app.schemas.resume import ParsedResumeStructure
from app.services.resume.section_parser import detect_section_heading


def _replace_paragraph_text_preserving_style(paragraph, new_text: str) -> None:
    """Replace paragraph text while preserving the primary run's font, size, color, and bold/italic."""
    if not paragraph.runs:
        paragraph.add_run(new_text)
        return
    primary_run = paragraph.runs[0]
    primary_run.text = new_text
    for extra_run in paragraph.runs[1:]:
        extra_run.text = ""


def _clone_paragraph_after(anchor_para, new_text: str, *, bold_override: bool | None = None):
    """Clone an existing paragraph's OOXML pPr and first run rPr, inserting immediately after anchor."""
    new_p = copy.deepcopy(anchor_para._p)
    # Remove all existing runs from cloned paragraph
    for child in list(new_p):
        if child.tag.endswith("}r"):
            new_p.remove(child)

    new_r = OxmlElement("w:r")
    if anchor_para.runs:
        orig_rPr = anchor_para.runs[0]._r.rPr
        if orig_rPr is not None:
            cloned_rPr = copy.deepcopy(orig_rPr)
            if bold_override is not None:
                b_el = cloned_rPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}b")
                if bold_override and b_el is None:
                    b_el = OxmlElement("w:b")
                    cloned_rPr.append(b_el)
                elif not bold_override and b_el is not None:
                    cloned_rPr.remove(b_el)
            new_r.append(cloned_rPr)

    t_el = OxmlElement("w:t")
    t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t_el.text = new_text
    new_r.append(t_el)
    new_p.append(new_r)

    anchor_para._p.addnext(new_p)
    return new_p


def _find_paragraph_by_location(doc: Document, loc: dict[str, Any]):
    kind = loc.get("kind")
    p_idx = loc.get("paragraph_index")
    if kind == "body_paragraph" and isinstance(p_idx, int) and 0 <= p_idx < len(doc.paragraphs):
        return doc.paragraphs[p_idx]
    if kind == "table_cell_paragraph":
        t_idx = loc.get("table_index")
        r_idx = loc.get("row_index")
        c_idx = loc.get("col_index")
        if (
            isinstance(t_idx, int)
            and isinstance(r_idx, int)
            and isinstance(c_idx, int)
            and isinstance(p_idx, int)
            and 0 <= t_idx < len(doc.tables)
        ):
            table = doc.tables[t_idx]
            if 0 <= r_idx < len(table.rows) and 0 <= c_idx < len(table.rows[r_idx].cells):
                cell = table.rows[r_idx].cells[c_idx]
                if 0 <= p_idx < len(cell.paragraphs):
                    return cell.paragraphs[p_idx]
    return None


def apply_changes_to_docx_in_place(
    *,
    source_docx_path: Path,
    output_docx_path: Path,
    approved_changes: list[dict[str, Any]],
) -> list[str]:
    """
    Edit the user's original DOCX file in place at paragraph and run level.
    Returns a list of any warnings/notes encountered during editing.
    """
    doc = Document(str(source_docx_path))
    warnings: list[str] = []

    # 1. Direct in-place text replacements (reworded, reordered)
    for ch in approved_changes:
        ctype = ch.get("change_type")
        if ctype not in {"reworded", "reordered"}:
            continue
        loc = ch.get("target_location") or {}
        final_text = (ch.get("edited_text") or ch.get("new_text") or "").strip()
        if not final_text:
            continue

        para = _find_paragraph_by_location(doc, loc)
        if para is None:
            # Fallback match by original_text
            orig = (ch.get("original_text") or "").strip()
            if orig:
                para = next((p for p in doc.paragraphs if orig in p.text), None)
        if para is None:
            warnings.append(f"Could not locate paragraph for {ch.get('section')} edit.")
            continue

        line_idx = loc.get("line_index")
        if isinstance(line_idx, int) and "\n" in para.text:
            lines = para.text.splitlines()
            if 0 <= line_idx < len(lines):
                prefix = "• " if lines[line_idx].lstrip().startswith("•") and not final_text.startswith("•") else ""
                lines[line_idx] = prefix + final_text
                _replace_paragraph_text_preserving_style(para, "\n".join(lines))
                continue

        # Preserve leading bullet symbol if paragraph relies on text bullet rather than numPr
        if para.text.lstrip().startswith(("• ", "- ", "– ")) and not final_text.startswith(("•", "-", "–")):
            final_text = "• " + final_text
        _replace_paragraph_text_preserving_style(para, final_text)

    # 2. Additions (added_from_github, user_confirmed_skill)
    additions = [
        ch
        for ch in approved_changes
        if ch.get("change_type") in {"added_from_github", "user_confirmed_skill"}
    ]
    if additions:
        # Locate existing section headings and sample bullet paragraph to clone style
        heading_paras: dict[str, Any] = {}
        sample_bullet_para = None
        sample_heading_para = None
        for p in doc.paragraphs:
            sec = detect_section_heading(p.text)
            if sec:
                heading_paras[sec] = p
                if sample_heading_para is None:
                    sample_heading_para = p
            if sample_bullet_para is None and (
                p.text.lstrip().startswith(("•", "-", "–"))
                or (p.style and "bullet" in (p.style.name or "").lower())
            ):
                sample_bullet_para = p

        for ch in additions:
            ctype = ch.get("change_type")
            text = (ch.get("edited_text") or ch.get("new_text") or "").strip()
            if not text:
                continue

            if ctype == "added_from_github":
                anchor = heading_paras.get("projects") or (doc.paragraphs[-1] if doc.paragraphs else None)
                if anchor is None:
                    doc.add_paragraph(text)
                    continue
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if not heading_paras.get("projects") and sample_heading_para is not None:
                    new_h = _clone_paragraph_after(anchor, "Projects", bold_override=True)
                    cursor_el = new_h
                    for ln in lines:
                        p = doc.add_paragraph(ln.lstrip("• ").strip())
                        cursor_el.addnext(p._p)
                        cursor_el = p._p
                else:
                    cursor = anchor
                    for idx, ln in enumerate(lines):
                        is_bullet_line = ln.startswith(("•", "-", "–")) or idx > 0
                        clean_ln = ln.lstrip("•-– ").strip()
                        if is_bullet_line and sample_bullet_para is not None:
                            # Clone bullet styling from sample_bullet_para, insert after cursor
                            new_p = copy.deepcopy(sample_bullet_para._p)
                            for child in list(new_p):
                                if child.tag.endswith("}r"):
                                    new_p.remove(child)
                            r_el = OxmlElement("w:r")
                            if sample_bullet_para.runs and sample_bullet_para.runs[0]._r.rPr is not None:
                                r_el.append(copy.deepcopy(sample_bullet_para.runs[0]._r.rPr))
                            t_el = OxmlElement("w:t")
                            t_el.text = clean_ln
                            r_el.append(t_el)
                            new_p.append(r_el)
                            cursor._p.addnext(new_p)
                            # Wrap in Paragraph-like anchor using doc.paragraphs lookup or direct xml
                            class _XmlAnchor:
                                def __init__(self, el):
                                    self._p = el
                            cursor = _XmlAnchor(new_p)
                        else:
                            new_p_el = _clone_paragraph_after(cursor, clean_ln, bold_override=True)
                            class _XmlAnchor:
                                def __init__(self, el):
                                    self._p = el
                            cursor = _XmlAnchor(new_p_el)

            elif ctype == "user_confirmed_skill":
                anchor = heading_paras.get("skill_development")
                if anchor is None and doc.paragraphs:
                    tail = doc.paragraphs[-1]
                    if sample_heading_para is not None:
                        h_el = _clone_paragraph_after(tail, "Skill Development", bold_override=True)
                        p = doc.add_paragraph(text)
                        h_el.addnext(p._p)
                    else:
                        doc.add_paragraph("Skill Development")
                        doc.add_paragraph(text)
                elif anchor is not None:
                    _clone_paragraph_after(anchor, text, bold_override=False)

    output_docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_docx_path))
    return warnings


def build_ats_docx_from_structure(
    *,
    structure: ParsedResumeStructure,
    approved_changes: list[dict[str, Any]],
    output_docx_path: Path,
) -> None:
    """
    Generate a clean, ATS-friendly DOCX approximating the user's parsed PDF content
    plus any approved changes (Section 6 PDF fallback).
    """
    doc = Document()
    h = doc.add_paragraph()
    r = h.add_run(structure.full_name or "Candidate Resume")
    r.bold = True
    r.font.size = Pt(16)
    r.font.name = "Calibri"

    contact_parts = [p for p in [structure.email, structure.phone, structure.location] if p]
    if contact_parts:
        doc.add_paragraph(" | ".join(contact_parts))

    if structure.summary:
        doc.add_paragraph("Summary").runs[0].bold = True
        doc.add_paragraph(structure.summary)

    reword_map = {
        ch.get("original_text", ""): (ch.get("edited_text") or ch.get("new_text") or "")
        for ch in approved_changes
        if ch.get("change_type") == "reworded"
    }

    if structure.experience:
        doc.add_paragraph("Experience").runs[0].bold = True
        for exp in structure.experience:
            doc.add_paragraph(f"{exp.title} | {exp.company} | {exp.dates}".strip(" |"))
            for b in exp.bullets:
                doc.add_paragraph(reword_map.get(b, b), style="List Bullet")

    github_adds = [ch for ch in approved_changes if ch.get("change_type") == "added_from_github"]
    if structure.projects or github_adds:
        doc.add_paragraph("Projects").runs[0].bold = True
        for proj in structure.projects:
            doc.add_paragraph(proj.name)
            for b in proj.bullets:
                doc.add_paragraph(b, style="List Bullet")
        for ch in github_adds:
            text = (ch.get("edited_text") or ch.get("new_text") or "").strip()
            for idx, ln in enumerate(text.splitlines()):
                if idx == 0:
                    doc.add_paragraph(ln)
                else:
                    doc.add_paragraph(ln.lstrip("• ").strip(), style="List Bullet")

    if structure.education:
        doc.add_paragraph("Education").runs[0].bold = True
        for edu in structure.education:
            doc.add_paragraph(f"{edu.institution} | {edu.degree} | {edu.dates}".strip(" |"))

    reordered_skills = next(
        (
            ch.get("edited_text") or ch.get("new_text")
            for ch in approved_changes
            if ch.get("change_type") == "reordered" and ch.get("section") == "skills"
        ),
        None,
    )
    if reordered_skills or structure.skills:
        doc.add_paragraph("Skills").runs[0].bold = True
        doc.add_paragraph(reordered_skills or ", ".join(structure.skills))

    learning_adds = [ch for ch in approved_changes if ch.get("change_type") == "user_confirmed_skill"]
    if learning_adds:
        doc.add_paragraph("Skill Development").runs[0].bold = True
        for ch in learning_adds:
            doc.add_paragraph(ch.get("edited_text") or ch.get("new_text") or "")

    output_docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_docx_path))


def _find_soffice() -> str | None:
    for candidate in ("soffice", "libreoffice"):
        found = shutil.which(candidate)
        if found:
            return found
    for win_path in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if Path(win_path).exists():
            return win_path
    return None


def convert_docx_to_pdf_and_preview(
    docx_path: Path,
    pdf_output_path: Path,
    preview_png_path: Path,
) -> tuple[int, bool, str | None]:
    """
    Convert DOCX to PDF using headless LibreOffice when available, count pages,
    and render page 1 preview PNG via PyMuPDF.
    Returns (page_count, used_libreoffice, note).
    """
    pdf_output_path.parent.mkdir(parents=True, exist_ok=True)
    soffice = _find_soffice()
    if soffice:
        try:
            res = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(pdf_output_path.parent),
                    str(docx_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            generated_pdf = pdf_output_path.parent / f"{docx_path.stem}.pdf"
            if res.returncode == 0 and generated_pdf.exists():
                if generated_pdf != pdf_output_path:
                    shutil.move(str(generated_pdf), str(pdf_output_path))
                pdf_doc = fitz.open(str(pdf_output_path))
                page_count = len(pdf_doc)
                if page_count > 0:
                    pix = pdf_doc[0].get_pixmap(dpi=130)
                    pix.save(str(preview_png_path))
                pdf_doc.close()
                return page_count, True, None
        except Exception as exc:
            logger.warning("LibreOffice conversion failed: %s", type(exc).__name__)

    # Fallback PDF & preview rendering via PyMuPDF when LibreOffice is absent locally
    doc = Document(str(docx_path))
    lines: list[str] = []
    for p in doc.paragraphs:
        if p.text.strip():
            lines.append(p.text.strip())
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p.text.strip():
                        lines.append(p.text.strip())

    pdf = fitz.open()
    page = pdf.new_page()
    y = 50
    for line in lines:
        if y > 770:
            page = pdf.new_page()
            y = 50
        page.insert_text((45, y), line[:105], fontsize=10)
        y += 16
    page_count = len(pdf)
    pdf.save(str(pdf_output_path))
    if page_count > 0:
        pdf[0].get_pixmap(dpi=130).save(str(preview_png_path))
    pdf.close()
    return (
        page_count,
        False,
        "Rendered using PyMuPDF fallback because LibreOffice is not installed on this host (Docker includes LibreOffice).",
    )
