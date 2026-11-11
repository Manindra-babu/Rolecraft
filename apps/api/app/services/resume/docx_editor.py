import copy
import shutil
import subprocess
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt, RGBColor

from app.core.logging import logger
from app.schemas.resume import ParsedResumeStructure
from app.services.resume.section_parser import detect_section_heading


def _iter_all_paragraphs(doc: Document):
    """Yield unique Paragraph objects across body and table cells."""
    seen_p: set[int] = set()
    for p in doc.paragraphs:
        pid = id(p._p)
        if pid not in seen_p:
            seen_p.add(pid)
            yield p
    seen_cells: set[int] = set()
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                cid = id(cell._tc)
                if cid in seen_cells:
                    continue
                seen_cells.add(cid)
                for p in cell.paragraphs:
                    pid = id(p._p)
                    if pid not in seen_p:
                        seen_p.add(pid)
                        yield p


def _hex_to_rgb(hex_color: str | None, default: tuple[int, int, int] = (30, 41, 59)) -> RGBColor:
    if hex_color and hex_color.startswith("#") and len(hex_color) == 7:
        try:
            return RGBColor(
                int(hex_color[1:3], 16),
                int(hex_color[3:5], 16),
                int(hex_color[5:7], 16),
            )
        except ValueError:
            pass
    return RGBColor(*default)


def _add_hyperlink(
    paragraph,
    url: str,
    text: str,
    *,
    font_name: str = "Calibri",
    font_size_pt: float = 10.0,
    color: RGBColor | None = None,
) -> None:
    """Append a clickable OOXML w:hyperlink run to a paragraph."""
    part = paragraph.part
    r_id = part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rPr.append(rFonts)

    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(round(font_size_pt * 2))))
    rPr.append(sz)

    rgb = color or RGBColor(15, 92, 110)
    c_el = OxmlElement("w:color")
    c_el.set(qn("w:val"), str(rgb))
    rPr.append(c_el)

    u_el = OxmlElement("w:u")
    u_el.set(qn("w:val"), "single")
    rPr.append(u_el)

    new_run.append(rPr)
    t_el = OxmlElement("w:t")
    t_el.set(qn("xml:space"), "preserve")
    t_el.text = text
    new_run.append(t_el)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _add_bottom_border(paragraph, hex_color: str = "CBD5E1") -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), hex_color.lstrip("#")[:6] or "CBD5E1")
    pBdr.append(bottom)
    pPr.append(pBdr)


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
            # Fallback match by original_text across body AND table cells
            orig = (ch.get("original_text") or "").strip()
            if orig:
                para = next((p for p in _iter_all_paragraphs(doc) if orig in p.text), None)
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
        # Locate existing section headings and sample bullet paragraph across body AND table cells
        heading_paras: dict[str, Any] = {}
        sample_bullet_para = None
        sample_heading_para = None
        all_paras = list(_iter_all_paragraphs(doc))
        for p in all_paras:
            sec = detect_section_heading(p.text)
            if sec and sec not in heading_paras:
                heading_paras[sec] = p
                if sample_heading_para is None:
                    sample_heading_para = p
            if sample_bullet_para is None and (
                p.text.lstrip().startswith(("•", "-", "–", "\uf0b7"))
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
                if anchor is None and all_paras:
                    tail = all_paras[-1]
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


def _clean_doc_text(text: str) -> str:
    return (
        (text or "")
        .replace("\uf0b7", "")
        .replace("\u25af", "")
        .replace("\ufffd", "-")
        .strip()
    )


def _format_docx_run(run, *, font_name: str, size_pt: float, color: RGBColor, bold: bool = False, italic: bool = False) -> None:
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.font.color.rgb = color
    run.bold = bold
    run.italic = italic


def build_ats_docx_from_structure(
    *,
    structure: ParsedResumeStructure,
    approved_changes: list[dict[str, Any]],
    output_docx_path: Path,
    style_tokens: dict[str, Any] | None = None,
) -> None:
    """
    Generate a styled, clean DOCX preserving the candidate's parsed style tokens,
    all sections (including Summary, Certifications, and hyperlinks), and approved changes.
    """
    tokens = style_tokens or {}
    body_font = str(tokens.get("body_font") or "Calibri").split("+")[-1].split("-")[0] or "Calibri"
    if "gill" in body_font.lower():
        body_font = "Gill Sans MT"
    heading_font = str(tokens.get("heading_font") or body_font).split("+")[-1].split("-")[0] or body_font
    if "gill" in heading_font.lower():
        heading_font = "Gill Sans MT"

    body_size = float(tokens.get("body_size") or 9.8)
    body_size = max(8.5, min(11.0, body_size))
    name_size = float(tokens.get("name_size") or 16.0)
    heading_size = float(tokens.get("heading_size") or 10.5)

    body_color = _hex_to_rgb(tokens.get("body_color"), (30, 41, 59))
    heading_color = _hex_to_rgb(tokens.get("heading_color"), (15, 92, 110))
    accent_color = _hex_to_rgb(tokens.get("accent_color") or tokens.get("link_color"), (15, 92, 110))
    rule_hex = str(tokens.get("rule_color") or "CBD5E1")

    doc = Document()
    for sec in doc.sections:
        sec.top_margin = Inches(0.45)
        sec.bottom_margin = Inches(0.45)
        sec.left_margin = Inches(0.55)
        sec.right_margin = Inches(0.55)

    normal_style = doc.styles["Normal"]
    normal_style.font.name = body_font
    normal_style.font.size = Pt(body_size)
    normal_style.font.color.rgb = body_color
    normal_style.paragraph_format.space_before = Pt(0)
    normal_style.paragraph_format.space_after = Pt(2)
    normal_style.paragraph_format.line_spacing = 1.08

    # Header: Name
    h = doc.add_paragraph()
    h.paragraph_format.space_after = Pt(1)
    r = h.add_run(_clean_doc_text(structure.full_name or "Candidate Resume"))
    _format_docx_run(r, font_name=heading_font, size_pt=name_size, color=heading_color, bold=True)

    # Deduplicated contact row + clickable hyperlinks
    contact_para = doc.add_paragraph()
    contact_para.paragraph_format.space_after = Pt(4)
    seen_pieces: set[str] = set()
    wrote_contact = False

    def _append_sep():
        nonlocal wrote_contact
        if wrote_contact:
            sr = contact_para.add_run("  |  ")
            _format_docx_run(sr, font_name=body_font, size_pt=body_size - 0.5, color=body_color)
        wrote_contact = True

    for piece in (structure.location, structure.phone, structure.email):
        cleaned = _clean_doc_text(piece or "")
        if cleaned and cleaned.lower() not in seen_pieces:
            seen_pieces.add(cleaned.lower())
            _append_sep()
            if "@" in cleaned and " " not in cleaned:
                _add_hyperlink(
                    contact_para,
                    f"mailto:{cleaned}",
                    cleaned,
                    font_name=body_font,
                    font_size_pt=body_size - 0.5,
                    color=accent_color,
                )
            else:
                cr = contact_para.add_run(cleaned)
                _format_docx_run(cr, font_name=body_font, size_pt=body_size - 0.5, color=body_color)

    for lnk in structure.links or []:
        url = (lnk.get("url", "") if isinstance(lnk, dict) else getattr(lnk, "url", "") or "").strip()
        if not url or url.lower() in seen_pieces:
            continue
        seen_pieces.add(url.lower())
        label = (lnk.get("label", "") if isinstance(lnk, dict) else getattr(lnk, "label", "") or "").strip()
        if not label or "://" in label:
            low = url.lower()
            if "linkedin.com" in low:
                label = "LinkedIn"
            elif "github.com" in low:
                label = "GitHub"
            elif "credly.com" in low:
                label = "Credly"
            else:
                label = url
        _append_sep()
        _add_hyperlink(
            contact_para,
            url,
            label,
            font_name=body_font,
            font_size_pt=body_size - 0.5,
            color=accent_color,
        )

    def _add_section_heading(title: str) -> None:
        hp = doc.add_paragraph()
        hp.paragraph_format.space_before = Pt(5)
        hp.paragraph_format.space_after = Pt(2)
        hp.paragraph_format.keep_with_next = True
        hr = hp.add_run(title.upper())
        _format_docx_run(hr, font_name=heading_font, size_pt=heading_size, color=heading_color, bold=True)
        _add_bottom_border(hp, rule_hex)

    reword_map = {
        (ch.get("original_text") or "").strip(): (ch.get("edited_text") or ch.get("new_text") or "").strip()
        for ch in approved_changes
        if ch.get("change_type") == "reworded"
    }
    section_override = {
        (ch.get("section") or "").strip(): (ch.get("edited_text") or ch.get("new_text") or "").strip()
        for ch in approved_changes
        if ch.get("change_type") in {"reworded", "reordered"} and ch.get("section")
    }

    summary_text = section_override.get("summary") or structure.summary
    if summary_text:
        _add_section_heading("Summary")
        sp = doc.add_paragraph(_clean_doc_text(summary_text))
        for sr in sp.runs:
            _format_docx_run(sr, font_name=body_font, size_pt=body_size, color=body_color)

    if structure.experience:
        _add_section_heading("Experience")
        for exp in structure.experience:
            header_parts = [p for p in (exp.title, exp.company, exp.location, exp.dates) if p]
            ep = doc.add_paragraph()
            ep.paragraph_format.space_after = Pt(1)
            er = ep.add_run(_clean_doc_text(" | ".join(header_parts)))
            _format_docx_run(er, font_name=body_font, size_pt=body_size, color=body_color, bold=True)
            for b in exp.bullets:
                replaced = reword_map.get(b.strip(), b)
                bp = doc.add_paragraph(_clean_doc_text(replaced), style="List Bullet")
                bp.paragraph_format.space_after = Pt(1)
                for br in bp.runs:
                    _format_docx_run(br, font_name=body_font, size_pt=body_size - 0.2, color=body_color)

    github_adds = [ch for ch in approved_changes if ch.get("change_type") == "added_from_github"]
    if structure.projects or github_adds:
        _add_section_heading("Projects")
        for proj in structure.projects:
            pp = doc.add_paragraph()
            pp.paragraph_format.space_after = Pt(1)
            pr = pp.add_run(_clean_doc_text(proj.name))
            _format_docx_run(pr, font_name=body_font, size_pt=body_size, color=body_color, bold=True)
            if proj.tech_stack:
                tr = pp.add_run(f" — {', '.join(_clean_doc_text(t).rstrip('.') for t in proj.tech_stack)}")
                _format_docx_run(tr, font_name=body_font, size_pt=body_size - 0.4, color=body_color, italic=True)
            if proj.url:
                sr = pp.add_run("  [")
                _format_docx_run(sr, font_name=body_font, size_pt=body_size - 0.5, color=body_color)
                _add_hyperlink(pp, proj.url, "GitHub", font_name=body_font, font_size_pt=body_size - 0.5, color=accent_color)
                er = pp.add_run("]")
                _format_docx_run(er, font_name=body_font, size_pt=body_size - 0.5, color=body_color)
            for b in proj.bullets:
                replaced = reword_map.get(b.strip(), b)
                bp = doc.add_paragraph(_clean_doc_text(replaced), style="List Bullet")
                bp.paragraph_format.space_after = Pt(1)
                for br in bp.runs:
                    _format_docx_run(br, font_name=body_font, size_pt=body_size - 0.2, color=body_color)
        for ch in github_adds:
            text = _clean_doc_text(ch.get("edited_text") or ch.get("new_text") or "")
            for idx, ln in enumerate(text.splitlines()):
                if idx == 0:
                    gp = doc.add_paragraph()
                    gr = gp.add_run(ln)
                    _format_docx_run(gr, font_name=body_font, size_pt=body_size, color=body_color, bold=True)
                else:
                    bp = doc.add_paragraph(ln.lstrip("•- ").strip(), style="List Bullet")
                    for br in bp.runs:
                        _format_docx_run(br, font_name=body_font, size_pt=body_size - 0.2, color=body_color)

    if structure.education:
        _add_section_heading("Education")
        for edu in structure.education:
            header_parts = [p for p in (edu.institution, edu.degree, edu.dates, *edu.details) if p]
            edp = doc.add_paragraph(_clean_doc_text(" | ".join(header_parts)))
            for edr in edp.runs:
                _format_docx_run(edr, font_name=body_font, size_pt=body_size, color=body_color)

    skill_reorders = [
        ch
        for ch in approved_changes
        if ch.get("change_type") == "reordered" and ch.get("section") == "skills"
    ]
    reordered_by_cat: dict[str, str] = {}
    reordered_flat: str | None = None
    for ch in skill_reorders:
        nt = _clean_doc_text(ch.get("edited_text") or ch.get("new_text") or "")
        if ":" in nt:
            cat_k, rest = nt.split(":", 1)
            reordered_by_cat[cat_k.strip().lower()] = rest.strip()
        elif nt:
            reordered_flat = nt

    if structure.skill_categories:
        _add_section_heading("Skills")
        for cat_label, items in structure.skill_categories.items():
            line_val = reordered_by_cat.get(cat_label.strip().lower())
            if not line_val:
                cleaned_items = [_clean_doc_text(i).rstrip(".") for i in items if _clean_doc_text(i)]
                line_val = ", ".join(cleaned_items)
            if not line_val:
                continue
            sp = doc.add_paragraph()
            sp.paragraph_format.space_after = Pt(1.5)
            lbl_run = sp.add_run(f"{cat_label}: ")
            _format_docx_run(lbl_run, font_name=body_font, size_pt=body_size, color=body_color, bold=True)
            val_run = sp.add_run(line_val.rstrip("."))
            _format_docx_run(val_run, font_name=body_font, size_pt=body_size, color=body_color)
    elif reordered_flat or structure.skills:
        _add_section_heading("Skills")
        flat_txt = reordered_flat or ", ".join(_clean_doc_text(s).rstrip(".") for s in structure.skills if _clean_doc_text(s))
        sp = doc.add_paragraph(flat_txt)
        for sr in sp.runs:
            _format_docx_run(sr, font_name=body_font, size_pt=body_size, color=body_color)

    if structure.certifications:
        _add_section_heading("Certifications")
        for cert in structure.certifications:
            cp = doc.add_paragraph(style="List Bullet")
            cp.paragraph_format.space_after = Pt(1)
            cr = cp.add_run(_clean_doc_text(cert))
            _format_docx_run(cr, font_name=body_font, size_pt=body_size - 0.2, color=body_color)

    learning_adds = [ch for ch in approved_changes if ch.get("change_type") == "user_confirmed_skill"]
    if learning_adds:
        _add_section_heading("Currently Learning")
        for ch in learning_adds:
            lp = doc.add_paragraph(_clean_doc_text(ch.get("edited_text") or ch.get("new_text") or ""))
            for lr in lp.runs:
                _format_docx_run(lr, font_name=body_font, size_pt=body_size, color=body_color)

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
