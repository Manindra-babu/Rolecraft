from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.schemas.resume import ParsedResumeStructure
from app.services.resume.section_parser import detect_section_heading, parse_lines_to_structure

PUA_AND_BAD_GLYPHS_RE = re.compile(r"[\uE000-\uF8FF\ufffd\u25af]+")

PDF_FORMAT_NOTICE = (
    "Exact-format editing requires a DOCX. "
    "We preserve your PDF theme in-place using its fonts, colors, links, and layout."
)


def _clean_glyphs(text: str) -> str:
    cleaned = PUA_AND_BAD_GLYPHS_RE.sub("", text or "")
    cleaned = cleaned.replace("\u202f", " ").replace("\xa0", " ")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _had_pua_bullet(raw_text: str) -> bool:
    stripped = (raw_text or "").lstrip()
    if not stripped:
        return False
    ch = stripped[0]
    if "\uE000" <= ch <= "\uF8FF":
        return True
    return ch in ("•", "▪", "◦", "○", "▸", "►", "●")


def _color_int_to_hex(color_int: int) -> str:
    return f"#{color_int & 0xFFFFFF:06x}"


def _rects_intersect(r1: list[float], r2: list[float], pad: float = 0.0) -> bool:
    return not (
        r1[2] + pad < r2[0]
        or r2[2] + pad < r1[0]
        or r1[3] + pad < r2[1]
        or r2[3] + pad < r1[1]
    )


def _match_links_for_bbox(
    bbox: list[float], page_links: list[dict[str, Any]]
) -> list[dict[str, str]]:
    matched: list[dict[str, str]] = []
    seen: set[str] = set()
    for lk in page_links:
        uri = (lk.get("uri") or "").strip()
        if not uri:
            continue
        r = lk.get("from")
        if r is None:
            continue
        # Shrink vertical overlap slightly so adjacent lines 1pt away never falsely catch a link
        l_rect = [float(r.x0), float(r.y0) + 1.5, float(r.x1), float(r.y1) - 1.5]
        if _rects_intersect(bbox, l_rect, pad=0.0):
            if uri in seen:
                continue
            seen.add(uri)
            low = uri.lower()
            label = "GitHub" if "github.com" in low else (
                "LinkedIn" if "linkedin.com" in low else "Website"
            )
            matched.append({"label": label, "url": uri})
    return matched


def _extract_raw_lines(page: fitz.Page, page_idx: int) -> list[dict[str, Any]]:
    page_links = page.get_links() or []
    tdict = page.get_text("dict")
    lines: list[dict[str, Any]] = []

    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            raw_joined = "".join(s.get("text", "") for s in spans)
            had_pua = _had_pua_bullet(raw_joined)
            cleaned = _clean_glyphs(raw_joined)
            bbox = [float(v) for v in line.get("bbox", (0, 0, 0, 0))]

            span_meta: list[dict[str, Any]] = []
            bold_chars = 0
            italic_chars = 0
            total_chars = 0
            weighted_size = 0.0
            color_counts: dict[str, int] = {}
            font_counts: dict[str, int] = {}

            for sp in spans:
                stxt = sp.get("text", "")
                sclean = PUA_AND_BAD_GLYPHS_RE.sub("", stxt).replace("\u202f", " ").replace("\xa0", " ")
                # Keep non-empty text spans (or record font/color of visible spans)
                n = len(sclean.strip())
                sfont = str(sp.get("font", ""))
                ssize = float(sp.get("size", 10.0))
                scol = _color_int_to_hex(int(sp.get("color", 0)))
                sflags = int(sp.get("flags", 0))
                sbbox = [float(v) for v in sp.get("bbox", bbox)]

                if n > 0:
                    total_chars += n
                    weighted_size += ssize * n
                    if "bold" in sfont.lower() or (sflags & 16):
                        bold_chars += n
                    if "italic" in sfont.lower() or (sflags & 2):
                        italic_chars += n
                    color_counts[scol] = color_counts.get(scol, 0) + n
                    font_counts[sfont] = font_counts.get(sfont, 0) + n

                if PUA_AND_BAD_GLYPHS_RE.sub("", stxt).strip() or sclean == " ":
                    span_meta.append(
                        {
                            "text": sclean,
                            "raw_text": stxt,
                            "font": sfont,
                            "size": round(ssize, 2),
                            "color": scol,
                            "is_bold": "bold" in sfont.lower() or bool(sflags & 16),
                            "is_italic": "italic" in sfont.lower() or bool(sflags & 2),
                            "bbox": sbbox,
                        }
                    )

            avg_size = (weighted_size / total_chars) if total_chars > 0 else float(spans[0].get("size", 10.0))
            dom_color = max(color_counts, key=color_counts.get) if color_counts else _color_int_to_hex(int(spans[0].get("color", 0)))
            dom_font = max(font_counts, key=font_counts.get) if font_counts else str(spans[0].get("font", "Helvetica"))
            links = _match_links_for_bbox(bbox, page_links)

            lines.append(
                {
                    "page": page_idx,
                    "bbox": bbox,
                    "text": cleaned,
                    "raw_text": raw_joined,
                    "only_bullet_glyph": had_pua and not cleaned,
                    "had_bullet": had_pua,
                    "font": dom_font,
                    "font_size": round(avg_size, 2),
                    "color": dom_color,
                    "is_bold": (bold_chars / max(1, total_chars)) >= 0.55,
                    "is_italic": (italic_chars / max(1, total_chars)) >= 0.55,
                    "spans": span_meta,
                    "links": links,
                }
            )
    return lines


def _merge_same_baseline_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge isolated bullet-glyph lines (e.g. U+F0B7 Symbol bullet emitted as its own line)
    or close same-row fragments into the text line at the same vertical band.
    Never merges widely separated left/right columns (x gap > 28pt).
    """
    if not lines:
        return []

    sorted_lines = sorted(lines, key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))
    merged: list[dict[str, Any]] = []

    for ln in sorted_lines:
        if ln["only_bullet_glyph"]:
            merged.append(ln)
            continue

        attached = False
        # Check if an immediately preceding line was an isolated bullet on same baseline
        for prev in reversed(merged[-5:] if len(merged) >= 5 else merged):
            if prev["page"] != ln["page"]:
                continue
            y_diff = abs(prev["bbox"][1] - ln["bbox"][1])
            y_mid_prev = (prev["bbox"][1] + prev["bbox"][3]) / 2
            y_mid_ln = (ln["bbox"][1] + ln["bbox"][3]) / 2
            if abs(y_mid_prev - y_mid_ln) <= 5.5 and 0 <= (ln["bbox"][0] - prev["bbox"][2]) <= 28.0:
                if prev["only_bullet_glyph"]:
                    ln["had_bullet"] = True
                    # Keep bullet bbox recorded so redaction can preserve or cover cleanly
                    ln["bullet_bbox"] = prev["bbox"]
                    merged.remove(prev)
                    merged.append(ln)
                    attached = True
                    break
        if not attached:
            merged.append(ln)

    # Drop any leftover isolated bullet-only lines with no text
    return [l for l in merged if l["text"]]


def _order_and_merge_paragraphs(lines: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """
    Segment lines into sections, handle two-column sections (reading left column
    before right column), and merge wrapped continuation lines into single paragraphs.
    """
    if not lines:
        return []

    # 1. Identify explicit section heading anchors top-to-bottom
    annotated: list[dict[str, Any]] = []
    for ln in sorted(lines, key=lambda l: (l["page"], round(l["bbox"][1], 1), l["bbox"][0])):
        sec = detect_section_heading(ln["text"])
        # Avoid mistaking short normal lines unless heading-like size/caps or standalone
        is_sec_heading = bool(sec) and (
            ln["font_size"] >= 11.5
            or ln["text"].isupper()
            or len(ln["text"].split()) <= 3
        )
        ln_copy = dict(ln)
        ln_copy["detected_section"] = sec if is_sec_heading else None
        ln_copy["is_heading"] = is_sec_heading
        annotated.append(ln_copy)

    # 2. Group into page+section bands
    bands: list[tuple[str, list[dict[str, Any]]]] = []
    current_sec = "header"
    current_band: list[dict[str, Any]] = []
    for ln in annotated:
        if ln["is_heading"]:
            if current_band:
                bands.append((current_sec, current_band))
            current_sec = str(ln["detected_section"])
            current_band = [ln]
        else:
            current_band.append(ln)
    if current_band:
        bands.append((current_sec, current_band))

    # Fix case where a right-column line (e.g. CUSTOM GPT - GITHUB) appeared at almost exact same Y
    # right BEFORE a left-column section heading (e.g. PROJECTS) due to sub-point float rounding:
    for i in range(len(bands) - 1):
        sec_name, b_lines = bands[i]
        next_sec, next_lines = bands[i + 1]
        if next_lines and b_lines:
            next_h = next_lines[0]
            moved: list[dict[str, Any]] = []
            keep: list[dict[str, Any]] = []
            for item in b_lines:
                if (
                    item["page"] == next_h["page"]
                    and abs(item["bbox"][1] - next_h["bbox"][1]) <= 6.0
                    and item["bbox"][0] > page_width * 0.45
                    and next_h["bbox"][0] < page_width * 0.35
                ):
                    moved.append(item)
                else:
                    keep.append(item)
            if moved:
                bands[i] = (sec_name, keep)
                bands[i + 1] = (next_sec, [next_h] + moved + next_lines[1:])

    mid_x = page_width * 0.48

    def _split_columns(band_lines: list[dict[str, Any]], section: str) -> list[list[dict[str, Any]]]:
        if section == "header":
            # Keep name/contact left vs contact right separate without merging horizontally
            left = [l for l in band_lines if l["bbox"][0] < mid_x]
            right = [l for l in band_lines if l["bbox"][0] >= mid_x]
            cols = []
            if left:
                cols.append(sorted(left, key=lambda l: l["bbox"][1]))
            if right:
                cols.append(sorted(right, key=lambda l: l["bbox"][1]))
            return cols

        headings = [l for l in band_lines if l["is_heading"]]
        body = [l for l in band_lines if not l["is_heading"]]
        if not body:
            return [headings] if headings else []

        left = [l for l in body if l["bbox"][2] <= mid_x + 25.0 and l["bbox"][0] < mid_x]
        right = [l for l in body if l["bbox"][0] >= mid_x - 15.0]

        # True two-column check: both left & right non-empty and their Y ranges overlap
        has_two_cols = False
        if left and right and len(left) + len(right) == len(body):
            l_y0, l_y1 = min(l["bbox"][1] for l in left), max(l["bbox"][3] for l in left)
            r_y0, r_y1 = min(r["bbox"][1] for r in right), max(r["bbox"][3] for r in right)
            overlap = min(l_y1, r_y1) - max(l_y0, r_y0)
            if overlap > 8.0:
                has_two_cols = True

        result_groups: list[list[dict[str, Any]]] = []
        if headings:
            result_groups.append(headings)
        if has_two_cols:
            result_groups.append(sorted(left, key=lambda l: (l["page"], l["bbox"][1])))
            result_groups.append(sorted(right, key=lambda l: (l["page"], l["bbox"][1])))
        else:
            result_groups.append(sorted(body, key=lambda l: (l["page"], l["bbox"][1], l["bbox"][0])))
        return result_groups

    def _can_merge_lines(prev: dict[str, Any], nxt: dict[str, Any], col_max_width: float, section: str) -> bool:
        if prev["page"] != nxt["page"]:
            return False
        if prev["is_heading"] or nxt["is_heading"]:
            return False
        if section == "header":
            # Never merge header name lines with phone/email/summary unless unheaded summary wraps
            if len(prev["text"]) < 55 or len(nxt["text"]) < 15:
                # Allow short tail of wrapped summary paragraph
                if len(prev["text"]) < 55:
                    return False
        if nxt["had_bullet"]:
            return False
        # Do not merge across bold title changes (except mixed bold-italic inside summary)
        if prev["is_bold"] != nxt["is_bold"] and section != "header":
            return False
        if prev["is_bold"] and nxt["is_bold"] and section in ("experience", "projects", "education"):
            # Wrapped 2-line degree or title
            if abs(prev["font_size"] - nxt["font_size"]) > 0.6:
                return False
        line_h = max(8.0, prev["bbox"][3] - prev["bbox"][1])
        y_gap = nxt["bbox"][1] - prev["bbox"][3]
        if y_gap > line_h * 0.55 or y_gap < -3.0:
            return False
        # Label prefix like 'Optimization:', 'Deployment:', 'Tools:' starts a new paragraph/bullet
        if re.match(r"^[A-Z][A-Za-z &/+]{1,28}:\s", nxt["text"]):
            return False
        # Previous line ended with a sentence period and next line starts a new item (action verb, ranking, or titled award)
        if prev["text"].rstrip().endswith(".") and (
            re.match(
                r"^(?:Built|Developed|Collaborated|Engineered|Implemented|Designed|Created|Managed|Led|Integrated|Optimized|Deployed|Architected|Tested|Automated|Worked|Participated|Gained|Top\s+\d+|Cleared|Awarded|Winner|Finalist)\b",
                nxt["text"],
            )
            or re.match(r"^[A-Z][A-Za-z0-9 &/+()-]{2,36}\s[-–]\s[A-Z]", nxt["text"])
        ):
            return False
        # Horizontal alignment / indentation shift (> 10pt except hanging indent)
        if abs(prev["bbox"][0] - nxt["bbox"][0]) > 14.0:
            return False
        # Short tail line on `prev` indicates previous paragraph finished
        prev_w = prev["bbox"][2] - prev["bbox"][0]
        if col_max_width > 120.0 and prev_w < col_max_width * 0.66 and prev["text"].rstrip().endswith((".", "!", "?")):
            return False
        # Date or pipe lines shouldn't absorb bullets
        if "|" in prev["text"] and re.search(r"20\d{2}|present|current", prev["text"], re.I):
            return False
        return True

    merged_elements: list[dict[str, Any]] = []
    for section, band_lines in bands:
        col_groups = _split_columns(band_lines, section)
        for group in col_groups:
            if not group:
                continue
            col_max_width = max((l["bbox"][2] - l["bbox"][0]) for l in group)
            paras: list[dict[str, Any]] = []
            for ln in group:
                if paras and _can_merge_lines(paras[-1], ln, col_max_width, section):
                    cur = paras[-1]
                    joiner = "" if cur["text"].endswith("-") and not cur["text"].endswith(" -") else " "
                    cur["text"] = f"{cur['text']}{joiner}{ln['text']}".strip()
                    cur["bbox"] = [
                        min(cur["bbox"][0], ln["bbox"][0]),
                        min(cur["bbox"][1], ln["bbox"][1]),
                        max(cur["bbox"][2], ln["bbox"][2]),
                        max(cur["bbox"][3], ln["bbox"][3]),
                    ]
                    cur["line_bboxes"].append(ln["bbox"])
                    cur["lines_count"] += 1
                    cur["spans"].extend(ln["spans"])
                    for lk in ln["links"]:
                        if lk not in cur["links"]:
                            cur["links"].append(lk)
                else:
                    p_copy = dict(ln)
                    p_copy["line_bboxes"] = [ln["bbox"]]
                    p_copy["lines_count"] = 1
                    p_copy["spans"] = list(ln["spans"])
                    p_copy["links"] = list(ln["links"])
                    p_copy["assigned_section"] = section
                    paras.append(p_copy)
            merged_elements.extend(paras)

    return merged_elements


def extract_pdf_style_tokens(doc: fitz.Document) -> dict[str, Any]:
    """Extract typography, color, rule-line, and margin tokens from a PDF for Strategy B & C."""
    name_font = "Helvetica"
    name_size = 20.0
    name_color = "#000000"
    heading_font = "Helvetica"
    heading_size = 14.0
    heading_color = "#333333"
    body_font = "Helvetica"
    body_size = 10.0
    body_color = "#404040"
    link_color = "#0563c1"
    rule_color = "#ACA8AA"
    rule_height = 2.5

    all_x0: list[float] = []
    all_x1: list[float] = []
    all_y0: list[float] = []
    all_y1: list[float] = []
    body_size_counts: dict[float, int] = {}
    body_color_counts: dict[str, int] = {}
    body_font_counts: dict[str, int] = {}
    drawings_summary: list[dict[str, Any]] = []

    for page_idx, page in enumerate(doc):
        for d in page.get_drawings() or []:
            r = d.get("rect")
            if r is None:
                continue
            w = r.x1 - r.x0
            h = r.y1 - r.y0
            fill = d.get("fill")
            color = d.get("color")
            fill_hex = (
                f"#{int(fill[0]*255):02x}{int(fill[1]*255):02x}{int(fill[2]*255):02x}"
                if fill and len(fill) >= 3
                else None
            )
            if w > 200 and h <= 6 and fill_hex:
                rule_color = fill_hex
                rule_height = max(0.8, round(h, 2))
            drawings_summary.append(
                {
                    "page": page_idx,
                    "rect": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                    "fill_hex": fill_hex,
                    "has_stroke": color is not None,
                }
            )

        tdict = page.get_text("dict")
        for block in tdict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    txt = _clean_glyphs(sp.get("text", ""))
                    if not txt:
                        continue
                    sb = sp.get("bbox", (0, 0, 0, 0))
                    all_x0.append(sb[0])
                    all_y0.append(sb[1])
                    all_x1.append(sb[2])
                    all_y1.append(sb[3])
                    sz = round(float(sp.get("size", 10.0)), 2)
                    col = _color_int_to_hex(int(sp.get("color", 0)))
                    fnt = re.sub(r"^[A-Z]{6}\+", "", str(sp.get("font", "Helvetica")))
                    if sz >= 18.0:
                        if sz > name_size or name_font == "Helvetica":
                            name_font, name_size, name_color = fnt, sz, col
                    elif detect_section_heading(txt) and sz >= 11.5:
                        heading_font, heading_size, heading_color = fnt, sz, col
                    elif col.lower() in ("#0563c1", "#1155cc", "#0000ee", "#0066cc"):
                        link_color = col
                    else:
                        n = len(txt)
                        body_size_counts[sz] = body_size_counts.get(sz, 0) + n
                        body_color_counts[col] = body_color_counts.get(col, 0) + n
                        if "symbol" not in fnt.lower():
                            body_font_counts[fnt] = body_font_counts.get(fnt, 0) + n

    if body_size_counts:
        body_size = max(body_size_counts, key=body_size_counts.get)
    if body_color_counts:
        body_color = max(body_color_counts, key=body_color_counts.get)
    if body_font_counts:
        body_font = max(body_font_counts, key=body_font_counts.get)

    page0 = doc[0]
    pw, ph = float(page0.rect.width), float(page0.rect.height)
    margins = {
        "left": round(min(all_x0), 1) if all_x0 else 46.8,
        "right": round(pw - max(all_x1), 1) if all_x1 else 46.8,
        "top": round(min(all_y0), 1) if all_y0 else 46.8,
        "bottom": round(ph - max(all_y1), 1) if all_y1 else 46.8,
    }

    return {
        "page_width": pw,
        "page_height": ph,
        "margins": margins,
        "name_font": name_font,
        "name_size": name_size,
        "name_color": name_color,
        "heading_font": heading_font,
        "heading_size": heading_size,
        "heading_color": heading_color,
        "body_font": body_font,
        "body_size": body_size,
        "body_color": body_color,
        "link_color": link_color,
        "rule_color": rule_color,
        "rule_height": rule_height,
        "drawings": drawings_summary,
    }


def parse_pdf_resume(
    file_path: Path,
) -> tuple[ParsedResumeStructure, dict[str, Any], str, int]:
    """
    Layout-aware PDF resume parser using PyMuPDF dict spans, vector drawings, and links.
    Handles multi-column sections, strips U+F0xx private-use bullets cleanly, merges
    wrapped lines into paragraphs, preserves skill categories & hyperlinks, and captures
    exact PDF style/coordinate metadata for theme-preserving export.
    """
    doc = fitz.open(str(file_path))
    page_count = len(doc)
    style_tokens = extract_pdf_style_tokens(doc)

    all_paragraphs: list[dict[str, Any]] = []
    all_page_links: list[dict[str, Any]] = []
    for pno, page in enumerate(doc):
        raw_lines = _extract_raw_lines(page, pno)
        baseline_merged = _merge_same_baseline_lines(raw_lines)
        ordered = _order_and_merge_paragraphs(baseline_merged, float(page.rect.width))
        all_paragraphs.extend(ordered)
        for lk in page.get_links() or []:
            if lk.get("uri") and lk.get("from"):
                r = lk["from"]
                all_page_links.append(
                    {
                        "page": pno,
                        "uri": lk["uri"],
                        "rect": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                    }
                )
    doc.close()

    elements: list[dict[str, Any]] = []
    for idx, para in enumerate(all_paragraphs):
        elements.append(
            {
                "id": f"pdf_{idx}",
                "text": para["text"],
                "kind": "pdf_line",
                "paragraph_index": idx,
                "page": para["page"],
                "bbox": [round(x, 2) for x in para["bbox"]],
                "line_bboxes": [[round(x, 2) for x in b] for b in para["line_bboxes"]],
                "bullet_bbox": [round(x, 2) for x in para["bullet_bbox"]] if para.get("bullet_bbox") else None,
                "font": re.sub(r"^[A-Z]{6}\+", "", para["font"]),
                "font_size": para["font_size"],
                "color": para["color"],
                "is_bold": para["is_bold"],
                "is_italic": para["is_italic"],
                "spans": para["spans"],
                "links": para["links"],
                "style_name": "",
                "is_bullet": bool(para["had_bullet"]),
                "is_heading": bool(para["is_heading"]),
                "section": "other",
            }
        )

    parsed = parse_lines_to_structure(elements)
    raw_text = "\n".join(el["text"] for el in elements)
    element_map = {
        "layout_type": "pdf_layout_aware",
        "elements": {el["id"]: el for el in elements},
        "order": [el["id"] for el in elements],
        "style_tokens": style_tokens,
        "pdf_links": all_page_links,
    }
    return parsed, element_map, raw_text, page_count
