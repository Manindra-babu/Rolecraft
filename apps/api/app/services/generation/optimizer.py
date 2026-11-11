import re
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.github import AnalyzedRepo, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedResumeStructure
from app.services.generation.validator import validate_claim_against_evidence
from app.services.llm.base import LLMClient
from app.services.llm.demo_client import DemoLLMClient
from app.services.matching.normalizer import normalize_skill, skill_in_text


class BulletRewriteSchema(BaseModel):
    rewritten_bullet: str
    reason: str


def _deterministic_bullet_tailor(bullet: str, job_skills: list[str]) -> tuple[str, str] | None:
    """
    Conservative, truthful deterministic refinement when LLM is in Demo mode or skipped.
    Strengthens weak openings without adding any unverified claim.
    """
    cleaned = bullet.strip()
    weak_starts = [
        (r"^worked on\s+", "Developed "),
        (r"^helped with\s+", "Contributed to "),
        (r"^responsible for\s+", "Managed "),
        (r"^made\s+", "Built "),
    ]
    updated = cleaned
    changed = False
    for pattern, repl in weak_starts:
        if re.match(pattern, updated, flags=re.IGNORECASE):
            updated = re.sub(pattern, repl, updated, count=1, flags=re.IGNORECASE)
            changed = True
            break

    if not changed:
        # Check if bullet already matches job skills; if so, tighten wording slightly if trailing period missing
        matched = [sk for sk in job_skills if skill_in_text(sk, cleaned)]
        if matched and not cleaned.endswith("."):
            updated = f"{cleaned}."
            changed = True
        elif matched:
            return cleaned, f"Aligned with target skill(s): {', '.join(matched[:3])}."
    return (updated, "Clarified action verb using existing resume evidence.") if changed else None


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
    the truthfulness validator before returning them to the user.
    """
    changes: list[dict[str, Any]] = []
    job_skills = list(dict.fromkeys(job.required_skills + job.preferred_skills + job.keywords))
    elements_by_id = (element_map or {}).get("elements", {})

    # 1. Experience bullet rewrites (using quality model tier when live LLM available)
    for exp in resume.experience:
        bullet_el_ids = [
            eid
            for eid in exp.element_ids
            if elements_by_id.get(eid, {}).get("is_bullet")
        ]
        for idx, bullet in enumerate(exp.bullets):
            target_eid = bullet_el_ids[idx] if idx < len(bullet_el_ids) else None
            target_loc = elements_by_id.get(target_eid, {}) if target_eid else {}

            rewritten = bullet
            reason = f"Retained verified bullet for {exp.company or exp.title}."

            if llm is not None and not isinstance(llm, DemoLLMClient):
                prompt = (
                    "Rewrite this single resume bullet to be clear, concise, and relevant to the target role. "
                    "STRICT RULE: Use ONLY technologies, numbers, metrics, and scope present in the original bullet "
                    "or confirmed resume evidence. NEVER invent percentages, counts, tools, or leadership verbs.\n\n"
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

            val = validate_claim_against_evidence(
                new_text=rewritten,
                original_text=bullet,
                resume=resume,
                raw_resume_text=raw_resume_text,
                github_repos=github_repos,
                confirmed_learning=confirmed_learning,
            )

            changes.append(
                {
                    "section": "experience",
                    "change_type": "reworded",
                    "target_location": target_loc,
                    "original_text": bullet,
                    "new_text": rewritten,
                    "reason": reason,
                    "evidence_refs": val.evidence_refs,
                    "validation_status": val.status,
                    "validation_warnings": val.warnings,
                    "status": "pending",
                }
            )

    # 2. Skill reordering (safe low-risk change eligible for bulk accept)
    if resume.skills:
        norm_job_skills = {normalize_skill(s) for s in job_skills}
        prioritized = [s for s in resume.skills if normalize_skill(s) in norm_job_skills]
        remaining = [s for s in resume.skills if normalize_skill(s) not in norm_job_skills]
        reordered = prioritized + remaining
        if reordered != resume.skills:
            # Find skills element location
            skills_loc = next(
                (
                    el
                    for el in elements_by_id.values()
                    if el.get("section") == "skills" and not el.get("is_heading")
                ),
                {},
            )
            orig_skills_line = ", ".join(resume.skills)
            new_skills_line = ", ".join(reordered)
            val = validate_claim_against_evidence(
                new_text=new_skills_line,
                original_text=orig_skills_line,
                resume=resume,
                raw_resume_text=raw_resume_text,
                github_repos=github_repos,
                confirmed_learning=confirmed_learning,
            )
            changes.append(
                {
                    "section": "skills",
                    "change_type": "reordered",
                    "target_location": skills_loc,
                    "original_text": orig_skills_line,
                    "new_text": new_skills_line,
                    "reason": (
                        f"Moved job-matching skills ({', '.join(prioritized[:5])}) to the front "
                        "without adding any unverified skills."
                    ),
                    "evidence_refs": [{"source": "resume", "detail": "Existing Skills section"}],
                    "validation_status": val.status,
                    "validation_warnings": val.warnings,
                    "status": "pending",
                }
            )

    # 3. Approved GitHub project additions (require individual user decision)
    for rec in approved_github_recs or []:
        if rec.decision != "approved":
            continue
        bullets = rec.draft_bullets or [rec.reason]
        proj_block = f"{rec.repo_name} | {', '.join(rec.matched_skills[:4])} | {rec.html_url}\n" + "\n".join(
            f"• {b}" for b in bullets
        )
        val = validate_claim_against_evidence(
            new_text=proj_block,
            original_text="",
            resume=resume,
            raw_resume_text=raw_resume_text,
            github_repos=github_repos,
            confirmed_learning=confirmed_learning,
        )
        changes.append(
            {
                "section": "projects",
                "change_type": "added_from_github",
                "target_location": {"insert_section": "projects", "repo_name": rec.repo_name},
                "original_text": "",
                "new_text": proj_block,
                "reason": rec.reason,
                "evidence_refs": [
                    {"source": "github", "detail": f"Verified GitHub repository {rec.full_name}"}
                ],
                "validation_status": val.status,
                "validation_warnings": val.warnings,
                "status": "pending",
            }
        )

    # 4. User-confirmed active learning (Section 5.7: honest "Currently learning" section only)
    for item in confirmed_learning or []:
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        val = validate_claim_against_evidence(
            new_text=label,
            original_text="",
            resume=resume,
            raw_resume_text=raw_resume_text,
            github_repos=github_repos,
            confirmed_learning=confirmed_learning,
        )
        changes.append(
            {
                "section": "skill_development",
                "change_type": "user_confirmed_skill",
                "target_location": {"insert_section": "skill_development"},
                "original_text": "",
                "new_text": label,
                "reason": "Explicitly confirmed active learning (kept separate from proficient Skills).",
                "evidence_refs": [{"source": "user_confirmed", "detail": label}],
                "validation_status": val.status,
                "validation_warnings": val.warnings,
                "status": "pending",
            }
        )

    return changes
