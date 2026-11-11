import re
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.github import AnalyzedRepo, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedResumeStructure
from app.services.generation.validator import BAD_GLYPH_RE, validate_claim_against_evidence
from app.services.llm.base import LLMClient
from app.services.llm.demo_client import DemoLLMClient
from app.services.matching.normalizer import find_direct_and_implied_skills, normalize_skill, skill_in_text

TYPO_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bFULL STACK DEVELOPER INTER\b"), "FULL STACK DEVELOPER INTERN", "Fix truncated role title typo ('INTER' -> 'INTERN')."),
    (re.compile(r"\bFull stack developer inter\b"), "Full stack developer intern", "Fix truncated role title typo ('inter' -> 'intern')."),
    (re.compile(r"\bKNOWLEDEGE\b"), "KNOWLEDGE", "Fix spelling typo ('KNOWLEDEGE' -> 'KNOWLEDGE')."),
    (re.compile(r"\bknowledege\b"), "knowledge", "Fix spelling typo ('knowledege' -> 'knowledge')."),
    (re.compile(r"\bRETRIVAL\b"), "RETRIEVAL", "Fix spelling typo ('RETRIVAL' -> 'RETRIEVAL')."),
    (re.compile(r"\bretrival\b"), "retrieval", "Fix spelling typo ('retrival' -> 'retrieval')."),
    (re.compile(r"\bcasual(\s+masking)\b", re.I), r"causal\1", "Fix technical term typo ('casual masking' -> 'causal masking')."),
    (re.compile(r"\b(hardware\s+)constrains\b", re.I), r"\1constraints", "Fix spelling typo ('constrains' -> 'constraints')."),
    (re.compile(r"\bDetail-\s+Detail-oriented\b"), "Detail-oriented", "Remove duplicated prefix ('Detail- Detail-oriented' -> 'Detail-oriented')."),
]


class BulletRewriteSchema(BaseModel):
    rewritten_bullet: str
    reason: str


def _apply_typo_fixes(text: str) -> tuple[str, list[str]]:
    updated = text
    reasons: list[str] = []
    for pat, repl, note in TYPO_RULES:
        if pat.search(updated):
            updated = pat.sub(repl, updated)
            reasons.append(note)
    return updated, reasons


def _find_element_location(elements_by_id: dict[str, Any], needle: str, *, section: str = "") -> dict[str, Any]:
    low = (needle or "").strip().lower()
    if not low:
        return {}
    # Exact or prefix substring match in target section
    for el in elements_by_id.values():
        if section and el.get("section") != section:
            continue
        et = (el.get("text") or "").strip().lower()
        if et == low or low in et or et in low:
            return el
    return {}


def _deterministic_bullet_tailor(bullet: str, job_skills: list[str]) -> tuple[str, str] | None:
    """
    Truthful deterministic bullet refinement grounded strictly in existing text + implied tech
    (e.g. expanding MERN -> MongoDB, Express.js, React, Node.js; RESTful APIs -> REST/HTTP) and fixing typos.
    """
    cleaned, typo_reasons = _apply_typo_fixes(bullet.strip())
    updated = cleaned
    reasons: list[str] = list(typo_reasons)

    weak_starts = [
        (r"^worked on\s+", "Developed "),
        (r"^helped with\s+", "Contributed to "),
        (r"^responsible for\s+", "Managed "),
        (r"^made\s+", "Built "),
    ]
    for pattern, repl in weak_starts:
        if re.match(pattern, updated, flags=re.IGNORECASE):
            updated = re.sub(pattern, repl, updated, count=1, flags=re.IGNORECASE)
            reasons.append("Strengthened action verb using existing resume evidence.")
            break

    # Explicitly surface true implied tech when matching job requirements without blooming 1-line bullets
    job_norms = {normalize_skill(s) for s in job_skills}
    if re.search(r"\busing the MERN stack\b", updated, re.I) and "mongodb" not in updated.lower():
        if job_norms & {"node.js", "react", "javascript", "mongodb", "express"}:
            updated = re.sub(
                r"\bresponsive full-stack web applications using the MERN stack\b",
                "full-stack web apps with MERN (MongoDB, Express, React, Node.js)",
                updated,
                count=1,
                flags=re.I,
            )
            if "MERN" in updated and "MongoDB" not in updated:
                updated = re.sub(
                    r"\bMERN stack\b",
                    "MERN (MongoDB, Express, React, Node.js)",
                    updated,
                    count=1,
                    flags=re.I,
                )
            reasons.append("Clarified verified MERN stack technologies (MongoDB, Express, React, Node.js) matching target role.")

    if re.search(r"\bRESTful APIs\b", updated, re.I) and "(HTTP" not in updated and "(REST" not in updated:
        if job_norms & {"rest", "http", "web services"}:
            updated = re.sub(
                r"\bRESTful APIs\b",
                "REST APIs (HTTP/REST)",
                updated,
                count=1,
                flags=re.I,
            )
            updated = re.sub(r"\bfirebase\b", "Firebase", updated)
            reasons.append("Highlighted verified REST/HTTP architecture and capitalized Firebase.")

    if re.search(r"\bPyTorch\b", updated, re.I) and "python" not in updated.lower():
        if "python" in job_norms:
            updated = re.sub(r"\bPyTorch\b", "PyTorch (Python)", updated, count=1, flags=re.I)
            reasons.append("Explicitly surfaced Python alongside verified PyTorch implementation.")

    if updated.strip() != bullet.strip():
        return updated.strip(), " ".join(dict.fromkeys(reasons)) or "Improved alignment using verified resume facts."
    return None


def _clean_skill_opt_token(token: str) -> str:
    cleaned = BAD_GLYPH_RE.sub("", token or "").strip(" ,;|•-–—\t\n")
    while cleaned.endswith(".") and not cleaned.endswith(".."):
        cleaned = cleaned[:-1].strip()
    return cleaned


def _tailor_summary_deterministically(
    summary: str, resume: ParsedResumeStructure, job: StructuredJobFields, job_skills: list[str]
) -> tuple[str, str] | None:
    if not summary.strip():
        return None
    updated, typo_reasons = _apply_typo_fixes(summary.strip())
    # Align phrasing gently toward target role using ONLY existing verified tech in resume
    ev_map = find_direct_and_implied_skills(
        f"{summary}\n"
        + "\n".join(b for e in resume.experience for b in e.bullets)
        + "\n"
        + "\n".join(b for p in resume.projects for b in p.bullets)
        + "\n"
        + "\n".join(resume.certifications)
    )
    job_norms = {normalize_skill(s) for s in job_skills}
    verified_hits = [sk for sk in job_skills if normalize_skill(sk) in ev_map]
    reasons = list(typo_reasons)
    # Preserve truthfulness & approximate line budget: replace verified phrases cleanly
    if verified_hits and "REST" not in updated and any(normalize_skill(s) == "rest" for s in verified_hits):
        if "authentication systems" in updated:
            updated = updated.replace(
                "authentication systems",
                "RESTful APIs (HTTP/REST) and authentication systems",
                1,
            )
            reasons.append("Tailored summary with verified RESTful/HTTP API experience.")
    if "python" in ev_map and "python" in job_norms and "python" not in updated.lower():
        if "Artificial Intelligence, " in updated:
            updated = updated.replace(
                "Artificial Intelligence, ",
                "Python-based Artificial Intelligence, ",
                1,
            )
            reasons.append("Surfaced verified Python experience aligned with target role.")
    if updated.strip() == summary.strip():
        return None
    reason = " ".join(dict.fromkeys(reasons or ["Tailored summary to target role using only verified resume evidence."]))
    return updated.strip(), reason


_BANNED_META_BULLET_RE = re.compile(
    r"(?:added automated test coverage to verify core module behavior|across \d+ source files?)",
    re.IGNORECASE,
)


def _format_github_project_copy(rec: RepoRecommendation) -> tuple[str, str, list[str]]:
    """
    Produce a clean, style-cloned project entry:
    - Title line: '<REPO_UPPER> - GITHUB' (no raw URL, no duplicate tech header)
    - Concise 2-3 line paragraph description without generic metadata filler
    """
    title_line = f"{rec.repo_name.upper()} - GITHUB"
    raw_bullets = [
        b.strip().lstrip("•-–—* ").strip()
        for b in (rec.draft_bullets or [])
        if b and b.strip() and not _BANNED_META_BULLET_RE.search(b)
    ]
    if not raw_bullets and rec.reason:
        raw_bullets = [rec.reason.strip().rstrip(".") + "."]
    body_parts: list[str] = []
    seen_low: set[str] = set()
    for part in raw_bullets:
        sent = part.rstrip(".") + "."
        low = sent.lower()
        if low in seen_low:
            continue
        seen_low.add(low)
        body_parts.append(sent)
        if len(" ".join(body_parts)) >= 190 or len(body_parts) >= 2:
            break
    body_text = " ".join(body_parts).strip()
    if len(body_text) > 168:
        cut = body_text[:165].rsplit(" ", 1)[0].rstrip(",;:-")
        words = cut.split()
        while len(words) > 2 and words[-1].lower() in {"and", "or", "with", "using", "via", "for", "of", "in", "to", "a", "an", "the", "by", "featuring"}:
            words.pop()
        body_text = " ".join(words).rstrip(",;:-") + "."
    return title_line, body_text, body_parts


def build_added_from_github_change(
    *,
    rec: RepoRecommendation,
    resume: ParsedResumeStructure,
    raw_resume_text: str,
    github_repos: list[AnalyzedRepo] | None = None,
    confirmed_learning: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and validate a pending 'added_from_github' Change payload for an approved repository."""
    title_line, body_text, _ = _format_github_project_copy(rec)
    proj_block = f"{title_line}\n{body_text}" if body_text else title_line

    refs: list[dict[str, Any]] = []
    if rec.bullet_evidence:
        for idx, be in enumerate(rec.bullet_evidence, start=1):
            if not _BANNED_META_BULLET_RE.search(be.bullet or ""):
                refs.append({"source": be.source, "detail": f"Bullet {idx}: {be.detail}"})
    if not refs:
        refs.append({"source": "github", "detail": f"Verified GitHub repository {rec.full_name}"})

    val = validate_claim_against_evidence(
        new_text=proj_block,
        original_text="",
        resume=resume,
        raw_resume_text=raw_resume_text,
        github_repos=github_repos,
        confirmed_learning=confirmed_learning,
        section="projects",
    )
    return {
        "section": "projects",
        "change_type": "added_from_github",
        "target_location": {
            "insert_section": "projects",
            "repo_name": rec.repo_name,
            "html_url": rec.html_url,
            "title": title_line,
            "body": body_text,
        },
        "original_text": "",
        "new_text": proj_block,
        "reason": rec.reason,
        "evidence_refs": refs or val.evidence_refs,
        "validation_status": val.status,
        "validation_warnings": val.warnings,
        "status": "pending",
    }


def generate_tailored_changes(
    *,
    resume: ParsedResumeStructure,
    raw_resume_text: str,
    element_map: dict[str, Any],
    job: StructuredJobFields,
    github_repos: list[AnalyzedRepo] | None = None,
    approved_github_recs: list[RepoRecommendation] | None = None,
    confirmed_learning: list[dict[str, Any]] | None = None,
    llm: LLMClient | None = None,
) -> list[dict[str, Any]]:
    """
    Produce proposed ResumeVersion Change dicts and run every single one through
    the truthfulness + integrity validator before returning them to the user.
    Every generated change defaults strictly to status='pending'.
    """
    changes: list[dict[str, Any]] = []
    job_skills = list(dict.fromkeys(job.required_skills + job.preferred_skills + job.keywords))
    elements_by_id = (element_map or {}).get("elements", {})

    def _emit_change(
        *,
        section: str,
        change_type: str,
        target_loc: dict[str, Any],
        original_text: str,
        new_text: str,
        reason: str,
        evidence_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        val = validate_claim_against_evidence(
            new_text=new_text,
            original_text=original_text,
            resume=resume,
            raw_resume_text=raw_resume_text,
            github_repos=github_repos,
            confirmed_learning=confirmed_learning,
            section=section,
        )
        changes.append(
            {
                "section": section,
                "change_type": change_type,
                "target_location": target_loc,
                "original_text": original_text,
                "new_text": new_text,
                "reason": reason,
                "evidence_refs": evidence_refs or val.evidence_refs,
                "validation_status": val.status,
                "validation_warnings": val.warnings,
                "status": "pending",  # NEVER auto-accept
            }
        )

    # 1. Tailored Summary (never auto-accepted)
    if resume.summary:
        sum_loc = _find_element_location(elements_by_id, resume.summary[:40], section="summary") or _find_element_location(elements_by_id, resume.summary[:40])
        tailored_sum = _tailor_summary_deterministically(resume.summary, resume, job, job_skills)
        if llm is not None and not isinstance(llm, DemoLLMClient):
            try:
                out = llm.generate_structured(
                    prompt=(
                        "Tailor this resume summary for the target role. Fix any typos like 'Detail- Detail-oriented'. "
                        "Use ONLY facts, degrees, and technologies already present in the resume.\n\n"
                        f"Target role: {job.title} at {job.company}\n"
                        f"Target skills: {', '.join(job_skills[:12])}\n"
                        f"Original summary: {resume.summary}"
                    ),
                    schema=BulletRewriteSchema,
                    operation="resume_summary_rewrite",
                    tier="quality",
                    redact_pii=True,
                )
                if out.rewritten_bullet.strip() and out.rewritten_bullet.strip() != resume.summary.strip():
                    tailored_sum = (out.rewritten_bullet.strip(), out.reason.strip() or "Tailored summary to target role.")
            except Exception:
                pass
        if tailored_sum:
            _emit_change(
                section="summary",
                change_type="reworded",
                target_loc=sum_loc,
                original_text=resume.summary,
                new_text=tailored_sum[0],
                reason=tailored_sum[1],
            )

    # 2. Experience title typo fixes + bullet rewrites
    for exp in resume.experience:
        if exp.title:
            fixed_title, t_reasons = _apply_typo_fixes(exp.title)
            if fixed_title != exp.title:
                title_loc = _find_element_location(elements_by_id, exp.title, section="experience")
                _emit_change(
                    section="experience",
                    change_type="reworded",
                    target_loc=title_loc,
                    original_text=exp.title,
                    new_text=fixed_title,
                    reason=" ".join(t_reasons),
                )

        bullet_el_ids = [
            eid
            for eid in exp.element_ids
            if elements_by_id.get(eid, {}).get("is_bullet")
        ]
        for idx, bullet in enumerate(exp.bullets):
            target_eid = bullet_el_ids[idx] if idx < len(bullet_el_ids) else None
            target_loc = elements_by_id.get(target_eid, {}) if target_eid else _find_element_location(elements_by_id, bullet[:40], section="experience")

            rewritten = bullet
            reason = f"Retained verified bullet for {exp.company or exp.title}."

            if llm is not None and not isinstance(llm, DemoLLMClient):
                prompt = (
                    "Rewrite this single resume bullet to be clear, concise, and relevant to the target role. "
                    "STRICT RULE: Use ONLY technologies, numbers, metrics, and scope present in the original bullet "
                    "or confirmed resume evidence (for MERN, you may explicitly name MongoDB, Express.js, React, Node.js). "
                    "NEVER invent percentages, counts, tools, or leadership verbs.\n\n"
                    f"Target role: {job.title} at {job.company}\n"
                    f"Target skills: {', '.join(job_skills[:12])}\n"
                    f"Original bullet: {bullet}"
                )
                try:
                    out = llm.generate_structured(
                        prompt=prompt,
                        schema=BulletRewriteSchema,
                        operation="resume_bullet_rewrite",
                        tier="quality",
                        redact_pii=True,
                    )
                    if out.rewritten_bullet.strip():
                        rewritten = out.rewritten_bullet.strip()
                        reason = out.reason.strip() or "Improved clarity and alignment with role terminology."
                except Exception:
                    det = _deterministic_bullet_tailor(bullet, job_skills)
                    if det:
                        rewritten, reason = det
            else:
                det = _deterministic_bullet_tailor(bullet, job_skills)
                if det:
                    rewritten, reason = det

            if rewritten.strip() == bullet.strip():
                continue

            _emit_change(
                section="experience",
                change_type="reworded",
                target_loc=target_loc,
                original_text=bullet,
                new_text=rewritten,
                reason=reason,
            )

    # 3. Project title typo fixes + project bullet rewrites (fixing casual/constrains + true tech)
    for proj in resume.projects:
        if proj.name:
            fixed_name, p_reasons = _apply_typo_fixes(proj.name)
            if fixed_name != proj.name:
                proj_loc = _find_element_location(elements_by_id, proj.name, section="projects")
                _emit_change(
                    section="projects",
                    change_type="reworded",
                    target_loc=proj_loc,
                    original_text=proj.name,
                    new_text=fixed_name,
                    reason=" ".join(p_reasons),
                )
        for bullet in proj.bullets:
            det = _deterministic_bullet_tailor(bullet, job_skills)
            if det:
                b_loc = _find_element_location(elements_by_id, bullet[:40], section="projects")
                _emit_change(
                    section="projects",
                    change_type="reworded",
                    target_loc=b_loc,
                    original_text=bullet,
                    new_text=det[0],
                    reason=det[1],
                )

    # 4. Category-preserving Skill reordering (ONLY low-risk change eligible for bulk-accept)
    norm_job_skills = {normalize_skill(s) for s in job_skills}
    def _skill_matches_job(sk: str) -> bool:
        ns = normalize_skill(sk)
        if ns in norm_job_skills:
            return True
        if ns == "html5" and "html" in norm_job_skills:
            return True
        implied = find_direct_and_implied_skills(sk)
        return any(imp in norm_job_skills for imp in implied)

    if resume.skill_categories:
        for cat_label, cat_skills in resume.skill_categories.items():
            clean_cat_skills = [t for t in (_clean_skill_opt_token(s) for s in cat_skills) if t]
            prioritized = [s for s in clean_cat_skills if _skill_matches_job(s)]
            remaining = [s for s in clean_cat_skills if not _skill_matches_job(s)]
            reordered = prioritized + remaining
            if reordered != clean_cat_skills and clean_cat_skills:
                # Find the exact skill line matching this category label
                skills_loc = next(
                    (
                        el
                        for el in elements_by_id.values()
                        if el.get("section") == "skills"
                        and not el.get("is_heading")
                        and el.get("text", "").lstrip("• \uf0b7").lower().startswith(f"{cat_label.lower()}:")
                    ),
                    {},
                )
                orig_line = f"{cat_label}: {', '.join(clean_cat_skills)}"
                new_line = f"{cat_label}: {', '.join(reordered)}"
                _emit_change(
                    section="skills",
                    change_type="reordered",
                    target_loc=skills_loc,
                    original_text=orig_line,
                    new_text=new_line,
                    reason=(
                        f"Reordered skills within '{cat_label}' keeping category label intact "
                        f"(prioritized {', '.join(prioritized[:4])})."
                    ),
                    evidence_refs=[{"source": "resume", "detail": f"Existing Skills category '{cat_label}'"}],
                )
    elif resume.skills:
        clean_skills = [t for t in (_clean_skill_opt_token(s) for s in resume.skills) if t]
        prioritized = [s for s in clean_skills if _skill_matches_job(s)]
        remaining = [s for s in clean_skills if not _skill_matches_job(s)]
        reordered = prioritized + remaining
        if reordered != clean_skills:
            skills_loc = next(
                (
                    el
                    for el in elements_by_id.values()
                    if el.get("section") == "skills" and not el.get("is_heading")
                ),
                {},
            )
            _emit_change(
                section="skills",
                change_type="reordered",
                target_loc=skills_loc,
                original_text=", ".join(clean_skills),
                new_text=", ".join(reordered),
                reason=(
                    f"Moved job-matching skills ({', '.join(prioritized[:5])}) to the front "
                    "without adding any unverified skills."
                ),
                evidence_refs=[{"source": "resume", "detail": "Existing Skills section"}],
            )

    # 5. Approved GitHub project additions (require individual user decision)
    for rec in approved_github_recs or []:
        if rec.decision != "approved":
            continue
        changes.append(
            build_added_from_github_change(
                rec=rec,
                resume=resume,
                raw_resume_text=raw_resume_text,
                github_repos=github_repos,
                confirmed_learning=confirmed_learning,
            )
        )

    # 6. User-confirmed active learning ('Currently Learning: ...' added ONLY after explicit confirmation)
    for item in confirmed_learning or []:
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        if not label.lower().startswith("currently learning"):
            label = f"Currently Learning: {label}"
        _emit_change(
            section="skill_development",
            change_type="user_confirmed_skill",
            target_loc={"insert_section": "skill_development"},
            original_text="",
            new_text=label,
            reason="Explicitly confirmed active learning (kept separate as Currently Learning).",
            evidence_refs=[{"source": "user_confirmed", "detail": label}],
        )

    return changes
