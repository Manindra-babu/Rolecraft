from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.schemas.resume import ParsedResumeStructure
from app.services.resume.pdf_parser import extract_pdf_style_tokens

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
    notice: str | None = None
    page_count_original: int = 1
    page_count_output: int = 1


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


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
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
) -> float:
    """Draw wrapped mixed-style lines onto `page` using fitz.TextWriter and return final bottom Y."""
    max_sz = max(sz for ln in wrapped_lines for _, _, _, sz in ln)
    # Approximate baseline offset (~0.78 * fontsize down from top of bbox)
    baseline_y = y0 + max_sz * 0.78
    last_y = y0
    for li, ln in enumerate(wrapped_lines):
        cur_x = x0
        for txt, fnt, col, sz in ln:
            tw = fitz.TextWriter(page.rect)
            tw.append((cur_x, baseline_y), txt, font=fnt, fontsize=sz)
            tw.write_text(page, color=col)
            cur_x += fnt.text_length(txt, fontsize=sz)
        last_y = baseline_y + max_sz * 0.25
        baseline_y += line_height
    return last_y


def _locate_edit_box(
    page: fitz.Page,
    change: dict[str, Any],
    style_tokens: dict[str, Any],
) -> tuple[list[float], dict[str, Any]]:
    """Resolve the best bounding box and style metadata on `page` for a change."""
    loc = change.get("target_location") or {}
    if isinstance(loc, dict) and loc.get("bbox"):
        return [float(v) for v in loc["bbox"]], loc

    orig = (change.get("original_text") or "").strip()
    if not orig:
        return [], {}

    # Search page dict lines for matching words
    needle = re.sub(r"\s+", " ", orig.lower())[:36]
    tdict = page.get_text("dict")
    matched_lines: list[dict[str, Any]] = []
    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            ltxt = re.sub(r"[\uE000-\uF8FF]+", "", "".join(s["text"] for s in ln["spans"])).strip()
            if needle[:18] and needle[:18] in ltxt.lower():
                matched_lines.append(ln)
    if matched_lines:
        bboxes = [l["bbox"] for l in matched_lines]
        rect = [min(b[0] for b in bboxes), min(b[1] for b in bboxes), max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
        sp0 = matched_lines[0]["spans"][0]
        return rect, {
            "font": sp0.get("font", style_tokens.get("body_font", "Helvetica")),
            "font_size": round(float(sp0.get("size", 10.0)), 2),
            "color": f"#{int(sp0.get('color', 0)) & 0xFFFFFF:06x}",
            "spans": [s for l in matched_lines for s in l["spans"]],
        }
    return [], {}


def apply_changes_to_pdf_in_place(
    *,
    source_pdf_path: Path,
    output_pdf_path: Path,
    approved_changes: list[dict[str, Any]],
    resume_structure: ParsedResumeStructure,
) -> PdfEditResult:
    """
    Strategy B: In-place PDF editing via PyMuPDF.
      - 0 approved changes -> copy original bytes verbatim (100% pixel identical).
      - Accepted edits -> redact ONLY original text bbox with fill=None preserving vector rules/links,
        insert replacement text matching font/size/color inside original bbox (floor >= 90% size).
      - Fallback to Strategy C (`build_style_cloned_pdf`) if an accepted addition/expansion overflows.
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
        src_doc.save(str(output_pdf_path))
        src_doc.close()
        return PdfEditResult(
            strategy_used="inplace_exact",
            page_count_original=orig_pages,
            page_count_output=orig_pages,
        )

    warnings: list[str] = []
    substitutions: list[str] = []
    changed_bboxes_by_page: dict[int, list[list[float]]] = {}

    pw = float(style_tokens.get("page_width", 612.0))
    margins = style_tokens.get("margins", {"left": 46.8, "right": 46.8, "bottom": 45.0})
    right_margin_x = pw - float(margins.get("right", 46.8))
    mid_col_x = pw * 0.48

    # Plan all redactions and text redraws before applying redactions
    planned_ops: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    overflow_needs_fallback = False

    # Build obstacle y0 index per page+column so we know available vertical headroom
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
            overflow_needs_fallback = True
            warnings.append(f"Could not pinpoint bounding box on PDF page for '{ch.get('section')}' change; using style-clone rebuild.")
            break

        orig_x0, orig_y0, orig_x1, orig_y1 = bbox
        is_two_col_left = orig_x1 <= mid_col_x + 25.0 and orig_x0 >= margins.get("left", 40) - 5 and len(ch.get("original_text", "")) < 90 and ch.get("section") in {"projects", "skills"}
        is_two_col_right = orig_x0 >= mid_col_x - 15.0 and ch.get("section") in {"projects", "skills"}

        # If editing project title before ' - GITHUB', protect trailing GITHUB hyperlink bbox!
        redact_rect = [orig_x0 - 0.5, orig_y0 - 0.5, orig_x1 + 0.5, orig_y1 + 0.5]
        max_line_width = right_margin_x - orig_x0
        if is_two_col_left:
            max_line_width = (mid_col_x - 6.0) - orig_x0
        elif is_two_col_right:
            max_line_width = right_margin_x - orig_x0

        # Protect adjacent hyperlink span inside project title if replacement does not include "- GITHUB"
        page_links = page.get_links() or []
        for lk in page_links:
            lr = lk.get("from")
            if lr and "github.com" in (lk.get("uri") or "").lower():
                if lr.y0 <= orig_y1 and lr.y1 >= orig_y0 and lr.x0 > orig_x0 + 30 and "github" not in new_text.lower():
                    # Stop redaction and text drawing cleanly before the GITHUB link!
                    redact_rect[2] = min(redact_rect[2], lr.x0 - 1.5)
                    max_line_width = min(max_line_width, (lr.x0 - 2.0) - orig_x0)
                    new_text = new_text.rstrip() + " - "

        # Compute max available bottom Y before next obstacle line/drawing in same column
        next_y0 = float(page.rect.height) - float(margins.get("bottom", 40.0))
        for p_i, ob_box, ob_txt in all_line_bboxes:
            if p_i != page_idx:
                continue
            if ob_box[1] > orig_y1 - 1.0:
                # Same horizontal band overlap?
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

        def _build_segments_for_scale(scale: float) -> tuple[list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]], float]:
            sz = round(orig_size * scale, 2)
            # Special mixed-style handling: Category label bold (Tools:, Programming Languages:, etc.)
            cat_m = re.match(r"^([A-Za-z][A-Za-z &/+]{1,35}:)(\s+.*)$", new_text, re.DOTALL)
            if ch.get("section") == "skills" and cat_m:
                bold_fnt, sub_b = _resolve_pdf_font(orig_font, force_bold=True, force_italic=False)
                reg_fnt, sub_r = _resolve_pdf_font(orig_font, force_bold=False, force_italic=False)
                for sm in (sub_b, sub_r):
                    if sm and sm not in substitutions:
                        substitutions.append(sm)
                # Clone label color/size from first span if present
                spans_list = meta.get("spans") or []
                lbl_sz = round(float(spans_list[0].get("size", orig_size)) * scale, 2) if spans_list else sz
                lbl_col = _hex_to_rgb01(spans_list[0].get("color", meta.get("color", "#404040"))) if spans_list else orig_col
                segs = [
                    (cat_m.group(1), bold_fnt, lbl_col, lbl_sz),
                    (cat_m.group(2), reg_fnt, orig_col, sz),
                ]
            else:
                # Preserve Italic / Bold from dominant element metadata
                f_bold = bool(meta.get("is_bold"))
                f_italic = bool(meta.get("is_italic"))
                fnt, sub_msg = _resolve_pdf_font(orig_font, force_bold=f_bold, force_italic=f_italic)
                if sub_msg and sub_msg not in substitutions:
                    substitutions.append(sub_msg)
                segs = [(new_text, fnt, orig_col, sz)]

            wrapped = _wrap_rich_segments_to_width(segs, max(40.0, max_line_width))
            lh = max(sz * 1.15, (orig_y1 - orig_y0) / max(1, len(meta.get("line_bboxes") or [1])))
            if len(wrapped) > len(meta.get("line_bboxes") or [1]):
                lh = sz * (1.12 if scale < 1.0 else 1.18)
            needed_h = sz * 0.95 + max(0, len(wrapped) - 1) * lh
            return wrapped, needed_h

        chosen_scale = 1.0
        wrapped_lines, needed_h = _build_segments_for_scale(1.0)
        if needed_h > avail_h + 1.0:
            fitted = False
            for s_try in (0.96, 0.93, 0.90):
                w_try, h_try = _build_segments_for_scale(s_try)
                if h_try <= avail_h + 1.0:
                    chosen_scale = s_try
                    wrapped_lines = w_try
                    needed_h = h_try
                    fitted = True
                    warnings.append(
                        f"Reduced font size to {int(s_try * 100)}% inside original bounding box for '{ch.get('section')}' edit."
                    )
                    break
            if not fitted:
                overflow_needs_fallback = True
                warnings.append(
                    f"Revised text in '{ch.get('section')}' exceeded original bounding box height ({needed_h:.1f}pt > {avail_h:.1f}pt) even at 90% minimum size."
                )
                break

        max_drawn_x1 = orig_x0 + max(
            (sum(fnt.text_length(t, fontsize=sz) for t, fnt, _, sz in ln) for ln in wrapped_lines),
            default=0.0,
        )
        lh = round(orig_size * chosen_scale * 1.16, 2)
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
                "wrapped_lines": wrapped_lines,
                "line_height": lh,
                "union_box": union_box,
            }
        )

    # Check bottom margin headroom for confirmed additions ('Currently Learning', etc.)
    if not overflow_needs_fallback and additions:
        last_pno = orig_pages - 1
        last_page = src_doc[last_pno]
        max_content_y = 0.0
        for p_i, ob_box, ob_txt in all_line_bboxes:
            if p_i == last_pno and ob_txt.strip():
                max_content_y = max(max_content_y, ob_box[3])
        bottom_limit = float(last_page.rect.height) - 20.0
        avail_bottom = bottom_limit - (max_content_y + 10.0)
        est_add_h = 26.0 + sum(14.0 * max(1, len(a.get("new_text", "").splitlines())) for a in additions)
        if est_add_h > avail_bottom:
            overflow_needs_fallback = True
            warnings.append("Confirmed additions require extra vertical room; using style-clone rebuild.")

    if overflow_needs_fallback:
        src_doc.close()
        return build_style_cloned_pdf(
            source_pdf_path=source_pdf_path,
            output_pdf_path=output_pdf_path,
            resume_structure=resume_structure,
            approved_changes=active_changes,
            style_tokens=style_tokens,
            extra_warnings=warnings + substitutions,
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
        # Re-insert any original links dropped by redaction overlap
        remaining_uris = {(lk.get("uri"), tuple(round(v, 1) for v in lk.get("from", ()))) for lk in (page.get_links() or []) if lk.get("from")}
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
            )
            changed_bboxes_by_page.setdefault(pno, []).append(op["union_box"])

    # Draw confirmed bottom additions on last page using cloned section heading + rule + body tokens
    if additions:
        last_pno = orig_pages - 1
        last_page = src_doc[last_pno]
        left_x = float(margins.get("left", 46.8))
        right_x = float(pw - margins.get("right", 46.8))
        y_cursor = max((b[3] for p_i, b, txt in all_line_bboxes if p_i == last_pno and txt.strip()), default=700.0) + 10.0

        h_fnt, sub_h = _resolve_pdf_font(str(style_tokens.get("heading_font", "Helvetica")), force_bold=False)
        b_fnt, sub_b = _resolve_pdf_font(str(style_tokens.get("body_font", "Helvetica")), force_bold=False)
        for sm in (sub_h, sub_b):
            if sm and sm not in substitutions:
                substitutions.append(sm)

        h_sz = float(style_tokens.get("heading_size", 13.0))
        b_sz = float(style_tokens.get("body_size", 10.0))
        h_col = _hex_to_rgb01(str(style_tokens.get("heading_color", "#575355")))
        b_col = _hex_to_rgb01(str(style_tokens.get("body_color", "#404040")))
        r_col = _hex_to_rgb01(str(style_tokens.get("rule_color", "#ACA8AA")))

        # Rule line matching original
        rule_rect = fitz.Rect(left_x, y_cursor, right_x, y_cursor + float(style_tokens.get("rule_height", 2.5)))
        last_page.draw_rect(rule_rect, color=None, fill=r_col)
        y_cursor += 8.0

        # Heading
        tw_h = fitz.TextWriter(last_page.rect)
        tw_h.append((left_x, y_cursor + h_sz * 0.8), "SKILL DEVELOPMENT", font=h_fnt, fontsize=h_sz)
        tw_h.write_text(last_page, color=h_col)
        add_box_start_y = y_cursor - 8.0
        y_cursor += h_sz + 3.0

        for add_ch in additions:
            txt = (add_ch.get("edited_text") or add_ch.get("new_text") or "").strip()
            for line in txt.splitlines():
                wrapped = _wrap_text_to_width(line.strip(), b_fnt, b_sz, right_x - left_x)
                for wline in wrapped:
                    tw_b = fitz.TextWriter(last_page.rect)
                    tw_b.append((left_x, y_cursor + b_sz * 0.8), wline, font=b_fnt, fontsize=b_sz)
                    tw_b.write_text(last_page, color=b_col)
                    y_cursor += b_sz * 1.18
        changed_bboxes_by_page.setdefault(last_pno, []).append(
            [left_x, add_box_start_y, right_x, y_cursor + 2.0]
        )

    src_doc.save(str(output_pdf_path))
    out_pages = len(src_doc)
    src_doc.close()

    return PdfEditResult(
        strategy_used="inplace_pdf",
        warnings=list(dict.fromkeys(warnings + substitutions)),
        font_substitutions=substitutions,
        changed_bboxes_by_page=changed_bboxes_by_page,
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
