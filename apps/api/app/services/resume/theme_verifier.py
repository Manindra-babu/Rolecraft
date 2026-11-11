from __future__ import annotations

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
    strategy_used: str = "inplace_pdf",
    dpi: int = 120,
    min_unchanged_similarity: float = 95.0,
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

        for y in range(h):
            row_masked = [r for r in masked_rects_px if r[1] <= y <= r[3]]
            row_off_orig = y * p_orig.stride
            row_off_out = y * p_out.stride
            for x in range(w):
                i1 = row_off_orig + x * 3
                i2 = row_off_out + x * 3
                r1, g1, b1 = s_orig[i1], s_orig[i1 + 1], s_orig[i1 + 2]
                r2, g2, b2 = s_out[i2], s_out[i2 + 1], s_out[i2 + 2]
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
            drift = max(drift, abs(oy - out_heads[sec]))
    drift = round(drift, 2)

    if len(out_heads) < len(orig_heads):
        missing = sorted(set(orig_heads) - set(out_heads))
        warnings.append(f"Section heading(s) missing in output: {', '.join(missing)}.")

    blocked_reason: str | None = None
    passed = True
    if strategy_used in {"inplace_exact", "inplace_pdf"}:
        if orig_pc != out_pc:
            passed = False
            blocked_reason = f"Layout check blocked export: page count changed from {orig_pc} to {out_pc}."
        elif unchanged_sim < min_unchanged_similarity:
            passed = False
            blocked_reason = (
                f"Layout-similarity check blocked export: unchanged regions scored {unchanged_sim}% "
                f"(minimum {min_unchanged_similarity}%)."
            )
        elif drift > 6.0:
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
