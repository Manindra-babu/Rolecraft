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
from app.services.matching.normalizer import (
    find_direct_and_implied_skills,
    normalize_skill,
    skill_in_text,
)


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
    job_or_groups: list[list[str]] | None = None,
    github_repos: list[AnalyzedRepo] | None = None,
    embedder: EmbeddingClient | None = None,
) -> tuple[CompatibilityBreakdown, list[SkillAssessmentItem]]:
    """
    Evaluate every job requirement into one of 5 evidence categories:
      - demonstrated: direct or implied evidence in Experience, Projects, Summary, Certifications, or Education
      - under_represented: present only in Skills section (including aliases like HTML5 -> HTML)
      - github_only: supported only in analyzed GitHub repositories
      - needs_development: semantic similarity only (labeled 'Possible adjacent skill', never 'demonstrated')
      - missing: no evidence found anywhere

    Also tracks OR groups ('any one of the following') so satisfied groups count properly.
    """
    active_repos = [r for r in (github_repos or []) if r.excluded_reason is None]

    skills_blob = ", ".join(resume.skills)
    summary_text = resume.summary or ""
    exp_bullets_text = "\n".join(
        f"{exp.title} {exp.company} " + "\n".join(exp.bullets)
        for exp in resume.experience
    )
    proj_bullets_text = "\n".join(
        f"{p.name} {' '.join(p.tech_stack)} " + "\n".join(p.bullets)
        for p in resume.projects
    )
    cert_text = "\n".join(resume.certifications)
    edu_text = "\n".join(
        f"{edu.degree} " + "\n".join(edu.details) for edu in resume.education
    )

    exp_map = find_direct_and_implied_skills(exp_bullets_text)
    proj_map = find_direct_and_implied_skills(proj_bullets_text)
    summary_map = find_direct_and_implied_skills(summary_text)
    cert_map = find_direct_and_implied_skills(cert_text)
    edu_map = find_direct_and_implied_skills(edu_text)
    skills_list_map = find_direct_and_implied_skills(skills_blob)

    resume_skills_norm = {normalize_skill(s): s for s in resume.skills}
    resume_evidence_text = "\n".join(
        part for part in [summary_text, exp_bullets_text, proj_bullets_text, cert_text, edu_text] if part
    )

    evidence_snippets = (
        [b for exp in resume.experience for b in exp.bullets]
        + [b for proj in resume.projects for b in proj.bullets]
        + list(resume.certifications)
        + ([summary_text] if summary_text else [])
    )
    if resume.skills:
        evidence_snippets.append("Skills: " + ", ".join(resume.skills))

    # Map normalized skills to OR-group label
    or_group_by_norm: dict[str, str] = {}
    for g_idx, grp in enumerate(job_or_groups or []):
        label = f"Any of: {', '.join(grp)}"
        for member in grp:
            or_group_by_norm[normalize_skill(member)] = label

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

    for skill, importance in all_job_skills:
        norm = normalize_skill(skill)
        in_skills_list = (
            norm in resume_skills_norm
            or norm in skills_list_map
            or any(skill_in_text(skill, s) for s in resume.skills)
        )

        # Check all demonstrated sections (direct + implied)
        demo_sources: list[str] = []
        if norm in exp_map or skill_in_text(skill, exp_bullets_text):
            why = exp_map.get(norm, ["Experience"])[0]
            demo_sources.append(f"Experience ({why})")
        if norm in proj_map or skill_in_text(skill, proj_bullets_text):
            why = proj_map.get(norm, ["Projects"])[0]
            demo_sources.append(f"Projects ({why})")
        if norm in cert_map or skill_in_text(skill, cert_text):
            why = cert_map.get(norm, ["Certifications"])[0]
            demo_sources.append(f"Certifications ({why})")
        if norm in summary_map or skill_in_text(skill, summary_text):
            why = summary_map.get(norm, ["Summary"])[0]
            demo_sources.append(f"Summary ({why})")
        if norm in edu_map or skill_in_text(skill, edu_text):
            demo_sources.append("Education")

        in_demonstrated = bool(demo_sources)

        matching_repos: list[str] = []
        for r in active_repos:
            repo_blob = f"{r.description} {r.readme_excerpt} {' '.join(r.inferred_skills)} {' '.join(r.dependencies)}"
            repo_implied = find_direct_and_implied_skills(repo_blob)
            if (
                any(normalize_skill(rs) == norm for rs in r.inferred_skills)
                or norm in repo_implied
                or skill_in_text(skill, repo_blob)
            ):
                matching_repos.append(r.name)

        sem_sim = sem_scores.get(skill, 0.0)

        if in_demonstrated:
            category = "demonstrated"
            repo_note = (
                f"; {len(matching_repos)} GitHub {'repo' if len(matching_repos) == 1 else 'repos'}"
                if matching_repos
                else ""
            )
            evidence_summary = f"Demonstrated in {'; '.join(demo_sources[:3])}{repo_note}."
            confidence = 0.95 if (in_skills_list or matching_repos) else 0.9
        elif in_skills_list:
            category = "under_represented"
            skill_why = skills_list_map.get(norm, [skill])[0]
            evidence_summary = (
                f"Insufficiently represented: found in Skills list ({skill_why}), "
                "but lacks concrete experience or project evidence."
            )
            confidence = 0.75
        elif matching_repos:
            category = "github_only"
            evidence_summary = (
                f"GitHub-supported ({', '.join(matching_repos[:3])}), absent from resume."
            )
            confidence = 0.82
        elif sem_sim >= 0.62:
            category = "needs_development"
            evidence_summary = (
                f"Possible adjacent skill detected via semantic similarity ({sem_sim:.2f}), "
                "not treated as demonstrated."
            )
            confidence = round(sem_sim, 2)
        else:
            category = "missing"
            evidence_summary = "No evidence found in resume sections or analyzed GitHub repositories."
            confidence = 0.9

        assessments.append(
            SkillAssessmentItem(
                skill_name=skill,
                category=category,
                importance=importance,
                evidence_summary=evidence_summary,
                confidence=confidence,
                or_group=or_group_by_norm.get(norm),
                or_group_satisfied=False,
            )
        )

    # Evaluate OR groups ('any one of the following' -> satisfied if any member is demonstrated)
    demonstrated_by_group: dict[str, list[str]] = {}
    for a in assessments:
        if a.or_group and a.category == "demonstrated":
            demonstrated_by_group.setdefault(a.or_group, []).append(a.skill_name)

    for a in assessments:
        if a.or_group and a.or_group in demonstrated_by_group:
            a.or_group_satisfied = True
            if a.category != "demonstrated":
                winners = ", ".join(demonstrated_by_group[a.or_group][:3])
                a.evidence_summary = f"{a.evidence_summary} OR-group satisfied via {winners}."

    # Build matched / missing summaries and compute weighted sub-scores
    req_matched: list[str] = []
    req_missing: list[str] = []
    pref_matched: list[str] = []
    pref_missing: list[str] = []

    for a in assessments:
        is_satisfied = a.category in {"demonstrated", "under_represented", "github_only"} or a.or_group_satisfied
        if a.importance == "required":
            (req_matched if is_satisfied else req_missing).append(a.skill_name)
        else:
            (pref_matched if is_satisfied else pref_missing).append(a.skill_name)

    def _group_weighted_score(items: list[SkillAssessmentItem]) -> float:
        if not items:
            return 100.0
        # Collapse OR-group members into a single logical requirement unit so having 1 of 4 doesn't penalize 75%
        standalone_points: list[float] = []
        grouped: dict[str, list[SkillAssessmentItem]] = {}
        for item in items:
            pts = (
                1.0
                if item.category == "demonstrated"
                else (0.7 if item.category in {"under_represented", "github_only"} else 0.0)
            )
            if item.or_group:
                grouped.setdefault(item.or_group, []).append(item)
            else:
                standalone_points.append(pts)

        for grp_items in grouped.values():
            best = max(
                1.0
                if g.category == "demonstrated"
                else (0.7 if g.category in {"under_represented", "github_only"} else 0.0)
                for g in grp_items
            )
            standalone_points.append(best)

        return round((sum(standalone_points) / max(1, len(standalone_points))) * 100.0, 1)

    req_list = [a for a in assessments if a.importance == "required"]
    pref_list = [a for a in assessments if a.importance == "preferred"]
    req_score = _group_weighted_score(req_list)
    pref_score = _group_weighted_score(pref_list)

    # Experience relevance
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

    # Keyword coverage (direct or implied across resume + skills + github)
    kw_pool = list(dict.fromkeys(job_keywords or [sk for sk, _ in all_job_skills]))
    full_evidence_blob = (
        resume_evidence_text
        + "\n"
        + skills_blob
        + "\n"
        + " ".join(
            f"{' '.join(r.inferred_skills)} {' '.join(r.dependencies)} {r.description}"
            for r in active_repos
        )
    )
    kw_evidence_map = find_direct_and_implied_skills(full_evidence_blob)
    kw_matched = [
        kw
        for kw in kw_pool
        if normalize_skill(kw) in kw_evidence_map or skill_in_text(kw, full_evidence_blob)
    ]
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
            if any(
                normalize_skill(sk) in find_direct_and_implied_skills(f"{p.name} {' '.join(p.tech_stack)} {' '.join(p.bullets)}")
                or skill_in_text(sk, f"{p.name} {' '.join(p.tech_stack)} {' '.join(p.bullets)}")
                for sk, _ in all_job_skills
            )
        ] + [
            r.name
            for r in active_repos
            if any(
                normalize_skill(sk) in {normalize_skill(rs) for rs in r.inferred_skills}
                for sk, _ in all_job_skills
            )
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
                f"Matched: {', '.join(req_matched[:6]) or 'None'}. "
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
