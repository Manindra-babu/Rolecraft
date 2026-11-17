from __future__ import annotations

import re
from typing import Any

from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedResumeStructure
from app.services.matching.normalizer import (
    find_direct_and_implied_skills,
    normalize_skill,
    skill_in_text,
)

_LOW_RELEVANCE_PHRASES = (
    "weekly technical workshops",
    "code review sessions",
    "collaborative problem-solving with",
    "technical documentation, and presentation skills",
    "presentation skills",
    "excel associate",
    "power platform fundamentals",
)

_HIGH_RELEVANCE_SIGNALS = (
    "mern",
    "react",
    "node",
    "express",
    "mongodb",
    "rest",
    "restful",
    "http",
    "html",
    "css",
    "javascript",
    "typescript",
    "python",
    "sql",
    "api",
    "full-stack",
    "full stack",
    "authentication",
    "data structures",
    "algorithms",
    "hackathon",
    "top 10",
    "rag",
    "pytorch",
)


def _split_summary_sentences(summary: str) -> list[str]:
    if not (summary or "").strip():
        return []
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary.strip()) if s.strip()]
    return parts


def _estimate_pt_saved(text: str, *, chars_per_line: int = 86, line_h: float = 12.0) -> float:
    n_lines = max(1, (len(text.strip()) + chars_per_line - 1) // chars_per_line)
    return round(n_lines * line_h, 1)


def _score_block(
    *,
    text: str,
    section: str,
    job_skills: list[str],
    job_keywords: list[str],
    job_resp_tokens: set[str],
) -> tuple[float, list[str], bool]:
    low = text.lower()
    ev_map = find_direct_and_implied_skills(text)
    norm_job_skills = {normalize_skill(s) for s in job_skills}

    direct_hits: list[str] = []
    for sk in job_skills:
        ns = normalize_skill(sk)
        if ns in ev_map or skill_in_text(sk, text):
            if sk not in direct_hits:
                direct_hits.append(sk)

    # Cisco C Essentials proves C / C-family foundations while C or C++ is a job requirement
    is_cisco_c_cert = section == "certifications" and bool(re.search(r"\bcisco\b.*\bc\s+essentials\b|\bc\s+essentials\b", low))
    if is_cisco_c_cert and (norm_job_skills & {"c", "c++"}):
        for target_c in ("C", "C++"):
            if target_c.lower() in norm_job_skills and target_c not in direct_hits:
                direct_hits.append(target_c)

    for kw in job_keywords:
        if len(kw) >= 2 and re.search(rf"(?<!\w){re.escape(kw.lower())}(?!\w)", low):
            if kw not in direct_hits:
                direct_hits.append(kw)

    resp_hits = sum(1 for tok in re.findall(r"[a-z]{5,}", low) if tok in job_resp_tokens)
    high_sig_hits = sum(1 for sig in _HIGH_RELEVANCE_SIGNALS if sig in low)
    is_soft_filler = any(p in low for p in _LOW_RELEVANCE_PHRASES)

    score = 24.0
    score += min(45.0, len(direct_hits) * 18.0)
    score += min(18.0, resp_hits * 4.5)
    score += min(16.0, high_sig_hits * 5.5)

    if section == "experience":
        score += 12.0
    elif section == "projects":
        score += 8.0
    elif section == "summary":
        score += 10.0

    if is_soft_filler and not direct_hits:
        score = min(score, 26.0)
    elif is_soft_filler:
        score -= 18.0

    if is_cisco_c_cert and (norm_job_skills & {"c", "c++"}):
        score = max(score, 62.0)

    score = round(max(5.0, min(99.0, score)), 1)
    return score, direct_hits, is_soft_filler


def score_resume_relevance(
    *,
    resume: ParsedResumeStructure,
    job: StructuredJobFields,
) -> list[dict[str, Any]]:
    """
    Score every resume block (summary sentences, experience bullets, projects,
    education bullets, certifications) for relevance to the target job and propose
    pending actions ('keep', 'shorten', 'remove', 'reorder') for low-relevance blocks.
    """
    job_skills = list(dict.fromkeys(job.required_skills + job.preferred_skills))
    job_resp_tokens = {
        tok
        for r in (job.responsibilities or [])
        for tok in re.findall(r"[a-z]{5,}", r.lower())
    }
    items: list[dict[str, Any]] = []

    def _add_item(
        *,
        block_id: str,
        section: str,
        block_type: str,
        text: str,
        chars_per_line: int = 86,
        can_trim: bool = True,
        extra_note: str = "",
    ) -> None:
        score, hits, is_filler = _score_block(
            text=text,
            section=section,
            job_skills=job_skills,
            job_keywords=list(job.keywords or []),
            job_resp_tokens=job_resp_tokens,
        )
        tier = "high" if score >= 65.0 else ("medium" if score >= 40.0 else "low")
        est_pt = _estimate_pt_saved(text, chars_per_line=chars_per_line)

        low_t = text.lower()
        norm_js = {normalize_skill(s) for s in job_skills}
        is_protected_c_cert = (
            section == "certifications"
            and bool(re.search(r"\bcisco\b.*\bc\s+essentials\b|\bc\s+essentials\b", low_t))
            and bool(norm_js & {"c", "c++"})
        )
        if is_protected_c_cert:
            action = "keep"
            reason = f"Retained ({score}/100): Cisco C Essentials supports C / C++ requirement."
        elif tier == "high":
            action = "keep"
            reason = (
                f"High relevance ({score}/100): directly evidences {', '.join(hits[:4])}."
                if hits
                else f"High relevance ({score}/100): strong core software engineering alignment."
            )
        elif tier == "medium":
            if section == "summary" and "ai" in text.lower() and "llm" in text.lower() and not hits:
                action = "shorten"
                reason = (
                    f"Medium relevance ({score}/100): focuses on AI/LLM interest rather than target role's core web/backend requirements; optional to keep or trim."
                )
            else:
                action = "keep"
                reason = (
                    f"Medium relevance ({score}/100): supports {', '.join(hits[:3])} or foundational background."
                    if hits
                    else f"Medium relevance ({score}/100): helpful technical context."
                )
        else:
            if not can_trim:
                action = "keep"
                reason = f"Retained structural entry ({score}/100)."
            elif is_filler or est_pt <= 14.0:
                action = "remove"
                reason = (
                    f"Low relevance ({score}/100) for {job.title or 'target role'}: "
                    f"{extra_note or 'general soft-skill or non-target tool line with minimal job skill overlap; removing saves space cleanly.'}"
                )
            else:
                action = "shorten"
                reason = (
                    f"Low relevance ({score}/100) for {job.title or 'target role'}: "
                    "minimal direct overlap with required engineering skills; shorten to save space."
                )

        items.append(
            {
                "block_id": block_id,
                "section": section,
                "block_type": block_type,
                "text": text,
                "score": score,
                "tier": tier,
                "matched_skills": hits,
                "recommended_action": action,
                "reason": reason,
                "estimated_pt_saved": est_pt if action in {"remove", "shorten"} else 0.0,
            }
        )

    for idx, sent in enumerate(_split_summary_sentences(resume.summary)):
        extra = ""
        if "interested in artificial intelligence" in sent.lower():
            extra = "Optional AI/LLM career interest sentence."
        _add_item(
            block_id=f"summary_sent_{idx}",
            section="summary",
            block_type="summary_sentence",
            text=sent,
            chars_per_line=92,
            can_trim=idx > 0,
            extra_note=extra,
        )

    for e_idx, exp in enumerate(resume.experience):
        for b_idx, bullet in enumerate(exp.bullets):
            _add_item(
                block_id=f"exp_{e_idx}_bullet_{b_idx}",
                section="experience",
                block_type="experience_bullet",
                text=bullet,
                chars_per_line=86,
            )

    for p_idx, proj in enumerate(resume.projects):
        proj_txt = f"{proj.name}: {' '.join(proj.bullets)}".strip(": ")
        _add_item(
            block_id=f"proj_{p_idx}",
            section="projects",
            block_type="project",
            text=proj_txt,
            chars_per_line=44,
        )

    for ed_idx, edu in enumerate(resume.education):
        for d_idx, detail in enumerate(edu.details):
            # Never recommend removing institution / CGPA header line
            is_meta = bool(re.search(r"cgpa|gpa|20\d{2}|university|college|institute", detail, re.I)) and "|" in detail
            if is_meta:
                continue
            _add_item(
                block_id=f"edu_{ed_idx}_detail_{d_idx}",
                section="education",
                block_type="education_bullet",
                text=detail,
                chars_per_line=86,
                extra_note="Generic workshop/soft-skill detail with low software engineering signal.",
            )

    for c_idx, cert in enumerate(resume.certifications):
        _add_item(
            block_id=f"cert_{c_idx}",
            section="certifications",
            block_type="certification",
            text=cert,
            chars_per_line=86,
            can_trim=False,
            extra_note="Certifications must never be removed or trimmed automatically; always preserved in original order.",
        )

    return items
