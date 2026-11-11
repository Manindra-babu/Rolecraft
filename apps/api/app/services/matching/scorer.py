import math
from typing import Any

from app.schemas.github import AnalyzedRepo
from app.schemas.matching import (
    CompatibilityBreakdown,
    SkillAssessmentItem,
    SubScoreExplanation,
)
from app.schemas.resume import ParsedResumeStructure
from app.services.llm.base import EmbeddingClient
from app.services.matching.normalizer import normalize_skill, skill_in_text


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _semantic_similarity_map(
    target_terms: list[str],
    evidence_texts: list[str],
    embedder: EmbeddingClient | None,
) -> dict[str, float]:
    """Compute best cosine similarity for each target term against evidence snippets."""
    if not embedder or not target_terms or not evidence_texts:
        return {}
    try:
        all_texts = target_terms + evidence_texts
        vectors = embedder.embed_texts(all_texts)
        if len(vectors) != len(all_texts):
            return {}
        t_vecs = vectors[: len(target_terms)]
        e_vecs = vectors[len(target_terms) :]
        scores: dict[str, float] = {}
        for term, t_vec in zip(target_terms, t_vecs):
            best = max((_cosine_similarity(t_vec, e_vec) for e_vec in e_vecs), default=0.0)
            scores[term] = best
        return scores
    except Exception:
        return {}


def assess_skills_and_score(
    *,
    resume: ParsedResumeStructure,
    job_required_skills: list[str],
    job_preferred_skills: list[str],
    job_responsibilities: list[str],
    job_keywords: list[str],
    github_repos: list[AnalyzedRepo] | None = None,
    embedder: EmbeddingClient | None = None,
) -> tuple[CompatibilityBreakdown, list[SkillAssessmentItem]]:
    """
    Evaluate every job requirement into one of 5 evidence categories and compute
    an explainable compatibility breakdown.
    """
    active_repos = [r for r in (github_repos or []) if r.excluded_reason is None]

    resume_skills_norm = {normalize_skill(s): s for s in resume.skills}
    exp_bullets_text = "\n".join(
        b for exp in resume.experience for b in exp.bullets
    )
    proj_bullets_text = "\n".join(
        b for proj in resume.projects for b in proj.bullets
    ) + "\n" + "\n".join(
        f"{p.name} {' '.join(p.tech_stack)}" for p in resume.projects
    )
    resume_evidence_text = "\n".join([resume.summary, exp_bullets_text, proj_bullets_text])

    # Evidence snippets for semantic embedding check
    evidence_snippets = [b for exp in resume.experience for b in exp.bullets] + [
        b for proj in resume.projects for b in proj.bullets
    ]
    if resume.skills:
        evidence_snippets.append("Skills: " + ", ".join(resume.skills))

    all_job_skills: list[tuple[str, str]] = []
    seen_norm: set[str] = set()
    for sk in job_required_skills:
        n = normalize_skill(sk)
        if n and n not in seen_norm:
            seen_norm.add(n)
            all_job_skills.append((sk, "required"))
    for sk in job_preferred_skills:
        n = normalize_skill(sk)
        if n and n not in seen_norm:
            seen_norm.add(n)
            all_job_skills.append((sk, "preferred"))

    sem_scores = _semantic_similarity_map(
        [sk for sk, _ in all_job_skills], evidence_snippets, embedder
    )

    assessments: list[SkillAssessmentItem] = []
    req_matched: list[str] = []
    req_missing: list[str] = []
    pref_matched: list[str] = []
    pref_missing: list[str] = []

    for skill, importance in all_job_skills:
        norm = normalize_skill(skill)
        in_skills_list = norm in resume_skills_norm or any(
            skill_in_text(skill, s) for s in resume.skills
        )
        in_bullets = skill_in_text(skill, resume_evidence_text)

        matching_repos = [
            r.name
            for r in active_repos
            if any(normalize_skill(rs) == norm for rs in r.inferred_skills)
            or skill_in_text(skill, f"{r.description} {r.readme_excerpt}")
        ]

        sem_sim = sem_scores.get(skill, 0.0)

        if in_bullets and (in_skills_list or len(matching_repos) >= 1):
            category = "demonstrated"
            repo_note = (
                f", {len(matching_repos)} GitHub {'repo' if len(matching_repos) == 1 else 'repos'}"
                if matching_repos
                else ""
            )
            evidence_summary = f"Demonstrated in resume bullets{repo_note}."
            confidence = 0.95
        elif in_bullets:
            category = "demonstrated"
            evidence_summary = "Demonstrated in resume experience/project bullets."
            confidence = 0.9
        elif in_skills_list:
            category = "under_represented"
            evidence_summary = (
                "Listed in resume Skills, but lacks concrete project or experience bullet evidence."
            )
            confidence = 0.75
        elif matching_repos:
            category = "github_only"
            evidence_summary = (
                f"Supported by GitHub ({', '.join(matching_repos[:3])}), absent from resume."
            )
            confidence = 0.82
        elif sem_sim >= 0.62:
            category = "needs_development"
            evidence_summary = (
                f"Adjacent experience detected semantically (similarity {sem_sim:.2f}), "
                "needs explicit practice or demonstration."
            )
            confidence = round(sem_sim, 2)
        else:
            category = "missing"
            evidence_summary = "No evidence found in resume or analyzed GitHub repositories."
            confidence = 0.9

        assessments.append(
            SkillAssessmentItem(
                skill_name=skill,
                category=category,
                importance=importance,
                evidence_summary=evidence_summary,
                confidence=confidence,
            )
        )

        is_satisfied = category in {"demonstrated", "under_represented", "github_only"}
        label = (
            f"{skill} (resume{', ' + str(len(matching_repos)) + ' projects' if matching_repos else ''})"
            if category in {"demonstrated", "under_represented"}
            else f"{skill} (GitHub: {', '.join(matching_repos[:2])})"
        )
        if importance == "required":
            (req_matched if is_satisfied else req_missing).append(
                label if is_satisfied else skill
            )
        else:
            (pref_matched if is_satisfied else pref_missing).append(
                label if is_satisfied else skill
            )

    # Sub-scores
    req_total = max(1, len([sk for sk, imp in all_job_skills if imp == "required"]))
    req_points = sum(
        1.0 if a.category == "demonstrated" else (0.7 if a.category in {"under_represented", "github_only"} else 0.0)
        for a in assessments
        if a.importance == "required"
    )
    req_score = round((req_points / req_total) * 100.0, 1) if any(imp == "required" for _, imp in all_job_skills) else 100.0

    pref_list = [a for a in assessments if a.importance == "preferred"]
    if pref_list:
        pref_points = sum(
            1.0 if a.category == "demonstrated" else (0.7 if a.category in {"under_represented", "github_only"} else 0.0)
            for a in pref_list
        )
        pref_score = round((pref_points / len(pref_list)) * 100.0, 1)
    else:
        pref_score = 100.0

    # Experience relevance (semantic + keyword overlap against job responsibilities)
    exp_items = [b for exp in resume.experience for b in exp.bullets]
    if job_responsibilities and exp_items:
        resp_sem = _semantic_similarity_map(job_responsibilities[:8], exp_items[:15], embedder)
        if resp_sem:
            avg_sim = sum(resp_sem.values()) / len(resp_sem)
            exp_score = round(min(100.0, max(20.0, avg_sim * 115.0)), 1)
        else:
            hits = sum(
                1
                for r in job_responsibilities
                if any(w in exp_bullets_text.lower() for w in r.lower().split() if len(w) > 4)
            )
            exp_score = round((hits / len(job_responsibilities)) * 100.0, 1)
    elif exp_items:
        exp_score = 70.0
    else:
        exp_score = 30.0

    # Keyword coverage
    kw_pool = list(dict.fromkeys(job_keywords or [sk for sk, _ in all_job_skills]))
    full_evidence_blob = resume_evidence_text + "\n" + " ".join(resume.skills) + "\n" + " ".join(
        " ".join(r.inferred_skills) for r in active_repos
    )
    kw_matched = [kw for kw in kw_pool if skill_in_text(kw, full_evidence_blob)]
    kw_missing = [kw for kw in kw_pool if kw not in kw_matched]
    kw_score = round((len(kw_matched) / max(1, len(kw_pool))) * 100.0, 1) if kw_pool else 100.0

    # Project relevance
    proj_count = len(resume.projects) + len(active_repos)
    if proj_count == 0:
        proj_score = 25.0
        proj_explanation = "No projects on resume or analyzed GitHub repos."
    else:
        relevant_projs = [
            p.name
            for p in resume.projects
            if any(skill_in_text(sk, f"{p.name} {' '.join(p.tech_stack)} {' '.join(p.bullets)}") for sk, _ in all_job_skills)
        ] + [
            r.name
            for r in active_repos
            if any(normalize_skill(sk) in {normalize_skill(rs) for rs in r.inferred_skills} for sk, _ in all_job_skills)
        ]
        proj_score = round(min(100.0, 40.0 + 20.0 * len(set(relevant_projs))), 1)
        proj_explanation = (
            f"Relevant projects: {', '.join(list(dict.fromkeys(relevant_projs))[:4])}."
            if relevant_projs
            else "Projects present, but few directly use the target role's stack."
        )

    overall = round(
        0.35 * req_score
        + 0.15 * pref_score
        + 0.20 * exp_score
        + 0.15 * kw_score
        + 0.15 * proj_score,
        1,
    )

    breakdown = CompatibilityBreakdown(
        overall_score=overall,
        required_skills=SubScoreExplanation(
            name="Required Skills",
            score=req_score,
            weight=0.35,
            explanation=(
                f"Matched: {', '.join(req_matched[:5]) or 'None'}. "
                f"Missing: {', '.join(req_missing[:5]) or 'None'}."
            ),
            matched_items=req_matched,
            missing_items=req_missing,
        ),
        preferred_skills=SubScoreExplanation(
            name="Preferred Skills",
            score=pref_score,
            weight=0.15,
            explanation=(
                f"Matched: {', '.join(pref_matched[:4]) or 'None'}. "
                f"Missing: {', '.join(pref_missing[:4]) or 'None'}."
            ),
            matched_items=pref_matched,
            missing_items=pref_missing,
        ),
        experience_relevance=SubScoreExplanation(
            name="Experience Relevance",
            score=exp_score,
            weight=0.20,
            explanation=f"Evaluated {len(exp_items)} experience bullets against {len(job_responsibilities)} job responsibilities.",
        ),
        keyword_coverage=SubScoreExplanation(
            name="Keyword Coverage",
            score=kw_score,
            weight=0.15,
            explanation=f"Covered {len(kw_matched)} of {len(kw_pool)} key role terms.",
            matched_items=kw_matched,
            missing_items=kw_missing,
        ),
        project_relevance=SubScoreExplanation(
            name="Project Relevance",
            score=proj_score,
            weight=0.15,
            explanation=proj_explanation,
        ),
    )

    return breakdown, assessments
