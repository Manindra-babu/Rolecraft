from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from app.schemas.github import AnalyzedRepo
from app.schemas.resume import ParsedExperienceItem, ParsedResumeStructure
from app.services.generation.validator import validate_claim_against_evidence
from app.services.resume.docx_editor import apply_changes_to_docx_in_place

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "resumes"


def test_truthfulness_validator_blocks_adversarial_claims() -> None:
    resume = ParsedResumeStructure(
        full_name="Priya Sharma",
        experience=[
            ParsedExperienceItem(
                company="CloudScale",
                title="Intern",
                bullets=["Built FastAPI microservices for invoice processing, cutting response latency by 28%."],
            )
        ],
        skills=["Python", "FastAPI", "PostgreSQL"],
    )
    raw = "Built FastAPI microservices for invoice processing, cutting response latency by 28%. Python FastAPI PostgreSQL."

    # 1. Legitimate rewording passes
    ok = validate_claim_against_evidence(
        new_text="Built FastAPI microservices in Python for invoice processing, reducing latency by 28%.",
        original_text="Built FastAPI microservices for invoice processing, cutting response latency by 28%.",
        resume=resume,
        raw_resume_text=raw,
    )
    assert ok.status == "passed"
    assert not ok.warnings

    # 2. Adversarial invented metric (45% or 100k users) is BLOCKED
    bad_metric = validate_claim_against_evidence(
        new_text="Built FastAPI microservices serving 100k users, cutting latency by 45%.",
        original_text="Built FastAPI microservices for invoice processing, cutting response latency by 28%.",
        resume=resume,
        raw_resume_text=raw,
    )
    assert bad_metric.status == "blocked"
    assert any("45%" in w or "100k" in w for w in bad_metric.warnings)

    # 3. Adversarial unverified tool (Kubernetes) is BLOCKED
    bad_tool = validate_claim_against_evidence(
        new_text="Built FastAPI microservices deployed on Kubernetes.",
        original_text="Built FastAPI microservices for invoice processing, cutting response latency by 28%.",
        resume=resume,
        raw_resume_text=raw,
    )
    assert bad_tool.status == "blocked"
    assert "Kubernetes" in bad_tool.unverified_tools

    # 4. Inflated leadership verb ("Led") when evidence is individual contribution is FLAGGED
    inflated = validate_claim_against_evidence(
        new_text="Led FastAPI microservices in Python for invoice processing.",
        original_text="Built FastAPI microservices for invoice processing, cutting response latency by 28%.",
        resume=resume,
        raw_resume_text=raw,
    )
    assert inflated.status == "flagged"
    assert inflated.inflated_verbs

    # 5. Tool present in verified GitHub repo passes!
    gh_repo = AnalyzedRepo(
        name="docker-api",
        full_name="priya/docker-api",
        html_url="https://github.com/priya/docker-api",
        inferred_skills=["Docker", "FastAPI"],
        quality_score=85.0,
    )
    from_gh = validate_claim_against_evidence(
        new_text="Containerized FastAPI service using Docker.",
        original_text="",
        resume=resume,
        raw_resume_text=raw,
        github_repos=[gh_repo],
    )
    assert from_gh.status == "passed"


def test_inplace_docx_editing_preserves_fonts_and_styles(tmp_path: Path) -> None:
    src = FIXTURES_DIR / "sample_single_column.docx"
    out = tmp_path / "edited.docx"

    # Find paragraph index of the first bullet in sample_single_column.docx
    orig_doc = Document(str(src))
    bullet_idx = next(
        i for i, p in enumerate(orig_doc.paragraphs) if "cutting response latency by 28%" in p.text
    )
    orig_font = orig_doc.paragraphs[bullet_idx].runs[0].font.name
    assert orig_font == "Calibri"

    apply_changes_to_docx_in_place(
        source_docx_path=src,
        output_docx_path=out,
        approved_changes=[
            {
                "section": "experience",
                "change_type": "reworded",
                "target_location": {"kind": "body_paragraph", "paragraph_index": bullet_idx},
                "original_text": orig_doc.paragraphs[bullet_idx].text,
                "new_text": "Implemented FastAPI microservices in Python for invoice processing, cutting response latency by 28%.",
            },
            {
                "section": "projects",
                "change_type": "added_from_github",
                "target_location": {"insert_section": "projects"},
                "original_text": "",
                "new_text": "BillingService | FastAPI, PostgreSQL | https://github.com/priya/billing\n• Built REST endpoints with automated pytest coverage.",
            },
        ],
    )

    edited_doc = Document(str(out))
    assert "Implemented FastAPI microservices in Python" in edited_doc.paragraphs[bullet_idx].text
    # Font family & bullet style preserved!
    assert edited_doc.paragraphs[bullet_idx].runs[0].font.name == "Calibri"
    assert edited_doc.paragraphs[bullet_idx].style.name == "List Bullet"
    all_text = "\n".join(p.text for p in edited_doc.paragraphs)
    assert "BillingService" in all_text


def test_end_to_end_application_review_and_export(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()

    # 1. Register
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "e2e@example.com", "full_name": "Priya Sharma", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # 2. Upload DOCX resume
    with (FIXTURES_DIR / "sample_single_column.docx").open("rb") as f:
        up = client.post(
            "/api/v1/resumes/upload",
            headers=headers,
            files={"file": ("priya.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
    assert up.status_code == 201
    resume_id = up.json()["id"]

    # 3. Extract job from pasted text
    job_resp = client.post(
        "/api/v1/jobs/extract-text",
        headers=headers,
        json={
            "raw_text": (
                "Backend Software Engineer Intern at Acme Cloud\n"
                "Responsibilities:\n- Build REST APIs using Python and FastAPI\n"
                "Requirements:\n- Python, FastAPI, PostgreSQL, Docker, Kubernetes"
            ),
            "title_hint": "Backend Software Engineer Intern",
            "company_hint": "Acme Cloud",
        },
    )
    assert job_resp.status_code == 200
    job_id = job_resp.json()["id"]

    # 4. Create Application (matching + 5-category gaps + learning plans)
    app_resp = client.post(
        "/api/v1/applications",
        headers=headers,
        json={"resume_file_id": resume_id, "job_posting_id": job_id},
    )
    assert app_resp.status_code == 201, app_resp.text
    app_data = app_resp.json()
    app_id = app_data["id"]
    assert app_data["compatibility_before"] is not None
    assert len(app_data["skill_assessments"]) >= 4
    assert any(p["skill_name"] == "Kubernetes" for p in app_data["learning_plans"])

    # 5. Generate tailored ResumeVersion
    gen_resp = client.post(f"/api/v1/applications/{app_id}/generate", headers=headers)
    assert gen_resp.status_code == 200, gen_resp.text
    versions = gen_resp.json()["versions"]
    assert len(versions) == 1
    ver = versions[0]
    ver_id = ver["id"]

    # 6. Accept/edit a change; ensure blocked unverified edit cannot be accepted
    if ver["changes"]:
        ch = ver["changes"][0]
        bad_edit = client.patch(
            f"/api/v1/applications/{app_id}/changes/{ch['id']}",
            headers=headers,
            json={"status": "edited", "edited_text": "Boosted revenue by 99% using Kubernetes."},
        )
        assert bad_edit.status_code == 200
        assert bad_edit.json()["validation_status"] == "blocked"
        assert bad_edit.json()["status"] == "pending"

        # Valid edit is accepted
        good_edit = client.patch(
            f"/api/v1/applications/{app_id}/changes/{ch['id']}",
            headers=headers,
            json={"status": "edited", "edited_text": "Built FastAPI microservices in Python with PostgreSQL."},
        )
        assert good_edit.status_code == 200
        assert good_edit.json()["validation_status"] == "passed"
        assert good_edit.json()["status"] == "edited"

    # 7. Export DOCX + PDF + preview PNG
    exp_resp = client.post(
        f"/api/v1/applications/{app_id}/versions/{ver_id}/export",
        headers=headers,
    )
    assert exp_resp.status_code == 200, exp_resp.text
    exp = exp_resp.json()
    assert exp["has_docx"] is True
    assert exp["has_pdf"] is True
    assert exp["has_preview"] is True
    assert exp["page_count_output"] >= 1

    dl_docx = client.get(
        f"/api/v1/applications/{app_id}/versions/{ver_id}/download/docx",
        headers=headers,
    )
    assert dl_docx.status_code == 200
    assert dl_docx.content.startswith(b"PK\x03\x04")

    dl_pdf = client.get(
        f"/api/v1/applications/{app_id}/versions/{ver_id}/download/pdf",
        headers=headers,
    )
    assert dl_pdf.status_code == 200
    assert dl_pdf.content.startswith(b"%PDF")

    get_settings.cache_clear()
