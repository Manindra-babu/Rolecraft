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

    # Build modified/replaced originals list so edited spans are expected to differ
    modified_norms: list[str] = []
    for ch in applied_changes or []:
        if ch.get("status") not in (None, "accepted", "edited") or ch.get("validation_status") == "blocked":
            continue
        orig = _norm_cmp(str(ch.get("original_text") or ""))
        if orig:
            modified_norms.append(orig)
        loc = ch.get("target_location") or {}
        if isinstance(loc, dict) and loc.get("text"):
            modified_norms.append(_norm_cmp(str(loc["text"])))

    # 1. Section heading order check
    orig_heads = _heading_positions(original_pdf_path)
    out_heads = _heading_positions(output_pdf_path)
    orig_ordered_secs = [sec for sec, _ in sorted(orig_heads.items(), key=lambda kv: kv[1])]
    missing_secs = [sec for sec in orig_ordered_secs if sec not in out_heads]
    if missing_secs:
        errors.append(f"Completeness check failed: missing section heading(s) {', '.join(missing_secs)}.")
    else:
        last_y = -1.0
        for sec in orig_ordered_secs:
            oy = out_heads[sec]
            if oy < last_y - 2.0:
                errors.append(f"Completeness check failed: section '{sec}' is out of original reading order.")
                break
            last_y = oy

    # 2. Untouched text content preservation check
    for pno, page in enumerate(orig_doc):
        tdict = page.get_text("dict")
        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                raw_ln = "".join(s.get("text", "") for s in ln.get("spans", []))
                norm_ln = _norm_cmp(raw_ln)
                if len(norm_ln) < 18:
                    continue
                if any(norm_ln in mod or mod[:24] in norm_ln for mod in modified_norms if len(mod) >= 8):
                    continue
                # Strip trailing hyphen/line-wrap spaces when comparing
                probe = norm_ln[:36]
                if probe not in out_full_norm:
                    errors.append(f"Completeness check failed: original content missing ('{raw_ln.strip()[:60]}').")
                    break
            if len(errors) >= 4:
                break

    # 3. Hyperlink preservation check
    orig_uris = {
        str(lk.get("uri")).strip()
        for page in orig_doc
        for lk in (page.get_links() or [])
        if lk.get("uri")
    }
    out_uris = {
        str(lk.get("uri")).strip()
        for page in out_doc
        for lk in (page.get_links() or [])
        if lk.get("uri")
    }
    lost_uris = sorted(orig_uris - out_uris)
    if lost_uris:
        errors.append(f"Completeness check failed: lost original hyperlink(s): {', '.join(lost_uris[:3])}.")

    orig_doc.close()
    out_doc.close()
    return len(errors) == 0, errors


def _heading_positions(pdf_path: Path) -> dict[str, float]:
    positions: dict[str, float] = {}
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
                    positions[sec] = pno * 1000.0 + float(ln["bbox"][1])
    doc.close()
    return positions


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

        def _orig_y_for_out_px(y_out_px: int) -> tuple[int, bool]:
            """Return (mapped_orig_y_px, is_trimmed_interband_gap)."""
            if not page_shifts:
                return y_out_px, False
            y_out_pt = y_out_px / scale
            for by0, by1, dy in page_shifts:
                if by0 + dy <= y_out_pt <= by1 + dy:
                    y_orig_px = int(round((y_out_pt - dy) * scale))
                    return max(0, min(p_orig.height - 1, y_orig_px)), False
            # Pixel falls inside a newly adjusted whitespace gap between shifted bands
            return y_out_px, True

        for y in range(h):
            row_masked = [r for r in masked_rects_px if r[1] <= y <= r[3]]
            y_orig_mapped, is_gap_row = _orig_y_for_out_px(y)
            row_off_orig = y_orig_mapped * p_orig.stride
            row_off_out = y * p_out.stride
            for x in range(w):
                i2 = row_off_out + x * 3
                r2, g2, b2 = s_out[i2], s_out[i2 + 1], s_out[i2 + 2]
                if is_gap_row:
                    # In a trimmed whitespace gap between bands: expect clean white paper in out_doc
                    r1, g1, b1 = 255, 255, 255
                else:
                    i1 = row_off_orig + x * 3
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

    orig_heads = _heading_positions(original_pdf_path)
    out_heads = _heading_positions(output_pdf_path)
    drift = 0.0
    for sec, oy in orig_heads.items():
        if sec in out_heads:
            p_sec = int(oy // 1000)
            local_oy = oy - p_sec * 1000.0
            expected_shift = 0.0
            for by0, by1, dy in (band_shifts_by_page or {}).get(p_sec, []):
                if by0 - 2.0 <= local_oy <= by1 + 2.0:
                    expected_shift = dy
                    break
            drift = max(drift, abs((oy + expected_shift) - out_heads[sec]))
    drift = round(drift, 2)

    if len(out_heads) < len(orig_heads):
        missing = sorted(set(orig_heads) - set(out_heads))
        warnings.append(f"Section heading(s) missing in output: {', '.join(missing)}.")

    blocked_reason: str | None = None
    passed = True
    # Enforce similarity, page-count, and heading-drift gates unconditionally across ALL strategies
    # so a rebuilt or degraded (<95% unchanged similarity) layout is never exportable.
    if orig_pc != out_pc and not allow_page_overflow:
        passed = False
        blocked_reason = f"Layout check blocked export: page count changed from {orig_pc} to {out_pc}."
    elif unchanged_sim < min_unchanged_similarity:
        passed = False
        blocked_reason = (
            f"Layout-similarity check blocked export: unchanged regions scored {unchanged_sim}% "
            f"(minimum {min_unchanged_similarity}%)."
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
    )
