import json
from pathlib import Path
import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

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
from tests.test_round11_verification import _make_manindra_repos

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANINDRA_PDF = FIXTURES / "resumes/manindra_resume.pdf"


def test_round13_skills_nextjs_source_attribution_and_exclusion():
    """Requirement 1: Show Next.js source in diff report; if absent from repos/master profile, exclude and ask first."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    
    # Case A: AI_Deck is present in repos -> Next.js IS included, and source (AI_Deck) is shown in diff report
    repos_with_next = _make_manindra_repos()  # Includes Decision-IQ, Surplus-Food-Donation-Matcher, AI_Deck
    job = StructuredJobFields(
        title="Full Stack Engineer",
        company="TechCorp",
        required_skills=["Next.js", "React", "TypeScript", "FastAPI"],
        preferred_skills=[],
        responsibilities=["Build web applications using Next.js"],
        keywords=["Next.js", "React"],
    )

    changes_with_next = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos_with_next,
        confirmed_learning=[],
    )
    skills_ch = [c for c in changes_with_next if c.get("section") == "skills"]
    assert skills_ch, "Expected skills changes proposed"
    all_new_text = " ".join(c.get("new_text", "") for c in skills_ch)
    all_reasons = " ".join(c.get("reason", "") for c in skills_ch)
    all_ev_details = " ".join(ref.get("detail", "") for c in skills_ch for ref in (c.get("evidence_refs") or []))

    assert "Next.js" in all_new_text, f"Next.js should be included when evidenced by AI_Deck: {all_new_text}"
    assert "AI_Deck" in all_reasons or "AI_Deck" in all_ev_details, f"Diff report must show source AI_Deck for Next.js: {all_reasons} {all_ev_details}"

    # Case B: AI_Deck is NOT in repos and NOT in master profile -> Next.js must be EXCLUDED and asked first
    repos_without_next = [r for r in repos_with_next if r.name != "AI_Deck"]
    changes_without_next = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos_without_next,
        confirmed_learning=[],
    )
    skills_ch_no_next = [c for c in changes_without_next if c.get("section") == "skills"]
    all_new_text_no_next = " ".join(c.get("new_text", "") for c in skills_ch_no_next)
    assert "Next.js" not in all_new_text_no_next, f"Next.js must be excluded when absent from repos & master profile: {all_new_text_no_next}"


def test_round13_confirmed_learned_saved_to_master_profile_and_reused(client: TestClient, db_session: Session):
    """Requirement 2: Confirmed learned skills (AJAX, Web Services, SOA) saved in master profile and added whenever JD mentions them."""
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r13user@example.com", "password": "StrongPassword123!", "full_name": "R13 Tester"},
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

    # Set a valid gemini key so require_valid_gemini_key passes
    from app.core.security import encrypt_api_key
    user_obj = db_session.query(User).filter(User.id == user_id).first()
    user_obj.encrypted_gemini_key = encrypt_api_key("dummy_test_gemini_key_1234567890")
    user_obj.gemini_key_status = "valid"
    db_session.add(user_obj)
    db_session.commit()

    # Create application 1
    job1 = JobPosting(
        user_id=user_id,
        title="Full Stack Web Engineer",
        company="Stripe",
        raw_text="Requires AJAX, Web Services, SOA, REST APIs, React",
        required_skills=["AJAX", "Web Services", "SOA", "React", "JavaScript"],
        preferred_skills=[],
        responsibilities=["Build SOA web services"],
        keywords=["AJAX", "Web Services", "SOA"],
    )
    db_session.add(job1)
    db_session.commit()
    db_session.refresh(job1)

    app1 = Application(
        user_id=user_id,
        resume_file_id=resume_id,
        job_posting_id=job1.id,
        status="Draft",
    )
    db_session.add(app1)
    db_session.commit()
    db_session.refresh(app1)

    # Confirm AJAX, Web Services, SOA via learning-confirm or progress
    conf_res = client.post(
        f"/api/v1/applications/{app1.id}/learning-confirm",
        json={"skills": ["AJAX", "Web Services", "SOA"], "note": "Completed enterprise web services certification"},
        headers=headers,
    )
    assert conf_res.status_code == 200

    # Verify they are saved in MasterProfile
    profile = db_session.query(MasterProfile).filter(MasterProfile.user_id == user_id).first()
    assert profile is not None
    assert "AJAX" in (profile.skills or [])
    assert "Web Services" in (profile.skills or [])
    assert "SOA" in (profile.skills or [])

    # Now create application 2 for a different job that ALSO mentions Web Services and AJAX
    job2 = JobPosting(
        user_id=user_id,
        title="Senior Platform Engineer",
        company="PayPal",
        raw_text="Must have experience with AJAX, Web Services, and React.",
        required_skills=["AJAX", "Web Services", "React", "JavaScript"],
        preferred_skills=[],
        responsibilities=["Develop platform web services with AJAX communication"],
        keywords=["AJAX", "Web Services"],
    )
    db_session.add(job2)
    db_session.commit()
    db_session.refresh(job2)

    app2 = Application(
        user_id=user_id,
        resume_file_id=resume_id,
        job_posting_id=job2.id,
        status="Draft",
    )
    db_session.add(app2)
    db_session.commit()
    db_session.refresh(app2)

    # Generate tailored version on app 2: should automatically pull confirmed skills from master profile!
    gen_res = client.post(
        f"/api/v1/applications/{app2.id}/generate",
        headers=headers,
    )
    assert gen_res.status_code == 200
    app2_detail = gen_res.json()
    v1 = app2_detail["versions"][0]
    changes = v1["changes"]

    wt_ch = next((c for c in changes if c.get("section") == "skills" and "web technologies:" in c.get("new_text", "").lower()), None)
    assert wt_ch is not None
    assert "JavaScript (AJAX)" in wt_ch["new_text"]

    ws_ch = next((c for c in changes if c.get("section") == "skills" and "web services & architecture:" in c.get("new_text", "").lower()), None)
    assert ws_ch is not None
    assert "Web Services" in ws_ch["new_text"]


def test_round13_project_selection_why_notes_and_dropped_explanation():
    """Requirement 3: One-line 'why selected' note for chosen projects, and explain why Knowledge Base was dropped."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    repos = _make_manindra_repos()
    recs = [
        RepoRecommendation(
            repo_name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            quality_score=90,
            relevance_score=130.0,
            matched_skills=["React", "FastAPI", "Python", "JavaScript (AJAX)"],
            reason="Highest job relevance (Decision-IQ, score 130) matching required skills",
            decision="approved",
            draft_bullets=[
                "Built Decision-IQ, an interactive platform using FastAPI and React with asynchronous AJAX calls and REST APIs",
            ],
            bullet_evidence=[BulletEvidenceItem(bullet="Built...", source="README", detail="README.md")],
        )
    ]
    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        company="TechCorp",
        required_skills=["React", "FastAPI", "Python", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=["React", "FastAPI"],
    )

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos,
        approved_github_recs=recs,
    )

    proj_changes = [c for c in changes if c.get("section") == "projects"]
    assert proj_changes, "Expected project changes proposed"

    # 1. Decision-IQ has a one-line 'Why selected:' note and keeps factual repo details (unit tests + async calls)
    diq_ch = next((c for c in proj_changes if "DECISION-IQ" in c.get("new_text", "").upper()), None)
    assert diq_ch is not None, proj_changes
    assert diq_ch["reason"].startswith("Why selected:"), f"Decision-IQ reason must start with 'Why selected:', got: {diq_ch['reason']}"
    assert "score 130" in diq_ch["reason"] or "Decision-IQ" in diq_ch["reason"]
    # Check factual repo details preserved: automated unit tests
    assert "automated unit tests" in diq_ch["new_text"].lower(), f"Decision-IQ must keep factual unit test details: {diq_ch['new_text']}"
    # Check async calls / AJAX only included because Decision-IQ makes async calls
    assert "ajax" in diq_ch["new_text"].lower() or "asynchronous" in diq_ch["new_text"].lower()

    # 2. Custom GPT has a 'Why selected:' note
    gpt_ch = next((c for c in proj_changes if "GPT" in c.get("original_text", "").upper() or "GPT" in c.get("new_text", "").upper()), None)
    assert gpt_ch is not None, proj_changes
    assert "why selected:" in gpt_ch["reason"].lower(), f"Custom GPT reason must explain why selected, got: {gpt_ch['reason']}"

    # 3. Knowledge Base Retrieval System has a 'Why left out:' / 'Why dropped:' explanation
    kb_ch = next((c for c in proj_changes if c.get("change_type") == "relevance_trim" and any(k in c.get("original_text", "").lower() for k in ["knowled", "retrival", "retrieval", "rag"])), None)
    assert kb_ch is not None, f"Expected dropped change for Knowledge Base Retrieval System, got: {proj_changes}"
    assert kb_ch["change_type"] == "relevance_trim"
    assert kb_ch["new_text"] == ""
    assert "why left out:" in kb_ch["reason"].lower() or "why dropped:" in kb_ch["reason"].lower(), f"Reason must explain why left out: {kb_ch['reason']}"
    assert "lowest relevance score" in kb_ch["reason"].lower() or "20" in kb_ch["reason"]


def test_round13_mern_parts_exclusion_no_duplicate_listing():
    """Requirement 4: In Web Technologies, don't list 'MERN' together with its parts (MongoDB, Express.js, React, Node.js)."""
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    # Original resume has 'MERN' under Web Technologies
    assert "MERN" in parsed.skill_categories.get("Web Technologies", [])

    job = StructuredJobFields(
        title="Web Developer",
        company="TechCorp",
        required_skills=["React", "Node.js", "Express", "MongoDB", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=["React", "Node.js"],
    )

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        confirmed_learning=[],
    )

    wt_ch = next((c for c in changes if c.get("section") == "skills" and "web technologies:" in c.get("new_text", "").lower()), None)
    assert wt_ch is not None
    wt_text = wt_ch["new_text"]

    # When parts (React, Node.js, Express.js, MongoDB) are listed, MERN must NOT be listed
    has_parts = any(p in wt_text for p in ["MongoDB", "Express", "React", "Node.js"])
    if has_parts:
        tokens = [t.strip().upper() for t in wt_text.split(":")[1].split(",")]
        assert "MERN" not in tokens, f"'MERN' should not be listed together with its parts: {wt_text}"
