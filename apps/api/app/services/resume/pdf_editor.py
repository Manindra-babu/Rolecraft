from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.schemas.resume import ParsedResumeStructure
from app.services.resume.pdf_parser import extract_pdf_style_tokens
from app.services.resume.section_parser import detect_section_heading

EXPORT_CODE_VERSION = "2026-11-11-inplace-v4"

PDF_REBUILD_NOTICE = (
    "We matched your original design as closely as possible. "
    "Upload the original DOCX for exact preservation."
)


@dataclass
class PdfEditResult:
    strategy_used: str  # inplace_exact | inplace_pdf | style_clone_fallback
    warnings: list[str] = field(default_factory=list)
    font_substitutions: list[str] = field(default_factory=list)
    changed_bboxes_by_page: dict[int, list[list[float]]] = field(default_factory=dict)
    band_shifts_by_page: dict[int, list[tuple[float, float, float]]] = field(default_factory=dict)
    notice: str | None = None
    page_count_original: int = 1
    page_count_output: int = 1
    overflow_detected: bool = False
    overflow_suggestions: list[str] = field(default_factory=list)


_FONT_CANDIDATES: dict[tuple[str, bool, bool], list[str]] = {
    ("gill sans mt", False, False): [
        r"C:\Windows\Fonts\GIL_____.TTF",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    ("gill sans mt", True, False): [
        r"C:\Windows\Fonts\GILB____.TTF",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    ("gill sans mt", False, True): [
        r"C:\Windows\Fonts\GILI____.TTF",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ],
    ("gill sans mt", True, True): [
        r"C:\Windows\Fonts\GILBI___.TTF",
        "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    ],
    ("calibri", False, False): [r"C:\Windows\Fonts\calibri.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    ("calibri", True, False): [r"C:\Windows\Fonts\calibrib.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    ("calibri", False, True): [r"C:\Windows\Fonts\calibrii.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"],
    ("calibri", True, True): [r"C:\Windows\Fonts\calibriz.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"],
    ("arial", False, False): [r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    ("arial", True, False): [r"C:\Windows\Fonts\arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    ("arial", False, True): [r"C:\Windows\Fonts\ariali.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"],
    ("arial", True, True): [r"C:\Windows\Fonts\arialbi.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"],
    ("georgia", False, False): [r"C:\Windows\Fonts\georgia.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"],
    ("georgia", True, False): [r"C:\Windows\Fonts\georgiab.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"],
    ("times new roman", False, False): [r"C:\Windows\Fonts\times.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"],
    ("times new roman", True, False): [r"C:\Windows\Fonts\timesbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"],
}


def _hex_to_rgb01(hex_color: str | int | None) -> tuple[float, float, float]:
    if isinstance(hex_color, int):
        h = f"{hex_color & 0xFFFFFF:06x}"
    else:
        h = (hex_color or "#000000").lstrip("#")
    if len(h) != 6:
        return (0.0, 0.0, 0.0)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _normalize_family_and_style(raw_font_name: str) -> tuple[str, bool, bool]:
    cleaned = re.sub(r"^[A-Z]{6}\+", "", raw_font_name or "Helvetica").strip()
    low = cleaned.lower()
    is_bold = "bold" in low or low.endswith("-b")
    is_italic = "italic" in low or "oblique" in low
    family = re.split(r"[,_-]", cleaned)[0].strip().lower()
    compact = re.sub(r"[\s-]+", "", family)
    if compact in {"gillsansmt", "gillsans"}:
        family = "gill sans mt"
    elif compact.startswith("timesnewroman"):
        family = "times new roman"
    return family, is_bold, is_italic


def _resolve_pdf_font(raw_font_name: str, *, force_bold: bool | None = None, force_italic: bool | None = None) -> tuple[fitz.Font, str | None]:
    """
    Match the original PDF font family/weight/style to an installed font file when possible,
    or fall back to the closest metric family with an explicit substitution report.
    """
    family, is_bold, is_italic = _normalize_family_and_style(raw_font_name)
    if force_bold is not None:
        is_bold = force_bold
    if force_italic is not None:
        is_italic = force_italic

    # 1. Exact candidate list
    for idx, candidate in enumerate(_FONT_CANDIDATES.get((family, is_bold, is_italic), [])):
        if os.path.exists(candidate):
            fnt = fitz.Font(fontfile=candidate)
            if idx == 0:
                return fnt, None
            sub_msg = (
                f"Font substitution: '{family}' ({'Bold' if is_bold else 'Regular'}) "
                f"matched to closest installed '{Path(candidate).stem}'."
            )
            return fnt, sub_msg

    # 2. Generic sans/serif system font fallback
    is_serif = any(k in family for k in ("times", "georgia", "garamond", "cambria", "serif"))
    is_mono = any(k in family for k in ("courier", "consola", "mono"))
    generic_paths: list[str] = []
    if is_mono:
        generic_paths = [
            r"C:\Windows\Fonts\cour.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
    elif is_serif:
        generic_paths = [
            r"C:\Windows\Fonts\georgiab.ttf" if is_bold else r"C:\Windows\Fonts\georgia.ttf",
            r"C:\Windows\Fonts\timesbd.ttf" if is_bold else r"C:\Windows\Fonts\times.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"
            if is_bold
            else "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        ]
    else:
        generic_paths = [
            r"C:\Windows\Fonts\calibrib.ttf" if is_bold else r"C:\Windows\Fonts\calibri.ttf",
            r"C:\Windows\Fonts\arialbd.ttf" if is_bold else r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            if is_bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if is_bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

    for gp in generic_paths:
        if os.path.exists(gp):
            fnt = fitz.Font(fontfile=gp)
            return fnt, f"Font substitution: '{family}' matched to closest installed '{Path(gp).stem}'."

    # 3. Last resort PyMuPDF Base14
    base_name = (
        ("cobo" if is_bold else "cour")
        if is_mono
        else (
            ("tibo" if is_bold else "tiro")
            if is_serif
            else ("hebi" if (is_bold and is_italic) else ("hebo" if is_bold else ("heit" if is_italic else "helv")))
        )
    )
    return fitz.Font(base_name), f"Font substitution: embedded subset '{raw_font_name}' rendered with Base14 '{base_name}'."


def _wrap_text_to_width(text: str, font: fitz.Font, font_size: float, max_width: float) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        candidate = f"{cur} {w}"
        if font.text_length(candidate, fontsize=font_size) <= max_width:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _wrap_rich_segments_to_width(
    segments: list[tuple[str, fitz.Font, tuple[float, float, float], float]],
    max_width: float,
) -> list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]]:
    """
    Wrap mixed-style inline segments (e.g. Bold category label + Regular skills list) across lines.
    Each segment is (text, font, color_rgb, size).
    """
    tokens: list[tuple[str, fitz.Font, tuple[float, float, float], float]] = []
    for txt, fnt, col, sz in segments:
        # Split preserving spaces attached to preceding or separated cleanly
        parts = re.findall(r"\S+\s*|\s+", txt)
        for p in parts:
            if p:
                tokens.append((p, fnt, col, sz))

    lines: list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]] = [[]]
    cur_x = 0.0
    for tok_txt, fnt, col, sz in tokens:
        w = fnt.text_length(tok_txt, fontsize=sz)
        trimmed_w = fnt.text_length(tok_txt.rstrip(), fontsize=sz)
        if lines[-1] and cur_x + trimmed_w > max_width:
            lines.append([(tok_txt.lstrip(), fnt, col, sz)])
            cur_x = fnt.text_length(tok_txt.lstrip(), fontsize=sz)
        else:
            lines[-1].append((tok_txt, fnt, col, sz))
            cur_x += w
    return [ln for ln in lines if ln]


def _draw_rich_lines(
    page: fitz.Page,
    x0: float,
    y0: float,
    wrapped_lines: list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]],
    line_height: float,
    first_baseline_y: float | None = None,
) -> float:
    """Draw wrapped mixed-style lines onto `page` using fitz.TextWriter and return final bottom Y."""
    max_sz = max(sz for ln in wrapped_lines for _, _, _, sz in ln)
    # Match exact original baseline when available, else approximate from bbox top
    baseline_y = first_baseline_y if first_baseline_y is not None else (y0 + max_sz * 0.78)
    last_y = y0
    for ln in wrapped_lines:
        cur_x = x0
        for txt, fnt, col, sz in ln:
            tw = fitz.TextWriter(page.rect)
            tw.append((cur_x, baseline_y), txt, font=fnt, fontsize=sz)
            tw.write_text(page, color=col)
            cur_x += fnt.text_length(txt, fontsize=sz)
        last_y = baseline_y + max_sz * 0.25
        baseline_y += line_height
    return last_y


def _strip_bullet_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading Symbol/PUA bullet spans so redactions preserve original bullet glyphs."""
    kept: list[dict[str, Any]] = []
    for sp in spans:
        txt = sp.get("text", "")
        fnt = str(sp.get("font", "")).lower()
        clean = re.sub(r"[\uE000-\uF8FF•▪◦·\s]+", "", txt)
        if not clean and ("symbol" in fnt or "wingding" in fnt or re.search(r"[\uE000-\uF8FF•]", txt)):
            continue
        kept.append(sp)
    return kept or spans


def _extract_span_meta(spans: list[dict[str, Any]], line_bboxes: list[list[float]], style_tokens: dict[str, Any]) -> dict[str, Any]:
    non_bullet = _strip_bullet_spans(spans)
    sp0 = non_bullet[0]
    fname = str(sp0.get("font", style_tokens.get("body_font", "Helvetica")))
    flags = int(sp0.get("flags", 0))
    total_chars = sum(len(s.get("text", "").strip()) for s in non_bullet) or 1
    bold_chars = sum(
        len(s.get("text", "").strip())
        for s in non_bullet
        if "bold" in str(s.get("font", "")).lower() or (int(s.get("flags", 0)) & 16)
    )
    italic_chars = sum(
        len(s.get("text", "").strip())
        for s in non_bullet
        if "italic" in str(s.get("font", "")).lower()
        or "oblique" in str(s.get("font", "")).lower()
        or (int(sp0.get("flags", 0)) & 2)
    )
    baselines: list[float] = []
    for lb in line_bboxes:
        # Compute first span origin on each line if close to lb[1]
        pass
    first_baseline = float(sp0["origin"][1]) if isinstance(sp0.get("origin"), (list, tuple)) and len(sp0["origin"]) >= 2 else None
    return {
        "font": fname,
        "font_size": round(float(sp0.get("size", style_tokens.get("body_size", 10.0))), 2),
        "color": f"#{int(sp0.get('color', 0)) & 0xFFFFFF:06x}",
        "is_bold": bold_chars >= total_chars * 0.6,
        "is_italic": italic_chars >= total_chars * 0.5,
        "first_baseline_y": first_baseline,
        "spans": non_bullet,
        "line_bboxes": line_bboxes,
    }


def _locate_edit_box(
    page: fitz.Page,
    change: dict[str, Any],
    style_tokens: dict[str, Any],
) -> tuple[list[float], dict[str, Any]]:
    """Resolve the exact bounding box (excluding leading bullet glyphs) and style metadata on `page`."""
    loc = change.get("target_location") or {}
    tdict = page.get_text("dict")
    orig = (change.get("original_text") or "").strip()

    if isinstance(loc, dict) and loc.get("bbox"):
        lb = [float(v) for v in loc["bbox"]]
        overlapping_spans: list[dict[str, Any]] = []
        overlapping_lboxes: list[list[float]] = []
        for block in tdict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for ln in block.get("lines", []):
                cleaned_sp = _strip_bullet_spans(ln["spans"])
                non_b_boxes = [s["bbox"] for s in cleaned_sp if s.get("bbox")]
                if not non_b_boxes:
                    continue
                lbx = [
                    min(b[0] for b in non_b_boxes),
                    min(b[1] for b in non_b_boxes),
                    max(b[2] for b in non_b_boxes),
                    max(b[3] for b in non_b_boxes),
                ]
                cy = (lbx[1] + lbx[3]) * 0.5
                h_ov = min(lbx[2], lb[2]) - max(lbx[0], lb[0])
                min_w = max(1.0, min(lbx[2] - lbx[0], lb[2] - lb[0]))
                # Strict vertical-center containment + column horizontal isolation
                if (
                    (lb[1] - 0.5) <= cy <= (lb[3] + 0.5)
                    and h_ov >= min_w * 0.35
                    and not (lb[0] > 180.0 and lbx[0] < lb[0] - 12.0)
                    and not (lb[2] < 320.0 and lbx[0] > lb[2] + 4.0)
                ):
                    overlapping_spans.extend(cleaned_sp)
                    overlapping_lboxes.append(lbx)
        if overlapping_spans and overlapping_lboxes:
            rect = [
                min(b[0] for b in overlapping_lboxes),
                min(b[1] for b in overlapping_lboxes),
                max(b[2] for b in overlapping_lboxes),
                max(b[3] for b in overlapping_lboxes),
            ]
            meta = dict(loc)
            span_meta = _extract_span_meta(overlapping_spans, overlapping_lboxes, style_tokens)
            # Only preserve explicit loc is_bold/is_italic if true or spans didn't override
            span_meta.setdefault("is_bold", bool(loc.get("is_bold")))
            span_meta.setdefault("is_italic", bool(loc.get("is_italic")))
            meta.update(span_meta)
            return rect, meta
        return lb, loc

    if not orig:
        return [], {}

    # Search page dict lines for matching words
    needle = re.sub(r"\s+", " ", orig.lower())[:36]
    matched_lines: list[dict[str, Any]] = []
    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            ltxt = re.sub(r"[\uE000-\uF8FF]+", "", "".join(s["text"] for s in ln["spans"])).strip()
            if needle[:18] and needle[:18] in ltxt.lower():
                matched_lines.append(ln)
    if matched_lines:
        # Handle single-word / partial-span substring edits (e.g., fixing "RETRIVAL" -> "RETRIEVAL")
        # without erasing adjacent words or links on the same line.
        if len(matched_lines) == 1 and orig:
            ln0 = matched_lines[0]
            ltxt0 = re.sub(r"[\uE000-\uF8FF]+", "", "".join(s["text"] for s in ln0["spans"])).strip()
            if len(orig) < len(ltxt0) * 0.72:
                hits = [
                    r for r in (page.search_for(orig) or [])
                    if (ln0["bbox"][1] - 2.0) <= ((r.y0 + r.y1) * 0.5) <= (ln0["bbox"][3] + 2.0)
                ]
                if hits:
                    hit = hits[0]
                    hit_cx = (hit.x0 + hit.x1) * 0.5
                    clean_ln_spans = _strip_bullet_spans(ln0["spans"])
                    chosen_sp = None
                    for sp in clean_ln_spans:
                        sb = sp.get("bbox") or [0, 0, 0, 0]
                        if sb[0] - 1.0 <= hit_cx <= sb[2] + 1.0:
                            chosen_sp = sp
                            break
                    chosen_sp = chosen_sp or (clean_ln_spans[0] if clean_ln_spans else ln0["spans"][0])
                    rect = [float(hit.x0), float(hit.y0), float(hit.x1), float(hit.y1)]
                    meta = _extract_span_meta([chosen_sp], [rect], style_tokens)
                    # Determine exact horizontal clearance before the next word on the same line
                    line_words = sorted(
                        (w for w in (page.get_text("words") or []) if (ln0["bbox"][1] - 2.0) <= ((w[1] + w[3]) * 0.5) <= (ln0["bbox"][3] + 2.0)),
                        key=lambda w: w[0],
                    )
                    next_word_x0 = None
                    for w in line_words:
                        if float(w[0]) >= hit.x1 - 0.5 and orig.lower() not in str(w[4]).lower():
                            next_word_x0 = float(w[0])
                            break
                    if next_word_x0 is not None:
                        meta["_inline_max_x1"] = next_word_x0 - 1.5
                    return rect, meta

        clean_spans = _strip_bullet_spans([s for l in matched_lines for s in l["spans"]])
        line_bboxes: list[list[float]] = []
        for l in matched_lines:
            csp = _strip_bullet_spans(l["spans"])
            bxs = [s["bbox"] for s in csp if s.get("bbox")]
            line_bboxes.append(
                [min(b[0] for b in bxs), min(b[1] for b in bxs), max(b[2] for b in bxs), max(b[3] for b in bxs)]
                if bxs else list(l["bbox"])
            )
        rect = [
            min(b[0] for b in line_bboxes),
            min(b[1] for b in line_bboxes),
            max(b[2] for b in line_bboxes),
            max(b[3] for b in line_bboxes),
        ]
        return rect, _extract_span_meta(clean_spans, line_bboxes, style_tokens)
    return [], {}


def _detect_horizontal_rules(page: fitz.Page, page_width: float) -> list[tuple[float, float]]:
    """Return sorted (y0, y1) spans for full/half-width horizontal divider rules on `page`."""
    raw_rules: list[tuple[float, float]] = []
    for d in page.get_drawings() or []:
        r = d.get("rect")
        if not r:
            continue
        if (r.x1 - r.x0) >= page_width * 0.42 and (r.y1 - r.y0) <= 6.5:
            raw_rules.append((float(r.y0), float(r.y1)))
    raw_rules.sort()
    merged: list[tuple[float, float]] = []
    for ry0, ry1 in raw_rules:
        if merged and abs(ry0 - merged[-1][0]) <= 2.5:
            merged[-1] = (min(merged[-1][0], ry0), max(merged[-1][1], ry1))
        else:
            merged.append((ry0, ry1))
    return merged


def _detect_projects_grid_style(
    page: fitz.Page,
    proj_y0: float,
    proj_limit_y: float,
    pw: float,
    margins: dict[str, Any],
    style_tokens: dict[str, Any],
) -> dict[str, Any]:
    """Inspect existing PROJECTS spans & layout (columns, fonts, link style, bottom Y)."""
    tdict = page.get_text("dict")
    mid_x = pw * 0.48
    left_x = float(margins.get("left", 46.8))
    right_x = pw - float(margins.get("right", 46.8))

    proj_lines: list[dict[str, Any]] = []
    for blk in tdict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            bx = ln["bbox"]
            if bx[1] >= proj_y0 and bx[3] <= proj_limit_y:
                txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if txt and detect_section_heading(txt) != "projects":
                    proj_lines.append(ln)

    has_left = any(ln["bbox"][0] < mid_x - 12.0 for ln in proj_lines)
    has_right = any(ln["bbox"][0] >= mid_x - 12.0 for ln in proj_lines)
    is_two_col = has_left and has_right

    col1_x0 = min((ln["bbox"][0] for ln in proj_lines if ln["bbox"][0] < mid_x - 12.0), default=left_x)
    col2_x0 = min((ln["bbox"][0] for ln in proj_lines if ln["bbox"][0] >= mid_x - 12.0), default=pw * 0.505)
    col1_w = (col2_x0 - 10.0) - col1_x0 if is_two_col else (right_x - col1_x0)
    col2_w = (right_x - col2_x0) if is_two_col else (right_x - col1_x0)

    # Extract exact title/body/link font specs from Projects spans
    title_span: dict[str, Any] | None = None
    body_span: dict[str, Any] | None = None
    link_span: dict[str, Any] | None = None
    had_bullets = False
    proj_content_y1 = proj_y0

    for ln in proj_lines:
        proj_content_y1 = max(proj_content_y1, float(ln["bbox"][3]))
        ln_txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        if ln_txt.startswith(("•", "-", "\uf0b7")):
            had_bullets = True
        for sp in ln.get("spans", []):
            stxt = (sp.get("text") or "").strip()
            if not stxt:
                continue
            fname = str(sp.get("font", ""))
            col_hex = f"#{int(sp.get('color', 0)) & 0xFFFFFF:06x}".lower()
            is_bold = "bold" in fname.lower() or bool(int(sp.get("flags", 0)) & 16)
            if stxt.upper() == "GITHUB" or col_hex in {"#0563c1", "#1155cc", "#0000ee"}:
                if link_span is None:
                    link_span = sp
            elif is_bold and title_span is None and ":" not in stxt[:16]:
                title_span = sp
            elif not is_bold and body_span is None and len(stxt) > 12:
                body_span = sp

    title_font_name = str((title_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    title_size = round(float((title_span or {}).get("size") or style_tokens.get("body_size", 10.0)), 2)
    title_color = f"#{int((title_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if title_span else str(style_tokens.get("body_color", "#404040"))

    body_font_name = str((body_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    body_size = round(float((body_span or {}).get("size") or style_tokens.get("body_size", 10.0)), 2)
    body_color = f"#{int((body_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if body_span else str(style_tokens.get("body_color", "#404040"))

    link_font_name = str((link_span or {}).get("font") or body_font_name)
    link_size = round(float((link_span or {}).get("size") or max(7.5, title_size * 0.78)), 2)
    link_color = f"#{int((link_span or {}).get('color', 0x0563C1)) & 0xFFFFFF:06x}" if link_span else "#0563c1"

    return {
        "is_two_col": is_two_col,
        "col1_x0": col1_x0,
        "col1_w": max(150.0, col1_w),
        "col2_x0": col2_x0,
        "col2_w": max(150.0, col2_w),
        "proj_content_y1": proj_content_y1,
        "had_bullets": had_bullets,
        "title_font_name": title_font_name,
        "title_size": title_size,
        "title_color": title_color,
        "body_font_name": body_font_name,
        "body_size": body_size,
        "body_color": body_color,
        "link_font_name": link_font_name,
        "link_size": link_size,
        "link_color": link_color,
    }


def _parse_github_card_data(ch: dict[str, Any], keep_bullets: bool = False) -> dict[str, str]:
    loc = ch.get("target_location") or {}
    raw_txt = (ch.get("edited_text") or ch.get("new_text") or "").strip()
    lines = [l.strip() for l in raw_txt.splitlines() if l.strip()]

    title = str(loc.get("title") or "").strip()
    html_url = str(loc.get("html_url") or "").strip()
    body = str(loc.get("body") or "").strip()

    if lines and not ch.get("edited_text"):
        pass
    elif lines:
        # User edited text manually: re-parse clean title + body
        first = lines[0]
        url_m = re.search(r"https?://\S+", raw_txt)
        if url_m and not html_url:
            html_url = url_m.group(0).rstrip(".,)")
        title_clean = re.sub(r"https?://\S+", "", first)
        title_clean = title_clean.split("|", 1)[0].strip(" -–—|")
        title_clean = re.sub(r"\s*[-–]\s*github\s*$", "", title_clean, flags=re.I).strip()
        title = f"{title_clean.upper()} - GITHUB" if title_clean else title
        body_lines = [
            l.lstrip("•-–—* ").strip()
            for l in lines[1:]
            if l.strip() and "core module behavior" not in l.lower()
        ]
        body = " ".join(body_lines)

    if not title:
        repo_name = str(loc.get("repo_name") or (lines[0] if lines else "GitHub Project")).split("|")[0].strip()
        repo_name = re.sub(r"\s*[-–]\s*github\s*$", "", repo_name, flags=re.I).strip()
        title = f"{repo_name.upper()} - GITHUB"
    title_base = re.sub(r"\s*[-–]\s*github\s*$", "", title, flags=re.I).strip().upper()

    if not body and len(lines) > 1:
        body = " ".join(
            l.lstrip("•-–—* ").strip()
            for l in lines[1:]
            if "core module behavior" not in l.lower()
        )
    if keep_bullets and body and not body.startswith("•"):
        body = f"• {body}"
    return {"title_base": title_base, "body": body, "html_url": html_url}


def _build_overflow_options(
    resume_structure: ParsedResumeStructure,
    github_adds: list[dict[str, Any]],
) -> list[str]:
    options: list[str] = []
    # Option 1: Trim weakest bullets
    weak_candidates: list[str] = []
    for ed in resume_structure.education:
        for d in ed.details:
            if any(k in d.lower() for k in ("workshop", "presentation", "documentation", "participated", "gained")):
                weak_candidates.append(f"Education: '{d[:70]}...'")
    if not weak_candidates and resume_structure.experience:
        for b in reversed(resume_structure.experience[0].bullets):
            weak_candidates.append(f"Experience: '{b[:70]}...'")
            if len(weak_candidates) >= 2:
                break
    if weak_candidates:
        options.append(
            f"Trim weakest bullets (~24pt saved): reject or shorten {'; '.join(weak_candidates[:2])}."
        )
    else:
        options.append("Trim weakest bullets: shorten 1-2 lower-impact bullets across Experience or Education to save ~24pt.")

    # Option 2: Drop lowest-relevance project
    if len(github_adds) > 1:
        last_name = str((github_adds[-1].get("target_location") or {}).get("repo_name") or "last added GitHub project")
        options.append(
            f"Drop lowest-relevance project: reject '{last_name}' so remaining projects fit cleanly inside the 2-column Projects grid on 1 page."
        )
    elif resume_structure.projects:
        options.append(
            f"Drop lowest-relevance project: replace or remove '{resume_structure.projects[-1].name}' to stay within 1 page."
        )

    # Option 3: Tighten spacing
    options.append(
        "Tighten section spacing: compress inter-section rule margins cleanly (~4-5pt per divider) without shrinking any font below 90%."
    )
    return options


def apply_changes_to_pdf_in_place(
    *,
    source_pdf_path: Path,
    output_pdf_path: Path,
    approved_changes: list[dict[str, Any]],
    resume_structure: ParsedResumeStructure,
    tighten_spacing: bool = True,
) -> PdfEditResult:
    """
    Strategy B: True In-Place PDF editing via PyMuPDF.
      - 0 approved changes -> copy original bytes verbatim (100% pixel identical).
      - Reworded/reordered edits -> redact ONLY target text bbox (preserving vector rules, bullets & links),
        insert replacement text matching font/size/color/baseline inside bbox (floor >= 90% size).
      - Added GitHub projects -> style-clone into the existing 2-column Projects grid by translating
        untouched PDF bands downward via show_pdf_page (no full-page text rebuild!).
      - Enforces 1-page constraint without <90% font shrinkage; flags overflow with concrete options.
    """
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    src_doc = fitz.open(str(source_pdf_path))
    orig_pages = len(src_doc)
    style_tokens = extract_pdf_style_tokens(src_doc)

    active_changes = [
        ch for ch in approved_changes
        if ch.get("status") in (None, "accepted", "edited") and ch.get("validation_status") != "blocked"
    ]

    if not active_changes:
        src_doc.close()
        shutil.copy2(source_pdf_path, output_pdf_path)
        return PdfEditResult(
            strategy_used="inplace_exact",
            page_count_original=orig_pages,
            page_count_output=orig_pages,
        )

    warnings: list[str] = []
    substitutions: list[str] = []
    changed_bboxes_by_page: dict[int, list[list[float]]] = {}
    band_shifts_by_page: dict[int, list[tuple[float, float, float]]] = {}

    pw = float(style_tokens.get("page_width", 612.0))
    margins = style_tokens.get("margins", {"left": 46.8, "right": 46.8, "bottom": 45.0})
    right_margin_x = pw - float(margins.get("right", 46.8))
    mid_col_x = pw * 0.48

    planned_ops: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    overflow_needs_fallback = False

    # Build obstacle y0 index per page+column
    all_line_bboxes: list[tuple[int, list[float], str]] = []
    for pno, page in enumerate(src_doc):
        td = page.get_text("dict")
        for blk in td.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                all_line_bboxes.append((pno, [float(v) for v in ln["bbox"]], txt))

    for ch in active_changes:
        ctype = ch.get("change_type", "")
        if ctype in {"added_from_github", "user_confirmed_skill"}:
            additions.append(ch)
            continue

        new_text = (ch.get("edited_text") or ch.get("new_text") or "").strip()
        if not new_text:
            continue

        loc = ch.get("target_location") or {}
        page_idx = int(loc.get("page", 0)) if isinstance(loc, dict) else 0
        if page_idx >= orig_pages:
            page_idx = 0
        page = src_doc[page_idx]

        bbox, meta = _locate_edit_box(page, ch, style_tokens)
        if not bbox or len(bbox) != 4:
            warnings.append(f"Skipped '{ch.get('section')}' edit: could not locate exact PDF text region.")
            continue

        orig_x0, orig_y0, orig_x1, orig_y1 = bbox
        is_two_col_left = orig_x1 <= mid_col_x + 25.0 and orig_x0 >= margins.get("left", 40) - 5 and len(ch.get("original_text", "")) < 90 and ch.get("section") in {"projects", "skills"}
        is_two_col_right = orig_x0 >= mid_col_x - 15.0 and ch.get("section") in {"projects", "skills"}

        # Clamp vertical redaction inset by +0.6pt so adjacent tight-leading lines never clip!
        y_inset = 0.6 if (orig_y1 - orig_y0) > 4.0 else 0.1
        redact_rect = [orig_x0 - 0.5, orig_y0 + y_inset, orig_x1 + 0.5, orig_y1 - y_inset]
        max_line_width = right_margin_x - orig_x0
        if meta.get("_inline_max_x1"):
            max_line_width = max(12.0, float(meta["_inline_max_x1"]) - orig_x0)
        elif is_two_col_left:
            max_line_width = (mid_col_x - 6.0) - orig_x0
        elif is_two_col_right:
            max_line_width = right_margin_x - orig_x0

        # Protect adjacent GITHUB hyperlink span inside project title if replacement does not include "- GITHUB"
        if not meta.get("_inline_max_x1"):
            page_links = page.get_links() or []
            for lk in page_links:
                lr = lk.get("from")
                if lr and "github.com" in (lk.get("uri") or "").lower():
                    if (
                        lr.y0 <= orig_y1
                        and lr.y1 >= orig_y0
                        and lr.x0 > orig_x0 + 30
                        and orig_x1 >= lr.x0 - 16.0
                        and "github" not in new_text.lower()
                    ):
                        redact_rect[2] = min(redact_rect[2], lr.x0 - 1.5)
                        max_line_width = min(max_line_width, (lr.x0 - 2.0) - orig_x0)
                        new_text = new_text.rstrip(" -") + " - "

        # Compute max available bottom Y before next obstacle line/drawing in same column
        next_y0 = float(page.rect.height) - float(margins.get("bottom", 40.0))
        for p_i, ob_box, ob_txt in all_line_bboxes:
            if p_i != page_idx:
                continue
            if ob_box[1] > orig_y1 - 1.0:
                if not (ob_box[2] < orig_x0 or ob_box[0] > orig_x0 + max_line_width):
                    next_y0 = min(next_y0, ob_box[1] - 1.5)
        for d in page.get_drawings() or []:
            dr = d.get("rect")
            if dr and dr.y0 > orig_y1 + 0.5 and (dr.x1 > orig_x0 and dr.x0 < orig_x0 + max_line_width):
                next_y0 = min(next_y0, float(dr.y0) - 2.0)

        avail_h = max(orig_y1 - orig_y0 + 1.5, next_y0 - orig_y0)

        # Build styled inline segments matching original spans
        orig_font = str(meta.get("font") or style_tokens.get("body_font", "Helvetica"))
        orig_size = float(meta.get("font_size") or style_tokens.get("body_size", 10.0))
        orig_col = _hex_to_rgb01(str(meta.get("color") or style_tokens.get("body_color", "#404040")))

        # Preserve exact original line spacing when multi-line
        orig_lboxes = meta.get("line_bboxes") or []
        if len(orig_lboxes) >= 2:
            orig_lh = (orig_lboxes[-1][1] - orig_lboxes[0][1]) / max(1, len(orig_lboxes) - 1)
        else:
            orig_lh = orig_size * 1.16

        def _build_segments_for_scale(scale: float) -> tuple[list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]], float, float]:
            sz = round(orig_size * scale, 2)
            cat_m = re.match(r"^([A-Za-z][A-Za-z &/+]{1,35}:)(\s+.*)$", new_text, re.DOTALL)
            if ch.get("section") == "skills" and cat_m:
                bold_fnt, sub_b = _resolve_pdf_font(orig_font, force_bold=True, force_italic=False)
                reg_fnt, sub_r = _resolve_pdf_font(orig_font, force_bold=False, force_italic=False)
                for sm in (sub_b, sub_r):
                    if sm and sm not in substitutions:
                        substitutions.append(sm)
                spans_list = meta.get("spans") or []
                lbl_sz = round(float(spans_list[0].get("size", orig_size)) * scale, 2) if spans_list else sz
                lbl_col = _hex_to_rgb01(spans_list[0].get("color", meta.get("color", "#404040"))) if spans_list else orig_col
                segs = [
                    (cat_m.group(1), bold_fnt, lbl_col, lbl_sz),
                    (cat_m.group(2), reg_fnt, orig_col, sz),
                ]
            else:
                f_bold = bool(meta.get("is_bold"))
                f_italic = bool(meta.get("is_italic"))
                fnt, sub_msg = _resolve_pdf_font(orig_font, force_bold=f_bold, force_italic=f_italic)
                if sub_msg and sub_msg not in substitutions:
                    substitutions.append(sub_msg)
                segs = [(new_text, fnt, orig_col, sz)]

            wrapped = _wrap_rich_segments_to_width(segs, max(40.0, max_line_width))
            lh = orig_lh if (scale == 1.0 and len(wrapped) <= max(1, len(orig_lboxes))) else max(sz * 1.12, orig_lh * scale)
            needed_h = sz * 0.95 + max(0, len(wrapped) - 1) * lh
            return wrapped, needed_h, round(lh, 2)

        def _line_width_exceeds(lines: list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]]) -> bool:
            if not meta.get("_inline_max_x1"):
                return False
            max_w = max((sum(fnt.text_length(t, fontsize=sz) for t, fnt, _, sz in ln) for ln in lines), default=0.0)
            return max_w > max_line_width + 0.5

        chosen_scale = 1.0
        wrapped_lines, needed_h, chosen_lh = _build_segments_for_scale(1.0)
        if needed_h > avail_h + 1.0 or _line_width_exceeds(wrapped_lines):
            fitted = False
            for s_try in (0.96, 0.93, 0.90):
                w_try, h_try, lh_try = _build_segments_for_scale(s_try)
                if h_try <= avail_h + 1.0 and not _line_width_exceeds(w_try):
                    chosen_scale = s_try
                    wrapped_lines = w_try
                    needed_h = h_try
                    chosen_lh = lh_try
                    fitted = True
                    warnings.append(
                        f"Reduced font size to {int(s_try * 100)}% inside original bounding box for '{ch.get('section')}' edit."
                    )
                    break
            if not fitted:
                # Never shrink < 90% and never rebuild page: clamp cleanly & flag overflow if needed
                wrapped_lines, needed_h, chosen_lh = _build_segments_for_scale(0.90)
                warnings.append(
                    f"'{ch.get('section')}' revision slightly exceeded original box bounds even at 90% minimum font scale."
                )

        max_drawn_x1 = orig_x0 + max(
            (sum(fnt.text_length(t, fontsize=sz) for t, fnt, _, sz in ln) for ln in wrapped_lines),
            default=0.0,
        )
        union_box = [
            round(min(orig_x0, redact_rect[0]), 2),
            round(orig_y0, 2),
            round(max(redact_rect[2], max_drawn_x1), 2),
            round(max(orig_y1, orig_y0 + needed_h), 2),
        ]
        planned_ops.append(
            {
                "page_idx": page_idx,
                "redact_rect": fitz.Rect(*redact_rect),
                "x0": orig_x0,
                "y0": orig_y0,
                "first_baseline_y": meta.get("first_baseline_y"),
                "wrapped_lines": wrapped_lines,
                "line_height": chosen_lh,
                "union_box": union_box,
            }
        )

    # Execute Strategy B redactions & redraws page-by-page while preserving links & vector graphics
    ops_by_page: dict[int, list[dict[str, Any]]] = {}
    for op in planned_ops:
        ops_by_page.setdefault(op["page_idx"], []).append(op)

    for pno, ops in ops_by_page.items():
        page = src_doc[pno]
        orig_links = list(page.get_links() or [])
        for op in ops:
            page.add_redact_annot(op["redact_rect"], fill=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        remaining_uris = {
            (lk.get("uri"), tuple(round(v, 1) for v in lk.get("from", ())))
            for lk in (page.get_links() or [])
            if lk.get("from")
        }
        for lk in orig_links:
            if lk.get("uri") and lk.get("from"):
                key = (lk.get("uri"), tuple(round(v, 1) for v in lk["from"]))
                if key not in remaining_uris:
                    page.insert_link(lk)

        for op in ops:
            _draw_rich_lines(
                page,
                op["x0"],
                op["y0"],
                op["wrapped_lines"],
                op["line_height"],
                first_baseline_y=op.get("first_baseline_y"),
            )
            changed_bboxes_by_page.setdefault(pno, []).append(op["union_box"])

    overflow_detected = False
    overflow_suggestions: list[str] = []

    if additions:
        github_adds = [a for a in additions if a.get("change_type") == "added_from_github"]
        other_adds = [a for a in additions if a.get("change_type") != "added_from_github"]

        # Find target page containing PROJECTS heading (default page 0)
        target_pno = 0
        proj_head_y0: float | None = None
        proj_head_y1: float | None = None
        for pno_i in range(orig_pages):
            td = src_doc[pno_i].get_text("dict")
            for blk in td.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    ltxt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                    if detect_section_heading(ltxt) == "projects":
                        target_pno = pno_i
                        proj_head_y0 = float(ln["bbox"][1])
                        proj_head_y1 = float(ln["bbox"][3])
                        break
                if proj_head_y0 is not None:
                    break
            if proj_head_y0 is not None:
                break

        page = src_doc[target_pno]
        ph = float(page.rect.height)
        rules = _detect_horizontal_rules(page, pw)

        # Fallback Projects band bounds if heading wasn't matched literally
        if proj_head_y0 is None:
            proj_head_y0 = ph * 0.42
            proj_head_y1 = proj_head_y0 + 14.0

        # Find rule immediately after Projects band
        proj_rule_idx: int | None = None
        for idx_r, (ry0, ry1) in enumerate(rules):
            if ry0 > proj_head_y1 + 8.0:
                proj_rule_idx = idx_r
                break
        proj_limit_y = rules[proj_rule_idx][0] if proj_rule_idx is not None else (ph - 40.0)

        grid_style = _detect_projects_grid_style(page, proj_head_y1 + 1.0, proj_limit_y - 1.0, pw, margins, style_tokens)
        proj_content_y1 = float(grid_style["proj_content_y1"])

        # Resolve Projects fonts at 100% original size (NEVER shrink < 90%!)
        t_fnt, sub_t = _resolve_pdf_font(grid_style["title_font_name"], force_bold=True, force_italic=False)
        b_fnt, sub_b = _resolve_pdf_font(grid_style["body_font_name"], force_bold=False, force_italic=False)
        lk_fnt, sub_l = _resolve_pdf_font(grid_style["link_font_name"], force_bold=False, force_italic=False)
        for sm in (sub_t, sub_b, sub_l):
            if sm and sm not in substitutions:
                substitutions.append(sm)

        t_sz = float(grid_style["title_size"])
        b_sz = float(grid_style["body_size"])
        lk_sz = float(grid_style["link_size"])
        t_col = _hex_to_rgb01(grid_style["title_color"])
        b_col = _hex_to_rgb01(grid_style["body_color"])
        lk_col = _hex_to_rgb01(grid_style["link_color"])
        b_lh = round(b_sz * 1.14, 2)

        def _measure_project_card(ch: dict[str, Any], col_w: float) -> dict[str, Any]:
            cdata = _parse_github_card_data(ch, keep_bullets=bool(grid_style["had_bullets"]))
            wrapped_body = _wrap_text_to_width(cdata["body"], b_fnt, b_sz, col_w) if cdata["body"] else []
            # Keep descriptions to concise 2-3 complete short lines matching existing projects
            if len(wrapped_body) > 3:
                trimmed = wrapped_body[:3]
                last_line = trimmed[-1].rstrip(" .,;:-")
                words = last_line.split()
                while len(words) > 2 and words[-1].lower() in {"and", "or", "with", "using", "via", "for", "of", "in", "to", "a", "an", "the", "by", "featuring"}:
                    words.pop()
                trimmed[-1] = (" ".join(words).rstrip(" .,;:-") + ".") if words else ""
                wrapped_body = [ln for ln in trimmed if ln]
            h = t_sz * 1.15 + 2.0 + len(wrapped_body) * b_lh
            return {
                "title_base": cdata["title_base"],
                "html_url": cdata["html_url"],
                "wrapped_body": wrapped_body,
                "height": h,
            }

        # Group GitHub projects into rows matching existing 2-column or 1-column grid
        is_two_col = bool(grid_style["is_two_col"])
        proj_rows: list[list[tuple[float, float, dict[str, Any]]]] = []
        if is_two_col:
            for idx in range(0, len(github_adds), 2):
                row_items: list[tuple[float, float, dict[str, Any]]] = []
                row_items.append((
                    grid_style["col1_x0"],
                    grid_style["col1_w"],
                    _measure_project_card(github_adds[idx], grid_style["col1_w"]),
                ))
                if idx + 1 < len(github_adds):
                    row_items.append((
                        grid_style["col2_x0"],
                        grid_style["col2_w"],
                        _measure_project_card(github_adds[idx + 1], grid_style["col2_w"]),
                    ))
                proj_rows.append(row_items)
        else:
            for ch in github_adds:
                proj_rows.append([(
                    grid_style["col1_x0"],
                    grid_style["col1_w"],
                    _measure_project_card(ch, grid_style["col1_w"]),
                )])

        # Compute exact band layout [band_y0..band_y1] partitioned at every rule on target_pno
        td_cur = page.get_text("dict")
        content_spans_y: list[tuple[float, float]] = []
        for blk in td_cur.get("blocks", []):
            if blk.get("type") == 0:
                for ln in blk.get("lines", []):
                    txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                    if txt.strip():
                        content_spans_y.append((float(ln["bbox"][1]), float(ln["bbox"][3])))

        # Partition vertically around rules so every text baseline + hyperlink underline stroke stays intact
        band_defs: list[dict[str, Any]] = []
        prev_y = 0.0
        for r_i, (ry0, ry1) in enumerate(rules):
            c_y0s = [y0 for y0, y1 in content_spans_y if prev_y <= y0 and y1 <= ry0 + 1.0]
            c_y1s = [y1 for y0, y1 in content_spans_y if prev_y <= y0 and y1 <= ry0 + 1.0]
            min_cy0 = max(prev_y, (min(c_y0s) - 1.0) if c_y0s else prev_y)
            max_cy1 = min(ry0 - 0.3, (max(c_y1s) + 2.2) if c_y1s else ry0)
            band_defs.append({
                "kind": "content",
                "rule_after_idx": r_i,
                "src_y0": prev_y if r_i == 0 else min_cy0,
                "src_y1": max_cy1,
                "is_projects": (proj_rule_idx == r_i),
                "after_projects": (proj_rule_idx is not None and r_i > proj_rule_idx),
            })
            band_defs.append({
                "kind": "rule",
                "rule_idx": r_i,
                "src_y0": ry0 - 0.5,
                "src_y1": ry1 + 0.5,
                "after_projects": (proj_rule_idx is not None and r_i >= proj_rule_idx),
            })
            prev_y = ry1 + 0.5
        # Final band after last rule
        tail_y0s = [y0 for y0, y1 in content_spans_y if y0 >= prev_y - 1.0]
        tail_y1s = [y1 for y0, y1 in content_spans_y if y0 >= prev_y - 1.0]
        bottom_content_y1 = (max(tail_y1s) + 2.8) if tail_y1s else prev_y
        if tail_y0s:
            band_defs.append({
                "kind": "content",
                "rule_after_idx": None,
                "src_y0": max(prev_y, min(tail_y0s) - 1.0),
                "src_y1": bottom_content_y1,
                "is_projects": False,
                "after_projects": True,
            })

        safe_bottom_limit = ph - 22.0
        avail_bottom_margin = max(0.0, safe_bottom_limit - bottom_content_y1)
        min_gap = 6.2 if tighten_spacing else 10.0

        # Compute total recoverable whitespace in gaps (prioritize gaps AFTER Projects first, then before)
        gap_below_proj_save = 0.0
        gap_above_proj_save = 0.0
        for idx_b in range(len(band_defs) - 1):
            cur_gap = max(0.0, band_defs[idx_b + 1]["src_y0"] - band_defs[idx_b]["src_y1"])
            # Keep >= min_gap around every rule/content transition (header top 0 is untouched)
            if cur_gap > min_gap:
                savable = cur_gap - min_gap
                if band_defs[idx_b].get("after_projects") or band_defs[idx_b + 1].get("after_projects"):
                    gap_below_proj_save += savable
                elif idx_b >= 2:  # Preserve header+summary top untouched unless really tight
                    gap_above_proj_save += savable

        max_recoverable_page1 = avail_bottom_margin + gap_below_proj_save + gap_above_proj_save
        row_heights = [max(c["height"] for _, _, c in r) for r in proj_rows]
        proj_insert_top_gap = 7.0
        row_gap = 7.0
        total_proj_h = (proj_insert_top_gap + sum(row_heights) + row_gap * max(0, len(row_heights) - 1) + 4.0) if row_heights else 0.0
        other_h = (26.0 + sum(13.0 * max(1, len((o.get("edited_text") or o.get("new_text") or "").splitlines())) for o in other_adds)) if other_adds else 0.0

        # Determine how many Project rows fit cleanly on Page 1 WITHOUT shrinking fonts < 90%
        rows_page1: list[list[tuple[float, float, dict[str, Any]]]] = []
        rows_overflow: list[list[tuple[float, float, dict[str, Any]]]] = []
        running_h = proj_insert_top_gap + 4.0 if proj_rows else 0.0
        for r_idx, row_items in enumerate(proj_rows):
            rh = row_heights[r_idx] + (row_gap if rows_page1 else 0.0)
            if running_h + rh <= max_recoverable_page1 + 1.0:
                rows_page1.append(row_items)
                running_h += rh
            else:
                rows_overflow.append(row_items)

        other_on_page1 = (not rows_overflow) and (running_h + other_h <= max_recoverable_page1 + 1.0)
        if rows_overflow or (other_adds and not other_on_page1):
            overflow_detected = True
            overflow_suggestions = _build_overflow_options(resume_structure, github_adds)
            warnings.append(
                "Approved additions exceed 1 page at >=90% original font size. "
                "Extra items were NOT silently shrunk; choose an optimization option below or export overflow."
            )

        used_proj_h_p1 = (
            proj_insert_top_gap + sum(max(c["height"] for _, _, c in r) for r in rows_page1) + row_gap * max(0, len(rows_page1) - 1) + 4.0
        ) if rows_page1 else 0.0

        # Build exact vertical band placement offsets for target_pno
        out_doc = fitz.open()
        orig_links = list(page.get_links() or [])
        band_dy_records: list[tuple[float, float, float]] = []

        # Allocate gap reductions gently from bottom-most gaps upward only as much as needed
        extra_needed = used_proj_h_p1 + (other_h if other_on_page1 else 0.0)
        need_above_proj = max(0.0, extra_needed - (avail_bottom_margin + gap_below_proj_save))

        gap_targets: list[float] = []
        for idx_b in range(len(band_defs) - 1):
            gap_targets.append(max(0.0, band_defs[idx_b + 1]["src_y0"] - band_defs[idx_b]["src_y1"]))

        if tighten_spacing and extra_needed > avail_bottom_margin:
            # First trim gaps after projects
            rem_below = max(0.0, extra_needed - avail_bottom_margin)
            for idx_b in range(len(band_defs) - 2, -1, -1):
                if rem_below <= 0.1:
                    break
                if band_defs[idx_b].get("after_projects") or band_defs[idx_b + 1].get("after_projects"):
                    take = min(rem_below, max(0.0, gap_targets[idx_b] - min_gap))
                    gap_targets[idx_b] -= take
                    rem_below -= take
            # If still needed, trim gaps above projects (except top page margin)
            if need_above_proj > 0.1:
                rem_above = need_above_proj
                for idx_b in range(len(band_defs) - 2, 1, -1):
                    if rem_above <= 0.1:
                        break
                    if not (band_defs[idx_b].get("after_projects") or band_defs[idx_b + 1].get("after_projects")):
                        take = min(rem_above, max(0.0, gap_targets[idx_b] - min_gap))
                        gap_targets[idx_b] -= take
                        rem_above -= take

        # Copy pages 0..orig_pages-1 into out_doc, reconstructing target_pno via exact band clips
        for p_idx in range(orig_pages):
            if p_idx != target_pno:
                out_doc.insert_pdf(src_doc, from_page=p_idx, to_page=p_idx)
                continue

            new_page = out_doc.new_page(width=pw, height=ph)
            cur_y = band_defs[0]["src_y0"]
            proj_insert_y0 = proj_content_y1 + proj_insert_top_gap

            for idx_b, bd in enumerate(band_defs):
                sy0, sy1 = float(bd["src_y0"]), float(bd["src_y1"])
                h_b = max(0.5, sy1 - sy0)
                # Quantize band shift dy onto 0.6pt grid (exact 1px increments at 120 DPI)
                dy = round((cur_y - sy0) / 0.6) * 0.6
                if abs(dy) < 0.3:
                    dy = 0.0
                src_clip = fitz.Rect(0, sy0, pw, sy1)
                dst_rect = fitz.Rect(0, sy0 + dy, pw, sy1 + dy)
                new_page.show_pdf_page(dst_rect, src_doc, target_pno, clip=src_clip)
                band_dy_records.append((sy0, sy1, dy))

                if bd.get("is_projects"):
                    proj_insert_y0 = sy1 + dy + proj_insert_top_gap
                    cur_y = sy0 + dy + h_b + used_proj_h_p1
                else:
                    cur_y = sy0 + dy + h_b

                if idx_b < len(gap_targets):
                    cur_y += gap_targets[idx_b]

            band_shifts_by_page[target_pno] = band_dy_records

            # Translate any previously-recorded edit bboxes on target_pno by their band's dy
            if changed_bboxes_by_page.get(target_pno):
                shifted_boxes: list[list[float]] = []
                for bx in changed_bboxes_by_page[target_pno]:
                    bcy = (bx[1] + bx[3]) * 0.5
                    bdy = 0.0
                    for by0, by1, dy_val in band_dy_records:
                        if by0 - 2.0 <= bcy <= by1 + 2.0:
                            bdy = dy_val
                            break
                    shifted_boxes.append([bx[0], round(bx[1] + bdy, 2), bx[2], round(bx[3] + bdy, 2)])
                changed_bboxes_by_page[target_pno] = shifted_boxes

            # Re-attach original links translated by their band's exact dy
            for lk in orig_links:
                lr = lk.get("from")
                if not lr:
                    continue
                cy = (lr.y0 + lr.y1) * 0.5
                dy_lk = 0.0
                for by0, by1, bdy in band_dy_records:
                    if by0 - 2.0 <= cy <= by1 + 2.0:
                        dy_lk = bdy
                        break
                moved_lk = dict(lk)
                moved_lk["from"] = fitz.Rect(lr.x0, lr.y0 + dy_lk, lr.x1, lr.y1 + dy_lk)
                new_page.insert_link(moved_lk)

            # Helper to draw one styled project card row cleanly matching original styling
            def _draw_project_row(dpage: fitz.Page, start_y: float, row_cards: list[tuple[float, float, dict[str, Any]]]) -> float:
                row_bottom = start_y
                for cx0, cw, card in row_cards:
                    y_t = start_y
                    # 1. Bold uppercase title
                    title_prefix = f"{card['title_base']} - "
                    tw_t = fitz.TextWriter(dpage.rect)
                    tw_t.append((cx0, y_t + t_sz * 0.8), title_prefix, font=t_fnt, fontsize=t_sz)
                    tw_t.write_text(dpage, color=t_col)
                    prefix_w = t_fnt.text_length(title_prefix, fontsize=t_sz)

                    # 2. Blue underlined GITHUB hyperlink (never a raw URL)
                    gh_x0 = cx0 + prefix_w
                    gh_base_y = y_t + t_sz * 0.8
                    gh_w = lk_fnt.text_length("GITHUB", fontsize=lk_sz)
                    tw_l = fitz.TextWriter(dpage.rect)
                    tw_l.append((gh_x0, gh_base_y), "GITHUB", font=lk_fnt, fontsize=lk_sz)
                    tw_l.write_text(dpage, color=lk_col)
                    # Underline stroke matching #0563c1
                    dpage.draw_line(
                        fitz.Point(gh_x0, gh_base_y + 1.5),
                        fitz.Point(gh_x0 + gh_w, gh_base_y + 1.5),
                        color=lk_col,
                        width=0.65,
                    )
                    if card["html_url"]:
                        dpage.insert_link({
                            "kind": fitz.LINK_URI,
                            "from": fitz.Rect(gh_x0 - 1.0, y_t, gh_x0 + gh_w + 1.0, y_t + t_sz + 2.5),
                            "uri": card["html_url"],
                        })

                    # 3. 2-3 concise description lines in body style
                    y_b = y_t + t_sz * 1.15 + 2.0
                    for wline in card["wrapped_body"]:
                        tw_b = fitz.TextWriter(dpage.rect)
                        tw_b.append((cx0, y_b + b_sz * 0.8), wline, font=b_fnt, fontsize=b_sz)
                        tw_b.write_text(dpage, color=b_col)
                        y_b += b_lh
                    row_bottom = max(row_bottom, y_b)
                return row_bottom

            y_proj_draw = proj_insert_y0
            for r_i, row_items in enumerate(rows_page1):
                end_y = _draw_project_row(new_page, y_proj_draw, row_items)
                changed_bboxes_by_page.setdefault(target_pno, []).append(
                    [grid_style["col1_x0"], y_proj_draw - 2.0, pw - float(margins.get("right", 46.8)), end_y + 2.0]
                )
                y_proj_draw = end_y + row_gap

            # Bottom other_adds (e.g. Currently Learning) on Page 1 if it fits cleanly
            if other_adds and other_on_page1:
                y_cur_bot = cur_y + 6.0
                h_fnt, _ = _resolve_pdf_font(str(style_tokens.get("heading_font", "Helvetica")), force_bold=False)
                h_sz = float(style_tokens.get("heading_size", 13.0))
                h_col = _hex_to_rgb01(str(style_tokens.get("heading_color", "#575355")))
                r_col = _hex_to_rgb01(str(style_tokens.get("rule_color", "#ACA8AA")))
                left_x = float(margins.get("left", 46.8))
                new_page.draw_rect(
                    fitz.Rect(left_x, y_cur_bot, right_margin_x, y_cur_bot + float(style_tokens.get("rule_height", 2.5))),
                    color=None,
                    fill=r_col,
                )
                y_cur_bot += 9.0
                tw_h = fitz.TextWriter(new_page.rect)
                tw_h.append((left_x, y_cur_bot + h_sz * 0.8), "SKILL DEVELOPMENT", font=h_fnt, fontsize=h_sz)
                tw_h.write_text(new_page, color=h_col)
                add_s_y = y_cur_bot - 9.0
                y_cur_bot += h_sz + 3.0
                for och in other_adds:
                    otxt = (och.get("edited_text") or och.get("new_text") or "").strip()
                    for wline in _wrap_text_to_width(otxt, b_fnt, b_sz, right_margin_x - left_x):
                        tw_o = fitz.TextWriter(new_page.rect)
                        tw_o.append((left_x, y_cur_bot + b_sz * 0.8), wline, font=b_fnt, fontsize=b_sz)
                        tw_o.write_text(new_page, color=b_col)
                        y_cur_bot += b_lh
                changed_bboxes_by_page.setdefault(target_pno, []).append([left_x, add_s_y, right_margin_x, y_cur_bot + 2.0])

            # Overflow rows/items placed onto clean continuation Page 2 when overflowed (never shrunk < 90%!)
            if rows_overflow or (other_adds and not other_on_page1):
                p2 = out_doc.new_page(width=pw, height=ph)
                y_p2 = float(margins.get("top", 46.8))
                h_fnt, _ = _resolve_pdf_font(str(style_tokens.get("heading_font", "Helvetica")), force_bold=False)
                h_sz = float(style_tokens.get("heading_size", 13.0))
                h_col = _hex_to_rgb01(str(style_tokens.get("heading_color", "#575355")))
                r_col = _hex_to_rgb01(str(style_tokens.get("rule_color", "#ACA8AA")))
                left_x = float(margins.get("left", 46.8))
                if rows_overflow:
                    tw_h2 = fitz.TextWriter(p2.rect)
                    tw_h2.append((left_x, y_p2 + h_sz * 0.8), "ADDITIONAL PROJECTS", font=h_fnt, fontsize=h_sz)
                    tw_h2.write_text(p2, color=h_col)
                    y_p2 += h_sz + 8.0
                    for r_items in rows_overflow:
                        y_p2 = _draw_project_row(p2, y_p2, r_items) + row_gap
                if other_adds and not other_on_page1:
                    p2.draw_rect(fitz.Rect(left_x, y_p2, right_margin_x, y_p2 + 2.5), color=None, fill=r_col)
                    y_p2 += 9.0
                    tw_ho = fitz.TextWriter(p2.rect)
                    tw_ho.append((left_x, y_p2 + h_sz * 0.8), "SKILL DEVELOPMENT", font=h_fnt, fontsize=h_sz)
                    tw_ho.write_text(p2, color=h_col)
                    y_p2 += h_sz + 4.0
                    for och in other_adds:
                        otxt = (och.get("edited_text") or och.get("new_text") or "").strip()
                        for wline in _wrap_text_to_width(otxt, b_fnt, b_sz, right_margin_x - left_x):
                            tw_o = fitz.TextWriter(p2.rect)
                            tw_o.append((left_x, y_p2 + b_sz * 0.8), wline, font=b_fnt, fontsize=b_sz)
                            tw_o.write_text(p2, color=b_col)
                            y_p2 += b_lh

        out_doc.save(str(output_pdf_path))
        out_pages = len(out_doc)
        out_doc.close()
        src_doc.close()

        return PdfEditResult(
            strategy_used="inplace_pdf",
            warnings=list(dict.fromkeys(warnings + substitutions)),
            font_substitutions=substitutions,
            changed_bboxes_by_page=changed_bboxes_by_page,
            band_shifts_by_page=band_shifts_by_page,
            page_count_original=orig_pages,
            page_count_output=out_pages,
            overflow_detected=overflow_detected,
            overflow_suggestions=overflow_suggestions,
        )

    src_doc.save(str(output_pdf_path))
    out_pages = len(src_doc)
    src_doc.close()

    return PdfEditResult(
        strategy_used="inplace_pdf",
        warnings=list(dict.fromkeys(warnings + substitutions)),
        font_substitutions=substitutions,
        changed_bboxes_by_page=changed_bboxes_by_page,
        band_shifts_by_page=band_shifts_by_page,
        page_count_original=orig_pages,
        page_count_output=out_pages,
    )


def build_style_cloned_pdf(
    *,
    source_pdf_path: Path,
    output_pdf_path: Path,
    resume_structure: ParsedResumeStructure,
    approved_changes: list[dict[str, Any]],
    style_tokens: dict[str, Any] | None = None,
    extra_warnings: list[str] | None = None,
) -> PdfEditResult:
    """
    Strategy C fallback: Rebuild PDF using extracted style tokens (name/heading/body fonts,
    sizes, colors, rule color/thickness, margins, clickable hyperlinks), never raw defaults.
    """
    src_doc = fitz.open(str(source_pdf_path))
    orig_pages = len(src_doc)
    tokens = style_tokens or extract_pdf_style_tokens(src_doc)
    src_doc.close()

    pw = float(tokens.get("page_width", 612.0))
    ph = float(tokens.get("page_height", 792.0))
    margins = tokens.get("margins", {"left": 46.8, "right": 46.8, "top": 46.8, "bottom": 40.0})
    left_x = float(margins.get("left", 46.8))
    right_x = pw - float(margins.get("right", 46.8))
    content_w = right_x - left_x

    out_doc = fitz.open()
    page = out_doc.new_page(width=pw, height=ph)

    substitutions: list[str] = []
    name_fnt, s1 = _resolve_pdf_font(str(tokens.get("name_font", "Helvetica")), force_bold=True)
    head_fnt, s2 = _resolve_pdf_font(str(tokens.get("heading_font", "Helvetica")), force_bold=False)
    body_fnt, s3 = _resolve_pdf_font(str(tokens.get("body_font", "Helvetica")), force_bold=False)
    body_b_fnt, s4 = _resolve_pdf_font(str(tokens.get("body_font", "Helvetica")), force_bold=True)
    for sm in (s1, s2, s3, s4):
        if sm and sm not in substitutions:
            substitutions.append(sm)

    name_col = _hex_to_rgb01(str(tokens.get("name_color", "#000000")))
    head_col = _hex_to_rgb01(str(tokens.get("heading_color", "#575355")))
    body_col = _hex_to_rgb01(str(tokens.get("body_color", "#404040")))
    link_col = _hex_to_rgb01(str(tokens.get("link_color", "#0563c1")))
    rule_col = _hex_to_rgb01(str(tokens.get("rule_color", "#ACA8AA")))
    body_sz = float(tokens.get("body_size", 10.0))
    head_sz = float(tokens.get("heading_size", 13.5))
    name_sz = min(22.0, float(tokens.get("name_size", 18.0)))
    rule_h = float(tokens.get("rule_height", 2.0))

    reword_map = {
        ch.get("original_text", "").strip(): (ch.get("edited_text") or ch.get("new_text") or "").strip()
        for ch in approved_changes
        if ch.get("change_type") in {"reworded", "reordered"} and ch.get("status") in {"accepted", "edited"}
    }

    y = float(margins.get("top", 46.8))

    def _ensure_space(need: float) -> None:
        nonlocal page, y
        if y + need > ph - float(margins.get("bottom", 40.0)):
            page = out_doc.new_page(width=pw, height=ph)
            y = float(margins.get("top", 46.8))

    def _draw_text_block(text: str, fnt: fitz.Font, sz: float, col: tuple[float, float, float], x: float = left_x, width: float = content_w, uri: str = "") -> None:
        nonlocal y
        for line in _wrap_text_to_width(text, fnt, sz, width):
            _ensure_space(sz * 1.25)
            tw = fitz.TextWriter(page.rect)
            tw.append((x, y + sz * 0.8), line, font=fnt, fontsize=sz)
            tw.write_text(page, color=col)
            if uri:
                lw = fnt.text_length(line, fontsize=sz)
                page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(x, y, x + lw, y + sz * 1.1), "uri": uri})
            y += sz * 1.18

    def _draw_section_header(title: str) -> None:
        nonlocal y
        _ensure_space(head_sz + 14.0)
        y += 6.0
        page.draw_rect(fitz.Rect(left_x, y, right_x, y + rule_h), color=None, fill=rule_col)
        y += rule_h + 6.0
        tw = fitz.TextWriter(page.rect)
        tw.append((left_x, y + head_sz * 0.8), title.upper(), font=head_fnt, fontsize=head_sz)
        tw.write_text(page, color=head_col)
        y += head_sz + 4.0

    # Header (deduplicated contact + preserved LinkedIn link)
    _draw_text_block(resume_structure.full_name or "Candidate", name_fnt, name_sz, name_col)
    contact_items: list[tuple[str, str]] = []
    if resume_structure.location:
        contact_items.append((resume_structure.location, ""))
    if resume_structure.phone and resume_structure.phone not in (resume_structure.location or ""):
        contact_items.append((resume_structure.phone, ""))
    if resume_structure.email:
        contact_items.append((resume_structure.email, f"mailto:{resume_structure.email}"))
    for lk in resume_structure.links:
        if lk.get("label") in {"LinkedIn", "GitHub"} and lk.get("url"):
            contact_items.append((lk["label"], lk["url"]))
            break
    if contact_items:
        cx = left_x
        _ensure_space(body_sz * 1.5)
        for idx, (lbl, uri) in enumerate(contact_items):
            if idx > 0:
                sep = " | "
                tw_s = fitz.TextWriter(page.rect)
                tw_s.append((cx, y + body_sz * 0.8), sep, font=body_fnt, fontsize=body_sz)
                tw_s.write_text(page, color=body_col)
                cx += body_fnt.text_length(sep, fontsize=body_sz)
            col = link_col if uri else body_col
            tw_i = fitz.TextWriter(page.rect)
            tw_i.append((cx, y + body_sz * 0.8), lbl, font=body_fnt, fontsize=body_sz)
            tw_i.write_text(page, color=col)
            iw = body_fnt.text_length(lbl, fontsize=body_sz)
            if uri:
                page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(cx, y, cx + iw, y + body_sz * 1.1), "uri": uri})
            cx += iw
        y += body_sz * 1.4

    if resume_structure.summary:
        _draw_section_header("Summary")
        sum_txt = reword_map.get(resume_structure.summary.strip(), resume_structure.summary)
        _draw_text_block(sum_txt, body_fnt, body_sz, body_col)

    if resume_structure.experience:
        _draw_section_header("Experience")
        for exp in resume_structure.experience:
            t_txt = reword_map.get(exp.title.strip(), exp.title)
            _draw_text_block(f"{t_txt} — {exp.company} ({exp.dates})".strip(" —()"), body_b_fnt, body_sz, (0.0, 0.0, 0.0))
            for b in exp.bullets:
                _draw_text_block(f"• {reword_map.get(b.strip(), b)}", body_fnt, body_sz, body_col, x=left_x + 8.0, width=content_w - 8.0)

    gh_adds = [c for c in approved_changes if c.get("change_type") == "added_from_github" and c.get("status") in {"accepted", "edited"}]
    if resume_structure.projects or gh_adds:
        _draw_section_header("Projects")
        for proj in resume_structure.projects:
            p_name = reword_map.get(proj.name.strip(), proj.name)
            _draw_text_block(p_name, body_b_fnt, body_sz, (0.0, 0.0, 0.0), uri=proj.url)
            for b in proj.bullets:
                _draw_text_block(f"• {reword_map.get(b.strip(), b)}", body_fnt, body_sz, body_col, x=left_x + 8.0, width=content_w - 8.0)
        for ch in gh_adds:
            for ln in (ch.get("edited_text") or ch.get("new_text") or "").splitlines():
                _draw_text_block(ln, body_fnt, body_sz, body_col)

    if resume_structure.education:
        _draw_section_header("Education")
        for edu in resume_structure.education:
            _draw_text_block(f"{edu.degree} — {edu.institution} ({edu.dates})".strip(" —()"), body_b_fnt, body_sz, (0.0, 0.0, 0.0))
            for d in edu.details:
                _draw_text_block(f"• {d}", body_fnt, body_sz, body_col, x=left_x + 8.0, width=content_w - 8.0)

    if resume_structure.skill_categories or resume_structure.skills:
        _draw_section_header("Skills")
        if resume_structure.skill_categories:
            for cat, sks in resume_structure.skill_categories.items():
                orig_line = f"{cat}: {', '.join(sks)}"
                line_txt = reword_map.get(orig_line, orig_line)
                _draw_text_block(f"• {line_txt}", body_fnt, body_sz, body_col, x=left_x + 8.0, width=content_w - 8.0)
        else:
            orig_line = ", ".join(resume_structure.skills)
            _draw_text_block(reword_map.get(orig_line, orig_line), body_fnt, body_sz, body_col)

    if resume_structure.certifications:
        _draw_section_header("Certifications")
        for cert in resume_structure.certifications:
            _draw_text_block(f"• {cert}", body_fnt, body_sz, body_col, x=left_x + 8.0, width=content_w - 8.0)

    learn_adds = [c for c in approved_changes if c.get("change_type") == "user_confirmed_skill" and c.get("status") in {"accepted", "edited"}]
    if learn_adds:
        _draw_section_header("Skill Development")
        for ch in learn_adds:
            _draw_text_block(f"• {(ch.get('edited_text') or ch.get('new_text') or '').strip()}", body_fnt, body_sz, body_col)

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.save(str(output_pdf_path))
    out_pages = len(out_doc)
    out_doc.close()

    return PdfEditResult(
        strategy_used="style_clone_fallback",
        warnings=list(dict.fromkeys((extra_warnings or []) + substitutions)),
        font_substitutions=substitutions,
        notice=PDF_REBUILD_NOTICE,
        page_count_original=orig_pages,
        page_count_output=out_pages,
    )
