from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import fitz
import pytest

from app.core.security import create_access_token
from app.models.application import Application
from app.models.job import JobPosting
from app.models.resume import ResumeFile
from app.models.user import User
from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.services.generation.optimizer import generate_tailored_changes
from app.services.github.scorer import build_factual_repo_bullets_with_evidence, rank_repos_for_job
from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.resume.theme_verifier import (
    verify_exported_pdf_completeness,
    verify_pdf_text_layer_integrity,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _real_manindra_repos() -> list[AnalyzedRepo]:
    """Manindra's real analyzed GitHub repositories (no mock repos)."""
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


def _mock_test_repo() -> AnalyzedRepo:
    """Explicitly mock repo marked with is_mock=True."""
    return AnalyzedRepo(
        name="API-Gateway-Demo",
        full_name="test-user/API-Gateway-Demo",
        html_url="https://github.com/test-user/API-Gateway-Demo",
        description="Demo gateway with Redis and Docker.",
        primary_language="Python",
        languages={"Python": 5000},
        dependencies=["fastapi", "redis", "docker"],
        inferred_skills=["Python", "FastAPI", "Redis", "Docker"],
        readme_excerpt="FastAPI gateway demo with Redis and Docker.",
        signals=RepoEvidenceSignals(has_readme=True, file_count=5),
        is_mock=True,
    )


def test_round9_evidence_source_integrity() -> None:
    """1. Evidence source integrity: real repos only, no Redis/Docker unless evidenced, no marketing buzzwords."""
    real_repos = _real_manindra_repos()
    for repo in real_repos:
        assert repo.is_mock is False
        assert "redis" not in [s.lower() for s in repo.inferred_skills]
        assert "docker" not in [s.lower() for s in repo.inferred_skills]

        bullets, _ = build_factual_repo_bullets_with_evidence(repo)
        for b in bullets:
            low_b = b.lower()
            assert "redis" not in low_b, f"Redis unevidenced in {repo.name}: {b}"
            assert "docker" not in low_b, f"Docker unevidenced in {repo.name}: {b}"
            assert "high-throughput" not in low_b
            assert "ultra-fast" not in low_b
            assert "blazing fast" not in low_b
            assert not re.search(r"built\s+\w+,\s+a\s+.*\s+built\s+with", low_b), f"Repetitive phrasing: {b}"

    # Verify mock repo gating: is_mock=True repos rejected in production runs
    os.environ["ENVIRONMENT"] = "production"
    try:
        mock_repo = _mock_test_repo()
        rec = RepoRecommendation(
            repo_name=mock_repo.name,
            full_name=mock_repo.full_name,
            html_url=mock_repo.html_url,
            quality_score=90.0,
            relevance_score=90.0,
            matched_skills=["Python"],
            reason="High alignment",
            draft_bullets=["Built API Gateway Demo with FastAPI."],
            decision="approved",
            is_mock=True,
        )
        src = FIXTURES / "resumes/manindra_resume.pdf"
        st, el, raw, _ = parse_pdf_resume(src)
        job = StructuredJobFields(
            title="Backend Engineer",
            required_skills=["Python", "FastAPI"],
            responsibilities=["Build backend REST APIs with Python and FastAPI"],
            keywords=["Backend", "REST"],
        )
        changes = generate_tailored_changes(
            resume=st,
            raw_resume_text=raw,
            element_map=el,
            job=job,
            github_repos=[mock_repo],
            approved_github_recs=[rec],
        )
        add_changes = [c for c in changes if c.get("change_type") == "added_from_github"]
        assert len(add_changes) == 0, "Mock repo must be rejected in non-test run"
    finally:
        os.environ["ENVIRONMENT"] = "test"


def test_round9_ats_text_layer_integrity(tmp_path: Path) -> None:
    """2. Text layer ATS cleanliness: typos & firebase gone, sections & added projects appear once, no ghosts."""
    src = FIXTURES / "resumes/manindra_resume.pdf"
    st, el, raw, _ = parse_pdf_resume(src)
    real_repos = _real_manindra_repos()
    recs = rank_repos_for_job(
        real_repos,
        job_skills=["Python", "FastAPI", "React", "LangGraph"],
        job_keywords=["REST", "Backend"],
        resume_text=raw,
    )
    for r in recs:
        r.decision = "approved"

    job = StructuredJobFields(
        title="Backend Software Engineer",
        required_skills=["Python", "FastAPI", "React", "REST"],
        keywords=["Backend", "Database"],
    )
    changes = generate_tailored_changes(
        resume=st,
        raw_resume_text=raw,
        element_map=el,
        job=job,
        github_repos=real_repos,
        approved_github_recs=recs[:1],
        confirmed_learning=[{"skill": "FastAPI"}],
    )
    accepted = [dict(c, status="accepted") for c in changes if c.get("change_type") != "relevance_trim"]

    out_pdf = tmp_path / "manindra_round9_ats.pdf"
    res = apply_changes_to_pdf_in_place(
        source_pdf_path=src,
        output_pdf_path=out_pdf,
        resume_structure=st,
        approved_changes=accepted,
    )
    assert res.overlap_passed is True

    doc = fitz.open(str(out_pdf))
    full_text = "\n".join(page.get_text("text") for page in doc).lower()
    doc.close()

    # Typo strings and removed keywords must be completely absent from extracted text
    assert "retrival" not in full_text
    assert "constrains" not in full_text
    assert "casual masking" not in full_text
    assert "firebase" not in full_text

    # ATS Text Layer Integrity validator must pass
    ok, errs = verify_pdf_text_layer_integrity(out_pdf, accepted_changes=accepted)
    assert ok is True, f"Text layer integrity failed: {errs}"

    # Completeness must pass
    comp_ok, comp_errs = verify_exported_pdf_completeness(
        original_pdf_path=src,
        output_pdf_path=out_pdf,
        applied_changes=accepted,
    )
    assert comp_ok is True, f"Completeness check failed: {comp_errs}"


def test_round9_style_cloning_alex_and_maya(tmp_path: Path) -> None:
    """3. Style cloning: Alex Rivera added project is plain text, Skills labels regular; Maya Nair Projects heading plain."""
    # 3A. Alex Rivera (sample_two_column.pdf)
    side_src = FIXTURES / "resumes/sample_two_column.pdf"
    side_st, side_el, side_raw, _ = parse_pdf_resume(side_src)
    real_repos = _real_manindra_repos()
    side_recs = rank_repos_for_job(
        real_repos,
        job_skills=["Python", "FastAPI", "Go"],
        job_keywords=["Backend", "Data"],
        resume_text=side_raw,
    )
    for r in side_recs:
        r.decision = "approved"

    side_job = StructuredJobFields(
        title="Software Engineer",
        required_skills=["Python", "FastAPI", "Go"],
    )
    side_changes = generate_tailored_changes(
        resume=side_st,
        raw_resume_text=side_raw,
        element_map=side_el,
        job=side_job,
        github_repos=real_repos,
        approved_github_recs=side_recs[:1],
        confirmed_learning=[{"skill": "FastAPI"}],
    )
    side_accepted = [dict(c, status="accepted") for c in side_changes if c.get("change_type") != "relevance_trim"]
    side_out = tmp_path / "alex_round9_styled.pdf"
    side_res = apply_changes_to_pdf_in_place(
        source_pdf_path=side_src,
        output_pdf_path=side_out,
        resume_structure=side_st,
        approved_changes=side_accepted,
    )
    assert side_res.overlap_passed is True

    side_doc = fitz.open(str(side_out))
    page0 = side_doc[0]
    spans = [
        s
        for b in page0.get_text("dict")["blocks"]
        if b["type"] == 0
        for l in b["lines"]
        for s in l["spans"]
    ]
    side_doc.close()

    # For Alex: TASKFLOW and added project are plain text (flags & 16 == 0, font is Helvetica regular)
    added_proj_name = side_recs[0].repo_name.upper()
    proj_title = next(
        (
            s
            for s in spans
            if "DECISION" in s["text"].upper() or added_proj_name in s["text"].replace("\u2010", "-").replace("\xa0", " ").upper()
        ),
        None,
    )
    assert proj_title is not None, f"Added project {added_proj_name} must appear in output"
    assert "bold" not in proj_title["font"].lower()
    assert (int(proj_title.get("flags", 0)) & 16) == 0, "Alex Rivera project title must NOT be bold"
    # Link color matches plain title color (#1f2933)
    proj_hex = f"#{int(proj_title.get('color', 0)) & 0xFFFFFF:06x}".lower()
    assert proj_hex not in {"#0563c1", "#1155cc", "#0000ee"}, "Alex Rivera link must not be blue"

    # Skills labels in Alex's sidebar are regular Helvetica
    lang_span = next((s for s in spans if s["text"].strip().startswith("Languages:")), None)
    assert lang_span is not None
    assert "bold" not in lang_span["font"].lower()
    assert (int(lang_span.get("flags", 0)) & 16) == 0, "Alex Rivera skills label must NOT be bold"

    # 3B. Maya Nair (sample_resume.pdf)
    maya_src = FIXTURES / "resumes/sample_resume.pdf"
    maya_st, maya_el, maya_raw, _ = parse_pdf_resume(maya_src)
    maya_job = StructuredJobFields(
        title="Full Stack Engineer",
        required_skills=["Python", "FastAPI", "React"],
    )
    maya_recs = rank_repos_for_job(
        real_repos,
        job_skills=["Python", "FastAPI", "React"],
        job_keywords=["Full Stack", "Backend"],
        resume_text=maya_raw,
    )
    for r in maya_recs:
        r.decision = "approved"

    maya_changes = generate_tailored_changes(
        resume=maya_st,
        raw_resume_text=maya_raw,
        element_map=maya_el,
        job=maya_job,
        github_repos=real_repos,
        approved_github_recs=maya_recs[:1],
        confirmed_learning=[{"skill": "FastAPI"}],
    )
    maya_accepted = [dict(c, status="accepted") for c in maya_changes if c.get("change_type") != "relevance_trim"]
    maya_out = tmp_path / "maya_round9_styled.pdf"
    maya_res = apply_changes_to_pdf_in_place(
        source_pdf_path=maya_src,
        output_pdf_path=maya_out,
        resume_structure=maya_st,
        approved_changes=maya_accepted,
    )
    assert maya_res.overlap_passed is True

    maya_doc = fitz.open(str(maya_out))
    m_spans = [
        s
        for b in maya_doc[0].get_text("dict")["blocks"]
        if b["type"] == 0
        for l in b["lines"]
        for s in l["spans"]
    ]
    maya_doc.close()

    # In Maya's resume, added Projects heading matches sibling headings (Summary, Experience) weight
    proj_heading = next((s for s in m_spans if s["text"].strip() == "Projects"), None)
    assert proj_heading is not None, "Projects heading must be added for Maya"
    assert "bold" not in proj_heading["font"].lower()
    assert (int(proj_heading.get("flags", 0)) & 16) == 0, "Maya Nair Projects heading must match sibling regular weight"


def test_round9_confirmed_learning_per_application_scope(client, db_session) -> None:
    """4. Confirmed learning per application: learning confirmed on App A does NOT leak to App B."""
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r9-learner@example.com", "full_name": "R9 Learner", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    user_id = reg.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    job_a = JobPosting(
        user_id=user_id,
        raw_text="Backend Go developer",
        title="Go Developer",
        required_skills=["Go", "Docker"],
    )
    job_b = JobPosting(
        user_id=user_id,
        raw_text="Rust Systems engineer",
        title="Rust Engineer",
        required_skills=["Rust"],
    )
    rf = ResumeFile(
        user_id=user_id,
        filename="test.pdf",
        file_type="pdf",
        file_size=1024,
        storage_path=str(FIXTURES / "resumes/sample_resume.pdf"),
        page_count=1,
    )
    db_session.add_all([job_a, job_b, rf])
    db_session.commit()

    app_a = Application(
        user_id=user_id,
        job_posting_id=job_a.id,
        resume_file_id=rf.id,
        status="Draft",
        confirmed_learning=[],
    )
    app_b = Application(
        user_id=user_id,
        job_posting_id=job_b.id,
        resume_file_id=rf.id,
        status="Draft",
        confirmed_learning=[],
    )
    db_session.add_all([app_a, app_b])
    db_session.commit()

    # Confirm learning for Application A
    conf_resp = client.post(
        f"/api/v1/applications/{app_a.id}/learning-confirm",
        headers=headers,
        json={"skills": ["Go", "Docker"], "note": "Active Udemy course"},
    )
    assert conf_resp.status_code == 200
    data_a = conf_resp.json()
    assert len(data_a.get("confirmed_learning", [])) == 2

    # Verify Application B confirmed_learning is still empty
    detail_b = client.get(f"/api/v1/applications/{app_b.id}", headers=headers).json()
    assert detail_b.get("confirmed_learning") == [], "Learning from App A must NOT leak to App B"
