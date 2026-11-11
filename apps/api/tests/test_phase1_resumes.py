from pathlib import Path

from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "resumes"


def _auth_headers(client: TestClient, email: str = "resume_tester@example.com") -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "full_name": "Resume Tester", "password": "StrongPassword123!"},
    )
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_upload_single_column_docx_and_confirm_to_master_profile(client: TestClient) -> None:
    headers = _auth_headers(client)
    docx_path = FIXTURES_DIR / "sample_single_column.docx"
    with docx_path.open("rb") as f:
        resp = client.post(
            "/api/v1/resumes/upload",
            headers=headers,
            files={"file": ("priya_resume.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["layout_type"] == "single_column"
    assert data["layout_support"] == "full"
    assert data["element_count"] > 5

    parsed = data["parsed_structure"]
    assert parsed["full_name"] == "Priya Sharma"
    assert parsed["email"] == "priya.sharma@example.com"
    assert len(parsed["experience"]) == 1
    assert parsed["experience"][0]["company"] == "CloudScale Systems"
    assert len(parsed["experience"][0]["bullets"]) == 2
    assert len(parsed["experience"][0]["element_ids"]) >= 3
    assert "Python" in parsed["skills"]
    assert len(parsed["projects"]) == 1

    # Confirm and correct into MasterProfile
    parsed["summary"] = "Updated confirmed summary for backend roles."
    confirm = client.post(
        f"/api/v1/resumes/{data['id']}/confirm",
        headers=headers,
        json={"parsed_structure": parsed},
    )
    assert confirm.status_code == 200, confirm.text
    profile = confirm.json()
    assert profile["full_name"] == "Priya Sharma"
    assert profile["summary"] == "Updated confirmed summary for backend roles."
    assert "FastAPI" in profile["skills"]


def test_upload_other_docx_layouts_and_pdf(client: TestClient) -> None:
    headers = _auth_headers(client, "layouts@example.com")

    # Two-column
    with (FIXTURES_DIR / "sample_two_column.docx").open("rb") as f:
        r2 = client.post("/api/v1/resumes/upload", headers=headers, files={"file": ("two.docx", f, "application/octet-stream")})
    assert r2.status_code == 201
    assert r2.json()["layout_type"] == "two_column"
    assert r2.json()["layout_support"] == "partial"

    # Table-based
    with (FIXTURES_DIR / "sample_table_based.docx").open("rb") as f:
        rt = client.post("/api/v1/resumes/upload", headers=headers, files={"file": ("table.docx", f, "application/octet-stream")})
    assert rt.status_code == 201
    assert rt.json()["layout_type"] == "table_based"
    assert rt.json()["layout_support"] == "full"
    assert rt.json()["parsed_structure"]["full_name"] == "Ananya Patel"

    # Textbox-based
    with (FIXTURES_DIR / "sample_textbox.docx").open("rb") as f:
        rx = client.post("/api/v1/resumes/upload", headers=headers, files={"file": ("tb.docx", f, "application/octet-stream")})
    assert rx.status_code == 201
    assert rx.json()["layout_type"] == "textbox_based"
    assert rx.json()["layout_support"] == "partial"

    # PDF (analysis only)
    with (FIXTURES_DIR / "sample_resume.pdf").open("rb") as f:
        rp = client.post("/api/v1/resumes/upload", headers=headers, files={"file": ("resume.pdf", f, "application/pdf")})
    assert rp.status_code == 201
    p_data = rp.json()
    assert p_data["file_type"] == "pdf"
    assert p_data["layout_support"] == "analysis_only"
    assert "Exact-format editing requires a DOCX" in p_data["format_notice"]
    assert p_data["page_count"] == 1
    assert p_data["parsed_structure"]["full_name"] == "Maya Nair"


def test_reject_invalid_uploads(client: TestClient) -> None:
    headers = _auth_headers(client, "badfile@example.com")

    bad_ext = client.post(
        "/api/v1/resumes/upload",
        headers=headers,
        files={"file": ("malware.exe", b"MZ1234", "application/octet-stream")},
    )
    assert bad_ext.status_code == 400

    fake_docx = client.post(
        "/api/v1/resumes/upload",
        headers=headers,
        files={"file": ("fake.docx", b"not-a-zip-archive", "application/octet-stream")},
    )
    assert fake_docx.status_code == 400
