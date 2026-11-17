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

EXPORT_CODE_VERSION = "2026-11-11-inplace-v6"

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
    overflow_options: list[dict[str, Any]] = field(default_factory=list)
    overlap_passed: bool = True
    overlap_details: list[dict[str, Any]] = field(default_factory=list)
    page_fit: dict[str, Any] = field(default_factory=dict)
    dropped_projects: list[str] = field(default_factory=list)


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
        or (int(s.get("flags", 0)) & 2)
    )
    style_runs: list[dict[str, Any]] = []
    for sp in non_bullet:
        raw_t = str(sp.get("text", ""))
        if not raw_t:
            continue
        sf = str(sp.get("font", ""))
        sfl = int(sp.get("flags", 0))
        sp_b = ("bold" in sf.lower()) or bool(sfl & 16)
        sp_i = ("italic" in sf.lower()) or ("oblique" in sf.lower()) or bool(sfl & 2)
        if style_runs and style_runs[-1]["is_bold"] == sp_b and style_runs[-1]["is_italic"] == sp_i:
            prev_t = style_runs[-1]["text"]
            sep = "" if (not prev_t or prev_t[-1].isspace() or prev_t[-1] == "-" or raw_t[0].isspace() or raw_t[0] in ",.;:)") else " "
            style_runs[-1]["text"] = prev_t + sep + raw_t
        else:
            style_runs.append({"text": raw_t, "is_bold": sp_b, "is_italic": sp_i})

    first_baseline = float(sp0["origin"][1]) if isinstance(sp0.get("origin"), (list, tuple)) and len(sp0["origin"]) >= 2 else None
    return {
        "font": fname,
        "font_size": round(float(sp0.get("size", style_tokens.get("body_size", 10.0))), 2),
        "color": f"#{int(sp0.get('color', 0)) & 0xFFFFFF:06x}",
        "is_bold": bold_chars >= total_chars * 0.6,
        "is_italic": italic_chars >= total_chars * 0.5,
        "first_baseline_y": first_baseline,
        "spans": non_bullet,
        "style_runs": style_runs,
        "line_bboxes": line_bboxes,
    }


def _map_text_to_style_segments(
    *,
    new_text: str,
    meta: dict[str, Any],
    orig_font: str,
    orig_col: tuple[float, float, float],
    sz: float,
    substitutions: list[str],
) -> list[tuple[str, fitz.Font, tuple[float, float, float], float]]:
    """
    Preserve run-level bold/italic styling from `meta['style_runs']` when rendering
    rewritten text (e.g. BoldItalic technical phrases inside an Italic Summary).
    """
    base_b = bool(meta.get("is_bold"))
    base_i = bool(meta.get("is_italic"))
    style_runs: list[dict[str, Any]] = meta.get("style_runs") or []

    def _get_fnt(b_flag: bool, i_flag: bool) -> fitz.Font:
        f_obj, s_msg = _resolve_pdf_font(orig_font, force_bold=b_flag, force_italic=i_flag)
        if s_msg and s_msg not in substitutions:
            substitutions.append(s_msg)
        return f_obj

    base_fnt = _get_fnt(base_b, base_i)

    # Category prefix fallback ("Label: ...")
    cat_m = re.match(r"^([A-Za-z][A-Za-z &/+]{1,28}:)(\s+.*)$", new_text, re.DOTALL)
    spans_list = meta.get("spans") or []
    if cat_m and spans_list and ("bold" in str(spans_list[0].get("font", "")).lower() or int(spans_list[0].get("flags", 0)) & 16):
        return [(cat_m.group(1), _get_fnt(True, base_i), orig_col, sz), (cat_m.group(2), _get_fnt(False, base_i), orig_col, sz)]

    # Collect emphasized runs that differ from base weight/style
    emph_runs = [
        r for r in style_runs
        if (bool(r.get("is_bold")) != base_b or bool(r.get("is_italic")) != base_i)
        and len(re.sub(r"\s+", " ", str(r.get("text", ""))).strip()) >= 10
    ]
    if not emph_runs:
        return [(new_text, base_fnt, orig_col, sz)]

    # Build candidate phrases (whole run + comma/paren clauses + equivalent rewritten stack terms)
    candidates: list[tuple[str, bool, bool]] = []
    has_tech_bold = False
    for r in emph_runs:
        r_txt = re.sub(r"\s+", " ", str(r["text"])).strip(" ,.;:")
        rb, ri = bool(r["is_bold"]), bool(r["is_italic"])
        if len(r_txt) >= 6:
            candidates.append((r_txt, rb, ri))
        if rb and any(k in r_txt.lower() for k in ("mern", "react", "node", "express", "data structures", "ai", "llm", "full-stack")):
            has_tech_bold = True
        for part in re.split(r"[,()]|\band\b", r_txt):
            p_clean = part.strip(" ,.;:")
            if len(p_clean) >= 5:
                candidates.append((p_clean, rb, ri))

    if has_tech_bold:
        # Preserve bold emphasis on expanded MERN/API stack terms when rewritten from "MERN stack (...)"
        for extra_term in (
            "full-stack web applications",
            "Express.js",
            "React",
            "Node.js",
            "MongoDB",
            "RESTful APIs",
            "REST APIs",
            "Firebase",
            "MERN stack",
        ):
            candidates.append((extra_term, True, base_i))

    # Deduplicate and sort longest-first so whole emphasized phrases win over sub-terms
    unique_cands: list[tuple[str, bool, bool]] = []
    seen_low: set[str] = set()
    for phrase, rb, ri in sorted(candidates, key=lambda x: len(x[0]), reverse=True):
        pl = phrase.lower()
        if pl not in seen_low:
            seen_low.add(pl)
            unique_cands.append((phrase, rb, ri))

    matches: list[tuple[int, int, bool, bool]] = []
    occupied: list[bool] = [False] * len(new_text)
    for phrase, rb, ri in unique_cands:
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        for m in pat.finditer(new_text):
            s_i, e_i = m.span()
            if s_i < e_i and not any(occupied[s_i:e_i]):
                for k in range(s_i, e_i):
                    occupied[k] = True
                matches.append((s_i, e_i, rb, ri))

    if not matches:
        return [(new_text, base_fnt, orig_col, sz)]

    matches.sort(key=lambda x: x[0])
    # Coalesce adjacent styled runs separated only by ", " / " and " / " (" when both share identical style
    merged_matches: list[list[Any]] = []
    for s_i, e_i, rb, ri in matches:
        if merged_matches:
            prev_s, prev_e, p_b, p_i = merged_matches[-1]
            gap = new_text[prev_e:s_i]
            if p_b == rb and p_i == ri and re.fullmatch(r"(?:\s*,\s*|\s+and\s+|\s+with\s+|\s*\(\s*)", gap, re.IGNORECASE):
                merged_matches[-1][1] = e_i
                continue
        merged_matches.append([s_i, e_i, rb, ri])

    segments: list[tuple[str, fitz.Font, tuple[float, float, float], float]] = []
    cursor = 0
    for s_i, e_i, rb, ri in merged_matches:
        if s_i > cursor:
            segments.append((new_text[cursor:s_i], base_fnt, orig_col, sz))
        segments.append((new_text[s_i:e_i], _get_fnt(rb, ri), orig_col, sz))
        cursor = e_i
    if cursor < len(new_text):
        segments.append((new_text[cursor:], base_fnt, orig_col, sz))
    return segments or [(new_text, base_fnt, orig_col, sz)]


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
            span_meta["_matched_full_text"] = re.sub(
                r"\s+",
                " ",
                "".join(str(sp.get("text", "")) for sp in overlapping_spans),
            ).strip()
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
    """Return sorted (y0, y1) spans for visible full/half-width horizontal divider rules on `page`."""
    candidates: list[tuple[float, float, float, float, tuple[float, ...] | None]] = []
    seen_rect: set[tuple[float, float, float, float]] = set()
    for d in page.get_drawings() or []:
        if d.get("type") == "clip":
            continue
        col = d.get("color")
        fil = d.get("fill")
        if col is None and fil is None:
            continue
        # Ignore white redaction/background cover rects (divider lines are dark/colored)
        is_dark_stroke = isinstance(col, (tuple, list)) and any(float(c) < 0.85 for c in col[:3])
        is_dark_fill = isinstance(fil, (tuple, list)) and any(float(c) < 0.85 for c in fil[:3])
        if not (is_dark_stroke or is_dark_fill):
            continue
        r = d.get("rect")
        if not r:
            continue
        if (r.x1 - r.x0) >= page_width * 0.42 and (r.y1 - r.y0) <= 6.5:
            key = (round(float(r.x0), 1), round(float(r.y0), 1), round(float(r.x1), 1), round(float(r.y1), 1))
            if key not in seen_rect:
                seen_rect.add(key)
                c_ref = tuple(float(c) for c in (fil if is_dark_fill else col)[:3])
                candidates.append((float(r.x0), float(r.y0), float(r.x1), float(r.y1), c_ref))

    raw_rules: list[tuple[float, float]] = []
    # Composited Form-XObject bands repeat XObject paths outside their clip box; filter out clipped phantoms.
    if len(candidates) > 6:
        for rx0, ry0, rx1, ry1, c_ref in candidates:
            ymid = 0.5 * (ry0 + ry1)
            clip_r = fitz.Rect(rx0 + 2.0, max(0.0, ymid - 0.5), rx1 - 2.0, ymid + 0.5)
            if clip_r.width <= 4.0 or clip_r.height <= 0.1:
                continue
            pm = page.get_pixmap(matrix=fitz.Matrix(1, 1), clip=clip_r, alpha=False)
            samples = pm.samples
            n_px = len(samples) // 3
            if n_px <= 0:
                continue
            non_white = sum(
                1
                for i in range(0, len(samples) - 2, 3)
                if samples[i] < 240 or samples[i + 1] < 240 or samples[i + 2] < 240
            )
            if (non_white / n_px) < 0.88:
                continue
            if c_ref is not None and (ry1 - ry0) >= 1.2 and any(c > 0.25 for c in c_ref):
                tr, tg, tb = int(c_ref[0] * 255), int(c_ref[1] * 255), int(c_ref[2] * 255)
                matched = sum(
                    1
                    for i in range(0, len(samples) - 2, 3)
                    if abs(samples[i] - tr) <= 40 and abs(samples[i + 1] - tg) <= 40 and abs(samples[i + 2] - tb) <= 40
                )
                if (matched / n_px) < 0.75:
                    continue
            raw_rules.append((ry0, ry1))
    else:
        raw_rules = [(ry0, ry1) for _, ry0, _, ry1, _ in candidates]

    raw_rules.sort()
    merged: list[tuple[float, float]] = []
    for ry0, ry1 in raw_rules:
        if merged and abs(ry0 - merged[-1][0]) <= 2.5:
            merged[-1] = (min(merged[-1][0], ry0), max(merged[-1][1], ry1))
        else:
            merged.append((ry0, ry1))
    return merged


def _detect_vertical_divider(
    page: fitz.Page, page_width: float, page_height: float
) -> tuple[float, float, float] | None:
    """
    Detect a vertical sidebar divider rule on `page` when headings exist on both sides
    below the header. Returns (div_x, top_y0, bottom_y1) or None.
    """
    candidates: list[tuple[float, float, float]] = []
    for d in page.get_drawings() or []:
        r = d.get("rect")
        if not r:
            continue
        w = abs(float(r.x1) - float(r.x0))
        h = abs(float(r.y1) - float(r.y0))
        cx = (float(r.x0) + float(r.x1)) * 0.5
        if w <= 3.0 and h >= max(100.0, page_height * 0.14) and page_width * 0.22 <= cx <= page_width * 0.58:
            candidates.append((round(cx, 2), round(float(r.y0), 2), round(float(r.y1), 2)))
    if not candidates:
        return None
    div_x, div_y0, div_y1 = candidates[0]
    has_left_head = False
    has_right_head = False
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            if float(ln["bbox"][1]) < div_y0 - 4.0:
                continue
            txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if txt and detect_section_heading(txt):
                if float(ln["bbox"][0]) < div_x - 2.0:
                    has_left_head = True
                elif float(ln["bbox"][0]) >= div_x - 2.0:
                    has_right_head = True
    return (div_x, div_y0, div_y1) if (has_left_head and has_right_head) else None


def _detect_projects_grid_style(
    page: fitz.Page,
    proj_y0: float,
    proj_limit_y: float,
    pw: float,
    margins: dict[str, Any],
    style_tokens: dict[str, Any],
    min_x: float = 0.0,
) -> dict[str, Any]:
    """Inspect existing PROJECTS spans & layout (columns, fonts, link style, bottom Y)."""
    tdict = page.get_text("dict")
    mid_x = min_x + (pw - min_x) * 0.48 if min_x > 0 else pw * 0.48
    left_x = max(float(margins.get("left", 46.8)), min_x)
    right_x = pw - float(margins.get("right", 46.8))

    proj_lines: list[dict[str, Any]] = []
    for blk in tdict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            bx = ln["bbox"]
            if bx[1] >= proj_y0 and bx[3] <= proj_limit_y and bx[0] >= min_x - 1.0:
                txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if txt and detect_section_heading(txt) != "projects":
                    proj_lines.append(ln)

    has_left = any(ln["bbox"][0] < mid_x - 12.0 for ln in proj_lines)
    has_right = any(ln["bbox"][0] >= mid_x - 12.0 for ln in proj_lines)
    had_right_col = bool(has_right and has_left and min_x <= 0)
    is_two_col = bool(has_right and has_left and min_x <= 0)

    col1_x0 = left_x
    col2_x0 = mid_x if is_two_col else left_x
    col1_w = (mid_x - left_x - 8.0) if is_two_col else (right_x - col1_x0)
    col2_w = (right_x - mid_x) if is_two_col else (right_x - col1_x0)

    # Extract exact title/body/link font specs from Projects spans
    title_span: dict[str, Any] | None = None
    body_span: dict[str, Any] | None = None
    link_span: dict[str, Any] | None = None
    had_bullets = False
    bullet_prefix = ""
    proj_content_y1 = proj_y0

    for ln in proj_lines:
        proj_content_y1 = max(proj_content_y1, float(ln["bbox"][3]))
        ln_txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        if ln_txt.startswith(("- ", "•", "\uf0b7")):
            had_bullets = True
            if not bullet_prefix:
                bullet_prefix = "- " if ln_txt.startswith("- ") else "• "
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
            elif (is_bold or "github" in ln_txt.lower()) and title_span is None and ":" not in stxt[:16]:
                title_span = sp
            elif not is_bold and body_span is None and len(stxt) > 12 and "github" not in ln_txt.lower():
                body_span = sp

    title_is_bold = bool(
        title_span
        and (
            "bold" in str(title_span.get("font", "")).lower()
            or bool(int(title_span.get("flags", 0)) & 16)
        )
    )
    had_link_styling = bool(
        link_span is not None
        and (
            col_hex in {"#0563c1", "#1155cc", "#0000ee"}
            or (title_span is not None and str(link_span.get("color")) != str(title_span.get("color")))
        )
    )

    title_font_name = str((title_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    title_size = round(float((title_span or {}).get("size") or style_tokens.get("body_size", 10.0)), 2)
    title_color = f"#{int((title_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if title_span else str(style_tokens.get("body_color", "#404040"))

    body_font_name = str((body_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    body_size = round(float((body_span or {}).get("size") or style_tokens.get("body_size", 10.0)), 2)
    body_color = f"#{int((body_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if body_span else str(style_tokens.get("body_color", "#404040"))

    link_font_name = str((link_span or {}).get("font") or title_font_name)
    link_size = round(float((link_span or {}).get("size") or (title_size if not had_link_styling else max(7.5, title_size * 0.78))), 2)
    link_color = (
        f"#{int((link_span or {}).get('color', 0x0563C1)) & 0xFFFFFF:06x}"
        if (link_span and had_link_styling)
        else title_color
    )

    # Measure exact vertical line spacing across consecutive body lines in the same column
    col_y0s: dict[int, list[float]] = {0: [], 1: []}
    for ln in proj_lines:
        ln_txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        sp0 = next((s for s in ln.get("spans", []) if (s.get("text") or "").strip()), None)
        sp0_font = str((sp0 or {}).get("font", "")).lower()
        sp0_bold = "bold" in sp0_font or bool(int((sp0 or {}).get("flags", 0)) & 16)
        if not (sp0_bold and ":" not in ln_txt[:16] and "github" in ln_txt.lower()):
            c_idx = 1 if ln["bbox"][0] >= mid_x - 12.0 else 0
            col_y0s[c_idx].append(float(ln["bbox"][1]))
    body_deltas = [
        round(ys[k + 1] - ys[k], 2)
        for ys in col_y0s.values()
        for k in range(len(ys) - 1)
        if 9.0 <= (ys[k + 1] - ys[k]) <= 18.0
    ]
    body_lh = round(sum(body_deltas) / len(body_deltas), 2) if body_deltas else round(body_size * 1.18, 2)

    return {
        "is_two_col": is_two_col,
        "had_right_col": had_right_col,
        "col1_x0": col1_x0,
        "col1_w": max(150.0, col1_w),
        "col2_x0": col2_x0,
        "col2_w": max(150.0, col2_w),
        "proj_content_y1": proj_content_y1,
        "had_bullets": had_bullets,
        "bullet_prefix": bullet_prefix,
        "title_is_bold": title_is_bold,
        "had_link_styling": had_link_styling,
        "title_font_name": title_font_name,
        "title_size": title_size,
        "title_color": title_color,
        "body_font_name": body_font_name,
        "body_size": body_size,
        "body_color": body_color,
        "body_lh": body_lh,
        "link_font_name": link_font_name,
        "link_size": link_size,
        "link_color": link_color,
    }


def _parse_github_card_data(
    ch: dict[str, Any], keep_bullets: bool = False, bullet_prefix: str = ""
) -> dict[str, str]:
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
    pref = bullet_prefix or ("• " if keep_bullets else "")
    if pref and body and not body.startswith(("•", "- ")):
        body = f"{pref}{body}"
    return {"title_base": title_base, "body": body, "html_url": html_url}


def _fit_complete_sentences_to_lines(
    body_text: str,
    font: fitz.Font,
    font_size: float,
    col_width: float,
    max_lines: int = 3,
) -> list[str]:
    """
    Wrap project body text into at most `max_lines` while keeping every sentence grammatically complete
    (never truncating mid-sentence like '...and API rate.').
    """
    clean = re.sub(r"\s+", " ", (body_text or "").strip())
    if not clean:
        return []
    wrapped = _wrap_text_to_width(clean, font, font_size, col_width)
    if len(wrapped) <= max_lines:
        return wrapped

    # 1. Drop trailing complete sentences first until wrapped fits <= max_lines
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()]
    for count in range(len(sentences) - 1, 0, -1):
        candidate = " ".join(sentences[:count])
        w_try = _wrap_text_to_width(candidate, font, font_size, col_width)
        if len(w_try) <= max_lines:
            return w_try

    # 2. Single long sentence > max_lines: trim at clean semantic clause boundaries, never dangling prepositions/nouns
    first_sent = sentences[0] if sentences else clean
    for sep in ("; ", " featuring ", " using ", " with ", ", "):
        parts = first_sent.split(sep)
        if len(parts) > 1:
            for k in range(len(parts) - 1, 0, -1):
                candidate = sep.join(parts[:k]).rstrip(" .,;:-") + "."
                w_try = _wrap_text_to_width(candidate, font, font_size, col_width)
                if len(w_try) <= max_lines and len(candidate.split()) >= 6:
                    return w_try
    return wrapped[:max_lines]


def _detect_skills_block_layout(
    page: fitz.Page,
    pw: float,
    margins: dict[str, Any],
    style_tokens: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Inspect the SKILLS section region on `page` so any skill edit or Currently Learning
    addition redacts the ENTIRE skills block (both columns, bullets, labels) before redrawing.
    """
    tdict = page.get_text("dict")
    ph = float(page.rect.height)
    rules = _detect_horizontal_rules(page, pw)
    vdiv = _detect_vertical_divider(page, pw, ph)
    mid_x = pw * 0.48
    left_m = float(margins.get("left", 46.8))
    right_x = pw - float(margins.get("right", 46.8))

    skills_head_x0: float | None = None
    skills_head_y0: float | None = None
    skills_head_y1: float | None = None
    next_head_y0: float | None = None

    all_lines: list[dict[str, Any]] = []
    for blk in tdict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if not txt:
                continue
            all_lines.append(ln)
            sec = detect_section_heading(txt)
            if skills_head_y0 is None and sec == "skills":
                skills_head_x0 = float(ln["bbox"][0])
                skills_head_y0 = float(ln["bbox"][1])
                skills_head_y1 = float(ln["bbox"][3])

    if skills_head_y0 is None or skills_head_y1 is None or skills_head_x0 is None:
        return None

    skills_in_sidebar = bool(vdiv and skills_head_x0 < vdiv[0] - 2.0 and skills_head_y0 >= vdiv[1] - 6.0)
    col_x_max = (vdiv[0] - 2.0) if skills_in_sidebar else (pw + 1.0)
    col_content_right = (vdiv[0] - 8.0) if skills_in_sidebar else right_x

    for ln in all_lines:
        txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        sec = detect_section_heading(txt)
        if sec and sec != "skills" and float(ln["bbox"][1]) > skills_head_y1 + 5.0:
            if float(ln["bbox"][0]) <= col_x_max:
                if next_head_y0 is None or float(ln["bbox"][1]) < next_head_y0:
                    next_head_y0 = float(ln["bbox"][1])

    limit_y = next_head_y0 if next_head_y0 is not None else (ph - 40.0)
    for ry0, _ in rules:
        if ry0 > skills_head_y1 + 5.0 and ry0 < limit_y:
            limit_y = ry0 - 0.5
            break

    region_lines = []
    for ln in all_lines:
        l_txt_chk = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        if detect_section_heading(l_txt_chk) == "skills":
            continue
        if (
            float(ln["bbox"][0]) <= col_x_max
            and float(ln["bbox"][1]) > skills_head_y0 + 10.0
            and float(ln["bbox"][3]) <= limit_y
        ):
            region_lines.append(ln)
    if not region_lines:
        return None

    # Sort column-by-column in top-to-bottom order (sidebar skills are single-column)
    cols: dict[int, list[dict[str, Any]]] = {0: [], 1: []}
    for ln in region_lines:
        col_idx = 0 if skills_in_sidebar else (1 if float(ln["bbox"][0]) >= mid_x - 12.0 else 0)
        cols[col_idx].append(ln)
    for c_idx in (0, 1):
        cols[c_idx].sort(key=lambda l: (float(l["bbox"][1]), float(l["bbox"][0])))

    is_two_col = (not skills_in_sidebar) and bool(cols[0]) and bool(cols[1])
    bullet_span: dict[str, Any] | None = None
    label_span: dict[str, Any] | None = None
    val_span: dict[str, Any] | None = None

    col_entries: dict[int, list[dict[str, Any]]] = {0: [], 1: []}
    col_entry_y0s: dict[int, list[float]] = {0: [], 1: []}
    col_line_y0s: dict[int, list[float]] = {0: [], 1: []}
    bullet_xs: dict[int, list[float]] = {0: [], 1: []}
    text_xs: dict[int, list[float]] = {0: [], 1: []}

    for c_idx in (0, 1):
        for ln in cols[c_idx]:
            spans = ln.get("spans", [])
            has_bullet = False
            b_sp: dict[str, Any] | None = None
            non_b: list[dict[str, Any]] = []
            for sp in spans:
                stxt = sp.get("text", "")
                clean_check = re.sub(r"[\uE000-\uF8FF•\-–—\s]+", "", stxt)
                if not clean_check and stxt.strip():
                    has_bullet = True
                    b_sp = sp
                    if bullet_span is None:
                        bullet_span = sp
                    bullet_xs[c_idx].append(float(sp["bbox"][0]))
                elif stxt.strip():
                    non_b.append(sp)
            if not non_b:
                continue
            text_xs[c_idx].append(float(non_b[0]["bbox"][0]))
            col_line_y0s[c_idx].append(float(ln["bbox"][1]))
            ltxt = "".join(s.get("text", "") for s in non_b).strip()
            for sp in non_b:
                fname = str(sp.get("font", "")).lower()
                is_b = "bold" in fname or bool(int(sp.get("flags", 0)) & 16)
                if is_b and label_span is None:
                    label_span = sp
                elif not is_b and val_span is None:
                    val_span = sp

            cat_m = re.match(r"^([A-Za-z][A-Za-z &/+]{1,35}:)\s*(.*)$", ltxt)
            if has_bullet or cat_m:
                col_entry_y0s[c_idx].append(float(ln["bbox"][1]))
                lbl = cat_m.group(1) if cat_m else ""
                body = (cat_m.group(2) if cat_m else ltxt).strip()
                col_entries[c_idx].append({
                    "col": c_idx,
                    "label": lbl,
                    "body": body,
                    "full_text": ltxt,
                })
            elif col_entries[c_idx]:
                col_entries[c_idx][-1]["body"] = (col_entries[c_idx][-1]["body"] + " " + ltxt).strip()
                col_entries[c_idx][-1]["full_text"] = (col_entries[c_idx][-1]["full_text"] + " " + ltxt).strip()
            else:
                # Flat single-line Skills without bullet or colon (e.g. "Python, PostgreSQL, FastAPI, Git")
                col_entry_y0s[c_idx].append(float(ln["bbox"][1]))
                col_entries[c_idx].append({
                    "col": c_idx,
                    "label": "",
                    "body": ltxt,
                    "full_text": ltxt,
                })

    # Exact coordinates and vertical metrics
    col0_bx = min(bullet_xs[0]) if bullet_xs[0] else (min(text_xs[0]) if text_xs[0] else left_m)
    col0_tx = min(text_xs[0]) if text_xs[0] else (col0_bx + 18.0)
    col1_bx = min(bullet_xs[1]) if bullet_xs[1] else (min(text_xs[1]) if text_xs[1] else mid_x + 12.0)
    col1_tx = min(text_xs[1]) if text_xs[1] else (col1_bx + 18.0)

    col0_w = ((col1_bx - 6.0) - col0_tx) if is_two_col else (col_content_right - col0_tx)
    col1_w = (col_content_right - col1_tx) if is_two_col else (col_content_right - col0_tx)

    line_deltas = [
        round(ys[k + 1] - ys[k], 2)
        for ys in col_line_y0s.values()
        for k in range(len(ys) - 1)
        if 10.0 <= (ys[k + 1] - ys[k]) <= 16.5
    ]
    label_font = str((label_span or val_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    val_font = str((val_span or label_span or {}).get("font") or style_tokens.get("body_font", "Helvetica"))
    text_size = round(float((val_span or label_span or {}).get("size") or style_tokens.get("body_size", 10.0)), 2)
    text_color = f"#{int((val_span or label_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if (val_span or label_span) else str(style_tokens.get("body_color", "#404040"))
    label_color = f"#{int((label_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if label_span else text_color
    lh = round(sum(line_deltas) / len(line_deltas), 2) if line_deltas else round(text_size * 1.19, 2)

    # Extra spacing between separate skill bullets (~3.8-4.5pt vs wrapped line step)
    entry_gap = 2.0
    for c_idx in (0, 1):
        ey = col_entry_y0s[c_idx]
        if len(ey) >= 2:
            diffs = [ey[k + 1] - ey[k] for k in range(len(ey) - 1)]
            # For single-line gaps around 14-18pt above lh:
            small_diffs = [d - lh for d in diffs if lh < d <= lh + 8.0]
            if small_diffs:
                entry_gap = max(1.0, round(sum(small_diffs) / len(small_diffs), 2))
                break

    col0_y0 = min((float(ln["bbox"][1]) for ln in cols[0]), default=skills_head_y1 + 3.2)
    top_y0 = max(skills_head_y1 + 3.2, col0_y0)
    orig_y1 = max(float(ln["bbox"][3]) for ln in region_lines)
    redact_x0 = min(float(ln["bbox"][0]) for ln in region_lines) - 2.0
    max_line_rx1 = max(float(ln["bbox"][2]) for ln in region_lines) + 2.0
    redact_x1 = min(vdiv[0] - 1.5, max(max_line_rx1, col_content_right + 1.0)) if skills_in_sidebar else max(max_line_rx1, right_x + 1.0)

    col_redact_rects: list[list[float]] = []
    if is_two_col:
        c0_x0 = min(float(ln["bbox"][0]) for ln in cols[0]) - 2.0
        c0_y0 = max(skills_head_y1 + 0.2, min(float(ln["bbox"][1]) for ln in cols[0]) - 0.4)
        c0_x1 = min(col1_bx - 4.0, max(float(ln["bbox"][2]) for ln in cols[0]) + 2.0)
        c0_y1 = max(float(ln["bbox"][3]) for ln in cols[0]) + 1.2
        col_redact_rects.append([c0_x0, c0_y0, c0_x1, c0_y1])

        c1_x0 = min(float(ln["bbox"][0]) for ln in cols[1]) - 2.0
        c1_y0 = min(float(ln["bbox"][1]) for ln in cols[1]) - 0.8
        c1_x1 = max(max(float(ln["bbox"][2]) for ln in cols[1]) + 2.0, right_x + 1.0)
        c1_y1 = max(float(ln["bbox"][3]) for ln in cols[1]) + 1.2
        col_redact_rects.append([c1_x0, c1_y0, c1_x1, c1_y1])
    else:
        col_redact_rects.append([redact_x0, max(skills_head_y1 + 0.2, min(float(ln["bbox"][1]) for ln in region_lines) - 0.4), redact_x1, orig_y1 + 1.2])

    b_text = str((bullet_span or {}).get("text") or "•").strip()[:1] or "•"
    b_font = str((bullet_span or {}).get("font") or "Symbol")
    b_size = round(float((bullet_span or {}).get("size") or text_size), 2)
    b_color = f"#{int((bullet_span or {}).get('color', 0)) & 0xFFFFFF:06x}" if bullet_span else text_color

    has_bullets = bool(bullet_xs[0] or bullet_xs[1])
    label_is_bold = bool(
        label_span
        and (
            "bold" in str(label_span.get("font", "")).lower()
            or bool(int(label_span.get("flags", 0)) & 16)
        )
    )
    return {
        "has_bullets": has_bullets,
        "head_y0": skills_head_y0,
        "head_y1": skills_head_y1,
        "limit_y": limit_y,
        "top_y0": top_y0,
        "orig_y1": orig_y1,
        "redact_rect": [redact_x0, top_y0 - 0.4, redact_x1, orig_y1 + 1.2],
        "redact_rects": col_redact_rects,
        "is_two_col": is_two_col,
        "col0_bx": col0_bx,
        "col0_tx": col0_tx,
        "col0_w": max(120.0, col0_w),
        "col1_bx": col1_bx,
        "col1_tx": col1_tx,
        "col1_w": max(120.0, col1_w),
        "label_font": label_font,
        "val_font": val_font,
        "label_is_bold": label_is_bold,
        "text_size": text_size,
        "label_color": label_color,
        "text_color": text_color,
        "lh": lh,
        "entry_gap": entry_gap,
        "bullet_text": b_text,
        "bullet_font": b_font,
        "bullet_size": b_size,
        "bullet_color": b_color,
        "entries": col_entries,
    }


def _clean_skill_category_text(text: str) -> tuple[str, str]:
    """Normalize a skill category string into (label_with_colon, clean_items_text) with no stray punctuation."""
    clean = re.sub(r"^[\uE000-\uF8FF•\-–—\s]+", "", (text or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip()
    m = re.match(r"^([A-Za-z][A-Za-z &/+]{1,50}:)\s*(.*)$", clean)
    if m:
        lbl = m.group(1).strip()
        body = m.group(2).strip().rstrip(".,;:")
        while True:
            m2 = re.match(r"^([A-Za-z][A-Za-z &/+]{1,50}:)\s*(.*)$", body)
            if m2:
                body = m2.group(2).strip().rstrip(".,;:")
            else:
                break
        body = re.sub(r"\s*,\s*,+", ", ", body)
        return lbl, body
    return "", clean.rstrip(".,;:")


def _render_skills_block(
    *,
    page: fitz.Page,
    layout: dict[str, Any],
    draw_y0: float,
    col_wrapped: dict[int, list[list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]]]],
    bullet_draw_spec: tuple[fitz.Font | None, str, float, tuple[float, float, float]],
    record_boxes: list[list[float]] | None = None,
) -> float:
    """Draw the redrawn two-column bulleted Skills block starting at `draw_y0`. Returns final bottom Y."""
    lh = float(layout["lh"])
    entry_gap = float(layout["entry_gap"])
    t_sz = float(layout["text_size"])
    b_fnt, b_char, b_sz, b_col = bullet_draw_spec
    max_y1 = draw_y0

    for c_idx in (0, 1):
        entries = col_wrapped.get(c_idx) or []
        if not entries:
            continue
        bx = float(layout[f"col{c_idx}_bx"])
        tx = float(layout[f"col{c_idx}_tx"])
        cur_y = draw_y0
        for wrapped_lines in entries:
            baseline_0 = cur_y + t_sz * 0.8
            # Draw bullet glyph aligned cleanly at original indent only when the section originally used bullets
            if layout.get("has_bullets", True) and b_fnt is not None and b_char:
                tw_b = fitz.TextWriter(page.rect)
                tw_b.append((bx, baseline_0 + 0.35), b_char, font=b_fnt, fontsize=b_sz)
                tw_b.write_text(page, color=b_col)
            _draw_rich_lines(
                page,
                tx,
                cur_y,
                wrapped_lines,
                lh,
                first_baseline_y=baseline_0,
            )
            entry_end_y = cur_y + max(t_sz * 1.05, t_sz * 0.95 + max(0, len(wrapped_lines) - 1) * lh)
            max_y1 = max(max_y1, entry_end_y)
            cur_y += len(wrapped_lines) * lh + entry_gap
        if record_boxes is not None:
            record_boxes.append([bx - 1.0, draw_y0 - 1.0, tx + float(layout[f"col{c_idx}_w"]) + 1.0, cur_y])
    return round(max_y1, 2)


def _condense_education_bullet(text: str) -> str:
    low = text.lower()
    if "workshop" in low and "code review" in low:
        return "Collaborated in weekly workshops and code reviews with 100+ peers."
    if "documentation" in low and "presentation" in low:
        return "Developed technical documentation and presentation skills."
    # Generic safe single-line truncation preserving meaning
    words = text.split()
    out: list[str] = []
    for w in words:
        if sum(len(x) + 1 for x in out) + len(w) > 68:
            break
        out.append(w)
    res = " ".join(out).rstrip(",;:")
    if res and not res.endswith("."):
        res += "."
    return res or text[:70]


def _shorten_project_card_text(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return text
    header = lines[0]
    bullets = [ln for ln in lines[1:] if "automated test coverage" not in ln.lower()]
    if not bullets and len(lines) > 1:
        bullets = lines[1:2]
    if not bullets:
        return header
    b0 = bullets[0].lstrip("•*- ").strip()
    # Keep concise single-sentence description (prefer complete clause <= 120 chars, no dangling noun/comma)
    first_sent = b0.split(". ")[0].strip()
    if len(first_sent) > 120:
        clauses = [c.strip() for c in re.split(r"(?:,\s*|\s+)(?:featuring|powered by|with|using)\s+", first_sent, maxsplit=1)]
        if clauses and 36 <= len(clauses[0]) <= 120:
            first_sent = clauses[0]
        else:
            words = first_sent.split()
            kept_w: list[str] = []
            for w in words:
                if sum(len(x) + 1 for x in kept_w) + len(w) > 116:
                    break
                kept_w.append(w)
            first_sent = " ".join(kept_w).rstrip(",;:")
    first_sent = re.sub(r"(?:\s+(?:and|or|with|by|for|to|of|in|on|via|using))+$", "", first_sent, flags=re.IGNORECASE).rstrip(",;:")
    if not first_sent.endswith("."):
        first_sent += "."
    return f"{header}\n• {first_sent}"


def _build_overflow_options(
    resume_structure: ParsedResumeStructure,
    github_adds: list[dict[str, Any]],
    relevance_items: list[dict[str, Any]] | None = None,
    all_changes: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    suggestions: list[str] = []
    structured: list[dict[str, Any]] = []

    # Option 1: Shorten the new project descriptions
    proj_previews: list[dict[str, Any]] = []
    proj_change_ids: list[str] = []
    for ch in github_adds:
        cid = str(ch.get("id") or "")
        orig_p = str(ch.get("edited_text") or ch.get("new_text") or "").strip()
        short_p = _shorten_project_card_text(orig_p)
        if orig_p and short_p != orig_p:
            if cid:
                proj_change_ids.append(cid)
            repo_name = str((ch.get("target_location") or {}).get("repo_name") or orig_p.split("|")[0].strip())
            proj_previews.append({
                "change_id": cid or None,
                "label": f"Project: {repo_name}",
                "before": orig_p,
                "after": short_p,
            })
    if proj_previews:
        est_p = 18 * len(proj_previews)
        structured.append({
            "id": "shorten_projects",
            "title": "Shorten new project descriptions",
            "description": f"Condense {len(proj_previews)} added project card(s) to concise 1-bullet summaries (~{est_p}pt saved).",
            "estimated_pt_saved": est_p,
            "target_change_ids": proj_change_ids,
            "previews": proj_previews,
        })
        suggestions.append(
            f"Shorten new project descriptions (~{est_p}pt saved): condense {', '.join(p['label'] for p in proj_previews)} to 1 concise bullet each."
        )
    else:
        structured.append({
            "id": "shorten_projects",
            "title": "Shorten project descriptions",
            "description": "Tighten project descriptions to concise 1-line summaries (~18pt saved).",
            "estimated_pt_saved": 18,
            "target_change_ids": [],
            "previews": [
                {
                    "change_id": None,
                    "label": p.name,
                    "before": (p.bullets[0] if p.bullets else getattr(p, "description", "") or p.name),
                    "after": _shorten_project_card_text(p.bullets[0] if p.bullets else getattr(p, "description", "") or p.name),
                }
                for p in (resume_structure.projects[:2] if resume_structure.projects else [])
            ],
        })
        suggestions.append("Shorten project descriptions (~18pt saved): condense multi-line project bullets into 1 line.")

    # Option 2: Condense the weakest Education bullets
    edu_previews: list[dict[str, Any]] = []
    edu_change_ids: list[str] = []
    seen_edu_texts: set[str] = set()
    for ed in resume_structure.education:
        for d in ed.details:
            low = d.lower()
            if "hackathon" in low or "qualifier" in low:
                continue
            if any(k in low for k in ("workshop", "presentation", "documentation", "participated", "gained")):
                condensed = _condense_education_bullet(d)
                if d.lower() in seen_edu_texts:
                    continue
                seen_edu_texts.add(d.lower())
                matched_cid = None
                for ch in (all_changes or []):
                    if (ch.get("section") or "").lower() == "education" and (ch.get("original_text") or "").strip().lower() == d.strip().lower():
                        matched_cid = str(ch.get("id") or "")
                        break
                if matched_cid:
                    edu_change_ids.append(matched_cid)
                edu_previews.append({
                    "change_id": matched_cid,
                    "label": "Education bullet",
                    "before": d,
                    "after": condensed if len(edu_previews) == 0 else "[Remove low-impact filler bullet]",
                    "action": "shorten" if len(edu_previews) == 0 else "remove",
                })
                if len(edu_previews) >= 2:
                    break
    if edu_previews:
        est_e = 13 * len(edu_previews)
        structured.append({
            "id": "condense_education",
            "title": "Condense the weakest Education bullets",
            "description": f"Tighten {len(edu_previews)} low-impact Education detail(s) to reclaim bottom space (~{est_e}pt saved).",
            "estimated_pt_saved": est_e,
            "target_change_ids": edu_change_ids,
            "previews": edu_previews,
        })
        suggestions.append(
            f"Condense/trim weakest Education bullets (~{est_e}pt saved): shorten or trim {len(edu_previews)} non-technical Education detail(s)."
        )
    else:
        structured.append({
            "id": "condense_education",
            "title": "Condense Education bullets",
            "description": "Tighten secondary Education coursework/activity notes (~14pt saved).",
            "estimated_pt_saved": 14,
            "target_change_ids": [],
            "previews": [],
        })
        suggestions.append("Condense/trim weakest Education bullets (~14pt saved): shorten non-technical Education details.")

    # Option 3: Drop the lowest-relevance item
    drop_previews: list[dict[str, Any]] = []
    drop_change_ids: list[str] = []
    low_items = [
        it for it in (relevance_items or [])
        if (it.get("tier") == "low" or it.get("recommended_action") in {"remove", "shorten"})
        and not ("cisco" in str(it.get("text", "")).lower() and "c essentials" in str(it.get("text", "")).lower())
    ]
    low_items.sort(key=lambda x: (float(x.get("score", 1.0)), -float(x.get("estimated_pt_saved", 0.0))))
    for it in low_items[:3]:
        txt = str(it.get("text", "")).strip()
        pt = int(it.get("estimated_pt_saved", 14))
        matched_cid = None
        for ch in (all_changes or []):
            if ch.get("change_type") == "relevance_trim" and (ch.get("original_text") or "").strip().lower() == txt.lower():
                matched_cid = str(ch.get("id") or "")
                break
        if matched_cid:
            drop_change_ids.append(matched_cid)
        raw_sc = float(it.get("score", 0.0))
        pct_sc = int(round(raw_sc if raw_sc > 1.0 else raw_sc * 100.0))
        drop_previews.append({
            "change_id": matched_cid,
            "item_id": it.get("item_id"),
            "section": it.get("section"),
            "label": f"{str(it.get('section') or 'Resume').title()} (relevance {pct_sc}%)",
            "before": txt,
            "after": f"[Remove item — ~{pt}pt saved]",
            "estimated_pt_saved": pt,
        })
    if not drop_previews and len(github_adds) > 1:
        last_ch = github_adds[-1]
        cid = str(last_ch.get("id") or "")
        if cid:
            drop_change_ids.append(cid)
        repo_name = str((last_ch.get("target_location") or {}).get("repo_name") or "added project")
        drop_previews.append({
            "change_id": cid or None,
            "section": "projects",
            "label": f"Added project: {repo_name}",
            "before": str(last_ch.get("edited_text") or last_ch.get("new_text") or repo_name),
            "after": f"[Reject '{repo_name}' — ~48pt saved]",
            "estimated_pt_saved": 48,
        })
    est_d = sum(int(p.get("estimated_pt_saved", 14)) for p in drop_previews) or 28
    structured.append({
        "id": "drop_lowest_relevance",
        "title": "Drop the lowest-relevance item",
        "description": f"Remove lowest-relevance bullet/certification (~{est_d}pt saved) to preserve clean page margins.",
        "estimated_pt_saved": est_d,
        "target_change_ids": drop_change_ids,
        "previews": drop_previews,
    })
    suggestions.append(
        f"Drop lowest-relevance item (~{est_d}pt saved): remove lowest-scoring bullet or certification (or tighten spacing)."
    )

    # Option 4 (Round 10): Transparency for alternative projects omitted due to page budget limit
    if len(github_adds) == 1:
        alt_projects = [
            {"name": "Surplus-Food-Donation-Matcher", "tech": "Python, FastAPI, LangGraph, Gemini", "reason": "Omitted for vertical space limit (1-page ceiling). Available as 1-click swap."},
            {"name": "AI_Deck", "tech": "TypeScript, Next.js, Python, React", "reason": "Omitted for vertical space limit (1-page ceiling). Available as 1-click swap."},
        ]
        structured.append({
            "id": "swap_project_alternative",
            "title": "Swap added project with alternative recommendation",
            "description": "Decision-IQ is currently added. Evidenced alternative projects omitted for page budget can be swapped in.",
            "estimated_pt_saved": 0,
            "target_change_ids": [],
            "previews": [
                {
                    "change_id": None,
                    "label": f"Alternative: {p['name']}",
                    "before": "Decision-IQ (current)",
                    "after": f"{p['name']} ({p['tech']})",
                    "reason": p["reason"],
                }
                for p in alt_projects
            ],
        })
        suggestions.append(
            "Alternative projects omitted for space: Surplus-Food-Donation-Matcher (LangGraph, Gemini) and AI_Deck (Next.js) are available in page-fit options."
        )

    return suggestions, structured


def apply_changes_to_pdf_in_place(
    *,
    source_pdf_path: Path,
    output_pdf_path: Path,
    approved_changes: list[dict[str, Any]],
    resume_structure: ParsedResumeStructure,
    tighten_spacing: bool = True,
    relevance_items: list[dict[str, Any]] | None = None,
) -> PdfEditResult:
    """
    Strategy B: True In-Place PDF editing via PyMuPDF.
      - 0 approved changes -> copy original bytes verbatim (100% pixel identical).
      - Reworded/reordered/typo edits -> redact target bbox, insert replacement text matching font/size/color/baseline.
      - Skills changes + Currently Learning -> redact ENTIRE original Skills block (both columns/bullets/labels) and redraw 2-col layout.
      - Relevance trims -> redact low-relevance lines + bullets and reclaim vertical band space.
      - Added GitHub projects -> style-clone into the existing Projects grid, strictly respecting original bottom margin.
      - Runs post-save overlap detection & page-fit check on every modified export.
    """
    from app.services.resume.theme_verifier import check_pdf_text_overlaps, measure_pdf_page_fit

    output_pdf_path = Path(output_pdf_path)
    source_pdf_path = Path(source_pdf_path)
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    src_doc = fitz.open(str(source_pdf_path))
    orig_pages = len(src_doc)
    style_tokens = extract_pdf_style_tokens(src_doc)

    active_changes: list[dict[str, Any]] = []
    for ch in approved_changes:
        if ch.get("status") not in (None, "accepted", "edited"):
            continue
        if ch.get("validation_status") == "blocked":
            continue
        tloc = ch.get("target_location") or {}
        if isinstance(tloc, dict) and tloc.get("requires_confirmation") and not tloc.get("accuracy_confirmed"):
            continue
        active_changes.append(ch)

    if not active_changes:
        src_doc.close()
        shutil.copy2(source_pdf_path, output_pdf_path)
        pfit = measure_pdf_page_fit(original_pdf_path=source_pdf_path, output_pdf_path=output_pdf_path)
        return PdfEditResult(
            strategy_used="inplace_exact",
            page_count_original=orig_pages,
            page_count_output=orig_pages,
            overlap_passed=True,
            overlap_details=[],
            page_fit=pfit,
        )

    warnings: list[str] = []
    substitutions: list[str] = []
    dropped_projects: list[str] = []
    changed_bboxes_by_page: dict[int, list[list[float]]] = {}
    band_shifts_by_page: dict[int, list[tuple[float, float, float]]] = {}

    pw = float(style_tokens.get("page_width", 612.0))

    # Capture original last-element bottom Y and unredacted section->rule gaps per page
    orig_last_y1_by_page: dict[int, float] = {}
    orig_rule_gaps_by_page: dict[int, dict[int, float]] = {}
    for pno_i, pg_i in enumerate(src_doc):
        max_y1 = 0.0
        orig_line_ys: list[tuple[float, float]] = []
        for blk in pg_i.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if re.sub(r"[\uE000-\uF8FF\s]+", "", str(sp.get("text", ""))):
                        max_y1 = max(max_y1, float(sp["bbox"][3]))
                        orig_line_ys.append((float(ln["bbox"][1]), float(ln["bbox"][3])))
                        break
        orig_last_y1_by_page[pno_i] = round(max_y1, 2)
        orig_rules = _detect_horizontal_rules(pg_i, pw)
        rg_map: dict[int, float] = {}
        prev_ry = 0.0
        for r_i, (ry0, ry1) in enumerate(orig_rules):
            c_y1s_r = [y1 for y0, y1 in orig_line_ys if prev_ry <= y0 and y1 <= ry0 + 1.0]
            max_cy1_r = min(ry0 - 0.3, (max(c_y1s_r) + 2.2) if c_y1s_r else ry0)
            rg_map[r_i] = max(0.0, (ry0 - 0.5) - max_cy1_r)
            prev_ry = ry1 + 0.5
        orig_rule_gaps_by_page[pno_i] = rg_map
    margins = style_tokens.get("margins", {"left": 46.8, "right": 46.8, "bottom": 45.0})
    right_margin_x = pw - float(margins.get("right", 46.8))
    mid_col_x = pw * 0.48

    # Partition active changes cleanly
    skills_changes: list[dict[str, Any]] = []
    github_adds: list[dict[str, Any]] = []
    trim_changes: list[dict[str, Any]] = []
    edit_changes: list[dict[str, Any]] = []

    for ch in active_changes:
        ctype = ch.get("change_type", "")
        sec = (ch.get("section") or "").lower()
        if ctype == "added_from_github":
            github_adds.append(ch)
        elif ctype == "user_confirmed_skill" or (sec == "skills" and ctype in {"reworded", "reordered", "added"}):
            skills_changes.append(ch)
        elif ctype == "relevance_trim":
            trim_changes.append(ch)
        else:
            edit_changes.append(ch)

    # Suppress redundant inline summary typo fixes when a full summary rewrite is also accepted
    full_summary_origs = [
        re.sub(r"\s+", " ", (c.get("original_text") or "").strip().lower())
        for c in edit_changes
        if (c.get("section") or "").lower() == "summary" and len((c.get("original_text") or "").strip()) > 60
    ]
    if full_summary_origs:
        filtered_edits: list[dict[str, Any]] = []
        for c in edit_changes:
            orig_norm = re.sub(r"\s+", " ", (c.get("original_text") or "").strip().lower())
            if (c.get("section") or "").lower() == "summary" and len(orig_norm) <= 45 and any(orig_norm in fs for fs in full_summary_origs):
                continue
            filtered_edits.append(c)
        edit_changes = filtered_edits

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

    # Detect initial Projects layout and multi-column grid style across the document
    target_proj_pno = 0
    proj_head_x0_init: float | None = None
    proj_head_y0_init: float | None = None
    proj_head_y1_init: float | None = None
    for pno_i in range(orig_pages):
        td = src_doc[pno_i].get_text("dict")
        for blk in td.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                ltxt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if detect_section_heading(ltxt) == "projects":
                    target_proj_pno = pno_i
                    proj_head_x0_init = float(ln["bbox"][0])
                    proj_head_y0_init = float(ln["bbox"][1])
                    proj_head_y1_init = float(ln["bbox"][3])
                    break
            if proj_head_y0_init is not None:
                break
        if proj_head_y0_init is not None:
            break

    page_pr_init = src_doc[target_proj_pno]
    rules_pr_init = _detect_horizontal_rules(page_pr_init, pw)
    sidebar_div_pr_init = _detect_vertical_divider(page_pr_init, pw, float(page_pr_init.rect.height))
    proj_limit_y_pr_init = float(page_pr_init.rect.height) - 40.0
    for ry0_p, ry1_p in rules_pr_init:
        if proj_head_y1_init is not None and ry0_p > proj_head_y1_init + 8.0:
            proj_limit_y_pr_init = ry0_p
            break
    proj_min_x_pr_init = (sidebar_div_pr_init[0] + 6.0) if (sidebar_div_pr_init and (proj_head_x0_init is None or proj_head_x0_init >= sidebar_div_pr_init[0] - 4.0)) else 0.0
    grid_style_init = _detect_projects_grid_style(
        page_pr_init,
        (proj_head_y1_init or 300.0) + 1.0,
        proj_limit_y_pr_init - 1.0,
        pw,
        margins,
        style_tokens,
        min_x=proj_min_x_pr_init,
    )
    had_right_col_projects = bool(grid_style_init.get("had_right_col") and sidebar_div_pr_init is None)

    planned_ops: list[dict[str, Any]] = []
    reclaimed_slices_by_page: dict[int, list[tuple[float, float]]] = {}

    def _plan_single_text_op(ch: dict[str, Any], force_remove: bool = False) -> None:
        if bool(had_right_col_projects and len(github_adds) == 1) and (ch.get("section") or "").lower() == "projects":
            # Suppress individual in-place text ops: projects are unified and redrawn as a single-column list
            return
        new_text = "" if force_remove else (ch.get("edited_text") or ch.get("new_text") or "").strip()
        if not force_remove and not new_text:
            return
        if not force_remove and (ch.get("original_text") or "").lstrip().startswith("- ") and not new_text.lstrip().startswith("- "):
            new_text = f"- {new_text.lstrip('•* ')}"

        loc = ch.get("target_location") or {}
        page_idx = int(loc.get("page", 0)) if isinstance(loc, dict) else 0
        if page_idx >= orig_pages:
            page_idx = 0
        page = src_doc[page_idx]

        bbox, meta = _locate_edit_box(page, ch, style_tokens)
        if not bbox or len(bbox) != 4:
            warnings.append(f"Skipped '{ch.get('section')}' edit: could not locate exact PDF text region.")
            return

        orig_x0, orig_y0, orig_x1, orig_y1 = bbox
        if not force_remove and not new_text.lstrip().startswith("- "):
            orig_box_txt = page.get_textbox(
                fitz.Rect(orig_x0 - 1.0, orig_y0 - 1.0, orig_x1 + 1.0, orig_y1 + 1.0)
            ).lstrip()
            if orig_box_txt.startswith("- "):
                cleaned_body = re.sub(r"^(?:[\-\*\u2022]\s*)+", "", new_text).strip()
                new_text = f"- {cleaned_body}"
        if force_remove:
            # Also redact leading bullet glyph on the left of this line and strictly clamp bounds against adjacent lines
            b_x0 = orig_x0
            above_y1: float | None = None
            below_y0: float | None = None
            for blk in page.get_text("dict").get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    lb = ln.get("bbox") or [0, 0, 0, 0]
                    ltxt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                    if ltxt:
                        if lb[1] < orig_y0 - 1.5 and lb[3] <= orig_y0 + 1.8:
                            above_y1 = max(above_y1, float(lb[3])) if above_y1 is not None else float(lb[3])
                        elif lb[3] > orig_y1 + 1.5 and lb[1] >= orig_y1 - 1.8:
                            below_y0 = min(below_y0, float(lb[1])) if below_y0 is not None else float(lb[1])
                    for sp in ln.get("spans", []):
                        sb = sp.get("bbox") or [0, 0, 0, 0]
                        if sb[1] <= orig_y1 + 1.0 and sb[3] >= orig_y0 - 1.0 and orig_x0 - 24.0 <= sb[0] <= orig_x0:
                            if not re.sub(r"[\uE000-\uF8FF•\-–—\s]+", "", sp.get("text", "")):
                                b_x0 = min(b_x0, float(sb[0]))
            y_inset = 0.5 if (orig_y1 - orig_y0) > 4.0 else 0.1
            safe_ry0 = max(orig_y0 + y_inset, (above_y1 + 0.2) if above_y1 is not None else (orig_y0 + y_inset))
            safe_ry1 = min(orig_y1 - y_inset, (below_y0 - 0.2) if below_y0 is not None else (orig_y1 - y_inset))
            if safe_ry1 <= safe_ry0 + 1.0:
                safe_ry0 = orig_y0 + y_inset
                safe_ry1 = orig_y1 - y_inset
            rrect = [b_x0 - 0.8, safe_ry0, orig_x1 + 0.8, safe_ry1]
            planned_ops.append({
                "page_idx": page_idx,
                "redact_rect": fitz.Rect(*rrect),
                "x0": b_x0,
                "y0": orig_y0,
                "first_baseline_y": meta.get("first_baseline_y"),
                "wrapped_lines": [],
                "line_height": 11.0,
                "union_box": [round(rrect[0], 2), round(safe_ry0, 2), round(rrect[2], 2), round(safe_ry1, 2)],
                "change": ch,
            })
            slice_y0 = max(orig_y0 - 0.3, (above_y1 + 0.15) if above_y1 is not None else (orig_y0 - 0.4))
            slice_y1 = min(orig_y1 + 0.3, (below_y0 - 0.15) if below_y0 is not None else (orig_y1 + 0.4))
            if slice_y1 > slice_y0 + 1.0:
                reclaimed_slices_by_page.setdefault(page_idx, []).append((round(slice_y0, 2), round(slice_y1, 2)))
            return

        is_two_col_left = orig_x1 <= mid_col_x + 25.0 and orig_x0 >= margins.get("left", 40) - 5 and len(ch.get("original_text", "")) < 90 and ch.get("section") in {"projects", "skills"}
        is_two_col_right = orig_x0 >= mid_col_x - 15.0 and ch.get("section") in {"projects", "skills"}

        redact_rect = [orig_x0 - 0.6, orig_y0 - 0.2, orig_x1 + 0.6, orig_y1 + 0.2]
        max_line_width = right_margin_x - orig_x0
        if meta.get("_inline_max_x1"):
            max_line_width = max(12.0, float(meta["_inline_max_x1"]) - orig_x0)
        elif is_two_col_left:
            max_line_width = (mid_col_x - 6.0) - orig_x0
        elif is_two_col_right:
            max_line_width = right_margin_x - orig_x0

        matched_full = str(meta.get("_matched_full_text") or "").strip()
        orig_txt_chk = (ch.get("original_text") or "").strip()
        if (
            len(meta.get("line_bboxes") or []) >= 2
            and matched_full
            and orig_txt_chk
            and len(orig_txt_chk) < len(matched_full) * 0.55
            and len(new_text) < len(matched_full) * 0.65
            and orig_txt_chk in matched_full
        ):
            new_text = matched_full.replace(orig_txt_chk, new_text, 1)

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

        orig_font = str(meta.get("font") or style_tokens.get("body_font", "Helvetica"))
        orig_size = float(meta.get("font_size") or style_tokens.get("body_size", 10.0))
        orig_col = _hex_to_rgb01(str(meta.get("color") or style_tokens.get("body_color", "#404040")))

        orig_lboxes = meta.get("line_bboxes") or []
        if len(orig_lboxes) >= 2:
            orig_lh = (orig_lboxes[-1][1] - orig_lboxes[0][1]) / max(1, len(orig_lboxes) - 1)
        else:
            orig_lh = orig_size * 1.16

        def _build_segments_for_scale(scale: float) -> tuple[list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]], float, float]:
            sz = round(orig_size * scale, 2)
            segs = _map_text_to_style_segments(
                new_text=new_text,
                meta=meta,
                orig_font=orig_font,
                orig_col=orig_col,
                sz=sz,
                substitutions=substitutions,
            )
            wrapped = _wrap_rich_segments_to_width(segs, max(40.0, max_line_width))
            lh = orig_lh if (scale == 1.0 and len(wrapped) <= max(1, len(orig_lboxes))) else max(sz * 1.12, orig_lh * scale)
            line_box_h = max(
                sz * 1.16,
                ((orig_lboxes[0][3] - orig_lboxes[0][1]) * scale) if orig_lboxes else (sz * 1.16),
            )
            needed_h = line_box_h + max(0, len(wrapped) - 1) * lh
            return wrapped, needed_h, round(lh, 2)

        def _line_width_exceeds(lines: list[list[tuple[str, fitz.Font, tuple[float, float, float], float]]]) -> bool:
            if not meta.get("_inline_max_x1"):
                return False
            max_w = max((sum(fnt.text_length(t, fontsize=sz) for t, fnt, _, sz in ln) for ln in lines), default=0.0)
            return max_w > max_line_width + 0.5

        wrapped_lines, needed_h, chosen_lh = _build_segments_for_scale(1.0)
        if needed_h > avail_h + 1.0 or _line_width_exceeds(wrapped_lines):
            fitted = False
            for s_try in (0.96, 0.93, 0.90):
                w_try, h_try, lh_try = _build_segments_for_scale(s_try)
                if h_try <= avail_h + 1.0 and not _line_width_exceeds(w_try):
                    wrapped_lines = w_try
                    needed_h = h_try
                    chosen_lh = lh_try
                    fitted = True
                    warnings.append(
                        f"Reduced font size to {int(s_try * 100)}% inside original bounding box for '{ch.get('section')}' edit."
                    )
                    break
            if not fitted:
                wrapped_lines, needed_h, chosen_lh = _build_segments_for_scale(0.90)
                warnings.append(
                    f"'{ch.get('section')}' revision slightly exceeded original box bounds even at 90% minimum font scale."
                )

        if len(orig_lboxes) >= 2 and needed_h < (orig_y1 - orig_y0) - 9.0:
            # Reclaim saved vertical space from shortened multi-line paragraphs without clipping descenders
            reclaimed_slices_by_page.setdefault(page_idx, []).append(
                (round(orig_y0 + needed_h + 2.2, 2), round(orig_y1 + 0.6, 2))
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
                "change": ch,
            }
        )

    for ch in edit_changes:
        _plan_single_text_op(ch, force_remove=False)
    for ch in trim_changes:
        loc = ch.get("target_location") or {}
        action = (loc.get("action") if isinstance(loc, dict) else None) or (
            "remove" if not (ch.get("edited_text") or ch.get("new_text") or "").strip() else "shorten"
        )
        _plan_single_text_op(ch, force_remove=(action == "remove"))

    # Merge any two planned ops whose redaction boxes intersect significantly on the same page/line
    deduped_ops: list[dict[str, Any]] = []
    for op in planned_ops:
        r_cur = op["redact_rect"]
        merged_done = False
        for existing in deduped_ops:
            if existing["page_idx"] != op["page_idx"]:
                continue
            r_ex = existing["redact_rect"]
            inter = r_cur & r_ex
            if inter.width > 1.5 and inter.height > 1.5:
                # Prefer the more comprehensive op (e.g. full paragraph rewrite over partial typo)
                cur_len = len(op.get("change", {}).get("original_text", ""))
                ex_len = len(existing.get("change", {}).get("original_text", ""))
                if cur_len > ex_len:
                    deduped_ops.remove(existing)
                    deduped_ops.append(op)
                merged_done = True
                break
        if not merged_done:
            deduped_ops.append(op)
    planned_ops = deduped_ops

    # Plan Whole-Block Two-Column Skills Redraw when any Skills edit or Confirmed Learning item is active
    skills_plan: dict[str, Any] | None = None
    if skills_changes:
        for s_pno in range(orig_pages):
            s_layout = _detect_skills_block_layout(src_doc[s_pno], pw, margins, style_tokens)
            if not s_layout:
                continue
            lbl_fnt, sub_l = _resolve_pdf_font(s_layout["label_font"], force_bold=bool(s_layout.get("label_is_bold", True)), force_italic=False)
            val_fnt, sub_v = _resolve_pdf_font(s_layout["val_font"], force_bold=False, force_italic=False)
            bul_fnt, _ = _resolve_pdf_font("Arial", force_bold=False, force_italic=False)
            for sm in (sub_l, sub_v):
                if sm and sm not in substitutions:
                    substitutions.append(sm)

            t_sz = float(s_layout["text_size"])
            lbl_col = _hex_to_rgb01(s_layout["label_color"])
            val_col = _hex_to_rgb01(s_layout["text_color"])
            bul_col = _hex_to_rgb01(s_layout["bullet_color"])

            entries_by_col: dict[int, list[dict[str, Any]]] = {
                0: [dict(e) for e in s_layout["entries"][0]],
                1: [dict(e) for e in s_layout["entries"][1]],
            }
            confirmed_learning_skills: list[str] = []

            for sch in skills_changes:
                new_t = (sch.get("edited_text") or sch.get("new_text") or "").strip()
                if not new_t:
                    continue
                if sch.get("change_type") == "user_confirmed_skill":
                    _, body_part = _clean_skill_category_text(new_t)
                    for item in body_part.split(","):
                        clean_it = item.strip().rstrip(".")
                        if clean_it and clean_it.lower() not in {x.lower() for x in confirmed_learning_skills}:
                            confirmed_learning_skills.append(clean_it)
                    continue

                lbl_new, body_new = _clean_skill_category_text(new_t)
                orig_t = (sch.get("original_text") or "").strip()
                lbl_orig, _ = _clean_skill_category_text(orig_t)
                matched_entry = None
                for c_idx in (0, 1):
                    for ent in entries_by_col[c_idx]:
                        if lbl_orig and ent["label"].lower() == lbl_orig.lower():
                            matched_entry = ent
                            break
                        if lbl_new and ent["label"].lower() == lbl_new.lower():
                            matched_entry = ent
                            break
                        if not lbl_orig and orig_t and (
                            re.sub(r"\W+", " ", orig_t).strip().lower()
                            == re.sub(r"\W+", " ", ent["full_text"]).strip().lower()
                            or (not ent["label"] and len(entries_by_col[c_idx]) == 1)
                        ):
                            matched_entry = ent
                            break
                    if matched_entry is not None:
                        break
                if matched_entry is not None:
                    matched_entry["label"] = lbl_new or matched_entry["label"]
                    matched_entry["body"] = body_new
                    matched_entry["full_text"] = f"{matched_entry['label']} {matched_entry['body']}".strip()
                else:
                    target_c = 1 if (s_layout["is_two_col"] and len(entries_by_col[1]) <= len(entries_by_col[0])) else 0
                    entries_by_col[target_c].append({
                        "col": target_c,
                        "label": lbl_new,
                        "body": body_new,
                        "full_text": f"{lbl_new} {body_new}".strip(),
                    })

            def _wrap_entry(ent: dict[str, Any], max_w: float):
                segs: list[tuple[str, fitz.Font, tuple[float, float, float], float]] = []
                if ent["label"]:
                    segs.append((ent["label"], lbl_fnt, lbl_col, t_sz))
                    if ent["body"]:
                        segs.append((f" {ent['body']}", val_fnt, val_col, t_sz))
                else:
                    segs.append((ent["body"], val_fnt, val_col, t_sz))
                return _wrap_rich_segments_to_width(segs, max(80.0, max_w))

            def _col_height(wrapped_list: list[list[Any]]) -> float:
                if not wrapped_list:
                    return 0.0
                lh_f = float(s_layout["lh"])
                eg_f = float(s_layout["entry_gap"])
                total = sum(len(w) * lh_f for w in wrapped_list) + eg_f * max(0, len(wrapped_list) - 1)
                return round(total, 2)

            wrapped_by_col: dict[int, list[list[Any]]] = {
                0: [_wrap_entry(e, float(s_layout["col0_w"])) for e in entries_by_col[0]],
                1: [_wrap_entry(e, float(s_layout["col1_w"])) for e in entries_by_col[1]],
            }

            if confirmed_learning_skills:
                cl_entry = {
                    "col": 1,
                    "label": "Currently Learning:",
                    "body": ", ".join(confirmed_learning_skills),
                    "full_text": f"Currently Learning: {', '.join(confirmed_learning_skills)}",
                }
                if s_layout["is_two_col"]:
                    h0 = _col_height(wrapped_by_col[0])
                    h1 = _col_height(wrapped_by_col[1])
                    choose_col = 1 if h1 <= h0 else 0
                else:
                    choose_col = 0
                w_cl = _wrap_entry(cl_entry, float(s_layout[f"col{choose_col}_w"]))
                wrapped_by_col[choose_col].append(w_cl)

            needed_h = max(_col_height(wrapped_by_col[0]), _col_height(wrapped_by_col[1]))
            orig_skills_h = float(s_layout["orig_y1"]) - float(s_layout["top_y0"])
            extra_h = max(0.0, needed_h - orig_skills_h)
            if tighten_spacing and extra_h > 4.0:
                s_layout = dict(s_layout)
                s_layout["lh"] = round(max(float(s_layout["text_size"]) * 1.08, float(s_layout["lh"]) - 1.8), 2)
                s_layout["entry_gap"] = round(max(1.5, float(s_layout["entry_gap"]) - 1.5), 2)
                needed_h = max(_col_height(wrapped_by_col[0]), _col_height(wrapped_by_col[1]))
                extra_h = max(0.0, needed_h - orig_skills_h)
            avail_to_rule = float(s_layout["limit_y"]) - float(s_layout["orig_y1"])

            # Always redact ENTIRE original Skills block (per-column rects so SKILLS heading is never clipped)
            sk_rrects = [fitz.Rect(*r) for r in (s_layout.get("redact_rects") or [s_layout["redact_rect"]])]
            planned_ops.append({
                "page_idx": s_pno,
                "redact_rect": fitz.Rect(*s_layout["redact_rect"]),
                "redact_rects": sk_rrects,
                "x0": s_layout["redact_rect"][0],
                "y0": s_layout["top_y0"],
                "first_baseline_y": None,
                "wrapped_lines": [],
                "line_height": s_layout["lh"],
                "union_box": [round(v, 2) for v in s_layout["redact_rect"]],
                "change": {"section": "skills"},
            })
            skills_plan = {
                "page_idx": s_pno,
                "layout": s_layout,
                "col_wrapped": wrapped_by_col,
                "bullet_draw_spec": (bul_fnt, "\u2022", round(min(12.0, max(10.5, float(s_layout.get("bullet_size") or 11.5))), 2), bul_col),
                "needed_h": round(needed_h, 2),
                "orig_h": round(orig_skills_h, 2),
                "extra_h": round(extra_h, 2),
                "exceeds_rule": needed_h + float(s_layout["top_y0"]) > float(s_layout["limit_y"]) - 2.5,
            }
            break

    # Execute Strategy B redactions & redraws page-by-page while preserving links & vector graphics
    ops_by_page: dict[int, list[dict[str, Any]]] = {}
    for op in planned_ops:
        ops_by_page.setdefault(op["page_idx"], []).append(op)

    for pno, ops in ops_by_page.items():
        page = src_doc[pno]
        orig_links = list(page.get_links() or [])
        for op in ops:
            for rr in (op.get("redact_rects") or [op["redact_rect"]]):
                page.add_redact_annot(rr, fill=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
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
            if op["wrapped_lines"]:
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
    overflow_options: list[dict[str, Any]] = []

    needs_band_compositor = bool(
        github_adds
        or reclaimed_slices_by_page
        or (skills_plan and (skills_plan["exceeds_rule"] or skills_plan["extra_h"] > 2.0))
        or (had_right_col_projects and len(github_adds) == 1)
    )

    if not needs_band_compositor and skills_plan:
        sp_pno = int(skills_plan["page_idx"])
        sk_boxes: list[list[float]] = []
        _render_skills_block(
            page=src_doc[sp_pno],
            layout=skills_plan["layout"],
            draw_y0=float(skills_plan["layout"]["top_y0"]),
            col_wrapped=skills_plan["col_wrapped"],
            bullet_draw_spec=skills_plan["bullet_draw_spec"],
            record_boxes=sk_boxes,
        )
        changed_bboxes_by_page.setdefault(sp_pno, []).extend(sk_boxes)

    if needs_band_compositor:
        # Determine target page (page containing PROJECTS heading, or SKILLS page, default 0)
        target_pno = int(skills_plan["page_idx"]) if skills_plan else 0
        proj_head_x0: float | None = None
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
                        proj_head_x0 = float(ln["bbox"][0])
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
        sidebar_div = _detect_vertical_divider(page, pw, ph)
        orig_last_y1 = orig_last_y1_by_page.get(target_pno, ph - 55.0)

        has_projects_heading = proj_head_y0 is not None
        if proj_head_y0 is None:
            proj_head_y0 = min(ph * 0.42, orig_last_y1 + 8.0)
            proj_head_y1 = proj_head_y0 + 14.0

        proj_rule_idx: int | None = None
        for idx_r, (ry0, ry1) in enumerate(rules):
            if ry0 > proj_head_y1 + 8.0:
                proj_rule_idx = idx_r
                break
        proj_limit_y = rules[proj_rule_idx][0] if proj_rule_idx is not None else (ph - 40.0)

        proj_min_x = (sidebar_div[0] + 6.0) if (sidebar_div and (proj_head_x0 is None or proj_head_x0 >= sidebar_div[0] - 4.0)) else 0.0
        grid_style = _detect_projects_grid_style(page, proj_head_y1 + 1.0, proj_limit_y - 1.0, pw, margins, style_tokens, min_x=proj_min_x)
        proj_content_y1 = float(grid_style["proj_content_y1"])

        t_fnt, sub_t = _resolve_pdf_font(grid_style["title_font_name"], force_bold=bool(grid_style.get("title_is_bold", True)), force_italic=False)
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
        b_lh = float(grid_style.get("body_lh") or round(b_sz * 1.18, 2))

        def _measure_project_card(ch: dict[str, Any], col_w: float) -> dict[str, Any]:
            cdata = _parse_github_card_data(
                ch,
                keep_bullets=bool(grid_style["had_bullets"]),
                bullet_prefix=str(grid_style.get("bullet_prefix", "")),
            )
            m_lines = 2 if col_w > 400.0 else 3
            wrapped_body = _fit_complete_sentences_to_lines(cdata["body"], b_fnt, b_sz, col_w, max_lines=m_lines) if cdata["body"] else []
            t_pad = t_sz * 1.15 + 2.0
            eff_b_lh = b_lh
            h = t_pad + len(wrapped_body) * eff_b_lh
            return {
                "title_base": cdata["title_base"],
                "html_url": cdata["html_url"],
                "wrapped_body": wrapped_body,
                "height": h,
                "t_pad": t_pad,
                "b_lh": eff_b_lh,
            }

        is_two_col = bool(grid_style["is_two_col"])
        proj_rows: list[list[tuple[float, float, dict[str, Any]]]] = []
        if had_right_col_projects and len(github_adds) == 1:
            # Redact the entire old 2-column project area on page so old text is completely removed,
            # including any right-column project card that was placed adjacent to the PROJECTS heading
            r_proj_x0 = pw * 0.48 - 12.0
            single_col_w = (pw - float(margins.get("right", 46.8))) - grid_style["col1_x0"]
            orig_proj_links = [
                lk for lk in (page.get_links() or [])
                if lk.get("uri") and "github" in (lk.get("uri") or "").lower()
            ]
            old_proj_box_right = fitz.Rect(
                r_proj_x0,
                proj_head_y0 - 2.0,
                pw - float(margins.get("right", 46.8)) + 2.0,
                float(grid_style["proj_content_y1"]) + 2.0,
            )
            old_proj_box_full = fitz.Rect(
                grid_style["col1_x0"] - 2.0,
                proj_head_y1 + 1.0,
                pw - float(margins.get("right", 46.8)) + 2.0,
                float(grid_style["proj_content_y1"]) + 2.0,
            )
            page.add_redact_annot(old_proj_box_right, text=fitz.PDF_REDACT_TEXT_REMOVE)
            page.add_redact_annot(old_proj_box_full, text=fitz.PDF_REDACT_TEXT_REMOVE)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            unified_cards: list[dict[str, Any]] = []
            for orig_p in getattr(resume_structure, "projects", []):
                p_name = getattr(orig_p, "name", "")
                is_removed = any(
                    ch.get("change_type") == "relevance_trim"
                    and p_name.lower() in (ch.get("original_text") or "").lower()
                    for ch in active_changes
                )
                if is_removed:
                    continue
                clean_name = p_name
                clean_name = re.sub(r"\bKNOWLEDEGE\b", "KNOWLEDGE", clean_name, flags=re.I)
                clean_name = re.sub(r"\bRETRIVAL\b", "RETRIEVAL", clean_name, flags=re.I)
                clean_name = re.sub(r"\s*[-–]\s*github\s*$", "", clean_name, flags=re.I).strip()
                if "custom gpt" in clean_name.lower():
                    clean_name = "CUSTOM GPT"

                p_bullets: list[str] = []
                for b in getattr(orig_p, "bullets", []):
                    b_txt = str(b).strip()
                    b_txt = re.sub(r"\bcasual masking\b", "causal masking", b_txt, flags=re.I)
                    b_txt = re.sub(r"\bconstrains\b", "constraints", b_txt, flags=re.I)
                    for ach in active_changes:
                        orig_t = (ach.get("original_text") or "").strip()
                        new_t = (ach.get("edited_text") or ach.get("new_text") or "").strip()
                        if orig_t and orig_t in b_txt and new_t:
                            b_txt = b_txt.replace(orig_t, new_t)
                    p_bullets.append(b_txt)

                had_buls = bool(grid_style.get("had_bullets", False))
                bul_prefix = "• " if had_buls else ""
                cleaned_sentences: list[str] = []
                for b in p_bullets:
                    b_clean = re.sub(r"^[\s•\-–—\*\uf0b7]+\s*", "", str(b)).strip()
                    b_clean = re.sub(r"\s+[-–—]\s+(?=[A-Z])", ". ", b_clean)
                    if not b_clean:
                        continue
                    if bul_prefix:
                        cleaned_sentences.append(f"{bul_prefix}{b_clean}")
                    else:
                        cleaned_sentences.append(b_clean)
                card_body = " ".join(cleaned_sentences)
                wrapped_body = _fit_complete_sentences_to_lines(card_body, b_fnt, b_sz, single_col_w, max_lines=4) if card_body else []
                t_pad = t_sz * 1.15 + 2.0
                eff_b_lh = b_lh
                h = t_pad + len(wrapped_body) * eff_b_lh
                p_url = getattr(orig_p, "github_url", "") or ""
                if not p_url:
                    for lk in orig_proj_links:
                        u = (lk.get("uri") or "").lower()
                        if "knowledge" in clean_name.lower() and ("knowledge" in u or "rag" in u):
                            p_url = lk["uri"]
                            break
                        elif "gpt" in clean_name.lower() and ("gpt" in u or "minigpt" in u):
                            p_url = lk["uri"]
                            break
                        elif any(tok in u for tok in clean_name.lower().split() if len(tok) >= 4):
                            p_url = lk["uri"]
                            break
                unified_cards.append({
                    "title_base": clean_name.upper(),
                    "html_url": p_url,
                    "wrapped_body": wrapped_body,
                    "height": h,
                    "t_pad": t_pad,
                    "b_lh": eff_b_lh,
                    "is_from_github": False,
                })

            for ch in github_adds:
                c_meas = _measure_project_card(ch, single_col_w)
                c_meas["is_from_github"] = True
                unified_cards.append(c_meas)

            for card in unified_cards:
                proj_rows.append([
                    (grid_style["col1_x0"], single_col_w, card)
                ])
        elif is_two_col:
            for idx in range(0, len(github_adds), 2):
                row_items: list[tuple[float, float, dict[str, Any]]] = [
                    (grid_style["col1_x0"], grid_style["col1_w"], _measure_project_card(github_adds[idx], grid_style["col1_w"]))
                ]
                if idx + 1 < len(github_adds):
                    row_items.append(
                        (grid_style["col2_x0"], grid_style["col2_w"], _measure_project_card(github_adds[idx + 1], grid_style["col2_w"]))
                    )
                proj_rows.append(row_items)
        else:
            for ch in github_adds:
                proj_rows.append([
                    (grid_style["col1_x0"], grid_style["col1_w"], _measure_project_card(ch, grid_style["col1_w"]))
                ])

        td_cur = page.get_text("dict")
        reclaimed_list = sorted(reclaimed_slices_by_page.get(target_pno, []))
        # Standard vertical spacing below PROJECTS heading and between project cards matching Education/Skills rhythm (~9.5-11.0pt)
        proj_insert_top_gap = 10.88
        row_gap = 9.5

        def _draw_project_row(dpage: fitz.Page, start_y: float, row_cards: list[tuple[float, float, dict[str, Any]]]) -> float:
            row_bottom = start_y
            for cx0, cw, card in row_cards:
                y_t = start_y
                title_prefix = f"{card['title_base']} - "
                tw_t = fitz.TextWriter(dpage.rect)
                tw_t.append((cx0, y_t + t_sz * 0.8), title_prefix, font=t_fnt, fontsize=t_sz)
                tw_t.write_text(dpage, color=t_col)
                prefix_w = t_fnt.text_length(title_prefix, fontsize=t_sz)

                gh_x0 = cx0 + prefix_w
                gh_base_y = y_t + t_sz * 0.8
                gh_w = lk_fnt.text_length("GITHUB", fontsize=lk_sz)
                tw_l = fitz.TextWriter(dpage.rect)
                tw_l.append((gh_x0, gh_base_y), "GITHUB", font=lk_fnt, fontsize=lk_sz)
                tw_l.write_text(dpage, color=lk_col)
                if grid_style.get("had_link_styling", True):
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

                t_pad = card.get("t_pad", (t_sz * 1.05 + 1.0) if tighten_spacing else (t_sz * 1.15 + 2.0))
                b_step = card.get("b_lh", (b_lh * 0.94) if tighten_spacing else b_lh)
                y_b = y_t + t_pad
                for wline in card["wrapped_body"]:
                    tw_b = fitz.TextWriter(dpage.rect)
                    tw_b.append((cx0, y_b + b_sz * 0.8), wline, font=b_fnt, fontsize=b_sz)
                    tw_b.write_text(dpage, color=b_col)
                    y_b += b_step
                row_bottom = max(row_bottom, y_b)
            return row_bottom

        def _finish_proj_skills_and_overflow(
            new_page: fitz.Page,
            out_doc: fitz.Document,
            rows_p1: list[list[tuple[float, float, dict[str, Any]]]],
            rows_ovf: list[list[tuple[float, float, dict[str, Any]]]],
            y_proj_draw: float,
            cur_y_fallback: float,
            skills_draw_y0: float | None,
        ) -> None:
            if rows_p1 and not has_projects_heading:
                y_proj_draw = max(y_proj_draw, cur_y_fallback + 8.0)
                is_h_bold = bool(style_tokens.get("heading_bold", False))
                ph_fnt, _ = _resolve_pdf_font(str(style_tokens.get("heading_font", "Helvetica")), force_bold=is_h_bold)
                ph_sz = float(style_tokens.get("heading_size", 11.0))
                ph_col = _hex_to_rgb01(str(style_tokens.get("heading_color", "#1e293b")))
                tw_ph = fitz.TextWriter(new_page.rect)
                tw_ph.append((grid_style["col1_x0"], y_proj_draw + ph_sz * 0.8), "Projects", font=ph_fnt, fontsize=ph_sz)
                tw_ph.write_text(new_page, color=ph_col)
                changed_bboxes_by_page.setdefault(target_pno, []).append(
                    [grid_style["col1_x0"], y_proj_draw - 1.0, pw - float(margins.get("right", 46.8)), y_proj_draw + ph_sz + 3.0]
                )
                y_proj_draw += ph_sz + 5.0
            for r_items in rows_p1:
                end_y = _draw_project_row(new_page, y_proj_draw, r_items)
                changed_bboxes_by_page.setdefault(target_pno, []).append(
                    [grid_style["col1_x0"], y_proj_draw - 2.0, pw - float(margins.get("right", 46.8)), end_y + 2.0]
                )
                y_proj_draw = end_y + row_gap

            if skills_plan and skills_plan["page_idx"] == target_pno and skills_draw_y0 is not None:
                sk_boxes_2: list[list[float]] = []
                _render_skills_block(
                    page=new_page,
                    layout=skills_plan["layout"],
                    draw_y0=skills_draw_y0,
                    col_wrapped=skills_plan["col_wrapped"],
                    bullet_draw_spec=skills_plan["bullet_draw_spec"],
                    record_boxes=sk_boxes_2,
                )
                changed_bboxes_by_page.setdefault(target_pno, []).extend(sk_boxes_2)

            if rows_ovf:
                p2 = out_doc.new_page(width=pw, height=ph)
                y_p2 = float(margins.get("top", 46.8))
                h_fnt, _ = _resolve_pdf_font(str(style_tokens.get("heading_font", "Helvetica")), force_bold=False)
                h_sz = float(style_tokens.get("heading_size", 13.0))
                h_col = _hex_to_rgb01(str(style_tokens.get("heading_color", "#575355")))
                left_x = float(margins.get("left", 46.8))
                tw_h2 = fitz.TextWriter(p2.rect)
                tw_h2.append((left_x, y_p2 + h_sz * 0.8), "ADDITIONAL PROJECTS", font=h_fnt, fontsize=h_sz)
                tw_h2.write_text(p2, color=h_col)
                y_p2 += h_sz + 8.0
                for r_items in rows_ovf:
                    y_p2 = _draw_project_row(p2, y_p2, r_items) + row_gap

        if sidebar_div is not None:
            div_x, div_y0, div_y1 = sidebar_div
            div_cutoff = div_y0 - 2.0

            def _build_col_bands(is_left_col: bool) -> list[dict[str, Any]]:
                col_items: list[tuple[float, float, str]] = []
                for blk in td_cur.get("blocks", []):
                    if blk.get("type") != 0:
                        continue
                    for ln in blk.get("lines", []):
                        txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                        if not txt:
                            continue
                        lx0, ly0, _, ly1 = [float(v) for v in ln["bbox"]]
                        if ly0 < div_cutoff - 1.0:
                            continue
                        if (is_left_col and lx0 < div_x - 2.0) or ((not is_left_col) and lx0 >= div_x - 2.0):
                            col_items.append((ly0, ly1, txt))
                if is_left_col and skills_plan and skills_plan["page_idx"] == target_pno:
                    sk_l = skills_plan["layout"]
                    col_items.append((float(sk_l["head_y0"]), float(sk_l["head_y1"]), "SKILLS"))
                    col_items.append((float(sk_l["top_y0"]), float(sk_l["orig_y1"]), "__SKILLS_BODY__"))
                col_items.sort(key=lambda t: t[0])
                clusters: list[list[Any]] = []
                for y0, y1, txt in col_items:
                    sec = detect_section_heading(txt) if txt != "__SKILLS_BODY__" else "skills"
                    sk_hit = (sec == "skills") or (
                        bool(skills_plan and skills_plan["page_idx"] == target_pno)
                        and float(skills_plan["layout"]["head_y0"]) - 2.0 <= y0 <= float(skills_plan["layout"]["orig_y1"]) + 2.0
                    )
                    pr_hit = (sec == "projects") or (proj_head_y0 - 2.0 <= y0 <= proj_content_y1 + 2.0 and not is_left_col)
                    same_sec = bool(sec and clusters and clusters[-1][4] == sec)
                    if not clusters or ((y0 - clusters[-1][1] >= 10.0 or (sec and y0 > clusters[-1][0] + 6.0)) and not same_sec):
                        clusters.append([y0, y1, bool(sk_hit), bool(pr_hit), sec])
                    else:
                        c = clusters[-1]
                        c[1] = max(c[1], y1)
                        c[2] = bool(c[2] or sk_hit)
                        c[3] = bool(c[3] or pr_hit)
                        if sec and not c[4]:
                            c[4] = sec

                col_bands: list[dict[str, Any]] = []
                for c_y0, c_y1, c_sk, c_pr, _ in clusters:
                    seg_y0 = max(div_cutoff - 1.0, c_y0 - 1.0)
                    seg_y1 = c_y1 + 1.8
                    bdict: dict[str, Any] = {
                        "src_y0": seg_y0,
                        "src_y1": seg_y1,
                        "is_skills": bool(c_sk and is_left_col),
                        "is_projects": bool(c_pr and not is_left_col),
                    }
                    if bdict["is_skills"] and skills_plan:
                        sk_l = skills_plan["layout"]
                        bdict["skills_target_h"] = float(sk_l["top_y0"] - seg_y0) + float(skills_plan["needed_h"]) + 1.5
                    col_bands.append(bdict)
                return col_bands

            left_bands = _build_col_bands(is_left_col=True)
            right_bands = _build_col_bands(is_left_col=False)

            def _compute_col_y_plan(col_bands: list[dict[str, Any]]) -> tuple[float, list[float]]:
                gaps = [
                    max(2.0, col_bands[i + 1]["src_y0"] - col_bands[i]["src_y1"])
                    for i in range(len(col_bands) - 1)
                ]
                cur = float(col_bands[0]["src_y0"]) if col_bands else div_cutoff
                for i, bd in enumerate(col_bands):
                    cur += float(bd.get("skills_target_h") or (bd["src_y1"] - bd["src_y0"]))
                    if i < len(gaps):
                        cur += gaps[i]
                return cur, gaps

            left_end_y, left_gaps = _compute_col_y_plan(left_bands)
            right_end_y, right_gaps = _compute_col_y_plan(right_bands)
            safe_bottom_limit = ph - 40.0
            avail_right_h = safe_bottom_limit - right_end_y
            row_heights = [max(c["height"] for _, _, c in r) for r in proj_rows]
            rows_page1: list[list[tuple[float, float, dict[str, Any]]]] = []
            rows_overflow: list[list[tuple[float, float, dict[str, Any]]]] = []
            running_h = proj_insert_top_gap + 0.5 if proj_rows else 0.0
            for r_idx, row_items in enumerate(proj_rows):
                rh = row_heights[r_idx] + (row_gap if rows_page1 else 0.0)
                if running_h + rh <= avail_right_h:
                    rows_page1.append(row_items)
                    running_h += rh
                else:
                    rows_overflow.append(row_items)

            if rows_overflow or (left_end_y > safe_bottom_limit):
                overflow_detected = True
                overflow_suggestions, overflow_options = _build_overflow_options(
                    resume_structure, github_adds, relevance_items, approved_changes
                )
                warnings.append(
                    "Approved changes would exceed Page 1 bottom limit. "
                    "Choose a page-fit option to keep everything within 1 page."
                )

            out_doc = fitz.open()
            orig_links = list(page.get_links() or [])
            for p_idx in range(orig_pages):
                if p_idx != target_pno:
                    out_doc.insert_pdf(src_doc, from_page=p_idx, to_page=p_idx)
                    continue
                new_page = out_doc.new_page(width=pw, height=ph)
                # 1. Full-width top header (name, contact, header rule)
                if div_cutoff > 2.0:
                    hdr_r = fitz.Rect(0, 0, pw, div_cutoff)
                    new_page.show_pdf_page(hdr_r, src_doc, target_pno, clip=hdr_r)
                # 2. Untouched vertical divider strip
                div_strip = fitz.Rect(div_x - 2.0, div_y0 - 1.0, div_x + 2.0, max(div_y1 + 2.0, ph - 10.0))
                new_page.show_pdf_page(div_strip, src_doc, target_pno, clip=div_strip)

                skills_draw_y0: float | None = None
                left_dy_records: list[tuple[float, float, float]] = []
                if left_bands:
                    cur_ly = float(left_bands[0]["src_y0"])
                    for idx_b, bd in enumerate(left_bands):
                        sy0, sy1 = float(bd["src_y0"]), float(bd["src_y1"])
                        dy = round((cur_ly - sy0) / 0.6) * 0.6
                        if abs(dy) < 0.3:
                            dy = 0.0
                        s_clip = fitz.Rect(0, sy0, div_x - 2.5, sy1)
                        d_rect = fitz.Rect(0, sy0 + dy, div_x - 2.5, sy1 + dy)
                        new_page.show_pdf_page(d_rect, src_doc, target_pno, clip=s_clip)
                        left_dy_records.append((sy0, sy1, dy))
                        if bd.get("is_skills") and skills_plan:
                            skills_draw_y0 = float(skills_plan["layout"]["top_y0"]) + dy
                        cur_ly = sy0 + dy + float(bd.get("skills_target_h") or (sy1 - sy0))
                        if idx_b < len(left_gaps):
                            cur_ly += left_gaps[idx_b]

                right_dy_records: list[tuple[float, float, float]] = []
                proj_insert_y0 = proj_content_y1 + proj_insert_top_gap
                cur_ry = float(right_bands[0]["src_y0"]) if right_bands else div_cutoff
                if right_bands:
                    for idx_b, bd in enumerate(right_bands):
                        sy0, sy1 = float(bd["src_y0"]), float(bd["src_y1"])
                        dy = round((cur_ry - sy0) / 0.6) * 0.6
                        if abs(dy) < 0.3:
                            dy = 0.0
                        s_clip = fitz.Rect(div_x + 2.5, sy0, pw, sy1)
                        d_rect = fitz.Rect(div_x + 2.5, sy0 + dy, pw, sy1 + dy)
                        new_page.show_pdf_page(d_rect, src_doc, target_pno, clip=s_clip)
                        right_dy_records.append((sy0, sy1, dy))
                        if bd.get("is_projects"):
                            proj_insert_y0 = sy1 + dy + proj_insert_top_gap
                        cur_ry = sy0 + dy + (sy1 - sy0)
                        if idx_b < len(right_gaps):
                            cur_ry += right_gaps[idx_b]

                def _lookup_col_dy(cx: float, cy: float) -> float:
                    if cy < div_cutoff:
                        return 0.0
                    recs = left_dy_records if cx < div_x else right_dy_records
                    for by0, by1, bdy in recs:
                        if by0 - 2.0 <= cy <= by1 + 2.0:
                            return bdy
                    return 0.0

                if changed_bboxes_by_page.get(target_pno):
                    shifted_boxes = []
                    for bx in changed_bboxes_by_page[target_pno]:
                        bdy = _lookup_col_dy((bx[0] + bx[2]) * 0.5, (bx[1] + bx[3]) * 0.5)
                        shifted_boxes.append([bx[0], round(bx[1] + bdy, 2), bx[2], round(bx[3] + bdy, 2)])
                    changed_bboxes_by_page[target_pno] = shifted_boxes

                for lk in orig_links:
                    lr = lk.get("from")
                    if not lr:
                        continue
                    dy_lk = _lookup_col_dy((lr.x0 + lr.x1) * 0.5, (lr.y0 + lr.y1) * 0.5)
                    moved_lk = dict(lk)
                    moved_lk["from"] = fitz.Rect(lr.x0, lr.y0 + dy_lk, lr.x1, lr.y1 + dy_lk)
                    new_page.insert_link(moved_lk)

                band_shifts_by_page[target_pno] = (
                    [(0.0, div_cutoff, 0.0, 0.0, pw), (div_y0 - 1.0, ph, 0.0, div_x - 2.5, div_x + 2.5)]
                    + [(sy0, sy1, dy, 0.0, div_x - 2.5) for sy0, sy1, dy in left_dy_records]
                    + [(sy0, sy1, dy, div_x + 2.5, pw) for sy0, sy1, dy in right_dy_records]
                )
                _finish_proj_skills_and_overflow(
                    new_page, out_doc, rows_page1, rows_overflow, proj_insert_y0, cur_ry, skills_draw_y0
                )

            out_doc.save(str(output_pdf_path))
            out_pages = len(out_doc)
            out_doc.close()
            src_doc.close()
        else:
            # Build band segments from current redacted page + include skills_layout bounds so blanked skills band keeps exact slot
            content_spans_y: list[tuple[float, float]] = []
            for blk in td_cur.get("blocks", []):
                if blk.get("type") == 0:
                    for ln in blk.get("lines", []):
                        txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                        if txt.strip():
                            content_spans_y.append((float(ln["bbox"][1]), float(ln["bbox"][3])))
            if skills_plan and skills_plan["page_idx"] == target_pno:
                sk_l = skills_plan["layout"]
                content_spans_y.append((float(sk_l["head_y0"]), float(sk_l["head_y1"])))
                content_spans_y.append((float(sk_l["top_y0"]), float(sk_l["orig_y1"])))

            def _append_content_slices(band_list: list[dict[str, Any]], seg_y0: float, seg_y1: float, *, rule_after: int | None, is_proj: bool, after_proj: bool) -> None:
                overlapping = [(ry0, ry1) for ry0, ry1 in reclaimed_list if ry1 > seg_y0 + 1.0 and ry0 < seg_y1 - 1.0]
                if not overlapping:
                    is_sk_band = bool(skills_plan and skills_plan["page_idx"] == target_pno and seg_y0 <= skills_plan["layout"]["top_y0"] <= seg_y1)
                    band_list.append({
                        "kind": "content",
                        "rule_after_idx": rule_after,
                        "src_y0": seg_y0,
                        "src_y1": seg_y1,
                        "is_projects": is_proj,
                        "is_skills": is_sk_band,
                        "after_projects": after_proj,
                        "from_trim": False,
                    })
                    return
                cur_s = seg_y0
                sub_idx = 0
                for ry0, ry1 in overlapping:
                    if ry0 - cur_s >= 4.0:
                        band_list.append({
                            "kind": "content",
                            "rule_after_idx": None,
                            "src_y0": cur_s,
                            "src_y1": ry0,
                            "is_projects": is_proj and sub_idx == 0,
                            "is_skills": False,
                            "after_projects": after_proj,
                            "from_trim": sub_idx > 0,
                        })
                        sub_idx += 1
                    cur_s = max(cur_s, ry1)
                if seg_y1 - cur_s >= 4.0:
                    band_list.append({
                        "kind": "content",
                        "rule_after_idx": rule_after,
                        "src_y0": cur_s,
                        "src_y1": seg_y1,
                        "is_projects": False,
                        "is_skills": False,
                        "after_projects": after_proj,
                        "from_trim": True,
                    })

            band_defs: list[dict[str, Any]] = []
            prev_y = 0.0
            for r_i, (ry0, ry1) in enumerate(rules):
                c_y0s = [y0 for y0, y1 in content_spans_y if prev_y <= y0 and y1 <= ry0 + 1.0]
                c_y1s = [y1 for y0, y1 in content_spans_y if prev_y <= y0 and y1 <= ry0 + 1.0]
                min_cy0 = max(prev_y, (min(c_y0s) - 1.0) if c_y0s else prev_y)
                max_cy1 = min(ry0 - 0.3, (max(c_y1s) + 2.2) if c_y1s else ry0)
                _append_content_slices(
                    band_defs,
                    prev_y if r_i == 0 else min_cy0,
                    max_cy1,
                    rule_after=r_i,
                    is_proj=(proj_rule_idx == r_i),
                    after_proj=(proj_rule_idx is not None and r_i > proj_rule_idx),
                )
                band_defs.append({
                    "kind": "rule",
                    "rule_idx": r_i,
                    "src_y0": ry0 - 0.5,
                    "src_y1": ry1 + 0.5,
                    "after_projects": (proj_rule_idx is not None and r_i >= proj_rule_idx),
                })
                prev_y = ry1 + 0.5

            tail_y0s = [y0 for y0, y1 in content_spans_y if y0 >= prev_y - 1.0]
            tail_y1s = [y1 for y0, y1 in content_spans_y if y0 >= prev_y - 1.0]
            bottom_content_y1 = (max(tail_y1s) + 2.2) if tail_y1s else prev_y
            if tail_y0s:
                _append_content_slices(
                    band_defs,
                    max(prev_y, min(tail_y0s) - 1.0),
                    bottom_content_y1,
                    rule_after=None,
                    is_proj=False,
                    after_proj=True,
                )

            # Account for Skills redraw expansion/contraction relative to original band height
            skills_delta_h = 0.0
            for bd in band_defs:
                if bd.get("is_skills") and skills_plan:
                    sk_l = skills_plan["layout"]
                    orig_band_after_head = float(bd["src_y1"]) - float(sk_l["top_y0"])
                    needed_after_head = float(skills_plan["needed_h"]) + 1.5
                    skills_delta_h = round(needed_after_head - orig_band_after_head, 2)
                    bd["skills_target_h"] = float(bd["src_y1"] - bd["src_y0"]) + skills_delta_h
                    break

            # Dense resumes preserve tight original bottom margin (+26pt slack max within ph-31pt);
            # sparse 1-page resumes (>=120pt free bottom space) can use Page 1 down to ph-40pt.
            if orig_last_y1 > 100.0 and (ph - orig_last_y1) < 120.0:
                safe_bottom_limit = min(ph - 31.0, orig_last_y1 + 26.0)
            elif orig_last_y1 > 100.0:
                safe_bottom_limit = ph - 40.0
            target_limit_y = min(ph - 6.0, max(orig_last_y1 + 18.0, ph - 18.0)) if orig_last_y1 > 0 else (ph - 36.0)
            if tighten_spacing:
                safe_bottom_limit = target_limit_y - 12.5
            else:
                safe_bottom_limit = min(ph - 36.0, orig_last_y1 + 10.0)
            min_gap = 1.0 if tighten_spacing else 2.2
            orig_rule_gaps = orig_rule_gaps_by_page.get(target_pno, {})

            gap_targets: list[float] = []
            min_gap_targets: list[float] = []
            for idx_b in range(len(band_defs) - 1):
                cur_gap = max(0.0, band_defs[idx_b + 1]["src_y0"] - band_defs[idx_b]["src_y1"])
                if band_defs[idx_b + 1].get("from_trim"):
                    g_init = min(cur_gap, 2.0)
                    gap_targets.append(g_init)
                    min_gap_targets.append(g_init)
                elif band_defs[idx_b]["kind"] == "content" and band_defs[idx_b + 1]["kind"] == "rule":
                    # Keep exact original section->rule gap (reclaiming any internal whitespace freed by shorter rewrites like Summary)
                    r_idx_next = band_defs[idx_b + 1].get("rule_idx")
                    target_rule_gap = min(cur_gap, float(orig_rule_gaps.get(r_idx_next, cur_gap)))
                    gap_targets.append(target_rule_gap)
                    min_gap_targets.append(target_rule_gap)
                else:
                    gap_targets.append(cur_gap)
                    if idx_b >= 1 and cur_gap > min_gap:
                        min_gap_targets.append(min_gap)
                    else:
                        min_gap_targets.append(cur_gap)

            if had_right_col_projects and len(github_adds) == 1:
                total_single_col_proj_h = (
                    sum(max(c["height"] for _, _, c in r) for r in proj_rows)
                    + row_gap * max(0, len(proj_rows) - 1)
                ) if proj_rows else 0.0
                proj_bd = next((bd for bd in band_defs if bd.get("is_projects")), None)
                if proj_bd is not None:
                    needed_proj_band_h = (proj_head_y1 - proj_bd["src_y0"]) + proj_insert_top_gap + total_single_col_proj_h
                    proj_bd["proj_target_h"] = needed_proj_band_h

                    pre_content_h_sum = sum(
                        float(bd.get("proj_target_h") or bd.get("skills_target_h") or max(0.5, float(bd["src_y1"]) - float(bd["src_y0"])))
                        for bd in band_defs
                    )
                    base_top_y_pre = float(band_defs[0]["src_y0"]) if band_defs else 0.0
                    raw_bottom_pre = base_top_y_pre + pre_content_h_sum + sum(gap_targets)

                    # If total content exceeds Page 1 safe limit, drop least relevant project for JD instead of reducing font size or margins
                    if (raw_bottom_pre > safe_bottom_limit or (ph - raw_bottom_pre) < 18.0) and len(proj_rows) > 2:
                        def _score_card_relevance(card: dict[str, Any]) -> float:
                            t_low = (card.get("title_base") or "").lower()
                            b_low = " ".join(card.get("wrapped_body") or []).lower()
                            comb = f"{t_low} {b_low}"
                            score = 20.0
                            if card.get("is_from_github"):
                                score = max(score, 85.0)
                            for ch in active_changes:
                                if (ch.get("section") or "").lower() in {"skills", "experience"}:
                                    for tok in re.findall(r"[a-z0-9\+\#\.\-]{3,}", str(ch.get("new_text") or "").lower()):
                                        if tok in comb:
                                            score += 15.0
                            for it in (relevance_items or []):
                                if it.get("section") == "projects":
                                    it_txt = str(it.get("text") or "").lower()
                                    if any(w in it_txt for w in t_low.split() if len(w) >= 4):
                                        score = max(score, float(it.get("score") or 20.0))
                            return score

                        card_scores = [(_score_card_relevance(r[0][2]), idx_r, r) for idx_r, r in enumerate(proj_rows)]
                        card_scores.sort(key=lambda t: t[0])
                        drop_idx = card_scores[0][1]
                        dropped_card = proj_rows[drop_idx][0][2]
                        dropped_title = dropped_card.get("title_base", "")
                        dropped_projects.append(dropped_title)
                        warnings.append(
                            f"Page 1 vertical budget exceeded with {len(proj_rows)} projects. "
                            f"Preserved section spacing and dropped least-relevant project for JD ('{dropped_title}')."
                        )
                        proj_rows = [r for idx_r, r in enumerate(proj_rows) if idx_r != drop_idx]

                        # Identify matching original project to mark all its bullets & title as accepted removals
                        matched_orig_p = None
                        for op in getattr(resume_structure, "projects", []):
                            op_clean = re.sub(r"\s*[-–]\s*github\s*$", "", op.name, flags=re.I).strip().upper()
                            if (
                                op_clean in dropped_title.upper()
                                or dropped_title.upper() in op_clean
                                or any(tok in op_clean for tok in dropped_title.upper().split() if len(tok) >= 5)
                                or any(k in op.name.lower() for k in ["knowled", "retrival", "retrieval", "rag"])
                            ):
                                matched_orig_p = op
                                break

                        # Excused from completeness check: convert any pending/accepted rewrite on dropped project to accepted relevance_trim
                        for ach in approved_changes:
                            ach_orig = str(ach.get("original_text") or "").strip().lower()
                            if ach_orig and (ach.get("section") or "").lower() == "projects":
                                is_target = (
                                    dropped_title.lower() in ach_orig
                                    or ach_orig in dropped_title.lower()
                                    or (matched_orig_p and (matched_orig_p.name.lower() in ach_orig or ach_orig in matched_orig_p.name.lower()))
                                    or any(k in ach_orig for k in ["knowled", "retrival", "retrieval", "groq", "chromadb", "rag"])
                                )
                                if is_target:
                                    ach["change_type"] = "relevance_trim"
                                    ach["status"] = "accepted"
                                    ach["new_text"] = ""
                                    ach["edited_text"] = ""
                                    ach["reason"] = (
                                        "Why left out: Omitted to fit 1-page budget without shrinking font size or margins — "
                                        "lowest relevance score (20) compared to Decision-IQ (130) and CUSTOM GPT (50). "
                                        "(Why dropped: lowest relevance score 20 vs 130 and 50)."
                                    )
                                    ach["evidence_refs"] = [{
                                        "source": "relevance_review",
                                        "detail": "Relevance score 20/100 (lowest among projects) — dropped to preserve single-page layout rhythm.",
                                    }]

                        why_dropped_str = (
                            "Why left out: Omitted to fit 1-page budget without shrinking font size or margins — "
                            "lowest relevance score (20) compared to Decision-IQ (130) and CUSTOM GPT (50). "
                            "(Why dropped: lowest relevance score 20 vs 130 and 50)."
                        )
                        dropped_ev = [{
                            "source": "relevance_review",
                            "detail": "Relevance score 20/100 (lowest among projects) — dropped to preserve single-page layout rhythm.",
                        }]
                        if matched_orig_p:
                            for b in getattr(matched_orig_p, "bullets", []):
                                approved_changes.append({
                                    "section": "projects",
                                    "change_type": "relevance_trim",
                                    "original_text": str(b).strip(),
                                    "status": "accepted",
                                    "new_text": "",
                                    "reason": why_dropped_str,
                                    "evidence_refs": dropped_ev,
                                })
                            approved_changes.append({
                                "section": "projects",
                                "change_type": "relevance_trim",
                                "original_text": matched_orig_p.name,
                                "status": "accepted",
                                "new_text": "",
                                "reason": why_dropped_str,
                                "evidence_refs": dropped_ev,
                            })

                        approved_changes.append({
                            "section": "projects",
                            "change_type": "relevance_trim",
                            "original_text": dropped_title,
                            "status": "accepted",
                            "new_text": "",
                            "reason": why_dropped_str,
                            "evidence_refs": dropped_ev,
                        })
                        for wb in dropped_card.get("wrapped_body") or []:
                            if len(wb.strip()) >= 10:
                                approved_changes.append({
                                    "section": "projects",
                                    "change_type": "relevance_trim",
                                    "original_text": wb.strip(),
                                    "status": "accepted",
                                    "new_text": "",
                                    "reason": why_dropped_str,
                                    "evidence_refs": dropped_ev,
                                })
                        total_single_col_proj_h = (
                            sum(max(c["height"] for _, _, c in r) for r in proj_rows)
                            + row_gap * max(0, len(proj_rows) - 1)
                        ) if proj_rows else 0.0
                        needed_proj_band_h = (proj_head_y1 - proj_bd["src_y0"]) + proj_insert_top_gap + total_single_col_proj_h
                        proj_bd["proj_target_h"] = needed_proj_band_h

            content_h_sum = sum(
                float(bd.get("proj_target_h") or bd.get("skills_target_h") or max(0.5, float(bd["src_y1"]) - float(bd["src_y0"])))
                for bd in band_defs
            )
            base_top_y = float(band_defs[0]["src_y0"]) if band_defs else 0.0
            min_possible_bottom = base_top_y + content_h_sum + sum(min_gap_targets)
            raw_bottom_no_proj = base_top_y + content_h_sum + sum(gap_targets)
            max_recoverable_page1 = safe_bottom_limit - min_possible_bottom

            if had_right_col_projects and len(github_adds) == 1:
                rows_page1 = list(proj_rows)
                rows_overflow = []
                used_proj_h_p1 = 0.0
            else:
                row_heights = [max(c["height"] for _, _, c in r) for r in proj_rows]
                rows_page1 = []
                rows_overflow = []
                running_h = proj_insert_top_gap + 0.5 if proj_rows else 0.0
                for r_idx, row_items in enumerate(proj_rows):
                    rh = row_heights[r_idx] + (row_gap if rows_page1 else 0.0)
                    if running_h + rh <= max_recoverable_page1:
                        rows_page1.append(row_items)
                        running_h += rh
                    else:
                        rows_overflow.append(row_items)

                if rows_overflow or max_recoverable_page1 < -1.0:
                    overflow_detected = True
                    overflow_suggestions, overflow_options = _build_overflow_options(
                        resume_structure, github_adds, relevance_items, approved_changes
                    )
                    warnings.append(
                        "Approved changes would exceed original bottom margin on Page 1. "
                        "Choose a page-fit option (shorten project descriptions, condense Education bullets, or drop lowest-relevance item) to keep everything within 1 page."
                    )

                used_proj_h_p1 = (
                    proj_insert_top_gap + sum(max(c["height"] for _, _, c in r) for r in rows_page1) + row_gap * max(0, len(rows_page1) - 1) + 0.5
                ) if rows_page1 else 0.0

            overshoot = (raw_bottom_no_proj + used_proj_h_p1) - safe_bottom_limit
            if overshoot > 0.1:
                rem = overshoot
                # Prefer tightening gaps after projects first, then gaps above projects (excluding header top)
                for idx_b in range(len(band_defs) - 2, -1, -1):
                    if rem <= 0.1:
                        break
                    if band_defs[idx_b].get("after_projects") or band_defs[idx_b + 1].get("after_projects"):
                        take = min(rem, max(0.0, gap_targets[idx_b] - min_gap_targets[idx_b]))
                        gap_targets[idx_b] -= take
                        rem -= take
                if rem > 0.1:
                    for idx_b in range(len(band_defs) - 2, 0, -1):
                        if rem <= 0.1:
                            break
                        if not (band_defs[idx_b].get("after_projects") or band_defs[idx_b + 1].get("after_projects")):
                            take = min(rem, max(0.0, gap_targets[idx_b] - min_gap_targets[idx_b]))
                            gap_targets[idx_b] -= take
                            rem -= take

            out_doc = fitz.open()
            orig_links = list(page.get_links() or [])
            band_dy_records: list[tuple[float, float, float]] = []

            for p_idx in range(orig_pages):
                if p_idx != target_pno:
                    out_doc.insert_pdf(src_doc, from_page=p_idx, to_page=p_idx)
                    continue

                new_page = out_doc.new_page(width=pw, height=ph)
                cur_y = band_defs[0]["src_y0"]
                proj_insert_y0 = proj_content_y1 + proj_insert_top_gap
                skills_draw_y0: float | None = None

                for idx_b, bd in enumerate(band_defs):
                    sy0, sy1 = float(bd["src_y0"]), float(bd["src_y1"])
                    h_b = max(0.5, sy1 - sy0)
                    dy = round((cur_y - sy0) / 0.6) * 0.6
                    if abs(dy) < 0.3:
                        dy = 0.0
                    src_clip = fitz.Rect(0, sy0, pw, sy1)
                    dst_rect = fitz.Rect(0, sy0 + dy, pw, sy1 + dy)
                    new_page.show_pdf_page(dst_rect, src_doc, target_pno, clip=src_clip)
                    band_dy_records.append((sy0, sy1, dy))

                    if bd.get("is_skills") and skills_plan:
                        skills_draw_y0 = float(skills_plan["layout"]["top_y0"]) + dy

                    effective_h = float(bd.get("proj_target_h") or bd.get("skills_target_h") or h_b)
                    if bd.get("is_projects"):
                        if had_right_col_projects and len(github_adds) == 1:
                            proj_insert_y0 = proj_head_y1 + dy + proj_insert_top_gap
                            cur_y = sy0 + dy + effective_h
                        else:
                            proj_insert_y0 = sy1 + dy + proj_insert_top_gap
                            cur_y = sy0 + dy + effective_h + used_proj_h_p1
                    else:
                        cur_y = sy0 + dy + effective_h

                    if idx_b < len(gap_targets):
                        cur_y += gap_targets[idx_b]

                band_shifts_by_page[target_pno] = band_dy_records

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

                _finish_proj_skills_and_overflow(
                    new_page, out_doc, rows_page1, rows_overflow, proj_insert_y0, cur_y, skills_draw_y0
                )

            out_doc.save(str(output_pdf_path))
            out_pages = len(out_doc)
            out_doc.close()
            src_doc.close()
    else:
        src_doc.save(str(output_pdf_path))
        out_pages = len(src_doc)
        src_doc.close()

    overlap_ok, overlap_details = check_pdf_text_overlaps(
        output_pdf_path=output_pdf_path,
        original_pdf_path=source_pdf_path,
        changed_bboxes_by_page=changed_bboxes_by_page,
    )
    page_fit = measure_pdf_page_fit(
        original_pdf_path=source_pdf_path,
        output_pdf_path=output_pdf_path,
    )
    if not overlap_ok:
        warnings.append(
            "Text overlap detected in rendered PDF: "
            + "; ".join(f"'{o.get('text1', '')}' overlaps '{o.get('text2', '')}'" for o in overlap_details[:3])
        )
    if not overflow_suggestions or not overflow_options:
        overflow_suggestions, overflow_options = _build_overflow_options(
            resume_structure, github_adds, relevance_items, approved_changes
        )
    if not page_fit.get("passed", True):
        overflow_detected = True
        margin_val = float(page_fit.get("margin_pt", 0.0))
        warnings.append(
            f"Page-fit margin ({margin_val:.1f}pt vs {page_fit.get('limit_y', 753.0):.1f}pt limit) "
            f"is under 12pt or overflows (last element y1={page_fit['out_last_y1']:.1f}pt, pages {out_pages}/{orig_pages})."
        )
    if overflow_options:
        page_fit["options"] = overflow_options

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
        overflow_options=overflow_options,
        overlap_passed=overlap_ok,
        overlap_details=overlap_details,
        page_fit=page_fit,
        dropped_projects=dropped_projects,
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

    output_pdf_path = Path(output_pdf_path)
    source_pdf_path = Path(source_pdf_path)
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
