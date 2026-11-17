import json
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import encrypt_api_key
from app.models.application import Application, Change, ResumeVersion
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.learning import LearningPlan
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.services.generation.optimizer import generate_tailored_changes
from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.resume.theme_verifier import (
    check_pdf_text_overlaps,
    verify_exported_pdf_completeness,
    verify_pdf_theme_match,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANINDRA_PDF = FIXTURES / "resumes/manindra_resume.pdf"


def _make_manindra_repos() -> list[AnalyzedRepo]:
    return [
        AnalyzedRepo(
            name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            description="Interactive AI roadmap generator",
            primary_language="JavaScript",
            languages={"JavaScript": 5000, "Python": 3000},
            dependencies=["react", "fastapi"],
            inferred_skills=["React", "FastAPI", "Python", "JavaScript"],
            readme_excerpt="Decision-IQ uses FastAPI and dynamic node canvas graphs.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=39, total_commits=15, user_commits=15, user_commit_share=1.0),
        ),
        AnalyzedRepo(
            name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            description="Autonomous surplus food rescue agent",
            primary_language="TypeScript",
            languages={"TypeScript": 4000, "Python": 3000},
            dependencies=["react", "fastapi", "langgraph", "google-genai"],
            inferred_skills=["FastAPI", "LangGraph", "Python", "Gemini", "TypeScript"],
            readme_excerpt="Surplus food rescue agent powered by LangGraph, Google Gemini, and FastAPI.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=25, total_commits=12, user_commits=12, user_commit_share=1.0),
        ),
        AnalyzedRepo(
            name="AI_Deck",
            full_name="Manindra-babu/AI_Deck",
            html_url="https://github.com/Manindra-babu/AI_Deck",
            description="AI slide generator",
            primary_language="TypeScript",
            languages={"TypeScript": 6000, "Python": 2000},
            dependencies=["next", "react", "typescript"],
            inferred_skills=["Next.js", "TypeScript", "React"],
            readme_excerpt="Slide deck generator built with Next.js, React, and TypeScript.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=20, total_commits=10, user_commits=10, user_commit_share=1.0),
        ),
    ]


def test_round11_skills_source_attribution_and_exclusion_of_unevidenced():
    """Requirement 1: Show source in diff report; unevidenced skills (e.g. Docker) must not appear."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    repos = _make_manindra_repos()
    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        company="TechCorp",
        required_skills=["Python", "FastAPI", "LangGraph", "Next.js", "Docker", "Kubernetes"],
        preferred_skills=["React", "TypeScript"],
        responsibilities=["Build backend web services"],
        keywords=["FastAPI", "LangGraph", "Next.js", "Docker"],
    )

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos,
        confirmed_learning=[],
    )

    skills_ch = [c for c in changes if c.get("section") == "skills"]
    assert skills_ch, "Expected skills changes proposed"

    all_reasons = " ".join(c.get("reason", "") for c in skills_ch)
    all_new_text = " ".join(c.get("new_text", "") for c in skills_ch)

    # 1. FastAPI, LangGraph, Next.js appear with repo sources
    assert "FastAPI" in all_new_text
    assert "LangGraph" in all_new_text
    assert "Next.js" in all_new_text

    # Verify sources in reason/notes or evidence_refs
    ev_details = [
        ref.get("detail", "")
        for c in skills_ch
        for ref in (c.get("evidence_refs") or [])
    ]
    ev_blob = " ".join(ev_details) + " " + all_reasons

    assert "Decision-IQ" in ev_blob or "Surplus-Food-Donation-Matcher" in ev_blob
    assert "Surplus-Food-Donation-Matcher" in ev_blob
    assert "AI_Deck" in ev_blob

    # 2. Docker and Kubernetes have NO source in resume/repos/master profile, so they must be EXCLUDED
    assert "Docker" not in all_new_text
    assert "Kubernetes" not in all_new_text


def test_round11_learned_skills_in_web_technologies_and_master_profile(client: TestClient, db_session: Session):
    """Requirement 2: Confirmed learned skills appear in Web Technologies & Architecture and save to profile."""
    # Register test user
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r11user@example.com", "password": "StrongPassword123!", "full_name": "R11 Tester"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    user_id = reg.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    # Upload resume
    pdf_bytes = MANINDRA_PDF.read_bytes()
    up = client.post(
        "/api/v1/resumes/upload",
        headers=headers,
        files={"file": ("manindra_resume.pdf", pdf_bytes, "application/pdf")},
    )
    assert up.status_code == 201
    resume_id = up.json()["id"]

    # Create job with AJAX, Web Services, SOA
    job = JobPosting(
        user_id=user_id,
        title="Senior Web Engineer",
        company="EnterpriseCo",
        raw_text="Requires AJAX, Web Services, SOA, REST APIs, React",
        required_skills=["AJAX", "Web Services", "SOA", "React", "JavaScript"],
        preferred_skills=[],
        responsibilities=["Build SOA web services with AJAX calls"],
        keywords=["AJAX", "Web Services", "SOA"],
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Create application
    app_obj = Application(
        user_id=user_id,
        resume_file_id=resume_id,
        job_posting_id=job.id,
        status="Draft",
    )
    db_session.add(app_obj)
    db_session.commit()
    db_session.refresh(app_obj)

    # Create learning plans for AJAX, Web Services, SOA
    for skill_name in ("AJAX", "Web Services", "SOA"):
        plan = LearningPlan(
            user_id=user_id,
            application_id=app_obj.id,
            skill_name=skill_name,
            category="Web Technologies",
            plan_data={"ordered_steps": []},
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        # Confirm learning as "learned"
        prog_res = client.put(
            f"/api/v1/learning/{plan.id}/progress",
            json={
                "user_confirmed_learning": True,
                "status": "learned",
                "confirmation_note": f"Completed and learned {skill_name} course and project",
                "application_id": app_obj.id,
            },
            headers=headers,
        )
        assert prog_res.status_code == 200

    # Verify master profile
    profile = db_session.query(MasterProfile).filter(MasterProfile.user_id == user_id).first()
    assert profile is not None
    assert "AJAX" in (profile.skills or [])
    assert "Web Services" in (profile.skills or [])
    assert "SOA" in (profile.skills or [])

    # Now generate tailored changes with confirmed learning
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=StructuredJobFields(
            title="Senior Web Engineer",
            company="EnterpriseCo",
            required_skills=["AJAX", "Web Services", "SOA", "React", "JavaScript"],
            preferred_skills=[],
            responsibilities=[],
            keywords=["AJAX", "Web Services", "SOA"],
        ),
        confirmed_learning=profile.confirmed_learning,
    )

    # Requirement 4 check at the source: summary must start with "Detail-oriented", never "Detail- Detail-oriented"
    assert parsed.summary.startswith("Detail-oriented"), f"Summary should start with 'Detail-oriented', got: {parsed.summary[:50]}"
    assert "Detail- Detail-oriented" not in parsed.summary

    # Requirement 1: Two separate clean lines, no merged/duplicated label
    wt_ch = next((c for c in changes if c.get("section") == "skills" and "web technologies:" in c.get("new_text", "").lower()), None)
    assert wt_ch is not None
    assert wt_ch["new_text"] == "Web Technologies: HTML5, CSS, JavaScript (AJAX), React, Node.js, Express.js, MongoDB, FastAPI"

    ws_ch = next((c for c in changes if c.get("section") == "skills" and "web services & architecture:" in c.get("new_text", "").lower()), None)
    assert ws_ch is not None
    assert ws_ch["new_text"] == "Web Services & Architecture: REST APIs, Web Services, SOA"

    # Must NEVER appear under Currently Learning
    cur_learning_ch = [c for c in changes if "currently learning" in str(c.get("new_text", "")).lower()]
    assert not cur_learning_ch, f"Learned skills incorrectly placed in Currently Learning: {cur_learning_ch}"


def test_round11_single_column_project_layout_no_overlap(tmp_path: Path):
    """Requirement 3: Single-column project list every time with uniform spacing and 0 overlap."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    repos = _make_manindra_repos()
    job = StructuredJobFields(
        title="Software Engineer Intern",
        company="PayPal",
        required_skills=["React", "FastAPI", "Python", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=["React", "FastAPI"],
    )

    recs = [
        RepoRecommendation(
            repo_name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            quality_score=90,
            relevance_score=85.0,
            matched_skills=["React", "FastAPI", "Python"],
            reason="Interactive AI roadmap generator",
            decision="approved",
            draft_bullets=[
                "Built Decision-IQ, an interactive AI roadmap generator using React and FastAPI.",
                "Added automated unit tests across core canvas and graph components.",
            ],
            bullet_evidence=[BulletEvidenceItem(bullet="Built...", source="README", detail="README.md")],
        )
    ]

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos,
        approved_github_recs=recs,
    )
    for c in changes:
        if c.get("change_type") != "relevance_trim":
            c["status"] = "accepted"

    out_pdf = tmp_path / "manindra_single_col.pdf"
    res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        resume_structure=parsed,
        approved_changes=changes,
        tighten_spacing=True,
    )

    # 1. Page fit and 0 overlaps
    assert res.overlap_passed is True, res.overlap_details
    assert res.page_fit["passed"] is True, res.page_fit
    assert res.page_fit["margin_pt"] >= 12.0
    assert res.page_count_output == 1

    # 2. Verify all projects are in single column (left aligned x0 ~ 39.9 pt)
    doc = fitz.open(str(out_pdf))
    page = doc[0]
    td = page.get_text("dict")
    full_pdf_text = page.get_text()
    doc.close()

    proj_titles = []
    for blk in td.get("blocks", []):
        if blk.get("type") == 0:
            for ln in blk.get("lines", []):
                txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if " - GITHUB" in txt.upper() or "KNOWLEDGE BASE" in txt.upper():
                    proj_titles.append((ln["bbox"], txt))

    # When vertical budget exceeds 1 page under matched spacing rhythm, the least relevant project (Knowledge Base) is dropped
    assert len(proj_titles) == 2, f"Expected 2 project cards after dropping least relevant project, got {proj_titles}"
    assert any("DECISION-IQ" in t for _, t in proj_titles)
    assert any("CUSTOM GPT" in t for _, t in proj_titles)
    assert not any("KNOWLEDGE BASE" in t for _, t in proj_titles)

    # Spacing verification: top gap below PROJECTS heading and item gap between projects
    proj_head_y1 = None
    for blk in td.get("blocks", []):
        if blk.get("type") == 0:
            for ln in blk.get("lines", []):
                txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if txt.upper() == "PROJECTS":
                    proj_head_y1 = float(ln["bbox"][3])
                    break
    assert proj_head_y1 is not None, "PROJECTS heading not found"
    first_proj_y0 = min(b[1] for b, _ in proj_titles)
    assert first_proj_y0 - proj_head_y1 >= 9.5, f"Gap below PROJECTS heading too small: {first_proj_y0 - proj_head_y1}pt"
    # All project card titles must start on the left column (x0 < 100 pt), NEVER on right column (x0 > 300 pt)
    for bbox, title in proj_titles:
        assert bbox[0] < 100.0, f"Project '{title}' was placed in right column at x0={bbox[0]}!"

    # Requirement 2: Assert project descriptions have no raw "- " markers and no stray " - " between sentences
    assert "- Architecture:" not in full_pdf_text, "Found raw '- Architecture:' marker in project body!"
    assert "- Optimization:" not in full_pdf_text, "Found raw '- Optimization:' marker in project body!"
    assert "- A full stack RAG" not in full_pdf_text, "Found raw '- A full stack RAG' marker in project body!"
    assert "masking. - Optimization:" not in full_pdf_text, "Found stray ' - ' between sentences in project body!"

    # Requirement 4: Summary starts with Detail-oriented and no Detail- Detail-oriented in output PDF
    assert "Detail- Detail-oriented" not in full_pdf_text
    assert "Detail-oriented B.Tech" in full_pdf_text

    # Completeness check: all original lines preserved
    comp_ok, missing = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        applied_changes=changes,
    )
    assert comp_ok is True, missing


def test_round11_experience_bullet_rewriting_with_jd_terms():
    """Requirement 4: Rewrite Experience bullets using JD terms truthfully based only on what candidate did."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = StructuredJobFields(
        title="Web Services Software Engineer",
        company="PayPal",
        required_skills=["REST APIs", "Web Services", "SOA", "AJAX", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=["REST web services", "asynchronous/AJAX calls"],
    )

    # Confirmed learning has AJAX and Web Services
    confirmed = [
        {"skill": "AJAX", "status": "learned"},
        {"skill": "Web Services", "status": "learned"},
        {"skill": "SOA", "status": "learned"},
    ]

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        confirmed_learning=confirmed,
    )

    exp_changes = [c for c in changes if c.get("section") == "experience" and c.get("change_type") == "reworded"]
    assert exp_changes, "Expected experience bullet rewrites"

    all_rewritten = " ".join(c.get("new_text", "") for c in exp_changes)

    # Verify JD terms incorporated truthfully
    assert "asynchronous/AJAX calls" in all_rewritten
    assert "REST web services" in all_rewritten
    # Firebase replaced with secure backend authentication
    assert "firebase" not in all_rewritten.lower()
