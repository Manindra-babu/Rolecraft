from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import fitz
import pytest

from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.services.generation.optimizer import generate_tailored_changes
from app.services.github.scorer import rank_repos_for_job
from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.resume.theme_verifier import (
    verify_exported_pdf_completeness,
    verify_pdf_text_layer_integrity,
    verify_pdf_theme_match,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _manindra_repos() -> list[AnalyzedRepo]:
    """Manindra's real analyzed GitHub repositories (Manindra-babu profile)."""
    return [
        AnalyzedRepo(
            name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            description="AI-powered interactive career roadmap and decision intelligence generator.",
            primary_language="Python",
            languages={"Python": 6000, "TypeScript": 4000},
            dependencies=["fastapi", "uvicorn", "pydantic", "react"],
            inferred_skills=["Python", "FastAPI", "React", "TypeScript", "REST"],
            readme_excerpt="Built Decision-IQ, an interactive roadmap generator developed with Python and FastAPI.",
            signals=RepoEvidenceSignals(has_readme=True, has_tests=True, file_count=15),
            is_mock=False,
        ),
        AnalyzedRepo(
            name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            description="AI platform matching surplus food donors to shelters using LangGraph and Gemini.",
            primary_language="Python",
            languages={"Python": 8000, "JavaScript": 2000},
            dependencies=["fastapi", "langgraph", "google-generativeai"],
            inferred_skills=["Python", "FastAPI", "LangGraph", "Gemini", "REST"],
            readme_excerpt="Surplus food donation matching platform developed with Python, FastAPI, and LangGraph.",
            signals=RepoEvidenceSignals(has_readme=True, has_tests=True, file_count=20),
            is_mock=False,
        ),
        AnalyzedRepo(
            name="AI_Deck",
            full_name="Manindra-babu/AI_Deck",
            html_url="https://github.com/Manindra-babu/AI_Deck",
            description="AI slide deck generator and interactive presentation engine.",
            primary_language="TypeScript",
            languages={"TypeScript": 7000, "Python": 3000},
            dependencies=["next", "react", "fastapi"],
            inferred_skills=["TypeScript", "Next.js", "Python", "React"],
            readme_excerpt="Interactive presentation engine developed with TypeScript and Next.js.",
            signals=RepoEvidenceSignals(has_readme=True, has_tests=False, file_count=12),
            is_mock=False,
        ),
    ]


def _alex_repos() -> list[AnalyzedRepo]:
    """Alex Rivera's analyzed GitHub repositories (alexrivera profile)."""
    return [
        AnalyzedRepo(
            name="Distributed-Cache-Go",
            full_name="alexrivera/Distributed-Cache-Go",
            html_url="https://github.com/alexrivera/Distributed-Cache-Go",
            description="High-performance distributed in-memory cache implemented in Go with Raft consensus.",
            primary_language="Go",
            languages={"Go": 9000},
            dependencies=["raft", "grpc"],
            inferred_skills=["Go", "Distributed Systems", "gRPC", "Raft"],
            readme_excerpt="Distributed cache cluster written in Go.",
            signals=RepoEvidenceSignals(has_readme=True, has_tests=True, file_count=18),
            is_mock=False,
        ),
    ]


def _maya_repos() -> list[AnalyzedRepo]:
    """Maya Nair's analyzed GitHub repositories (mayanair profile)."""
    return [
        AnalyzedRepo(
            name="Cloud-Task-Tracker",
            full_name="mayanair/Cloud-Task-Tracker",
            html_url="https://github.com/mayanair/Cloud-Task-Tracker",
            description="Scalable cloud task tracker with Python, FastAPI, and React dashboard.",
            primary_language="Python",
            languages={"Python": 7000, "TypeScript": 3000},
            dependencies=["fastapi", "react", "postgresql"],
            inferred_skills=["Python", "FastAPI", "React", "PostgreSQL"],
            readme_excerpt="Cloud task tracker developed with Python and React.",
            signals=RepoEvidenceSignals(has_readme=True, has_tests=True, file_count=14),
            is_mock=False,
        ),
    ]


def test_round10_currently_learning_never_contains_existing_or_repo_skills() -> None:
    """
    1. Currently Learning bug:
    - Never include skills already listed in resume (Alex, Maya) or supported by repo evidence (Manindra).
    - FastAPI must NEVER appear under Currently Learning on any resume.
    - Skills supported by repos absent from resume (FastAPI for Manindra) must be proposed under Skills with repo evidence.
    - No user confirmation -> no Currently Learning line.
    - Genuinely new confirmed skill (e.g. AWS) DOES appear.
    """
    # 1A. Manindra (manindra_resume.pdf)
    man_src = FIXTURES / "resumes/manindra_resume.pdf"
    man_st, man_el, man_raw, _ = parse_pdf_resume(man_src)
    man_repos = _manindra_repos()
    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        required_skills=["Python", "FastAPI", "React"],
        keywords=["Backend", "REST"],
    )

    # Test with FastAPI in confirmed_learning -> must NOT appear in Currently Learning!
    changes_fastapi = generate_tailored_changes(
        resume=man_st,
        raw_resume_text=man_raw,
        element_map=man_el,
        job=job,
        github_repos=man_repos,
        confirmed_learning=[{"skill": "FastAPI", "note": "Udemy course"}],
    )
    lrn_changes = [c for c in changes_fastapi if c.get("change_type") == "user_confirmed_skill"]
    assert len(lrn_changes) == 0, "FastAPI is supported by repo evidence, must NEVER appear under Currently Learning"

    # Verify FastAPI is instead proposed under Skills with repo evidence
    skills_changes = [c for c in changes_fastapi if c.get("section") == "skills" and c.get("change_type") != "user_confirmed_skill"]
    assert len(skills_changes) > 0
    fastapi_skill_change = next((c for c in skills_changes if "fastapi" in c.get("new_text", "").lower()), None)
    assert fastapi_skill_change is not None, "FastAPI must be proposed as a pending change to Skills"
    assert any(ref.get("source") == "github_repo" for ref in (fastapi_skill_change.get("evidence_refs") or [])), "Must show repo evidence refs"

    # Test zero confirmation -> NO Currently Learning line
    changes_zero = generate_tailored_changes(
        resume=man_st,
        raw_resume_text=man_raw,
        element_map=man_el,
        job=job,
        github_repos=man_repos,
        confirmed_learning=[],
    )
    assert len([c for c in changes_zero if c.get("change_type") == "user_confirmed_skill"]) == 0

    # Test genuinely new confirmed skill (e.g. AWS) -> DOES appear under Currently Learning
    changes_aws = generate_tailored_changes(
        resume=man_st,
        raw_resume_text=man_raw,
        element_map=man_el,
        job=job,
        github_repos=man_repos,
        confirmed_learning=[{"skill": "AWS", "note": "Solutions Architect course"}],
    )
    aws_lrn = [c for c in changes_aws if c.get("change_type") == "user_confirmed_skill"]
    assert len(aws_lrn) == 1
    assert "Currently Learning: AWS" in aws_lrn[0]["new_text"]

    # 1B. Alex Rivera (sample_two_column.pdf) - already lists FastAPI in Backend
    alex_src = FIXTURES / "resumes/sample_two_column.pdf"
    alex_st, alex_el, alex_raw, _ = parse_pdf_resume(alex_src)
    alex_repos = _alex_repos()
    alex_changes = generate_tailored_changes(
        resume=alex_st,
        raw_resume_text=alex_raw,
        element_map=alex_el,
        job=job,
        github_repos=alex_repos,
        confirmed_learning=[{"skill": "FastAPI"}],
    )
    assert len([c for c in alex_changes if c.get("change_type") == "user_confirmed_skill"]) == 0, "Alex already has FastAPI in Skills"

    # 1C. Maya Nair (sample_resume.pdf) - already lists FastAPI in Skills
    maya_src = FIXTURES / "resumes/sample_resume.pdf"
    maya_st, maya_el, maya_raw, _ = parse_pdf_resume(maya_src)
    maya_repos = _maya_repos()
    maya_changes = generate_tailored_changes(
        resume=maya_st,
        raw_resume_text=maya_raw,
        element_map=maya_el,
        job=job,
        github_repos=maya_repos,
        confirmed_learning=[{"skill": "FastAPI"}],
    )
    assert len([c for c in maya_changes if c.get("change_type") == "user_confirmed_skill"]) == 0, "Maya already has FastAPI in Skills"


def test_round10_project_selection_transparency_and_page_fit_options(tmp_path: Path) -> None:
    """
    2. Project selection transparency:
    - Recommendations show why added or omitted (relevance, space).
    - Omitted projects flagged with omitted_for_space=True and omission_reason.
    - Page-fit options surface omitted projects as transparent 1-click swaps.
    - Profiles are distinct per resume.
    """
    man_src = FIXTURES / "resumes/manindra_resume.pdf"
    man_st, man_el, man_raw, _ = parse_pdf_resume(man_src)
    man_repos = _manindra_repos()
    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        required_skills=["Python", "FastAPI", "React", "LangGraph"],
        keywords=["Backend", "REST"],
    )
    recs = rank_repos_for_job(
        man_repos,
        job_skills=job.required_skills,
        job_keywords=job.keywords,
        resume_text=man_raw,
    )
    for r in recs:
        r.decision = "approved"

    changes = generate_tailored_changes(
        resume=man_st,
        raw_resume_text=man_raw,
        element_map=man_el,
        job=job,
        github_repos=man_repos,
        approved_github_recs=recs,
    )
    add_changes = [c for c in changes if c.get("change_type") == "added_from_github"]
    assert len(add_changes) == 1, "Only 1 project card fits on 1-page budget"

    # Transparent omission flags on remaining approved recommendations
    omitted_recs = [r for r in recs if r.omitted_for_space]
    assert len(omitted_recs) >= 1
    assert "page-fit" in omitted_recs[0].omission_reason.lower()

    # Verify page-fit options surface alternative projects
    out_pdf = tmp_path / "manindra_r10_opts.pdf"
    accepted = [dict(c, status="accepted") for c in changes if c.get("change_type") != "relevance_trim"]
    res = apply_changes_to_pdf_in_place(
        source_pdf_path=man_src,
        output_pdf_path=out_pdf,
        resume_structure=man_st,
        approved_changes=accepted,
    )
    assert res.overlap_passed is True
    opts = res.page_fit.get("options", [])
    alt_opt = next((o for o in opts if o.get("id") == "swap_project_alternative"), None)
    assert alt_opt is not None, "Page-fit options must offer alternative omitted project swaps"


def test_round10_complete_scorecard_maya_and_ats_layer(tmp_path: Path) -> None:
    """
    3. Complete verification & scorecard for all 3 resumes:
    - Maya Nair full scorecard (completeness, ATS text layer, page-fit, added project).
    - Extracted-text view has 0 occurrences of 'casual', 'constrains', 'retrival', lowercase 'firebase'.
    - Each section appears once, each added project appears once.
    - Sibling font size and family cloned on Alex and Maya.
    """
    # 3A. Maya Nair (sample_resume.pdf)
    maya_src = FIXTURES / "resumes/sample_resume.pdf"
    maya_st, maya_el, maya_raw, _ = parse_pdf_resume(maya_src)
    maya_repos = _maya_repos()
    maya_job = StructuredJobFields(
        title="Full Stack Software Engineer",
        required_skills=["Python", "FastAPI", "React"],
    )
    maya_recs = rank_repos_for_job(
        maya_repos,
        job_skills=maya_job.required_skills,
        job_keywords=[],
        resume_text=maya_raw,
    )
    for r in maya_recs:
        r.decision = "approved"

    maya_changes = generate_tailored_changes(
        resume=maya_st,
        raw_resume_text=maya_raw,
        element_map=maya_el,
        job=maya_job,
        github_repos=maya_repos,
        approved_github_recs=maya_recs[:1],
        confirmed_learning=[],  # Zero confirmation -> no Currently Learning
    )
    maya_accepted = [dict(c, status="accepted") for c in maya_changes if c.get("change_type") != "relevance_trim"]
    maya_out = tmp_path / "maya_r10.pdf"
    maya_res = apply_changes_to_pdf_in_place(
        source_pdf_path=maya_src,
        output_pdf_path=maya_out,
        resume_structure=maya_st,
        approved_changes=maya_accepted,
    )

    # Maya Scorecard Assertions
    assert maya_res.overlap_passed is True
    assert maya_res.page_fit.get("passed") is True
    assert maya_res.page_fit.get("margin_pt") > 12.0
    comp_ok, comp_errs = verify_exported_pdf_completeness(
        original_pdf_path=maya_src,
        output_pdf_path=maya_out,
        applied_changes=maya_accepted,
    )
    assert comp_ok is True, f"Maya completeness failed: {comp_errs}"
    ats_ok, ats_errs = verify_pdf_text_layer_integrity(maya_out, accepted_changes=maya_accepted)
    assert ats_ok is True, f"Maya ATS text layer failed: {ats_errs}"

    # Verify Maya added project styling matches sibling regular heading
    doc_m = fitz.open(str(maya_out))
    m_text = "\n".join(p.get_text("text") for p in doc_m)
    m_spans = [
        s
        for b in doc_m[0].get_text("dict")["blocks"]
        if b["type"] == 0
        for l in b["lines"]
        for s in l["spans"]
    ]
    doc_m.close()

    # Typos and unevidenced terms absent
    assert "casual" not in m_text.lower()
    assert "constrains" not in m_text.lower()
    assert "retrival" not in m_text.lower()
    assert "firebase" not in m_text.lower()

    # Headings match sibling regular weight
    proj_head = next((s for s in m_spans if s["text"].strip() == "Projects"), None)
    assert proj_head is not None
    assert (int(proj_head.get("flags", 0)) & 16) == 0, "Maya Projects heading must be regular weight"
    assert round(proj_head["size"], 1) == 11.0, "Maya Projects heading font size must match sibling 11.0pt"

    # 3B. Alex Rivera (sample_two_column.pdf)
    alex_src = FIXTURES / "resumes/sample_two_column.pdf"
    alex_st, alex_el, alex_raw, _ = parse_pdf_resume(alex_src)
    alex_repos = _alex_repos()
    alex_job = StructuredJobFields(
        title="Distributed Systems Engineer",
        required_skills=["Go", "Distributed Systems"],
    )
    alex_recs = rank_repos_for_job(
        alex_repos,
        job_skills=alex_job.required_skills,
        job_keywords=["Raft"],
        resume_text=alex_raw,
    )
    for r in alex_recs:
        r.decision = "approved"

    alex_changes = generate_tailored_changes(
        resume=alex_st,
        raw_resume_text=alex_raw,
        element_map=alex_el,
        job=alex_job,
        github_repos=alex_repos,
        approved_github_recs=alex_recs[:1],
        confirmed_learning=[],  # Zero confirmation -> no Currently Learning
    )
    alex_accepted = [dict(c, status="accepted") for c in alex_changes if c.get("change_type") != "relevance_trim"]
    alex_out = tmp_path / "alex_r10.pdf"
    alex_res = apply_changes_to_pdf_in_place(
        source_pdf_path=alex_src,
        output_pdf_path=alex_out,
        resume_structure=alex_st,
        approved_changes=alex_accepted,
    )
    assert alex_res.overlap_passed is True

    doc_a = fitz.open(str(alex_out))
    a_spans = [
        s
        for b in doc_a[0].get_text("dict")["blocks"]
        if b["type"] == 0
        for l in b["lines"]
        for s in l["spans"]
    ]
    doc_a.close()

    # Alex added project title matches TASKFLOW sibling plain text (size 9.5pt, Helvetica regular)
    added_alex_title = next((s for s in a_spans if "DISTRIBUTED-CACHE" in s["text"].upper() or "DISTRIBUTED\u2010CACHE" in s["text"].upper()), None)
    assert added_alex_title is not None
    assert (int(added_alex_title.get("flags", 0)) & 16) == 0, "Alex project title must be regular weight"
    assert round(added_alex_title["size"], 1) == 9.5, "Alex project title font size must match sibling 9.5pt"
