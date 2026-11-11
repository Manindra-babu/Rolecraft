from pathlib import Path

from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "resumes"


def test_extension_capture_tracker_and_privacy_deletion(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()

    # Register & save a valid Gemini key
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "privacy@example.com", "full_name": "Privacy User", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    monkeypatch.setattr("app.api.profile.validate_gemini_key", lambda k: (True, None))
    secret_key = "AIzaSyPrivacyTestKey1234567890ABCD"
    client.put("/api/v1/profile/api-key", headers=headers, json={"api_key": secret_key})

    # Upload resume
    with (FIXTURES_DIR / "sample_single_column.docx").open("rb") as f:
        up = client.post(
            "/api/v1/resumes/upload",
            headers=headers,
            files={"file": ("priya.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
    assert up.status_code == 201
    resume_id = up.json()["id"]

    # Capture job via browser extension endpoint
    ext_resp = client.post(
        "/api/v1/extension/capture-job",
        headers=headers,
        json={
            "url": "https://www.linkedin.com/jobs/view/99887766/",
            "page_title": "Platform Engineer Intern | LinkedIn",
            "raw_text": (
                "Platform Engineer Intern\n"
                "Responsibilities:\n- Build FastAPI APIs and Docker workflows\n"
                "Requirements:\n- Python, FastAPI, PostgreSQL, Docker"
            ),
        },
    )
    assert ext_resp.status_code == 200
    job_id = ext_resp.json()["id"]
    assert ext_resp.json()["extraction_method"] == "browser_extension"

    # Create application and update tracker status
    app_resp = client.post(
        "/api/v1/applications",
        headers=headers,
        json={"resume_file_id": resume_id, "job_posting_id": job_id},
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    status_resp = client.patch(
        f"/api/v1/applications/{app_id}/status",
        headers=headers,
        json={"status": "Applied", "notes": "Submitted via referral"},
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "Applied"

    # Privacy export contains user records, NEVER contains raw Gemini key or password hash
    export_resp = client.get("/api/v1/privacy/export", headers=headers)
    assert export_resp.status_code == 200
    export_text = export_resp.text
    assert secret_key not in export_text
    assert "hashed_password" not in export_text
    assert len(export_resp.json()["applications"]) == 1

    # Delete account removes everything
    del_resp = client.delete("/api/v1/privacy/account", headers=headers)
    assert del_resp.status_code == 204

    me_after = client.get("/api/v1/auth/me", headers=headers)
    assert me_after.status_code == 401
    get_settings.cache_clear()
