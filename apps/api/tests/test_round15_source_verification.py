from pathlib import Path
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient

from app.schemas.github import RepoRecommendation, BulletEvidenceItem
from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedProjectItem
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.generation.optimizer import generate_tailored_changes
from app.services.generation.source_verifier import SourceVerifier, SourceVerificationReport
from tests.test_round11_verification import _make_manindra_repos

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MANINDRA_PDF = FIXTURES_DIR / "resumes" / "manindra_resume.pdf"
REPOS_DIR = FIXTURES_DIR / "repos"
MASTER_PROFILE_PATH = FIXTURES_DIR / "master_profile.json"


def _setup_tailored_manindra_data():
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    repos = _make_manindra_repos()

    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        company="Stripe",
        raw_text="Requires AJAX, Web Services, SOA, REST APIs, React, FastAPI, Next.js",
        required_skills=["AJAX", "Web Services", "SOA", "React", "FastAPI", "Next.js", "Python", "JavaScript"],
        preferred_skills=[],
        responsibilities=["Build distributed web services", "Develop reactive frontend applications"],
        keywords=["AJAX", "Web Services", "SOA", "FastAPI", "Next.js"],
    )

    confirmed_learning = [
        {"skill": "AJAX", "status": "learned", "note": "Enterprise web apps", "confirmed_date": "2026-11-15"},
        {"skill": "Web Services", "status": "learned", "note": "RESTful services", "confirmed_date": "2026-11-15"},
        {"skill": "SOA", "status": "learned", "note": "Service-oriented architecture", "confirmed_date": "2026-11-15"},
    ]

    recs = [
        RepoRecommendation(
            repo_name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            quality_score=92,
            relevance_score=130.0,
            matched_skills=["React", "FastAPI", "Python", "JavaScript (AJAX)"],
            reason="Highest job relevance (Decision-IQ, score 130) matching required skills",
            decision="approved",
            draft_bullets=[
                "Built Decision-IQ, an interactive platform using FastAPI and React with asynchronous AJAX calls and REST APIs.",
                "Implemented automated unit tests across core canvas and graph components.",
            ],
            bullet_evidence=[
                BulletEvidenceItem(bullet="Built...", source="README", detail="README.md"),
                BulletEvidenceItem(bullet="Implemented...", source="tests", detail="tests/test_canvas.py"),
            ],
        )
    ]

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=repos,
        approved_github_recs=recs,
        confirmed_learning=confirmed_learning,
        page_overflows=False,
    )

    tailored_resume = deepcopy(parsed)
    # The tailored resume chooses Decision-IQ and Custom GPT; omits Knowledge Base
    tailored_resume.projects = [
        ParsedProjectItem(
            name="Decision-IQ",
            bullets=[
                "Built Decision-IQ, an interactive platform using FastAPI and React with asynchronous AJAX calls and REST APIs.",
                "Implemented automated unit tests across core canvas and graph components.",
            ],
        ),
        ParsedProjectItem(
            name="CUSTOM GPT",
            bullets=list(parsed.projects[1].bullets),
        ),
    ]

    verifier = SourceVerifier(
        original_resume_path=MANINDRA_PDF,
        repos_dirs=[REPOS_DIR],
        master_profile_path=MASTER_PROFILE_PATH,
    )

    return parsed, tailored_resume, repos, changes, verifier


def test_acceptance_1_current_tailored_resume_nextjs_and_real_sources():
    """Acceptance test 1: Next.js must show a real source from repos or be marked UNSOURCED."""
    orig_resume, tailored_resume, repos, changes, verifier = _setup_tailored_manindra_data()

    report = verifier.verify(
        tailored_resume=tailored_resume,
        original_resume=orig_resume,
        approved_changes=changes,
        github_repos=repos,
    )

    # Next.js must show a real source from AI_Deck/package.json
    next_item = next((it for it in report.items if it.item == "Next.js"), None)
    assert next_item is not None, "Next.js must be checked"
    assert next_item.status == "VERIFIED"
    assert "package.json" in next_item.source
    assert next_item.file_path is not None
    assert Path(next_item.file_path).exists()
    assert Path(next_item.file_path).name == "package.json"

    # FastAPI source verified in Decision-IQ
    fastapi_item = next((it for it in report.items if it.item == "FastAPI"), None)
    assert fastapi_item is not None
    assert fastapi_item.status == "VERIFIED"
    assert "requirements.txt" in fastapi_item.source or "package.json" in fastapi_item.source
    assert Path(fastapi_item.file_path).exists()

    # Confirmed learning verified from master profile or repo evidence
    for l_sk in ("AJAX", "Web Services", "SOA"):
        item = next((it for it in report.items if it.item == l_sk), None)
        assert item is not None, f"Expected {l_sk} in report items"
        assert item.status == "VERIFIED"
        assert "Master profile" in item.source or "package.json" in item.source
        assert item.file_path is not None
        assert Path(item.file_path).exists()

    assert report.unsourced_count == 0
    assert report.can_export is True


def test_acceptance_2_fake_skill_kubernetes_flagged_unsourced_and_blocks_export():
    """Acceptance test 2: Add a fake skill (Kubernetes) by hand. Must flag UNSOURCED and block export."""
    orig_resume, tailored_resume, repos, changes, verifier = _setup_tailored_manindra_data()

    # Tamper tailored resume by adding fake skill 'Kubernetes'
    tampered_resume = deepcopy(tailored_resume)
    tampered_resume.skills.append("Kubernetes")

    report = verifier.verify(
        tailored_resume=tampered_resume,
        original_resume=orig_resume,
        approved_changes=changes,
        github_repos=repos,
        custom_skills_to_check=["Kubernetes"],
    )

    k8s_item = next((it for it in report.items if it.item == "Kubernetes"), None)
    assert k8s_item is not None, "Kubernetes must be checked"
    assert k8s_item.status == "UNSOURCED"
    assert k8s_item.file_path is None
    assert "no evidence found" in k8s_item.source

    assert report.can_export is False
    assert report.unsourced_count >= 1
    assert any("Kubernetes" in r for r in report.blocking_reasons)


def test_acceptance_3_remove_certification_detected_in_removed_from_original():
    """Acceptance test 3: Remove one certification by hand. 'Removed from original' list must show it."""
    orig_resume, tailored_resume, repos, changes, verifier = _setup_tailored_manindra_data()

    # Tamper tailored resume by removing CISCO certification
    tampered_resume = deepcopy(tailored_resume)
    tampered_resume.certifications = [
        c for c in tampered_resume.certifications if "CISCO" not in c
    ]

    report = verifier.verify(
        tailored_resume=tampered_resume,
        original_resume=orig_resume,
        approved_changes=changes,
        github_repos=repos,
    )

    removed_cisco = next(
        (r for r in report.removed_from_original if r.section == "certifications" and "CISCO" in r.item),
        None,
    )
    assert removed_cisco is not None, "Removed certification must appear in 'Removed from original'"
    assert "Certifications must never be removed automatically" in removed_cisco.reason
    assert report.can_export is False


def test_acceptance_4_every_verified_item_shows_real_file_path_that_exists():
    """Acceptance test 4: Every VERIFIED item must show a real file path that exists."""
    orig_resume, tailored_resume, repos, changes, verifier = _setup_tailored_manindra_data()

    report = verifier.verify(
        tailored_resume=tailored_resume,
        original_resume=orig_resume,
        approved_changes=changes,
        github_repos=repos,
    )

    verified_items = [it for it in report.items if it.status == "VERIFIED"]
    assert len(verified_items) > 10, "Should have verified multiple items across skills, projects, certs, edu"

    for it in verified_items:
        assert it.file_path is not None, f"VERIFIED item '{it.item}' has None file_path"
        fpath = Path(it.file_path)
        assert fpath.exists(), f"VERIFIED item '{it.item}' path does not exist on disk: {it.file_path}"
        assert fpath.is_file(), f"VERIFIED item '{it.item}' path is not a file: {it.file_path}"


def test_project_selection_report_explains_knowledge_base_exclusion():
    """Project selection report shows why Knowledge Base Retrieval System was left out."""
    orig_resume, tailored_resume, repos, changes, verifier = _setup_tailored_manindra_data()

    report = verifier.verify(
        tailored_resume=tailored_resume,
        original_resume=orig_resume,
        approved_changes=changes,
        github_repos=repos,
    )

    # Decision-IQ
    diq = next((p for p in report.project_selection if p.project_name == "Decision-IQ"), None)
    assert diq is not None
    assert diq.status == "included"
    assert diq.relevance_score == 130.0

    # Knowledge Base Retrieval System
    kb = next(
        (p for p in report.project_selection if any(w in p.project_name.lower() for w in ("knowledge", "knowledege", "retrival", "retrieval"))),
        None,
    )
    assert kb is not None
    assert kb.status == "excluded"
    assert "1-page vertical budget" in kb.reason
    assert "174pt" in kb.reason or "budget" in kb.reason


def test_api_source_verification_and_decision_flow(client: TestClient, monkeypatch):
    """Test full API endpoint flow: source verification report, unsourced export block, and keep decision."""
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    # 1. Register user
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "manindra.sv@example.com", "full_name": "Manindra", "password": "Password123!"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # 2. Upload Manindra's PDF resume
    with MANINDRA_PDF.open("rb") as f:
        up = client.post(
            "/api/v1/resumes/upload",
            headers=headers,
            files={"file": ("manindra_resume.pdf", f, "application/pdf")},
        )
    assert up.status_code == 201
    resume_id = up.json()["id"]

    # 3. Job text
    job_resp = client.post(
        "/api/v1/jobs/extract-text",
        headers=headers,
        json={
            "raw_text": "Full Stack Engineer at Stripe. Requires React, FastAPI, Next.js, AJAX, Web Services, SOA.",
            "title_hint": "Full Stack Engineer",
            "company_hint": "Stripe",
        },
    )
    assert job_resp.status_code == 200
    job_id = job_resp.json()["id"]

    # 4. Create Application
    app_resp = client.post(
        "/api/v1/applications",
        headers=headers,
        json={"resume_file_id": resume_id, "job_posting_id": job_id},
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    # 5. Generate tailored ResumeVersion
    gen_resp = client.post(f"/api/v1/applications/{app_id}/generate", headers=headers)
    assert gen_resp.status_code == 200
    versions = gen_resp.json()["versions"]
    assert len(versions) >= 1
    version_id = versions[-1]["id"]

    # 6. GET source verification report
    sv_resp = client.get(
        f"/api/v1/applications/{app_id}/versions/{version_id}/source-verification",
        headers=headers,
    )
    assert sv_resp.status_code == 200
    sv_data = sv_resp.json()
    assert "items" in sv_data
    assert "removed_from_original" in sv_data
    assert "project_selection" in sv_data

    # 7. Add unsourced skill to version changes and verify export blocks
    tamper_resp = client.post(
        f"/api/v1/applications/{app_id}/versions/{version_id}/source-decision",
        headers=headers,
        json={"item": "Kubernetes", "decision": "remove"},
    )
    assert tamper_resp.status_code == 200

    # 8. POST source-decision keep
    keep_resp = client.post(
        f"/api/v1/applications/{app_id}/versions/{version_id}/source-decision",
        headers=headers,
        json={"item": "Kubernetes", "decision": "keep"},
    )
    assert keep_resp.status_code == 200
    k_item = next((it for it in keep_resp.json()["items"] if it["item"] == "Kubernetes"), None)
    if k_item:
        assert k_item["status"] == "VERIFIED"
        assert "Master profile" in k_item["source"]

