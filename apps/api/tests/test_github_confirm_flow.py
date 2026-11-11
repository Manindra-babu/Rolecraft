from datetime import datetime, timedelta, timezone
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import encrypt_api_key
from app.models.application import Application, GitHubConfirmJob
from app.models.github import GitHubAnalysis
from app.models.user import User
from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals
from app.services.github.scorer import build_factual_repo_bullets_with_evidence
from app.services.llm.base import LLMKeyInvalidError


def _make_docx_bytes() -> bytes:
    doc = Document()
    doc.add_heading("Alex Candidate", level=1)
    doc.add_paragraph("alex@example.com | https://github.com/Manindra-babu")
    doc.add_heading("Experience", level=2)
    doc.add_paragraph("Software Engineer Intern - Acme Corp (2024)")
    doc.add_paragraph("Built FastAPI endpoints and React interfaces with Python and JavaScript.")
    doc.add_heading("Skills", level=2)
    doc.add_paragraph("Languages: Python, JavaScript, TypeScript")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _seed_app_with_github(
    client: TestClient,
    db_session: Session,
    *,
    email: str = "ghflow@example.com",
) -> tuple[dict[str, str], str, str]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Alex Candidate"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    user_id = reg.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    up = client.post(
        "/api/v1/resumes/upload",
        headers=headers,
        files={
            "file": (
                "alex.docx",
                _make_docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert up.status_code == 201
    resume_id = up.json()["id"]

    job = client.post(
        "/api/v1/jobs/extract-text",
        headers=headers,
        json={
            "title": "Software Engineer",
            "company": "PayPal",
            "raw_text": "Required skills: Python, JavaScript, React, FastAPI, REST APIs.",
        },
    )
    assert job.status_code == 200
    job_id = job.json()["id"]

    gh = GitHubAnalysis(
        user_id=user_id,
        github_username="Manindra-babu",
        profile_url="https://github.com/Manindra-babu",
        repos_data=[
            {
                "name": "AI_Deck",
                "full_name": "Manindra-babu/AI_Deck",
                "html_url": "https://github.com/Manindra-babu/AI_Deck",
                "description": "",
                "primary_language": "TypeScript",
                "languages": {"TypeScript": 6000, "Python": 4000, "HTML": 1000, "PowerShell": 200},
                "dependencies": ["react", "fastapi", "pytest"],
                "inferred_skills": ["TypeScript", "Python", "React", "FastAPI", "PowerShell"],
                "key_files": ["api/main.py", "web/App.tsx", "tests/test_slides.py"],
                "test_files": ["tests/test_slides.py"],
                "commit_messages": [
                    "feat: add slide layout streaming and export pipeline",
                    "initial commit",
                ],
                "readme_excerpt": "# AI_Deck\nAI presentation builder that converts structured outlines into editable slide decks.",
                "signals": {
                    "has_tests": True,
                    "has_ci": False,
                    "has_dockerfile": False,
                    "has_readme": True,
                    "readme_length": 400,
                    "file_count": 64,
                    "total_commits": 20,
                    "user_commits": 20,
                    "user_commit_share": 1.0,
                },
                "quality_score": 86.0,
                "quality_notes": ["Automated tests present"],
            },
            {
                "name": "Decision-IQ",
                "full_name": "Manindra-babu/Decision-IQ",
                "html_url": "https://github.com/Manindra-babu/Decision-IQ",
                "description": "AI-powered roadmap generator with interactive graph visualization",
                "primary_language": "JavaScript",
                "languages": {"JavaScript": 5000, "Python": 4000},
                "dependencies": ["fastapi", "react"],
                "inferred_skills": ["JavaScript", "Python", "FastAPI", "React"],
                "key_files": ["backend/app.py", "src/GraphCanvas.jsx"],
                "test_files": [],
                "commit_messages": ["feat: implement interactive DAG node editor"],
                "readme_excerpt": "# Decision-IQ\nInteractive roadmap generator and learning path tracker.",
                "signals": {
                    "has_tests": False,
                    "has_ci": False,
                    "has_dockerfile": False,
                    "has_readme": True,
                    "readme_length": 350,
                    "file_count": 39,
                    "total_commits": 15,
                    "user_commits": 15,
                    "user_commit_share": 1.0,
                },
                "quality_score": 95.0,
                "quality_notes": ["15 personal commits"],
            },
        ],
        detected_skills=[],
        fetched_at=datetime.now(timezone.utc),
    )
    db_session.add(gh)
    db_session.commit()
    db_session.refresh(gh)

    app_resp = client.post(
        "/api/v1/applications",
        headers=headers,
        json={
            "resume_file_id": resume_id,
            "job_posting_id": job_id,
            "github_analysis_id": gh.id,
        },
    )
    assert app_resp.status_code == 201
    return headers, user_id, app_resp.json()["id"]


def test_improved_github_bullets_no_metadata_and_specific_tests() -> None:
    repo_with_tests = AnalyzedRepo(
        name="AI_Deck",
        full_name="Manindra-babu/AI_Deck",
        html_url="https://github.com/Manindra-babu/AI_Deck",
        description="",
        primary_language="TypeScript",
        languages={"TypeScript": 6000, "Python": 4000, "PowerShell": 200, "HTML": 500},
        dependencies=["fastapi", "react", "pytest"],
        inferred_skills=["TypeScript", "Python", "React", "FastAPI", "PowerShell"],
        key_files=["api/main.py", "tests/test_slides.py"],
        test_files=["tests/test_slides.py"],
        commit_messages=["feat: implement slide deck outline generator"],
        readme_excerpt="# AI_Deck\nAI presentation builder generating editable slide decks from Markdown.",
        signals=RepoEvidenceSignals(
            has_tests=True,
            has_readme=True,
            readme_length=300,
            file_count=64,
            total_commits=12,
            user_commits=12,
            user_commit_share=1.0,
        ),
        quality_score=86.0,
    )
    bullets, evidence = build_factual_repo_bullets_with_evidence(repo_with_tests)
    joined = " ".join(bullets)
    assert "source files" not in joined.lower()
    assert "core module behavior" not in joined.lower()
    assert "powershell" not in joined.lower()
    assert "AI_Deck" in bullets[0]
    assert any("test_slides.py" in b for b in bullets)
    assert len(evidence) == len(bullets)
    assert all(ev.source and ev.detail for ev in evidence)

    repo_without_test_files = AnalyzedRepo(
        name="Plain-App",
        full_name="Manindra-babu/Plain-App",
        html_url="https://github.com/Manindra-babu/Plain-App",
        description="Task routing microservice",
        primary_language="Python",
        dependencies=["fastapi"],
        inferred_skills=["Python", "FastAPI"],
        signals=RepoEvidenceSignals(has_tests=False, file_count=18),
    )
    b2, _ = build_factual_repo_bullets_with_evidence(repo_without_test_files)
    assert "test" not in " ".join(b2).lower()
    assert "source files" not in " ".join(b2).lower()


def test_github_decisions_and_confirm_validation_and_missing_key(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    headers, user_id, app_id = _seed_app_with_github(client, db_session, email="val@example.com")

    # Approve -> Reject -> Pending decision persistence
    d1 = client.post(
        f"/api/v1/applications/{app_id}/github-decisions",
        headers=headers,
        json={"decisions": [{"repo_name": "AI_Deck", "decision": "approved"}]},
    )
    assert d1.status_code == 200
    rec_map = {r["repo_name"]: r for r in d1.json()["github_recommendations"]}
    assert rec_map["AI_Deck"]["decision"] == "approved"

    d2 = client.post(
        f"/api/v1/applications/{app_id}/github-decisions",
        headers=headers,
        json={"decisions": [{"repo_name": "AI_Deck", "decision": "pending"}]},
    )
    assert d2.status_code == 200
    assert {r["repo_name"]: r["decision"] for r in d2.json()["github_recommendations"]}["AI_Deck"] == "pending"

    # Empty repo_names rejected by Pydantic validation (422)
    empty_confirm = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": []},
    )
    assert empty_confirm.status_code == 422

    blank_confirm = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["   "]},
    )
    assert blank_confirm.status_code == 422

    # Unknown repo name rejected (400)
    bad_repo = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["does-not-exist"]},
    )
    assert bad_repo.status_code == 400

    # Missing Gemini key when DEMO_MODE=false returns clear 403
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    no_key = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["AI_Deck"]},
    )
    assert no_key.status_code == 403
    assert no_key.json()["detail"]["code"] == "GEMINI_KEY_REQUIRED"


def test_github_confirm_end_to_end_creates_pending_changes_and_updates_existing_version(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    headers, _, app_id = _seed_app_with_github(client, db_session, email="e2e@example.com")

    confirm_resp = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["AI_Deck"]},
    )
    assert confirm_resp.status_code == 202
    job_data = confirm_resp.json()
    assert job_data["status"] == "done"
    assert job_data["added_count"] >= 1
    assert job_data["version_id"] is not None

    poll_resp = client.get(
        f"/api/v1/applications/{app_id}/github-confirm/{job_data['job_id']}",
        headers=headers,
    )
    assert poll_resp.status_code == 200
    assert poll_resp.json()["status"] == "done"

    detail = client.get(f"/api/v1/applications/{app_id}", headers=headers)
    assert detail.status_code == 200
    versions = detail.json()["versions"]
    assert len(versions) == 1
    gh_changes = [c for c in versions[0]["changes"] if c["change_type"] == "added_from_github"]
    assert len(gh_changes) == 1
    assert gh_changes[0]["status"] == "pending"
    assert "AI_Deck" in gh_changes[0]["new_text"]
    assert "source files" not in gh_changes[0]["new_text"].lower()
    assert "core module behavior" not in gh_changes[0]["new_text"].lower()
    assert len(gh_changes[0]["evidence_refs"]) >= 1

    # Mark first GitHub change accepted, then confirm Decision-IQ -> preserves existing decisions & adds Decision-IQ as pending
    patch_resp = client.patch(
        f"/api/v1/applications/{app_id}/changes/{gh_changes[0]['id']}",
        headers=headers,
        json={"status": "accepted"},
    )
    assert patch_resp.status_code == 200

    confirm_second = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["Decision-IQ"]},
    )
    assert confirm_second.status_code == 202
    assert confirm_second.json()["status"] == "done"

    detail2 = client.get(f"/api/v1/applications/{app_id}", headers=headers)
    gh_changes2 = [c for c in detail2.json()["versions"][0]["changes"] if c["change_type"] == "added_from_github"]
    by_repo = {c["target_location"]["repo_name"]: c for c in gh_changes2}
    assert by_repo["AI_Deck"]["status"] == "accepted"
    assert by_repo["Decision-IQ"]["status"] == "pending"


def test_github_confirm_handles_invalid_gemini_key_and_timeout(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    headers, user_id, app_id = _seed_app_with_github(client, db_session, email="err@example.com")

    # Configure a real-looking encrypted key and switch DEMO_MODE=false
    user = db_session.query(User).filter(User.id == user_id).first()
    assert user is not None
    user.encrypted_gemini_key = encrypt_api_key("AIzaSyFakeKeyForWorkerErrorTest123456")
    user.gemini_key_status = "valid"
    db_session.add(user)
    db_session.commit()

    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()

    class BrokenGeminiLLM:
        def generate_structured(self, **_kwargs):
            raise LLMKeyInvalidError("API key expired or revoked")

    monkeypatch.setattr(
        "app.services.github.client.get_llm_client_for_user",
        lambda *_args, **_kwargs: BrokenGeminiLLM(),
    )
    monkeypatch.setattr(
        "app.workers.tasks.get_llm_client_for_user",
        lambda *_args, **_kwargs: BrokenGeminiLLM(),
    )

    resp = client.post(
        f"/api/v1/applications/{app_id}/github-confirm",
        headers=headers,
        json={"repo_names": ["AI_Deck"]},
    )
    assert resp.status_code == 202
    job_body = resp.json()
    assert job_body["status"] == "failed"
    assert job_body["error_code"] == "GEMINI_KEY_INVALID"
    assert "Gemini key needs attention" in (job_body["error_message"] or "")

    # Verify stale job timeout detection
    stale_job = GitHubConfirmJob(
        application_id=app_id,
        user_id=user_id,
        repo_names=["AI_Deck"],
        status="generating",
        progress_percent=35,
        stage_message="Generating...",
    )
    db_session.add(stale_job)
    db_session.commit()
    db_session.refresh(stale_job)
    # Backdate updated_at past 90s timeout
    stale_job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db_session.add(stale_job)
    db_session.commit()

    timeout_resp = client.get(
        f"/api/v1/applications/{app_id}/github-confirm/{stale_job.id}",
        headers=headers,
    )
    assert timeout_resp.status_code == 200
    assert timeout_resp.json()["status"] == "failed"
    assert timeout_resp.json()["error_code"] == "TIMEOUT"
