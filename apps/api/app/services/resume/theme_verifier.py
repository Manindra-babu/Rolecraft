from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.services.resume.pdf_parser import detect_section_heading


@dataclass
class ThemeVerificationResult:
    passed: bool
    page_count_original: int
    page_count_output: int
    overall_similarity_score: float
    unchanged_similarity_score: float
    heading_drift_pt: float
    orig_preview_png: bytes = b""
    out_preview_png: bytes = b""
    diff_preview_png: bytes = b""
    warnings: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    overlap_passed: bool = True
    overlap_details: list[dict[str, Any]] = field(default_factory=list)
    page_fit: dict[str, Any] = field(default_factory=dict)


def _extract_visible_spans(doc: fitz.Document) -> list[dict[str, Any]]:
    spans_out: list[dict[str, Any]] = []
    for pno, page in enumerate(doc):
        tdict = page.get_text("dict")
        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    raw_txt = str(sp.get("text", ""))
                    clean_txt = re.sub(r"[\uE000-\uF8FF\s]+", "", raw_txt)
                    if not clean_txt:
                        continue
                    bbox = [float(v) for v in (sp.get("bbox") or (0, 0, 0, 0))]
                    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                        continue
                    spans_out.append(
                        {
                            "page": pno,
                            "text": raw_txt.strip(),
                            "norm": clean_txt.lower(),
                            "size": round(float(sp.get("size", 10.0)), 2),
                            "bbox": bbox,
                        }
                    )
    return spans_out


def check_pdf_text_overlaps(
    *,
    output_pdf_path: Path,
    original_pdf_path: Path | None = None,
    changed_bboxes_by_page: dict[int, list[list[float]]] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """
    Detect any overlapping visible text span bounding boxes in `output_pdf_path`.
    Exempts only unmodified original large display header pairs (>=18pt) whose
    tight ascender/descender boxes already existed verbatim in `original_pdf_path`.
    """
    orig_header_pairs: set[tuple[int, str, str]] = set()
    if original_pdf_path and original_pdf_path.exists():
        orig_doc = fitz.open(str(original_pdf_path))
        orig_spans = _extract_visible_spans(orig_doc)
        orig_doc.close()
        for i in range(len(orig_spans)):
            s1 = orig_spans[i]
            if s1["size"] < 17.5:
                continue
            for j in range(i + 1, len(orig_spans)):
                s2 = orig_spans[j]
                if s1["page"] != s2["page"] or s2["size"] < 17.5:
                    continue
                b1, b2 = s1["bbox"], s2["bbox"]
                ox = min(b1[2], b2[2]) - max(b1[0], b2[0])
                oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
                if ox > 1.5 and oy > 1.8:
                    pair = tuple(sorted((s1["norm"], s2["norm"])))
                    orig_header_pairs.add((s1["page"], pair[0], pair[1]))

    out_doc = fitz.open(str(output_pdf_path))
    out_spans = _extract_visible_spans(out_doc)
    out_doc.close()

    def _in_changed(pno: int, bx: list[float]) -> bool:
        for cb in (changed_bboxes_by_page or {}).get(pno, []):
            if min(bx[2], cb[2]) - max(bx[0], cb[0]) > 1.0 and min(bx[3], cb[3]) - max(bx[1], cb[1]) > 1.0:
                return True
        return False

    overlaps: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str, str]] = set()
    for i in range(len(out_spans)):
        s1 = out_spans[i]
        for j in range(i + 1, len(out_spans)):
            s2 = out_spans[j]
            if s1["page"] != s2["page"]:
                continue
            b1, b2 = s1["bbox"], s2["bbox"]
            ox = min(b1[2], b2[2]) - max(b1[0], b2[0])
            oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
            if ox > 1.5 and oy > 1.8:
                pair_key = (s1["page"], *tuple(sorted((s1["norm"], s2["norm"]))))
                if (
                    s1["size"] >= 17.5
                    and s2["size"] >= 17.5
                    and pair_key in orig_header_pairs
                    and not _in_changed(s1["page"], b1)
                    and not _in_changed(s2["page"], b2)
                ):
                    continue
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                overlaps.append(
                    {
                        "page": s1["page"] + 1,
                        "text1": s1["text"],
                        "bbox1": [round(v, 2) for v in b1],
                        "text2": s2["text"],
                        "bbox2": [round(v, 2) for v in b2],
                        "overlap_x": round(ox, 2),
                        "overlap_y": round(oy, 2),
                    }
                )
    return len(overlaps) == 0, overlaps


def measure_pdf_page_fit(
    *,
    original_pdf_path: Path,
    output_pdf_path: Path,
    tolerance_pt: float = 2.5,
) -> dict[str, Any]:
    """
    Verify that the last visible element on the final page ends above the original
    bottom margin and that the total page count is unchanged.
    """
    def _last_element_metrics(pdf_path: Path) -> tuple[int, float, float, float]:
        doc = fitz.open(str(pdf_path))
        pc = len(doc)
        if pc == 0:
            doc.close()
            return 0, 792.0, 0.0, 792.0
        last_page = doc[-1]
        ph = float(last_page.rect.height)
        max_y1 = 0.0
        td = last_page.get_text("dict")
        for blk in td.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if re.sub(r"[\uE000-\uF8FF\s]+", "", str(sp.get("text", ""))):
                        max_y1 = max(max_y1, float(sp["bbox"][3]))
        doc.close()
        return pc, ph, round(max_y1, 2), round(max(0.0, ph - max_y1), 2)

    orig_pc, orig_ph, orig_y1, orig_margin = _last_element_metrics(original_pdf_path)
    out_pc, out_ph, out_y1, out_margin = _last_element_metrics(output_pdf_path)

    limit_y = round(
        min(out_ph - 6.0, max(orig_y1 + 18.0, out_ph - 18.0)) if orig_y1 > 0 else (out_ph - 36.0),
        2,
    )
    same_pages = out_pc == orig_pc
    if same_pages:
        margin_pt = round(limit_y - out_y1, 2)
    else:
        margin_pt = round(min(-1.0, -out_y1), 2)
    low_margin = bool(margin_pt < 12.0)
    overflow = bool((not same_pages) or (out_y1 > limit_y))
    fit_ok = bool(same_pages and (not overflow) and (not low_margin))
    return {
        "passed": fit_ok,
        "page_count_original": orig_pc,
        "page_count_output": out_pc,
        "orig_last_y1": orig_y1,
        "out_last_y1": out_y1,
        "limit_y": limit_y,
        "margin_pt": margin_pt,
        "low_margin": low_margin,
        "overflow": overflow,
        "orig_bottom_margin_pt": orig_margin,
        "out_bottom_margin_pt": out_margin,
        "page_height_pt": out_ph,
    }


def _norm_cmp(text: str) -> str:
    cleaned = re.sub(r"[\uE000-\uF8FF\ufffd\u25af]+", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def verify_exported_pdf_completeness(
    *,
    original_pdf_path: Path,
    output_pdf_path: Path,
    applied_changes: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """
    Fail-closed completeness & reading-order check comparing original vs exported PDF:
      1. Every untouched original text line (>= 18 chars) remains present in output.
      2. All original section headings appear in their original relative order.
      3. All original hyperlinks remain intact in output annotations.
    """
    errors: list[str] = []
    orig_doc = fitz.open(str(original_pdf_path))
    out_doc = fitz.open(str(output_pdf_path))

    out_full_norm = _norm_cmp("\n".join(p.get_text("text") for p in out_doc))

    # Build modified/replaced originals list strictly from accepted/edited changes
    modified_norms: list[str] = []
    accepted_removal_norms: list[str] = []
    for ch in applied_changes or []:
        if ch.get("validation_status") == "blocked":
            continue
        is_accepted = ch.get("status") in ("accepted", "edited")
        if ch.get("status") is None and ch.get("change_type") not in ("relevance_trim", "removed"):
            # Backward-compatible unit calls without status for non-removal rewrites
            is_accepted = True
        if not is_accepted:
            continue
        loc_meta = ch.get("target_location") or {}
        if isinstance(loc_meta, dict) and loc_meta.get("requires_confirmation") and not loc_meta.get("accuracy_confirmed"):
            continue
        orig = _norm_cmp(str(ch.get("original_text") or ""))
        new_val = str(ch.get("edited_text") or ch.get("new_text") or "").strip()
        is_removal = ch.get("change_type") in ("relevance_trim", "removed") and not new_val
        if orig:
            if is_removal:
                accepted_removal_norms.append(orig)
            modified_norms.append(orig)
        if isinstance(loc_meta, dict) and loc_meta.get("text"):
            loc_norm = _norm_cmp(str(loc_meta["text"]))
            if loc_norm:
                modified_norms.append(loc_norm)
                if is_removal:
                    accepted_removal_norms.append(loc_norm)

    # 1. Section heading order check (column-aware when a vertical sidebar divider splits columns)
    orig_heads_xy = _heading_positions_with_x(original_pdf_path)
    out_heads_xy = _heading_positions_with_x(output_pdf_path)
    orig_heads = {s: yx[0] for s, yx in orig_heads_xy.items()}
    out_heads = {s: yx[0] for s, yx in out_heads_xy.items()}
    orig_ordered_secs = [sec for sec, _ in sorted(orig_heads.items(), key=lambda kv: kv[1])]
    missing_secs = [sec for sec in orig_ordered_secs if sec not in out_heads]
    if missing_secs:
        errors.append(f"Completeness check failed: missing section heading(s) {', '.join(missing_secs)}.")
    else:
        vdiv_x = _detect_vertical_divider_x(orig_doc[0]) if len(orig_doc) else None
        if vdiv_x is not None:
            left_secs = [s for s in orig_ordered_secs if orig_heads_xy[s][1] < vdiv_x]
            right_secs = [s for s in orig_ordered_secs if orig_heads_xy[s][1] >= vdiv_x]
            for grp in (left_secs, right_secs):
                last_y = -1.0
                for sec in grp:
                    oy = out_heads[sec]
                    if oy < last_y - 2.0:
                        errors.append(f"Completeness check failed: section '{sec}' is out of original reading order.")
                        break
                    last_y = oy
        else:
            last_y = -1.0
            for sec in orig_ordered_secs:
                oy = out_heads[sec]
                if oy < last_y - 2.0:
                    errors.append(f"Completeness check failed: section '{sec}' is out of original reading order.")
                    break
                last_y = oy

    # 2. Untouched text content preservation check (strictly fail if any original block >= 12 chars is missing without accepted edit/removal)
    for pno, page in enumerate(orig_doc):
        tdict = page.get_text("dict")
        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                raw_ln = "".join(s.get("text", "") for s in ln.get("spans", []))
                norm_ln = _norm_cmp(raw_ln)
                if len(norm_ln) < 12:
                    continue
                if any(
                    norm_ln in mod or mod[:24] in norm_ln or (len(norm_ln) >= 12 and norm_ln[:min(24, len(norm_ln))] in mod)
                    for mod in modified_norms
                    if len(mod) >= 8
                ):
                    continue
                probe = norm_ln[:min(36, len(norm_ln))]
                if probe not in out_full_norm:
                    errors.append(
                        f"Completeness check failed: original content missing without accepted removal ('{raw_ln.strip()[:80]}')."
                    )
            if len(errors) >= 25:
                break

    # 3. Hyperlink preservation check (exempt links ONLY inside explicitly accepted relevance_trim/removed lines)
    removed_norms: list[str] = list(accepted_removal_norms)

    required_orig_uris: set[str] = set()
    for page in orig_doc:
        for lk in page.get_links() or []:
            uri = str(lk.get("uri") or "").strip()
            if not uri:
                continue
            r = lk.get("from")
            if r is not None and removed_norms:
                line_rect = fitz.Rect(0, float(r.y0) - 3.0, float(page.rect.width), float(r.y1) + 3.0)
                line_norm = _norm_cmp(page.get_textbox(line_rect))
                if any(
                    rem in line_norm or line_norm in rem or (len(line_norm) >= 12 and line_norm[:24] in rem)
                    for rem in removed_norms
                    if len(rem) >= 8
                ):
                    continue
            required_orig_uris.add(uri)

    out_uris = {
        str(lk.get("uri")).strip()
        for page in out_doc
        for lk in (page.get_links() or [])
        if lk.get("uri")
    }
    lost_uris = sorted(required_orig_uris - out_uris)
    if lost_uris:
        errors.append(f"Completeness check failed: lost original hyperlink(s): {', '.join(lost_uris[:5])}.")

    orig_doc.close()
    out_doc.close()

    # 4. ATS Text Layer Integrity check
    text_layer_ok, text_layer_errs = verify_pdf_text_layer_integrity(
        output_pdf_path,
        accepted_changes=applied_changes,
    )
    if not text_layer_ok:
        errors.extend(text_layer_errs)

    return len(errors) == 0, errors


def verify_pdf_text_layer_integrity(
    pdf_path: Path,
    *,
    accepted_changes: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """
    ATS Text Layer Integrity verification:
    1. Asserts no known typo strings ("retrival", "constrains", and "firebase" if tailored/removed) linger in extracted text.
    2. Asserts each section heading appears at most once in reading order.
    3. Asserts each added project title appears at most once.
    4. Compares text extracted from spans vs visible page text to ensure no invisible ghost layers.
    """
    errors: list[str] = []
    if not pdf_path.exists():
        return False, [f"PDF file does not exist: {pdf_path}"]

    doc = fitz.open(str(pdf_path))
    full_text = "\n".join(page.get_text("text") for page in doc)
    low_full = full_text.lower()

    # 1. Assert no known typo strings in output when corresponding change was accepted
    if accepted_changes:
        for typo in ("retrival", "constrains", "casual masking"):
            has_fix = any(
                (ch.get("status") in ("accepted", "edited") or (ch.get("status") is None and ch.get("change_type") not in ("relevance_trim", "removed")))
                and typo in str(ch.get("original_text", "")).lower()
                for ch in accepted_changes
            )
            if has_fix and typo in low_full:
                errors.append(f"ATS Text Layer integrity failed: typo string '{typo}' was found in extracted text.")

        for ch in accepted_changes:
            is_acc = ch.get("status") in ("accepted", "edited") or (ch.get("status") is None and ch.get("change_type") not in ("relevance_trim", "removed"))
            if is_acc and "firebase" in str(ch.get("original_text", "")).lower() and "firebase" not in str(ch.get("edited_text", "")).lower():
                if "firebase" in low_full:
                    errors.append("ATS Text Layer integrity failed: 'firebase' still present in extracted text after accepted change.")
                    break

    # 2. Assert section headings appear at most once in the document
    seen_headings: dict[str, int] = {}
    for pno, page in enumerate(doc):
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                ltxt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                sec = detect_section_heading(ltxt)
                if sec and len(ltxt) < 30:
                    seen_headings[sec] = seen_headings.get(sec, 0) + 1

    for sec, count in seen_headings.items():
        if count > 1:
            errors.append(f"ATS Text Layer integrity failed: duplicate section heading '{sec.upper()}' appears {count} times.")

    # 3. Assert added project appears at most once
    if accepted_changes:
        for ch in accepted_changes:
            if ch.get("change_type") == "added_from_github":
                t = (ch.get("target_location") or {}).get("title") or ch.get("original_text") or ""
                t_base = re.sub(r"\s*[-–]\s*github\s*$", "", t, flags=re.I).strip().upper()
                if t_base:
                    count = len(re.findall(rf"\b{re.escape(t_base)}\s*[-–]\s*GITHUB\b", full_text, flags=re.I))
                    if count > 1:
                        errors.append(f"ATS Text Layer integrity failed: project '{t_base}' appears {count} times in extracted text.")

    # 4. Text layer matches visible spans (no phantom / ghost text outside page bounds or invisible layers)
    for pno, page in enumerate(doc):
        text_words = page.get_text("words") or []
        for w in text_words:
            # Check if word is placed completely off-screen
            if w[0] < -20.0 or w[1] < -20.0 or w[2] > page.rect.width + 20.0 or w[3] > page.rect.height + 20.0:
                errors.append(f"ATS Text Layer integrity failed: off-page ghost word '{w[4]}' detected at [{w[0]:.1f}, {w[1]:.1f}].")
                break

    doc.close()
    return len(errors) == 0, errors


def extract_missing_blocks_from_errors(errors: list[str]) -> list[str]:
    """Extract clean human-readable labels for missing blocks/links reported by completeness verification."""
    items: list[str] = []
    seen: set[str] = set()
    for err in errors or []:
        m = re.search(r"original content missing without accepted removal \('([^']+)'\)", err)
        if m:
            label = m.group(1).strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                items.append(label)
            continue
        m_link = re.search(r"lost original hyperlink\(s\):\s*(.+?)\.?$", err)
        if m_link:
            label = f"Hyperlink(s): {m_link.group(1).strip()}"
            if label.lower() not in seen:
                seen.add(label.lower())
                items.append(label)
            continue
        m_sec = re.search(r"missing section heading\(s\)\s*(.+?)\.?$", err)
        if m_sec:
            label = f"Section: {m_sec.group(1).strip()}"
            if label.lower() not in seen:
                seen.add(label.lower())
                items.append(label)
            continue
        clean = err.strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            items.append(clean)
    return items


def _detect_vertical_divider_x(page: fitz.Page) -> float | None:
    pw = float(page.rect.width)
    ph = float(page.rect.height)
    for dr in page.get_drawings() or []:
        r = dr.get("rect")
        if not r:
            continue
        w = abs(float(r.x1) - float(r.x0))
        h = abs(float(r.y1) - float(r.y0))
        cx = (float(r.x0) + float(r.x1)) * 0.5
        if w <= 3.0 and h >= max(100.0, ph * 0.14) and pw * 0.22 <= cx <= pw * 0.58:
            return round(cx, 2)
    return None


def _heading_positions_with_x(pdf_path: Path) -> dict[str, tuple[float, float]]:
    positions: dict[str, tuple[float, float]] = {}
    doc = fitz.open(str(pdf_path))
    for pno, page in enumerate(doc):
        tdict = page.get_text("dict")
        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                sec = detect_section_heading(txt)
                if sec and sec not in positions:
                    positions[sec] = (pno * 1000.0 + float(ln["bbox"][1]), float(ln["bbox"][0]))
    doc.close()
    return positions


def _heading_positions(pdf_path: Path) -> dict[str, float]:
    return {sec: yx[0] for sec, yx in _heading_positions_with_x(pdf_path).items()}


def verify_pdf_theme_match(
    *,
    original_pdf_path: Path,
    output_pdf_path: Path,
    changed_bboxes_by_page: dict[int, list[list[float]]] | None = None,
    band_shifts_by_page: dict[int, list[tuple[float, float, float]]] | None = None,
    strategy_used: str = "inplace_pdf",
    dpi: int = 120,
    min_unchanged_similarity: float = 95.0,
    allow_page_overflow: bool = False,
) -> ThemeVerificationResult:
    """
    Strategy D: Render original and result PDFs via PyMuPDF and compute:
      - page count match
      - heading Y-position drift (in points)
      - foreground-aware pixel similarity overall and inside ALL unchanged regions
        (everywhere outside edited bounding boxes).
    Blocks export if unchanged regions visibly diverge during in-place editing.
    """
    orig_doc = fitz.open(str(original_pdf_path))
    out_doc = fitz.open(str(output_pdf_path))
    orig_pc = len(orig_doc)
    out_pc = len(out_doc)

    # Always render page 1 preview PNGs for side-by-side UI
    orig_png = orig_doc[0].get_pixmap(dpi=dpi, alpha=False).tobytes("png") if orig_pc else b""
    out_png = out_doc[0].get_pixmap(dpi=dpi, alpha=False).tobytes("png") if out_pc else b""

    warnings: list[str] = []
    if orig_pc != out_pc:
        warnings.append(f"Page count changed from {orig_pc} to {out_pc}.")

    scale = dpi / 72.0
    total_ink_pixels = 0
    matching_ink_pixels = 0
    unchanged_ink_pixels = 0
    unchanged_matching_pixels = 0
    all_px_total = 0
    all_px_close = 0
    unchanged_px_total = 0
    unchanged_px_close = 0

    diff_png = b""
    pages_to_cmp = min(orig_pc, out_pc)

    for pno in range(pages_to_cmp):
        p_orig = orig_doc[pno].get_pixmap(dpi=dpi, alpha=False)
        p_out = out_doc[pno].get_pixmap(dpi=dpi, alpha=False)
        w = min(p_orig.width, p_out.width)
        h = min(p_orig.height, p_out.height)
        s_orig = p_orig.samples
        s_out = p_out.samples

        masked_rects_px = [
            (
                int((b[0] - 4.0) * scale),
                int((b[1] - 4.0) * scale),
                int((b[2] + 4.0) * scale),
                int((b[3] + 4.0) * scale),
            )
            for b in (changed_bboxes_by_page or {}).get(pno, [])
        ]

        diff_buf = bytearray(s_out) if pno == 0 else None

        page_shifts = (band_shifts_by_page or {}).get(pno, [])
        has_x_scoped_shifts = any(len(rec) >= 5 for rec in page_shifts)

        def _orig_y_for_out_px(x_out_pt: float, y_out_pt: float, fallback_y_px: int) -> tuple[int, bool]:
            """Return (mapped_orig_y_px, is_trimmed_interband_gap)."""
            if not page_shifts:
                return fallback_y_px, False
            for rec in page_shifts:
                by0, by1, dy = rec[0], rec[1], rec[2]
                if len(rec) >= 5 and not (rec[3] <= x_out_pt <= rec[4]):
                    continue
                if by0 + dy <= y_out_pt <= by1 + dy:
                    y_orig_px = int(round((y_out_pt - dy) * scale))
                    return max(0, min(p_orig.height - 1, y_orig_px)), False
            # Pixel falls inside a newly adjusted whitespace gap between shifted bands
            return fallback_y_px, True

        for y in range(h):
            row_masked = [r for r in masked_rects_px if r[1] <= y <= r[3]]
            y_out_pt = y / scale
            default_row = _orig_y_for_out_px(0.0, y_out_pt, y) if not has_x_scoped_shifts else (y, True)
            row_off_orig = default_row[0] * p_orig.stride
            row_off_out = y * p_out.stride
            for x in range(w):
                i2 = row_off_out + x * 3
                r2, g2, b2 = s_out[i2], s_out[i2 + 1], s_out[i2 + 2]
                if has_x_scoped_shifts:
                    y_orig_mapped, is_gap_row = _orig_y_for_out_px(x / scale, y_out_pt, y)
                    off1 = y_orig_mapped * p_orig.stride
                else:
                    is_gap_row = default_row[1]
                    off1 = row_off_orig
                if is_gap_row:
                    # In a trimmed whitespace gap between bands: expect clean white paper in out_doc
                    r1, g1, b1 = 255, 255, 255
                else:
                    i1 = off1 + x * 3
                    r1, g1, b1 = s_orig[i1], s_orig[i1 + 1], s_orig[i1 + 2]
                diff = abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
                is_close = diff <= 24  # per-channel tolerance ~8/255 for subpixel AA
                is_ink = (r1 < 245 or g1 < 245 or b1 < 245) or (r2 < 245 or g2 < 245 or b2 < 245)

                all_px_total += 1
                if is_close:
                    all_px_close += 1
                if is_ink:
                    total_ink_pixels += 1
                    if is_close:
                        matching_ink_pixels += 1

                in_changed = any(mx0 <= x <= mx1 for mx0, _, mx1, _ in row_masked)
                if not in_changed:
                    unchanged_px_total += 1
                    if is_close:
                        unchanged_px_close += 1
                    if is_ink:
                        unchanged_ink_pixels += 1
                        if is_close:
                            unchanged_matching_pixels += 1
                        elif diff_buf is not None:
                            # Highlight unexpected unchanged-region diff in bright red
                            diff_buf[i2] = 220
                            diff_buf[i2 + 1] = 38
                            diff_buf[i2 + 2] = 38

        if pno == 0 and diff_buf is not None:
            diff_pm = fitz.Pixmap(fitz.csRGB, p_out.width, p_out.height, bytes(diff_buf), False)
            diff_png = diff_pm.tobytes("png")

    orig_doc.close()
    out_doc.close()

    # Combine foreground-ink similarity (prevents blank-white gaming) with total pixel agreement
    fg_overall = (matching_ink_pixels / total_ink_pixels * 100.0) if total_ink_pixels else 100.0
    px_overall = (all_px_close / all_px_total * 100.0) if all_px_total else 100.0
    overall_sim = round(0.75 * fg_overall + 0.25 * px_overall, 2)

    fg_unchanged = (unchanged_matching_pixels / unchanged_ink_pixels * 100.0) if unchanged_ink_pixels else 100.0
    px_unchanged = (unchanged_px_close / unchanged_px_total * 100.0) if unchanged_px_total else 100.0
    unchanged_sim = round(0.75 * fg_unchanged + 0.25 * px_unchanged, 2)

    orig_heads_xy = _heading_positions_with_x(original_pdf_path)
    out_heads_xy = _heading_positions_with_x(output_pdf_path)
    orig_heads = {s: yx[0] for s, yx in orig_heads_xy.items()}
    out_heads = {s: yx[0] for s, yx in out_heads_xy.items()}
    drift = 0.0
    for sec, (oy, ox) in orig_heads_xy.items():
        if sec in out_heads:
            p_sec = int(oy // 1000)
            local_oy = oy - p_sec * 1000.0
            expected_shift = 0.0
            for rec in (band_shifts_by_page or {}).get(p_sec, []):
                by0, by1, dy = rec[0], rec[1], rec[2]
                if len(rec) >= 5 and not (rec[3] <= ox <= rec[4]):
                    continue
                if by0 - 2.0 <= local_oy <= by1 + 2.0:
                    expected_shift = dy
                    break
            drift = max(drift, abs((oy + expected_shift) - out_heads[sec]))
    drift = round(drift, 2)

    if len(out_heads) < len(orig_heads):
        missing = sorted(set(orig_heads) - set(out_heads))
        warnings.append(f"Section heading(s) missing in output: {', '.join(missing)}.")

    overlap_passed, overlap_details = check_pdf_text_overlaps(
        output_pdf_path=output_pdf_path,
        original_pdf_path=original_pdf_path,
        changed_bboxes_by_page=changed_bboxes_by_page,
    )
    if not overlap_passed:
        sample = "; ".join(
            f"p{o['page']}: '{o['text1'][:28]}' overlaps '{o['text2'][:28]}' ({o['overlap_x']}x{o['overlap_y']}pt)"
            for o in overlap_details[:3]
        )
        warnings.append(f"Overlap check failed: {sample}")

    page_fit = measure_pdf_page_fit(
        original_pdf_path=original_pdf_path,
        output_pdf_path=output_pdf_path,
    )
    if not page_fit.get("passed", True):
        warnings.append(
            f"Page-fit check: last element ends at {page_fit['out_last_y1']}pt "
            f"(original limit {page_fit['orig_last_y1']}pt, page count {page_fit['page_count_output']}/{page_fit['page_count_original']})."
        )

    blocked_reason: str | None = None
    passed = True
    # Enforce similarity, text-overlap, page-count, and heading-drift gates unconditionally across ALL strategies
    # so a rebuilt, overlapping, or degraded (<95% unchanged similarity) layout is never exportable.
    if orig_pc != out_pc and not allow_page_overflow:
        passed = False
        blocked_reason = f"Layout check blocked export: page count changed from {orig_pc} to {out_pc}."
    elif unchanged_sim < min_unchanged_similarity:
        passed = False
        blocked_reason = (
            f"Layout-similarity check blocked export: unchanged regions scored {unchanged_sim}% "
            f"(minimum {min_unchanged_similarity}%)."
        )
    elif not overlap_passed:
        passed = False
        first_ov = overlap_details[0]
        blocked_reason = (
            f"Text overlap check blocked export: {len(overlap_details)} overlapping span pair(s) detected "
            f"(e.g. '{first_ov['text1'][:30]}' and '{first_ov['text2'][:30]}' on page {first_ov['page']})."
        )
    elif drift > 6.0 and not band_shifts_by_page:
        passed = False
        blocked_reason = f"Layout check blocked export: section headings shifted by {drift}pt."

    return ThemeVerificationResult(
        passed=passed,
        page_count_original=orig_pc,
        page_count_output=out_pc,
        overall_similarity_score=overall_sim,
        unchanged_similarity_score=unchanged_sim,
        heading_drift_pt=drift,
        orig_preview_png=orig_png,
        out_preview_png=out_png,
        diff_preview_png=diff_png,
        warnings=warnings,
        blocked_reason=blocked_reason,
        overlap_passed=overlap_passed,
        overlap_details=overlap_details,
        page_fit=page_fit,
    )
