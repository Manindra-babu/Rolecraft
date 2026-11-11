from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.jobs.ats import detect_ats_url, parse_ats_json_payload
from app.services.jobs.extractor import extract_job_from_html_fixture
from app.services.jobs.ssrf import SSRFError, normalize_job_url, validate_public_url
from app.services.llm.demo_client import DemoLLMClient

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jobs"


def _setup_user_with_key(client: TestClient, monkeypatch, email: str = "job_tester@example.com") -> dict[str, str]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "full_name": "Job Tester", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    monkeypatch.setattr("app.api.profile.validate_gemini_key", lambda k: (True, None))
    save = client.put("/api/v1/profile/api-key", headers=headers, json={"api_key": "AIzaSyValidTestKey1234567890"})
    assert save.status_code == 200
    return headers


def test_ssrf_blocks_private_internal_and_non_http() -> None:
    for bad in [
        "http://localhost:8000/admin",
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "file:///etc/passwd",
        "ftp://example.com/job",
    ]:
        with pytest.raises(SSRFError):
            validate_public_url(bad)


def test_url_normalization_strips_tracking() -> None:
    u1 = "https://boards.greenhouse.io/acme/jobs/12345/?utm_source=linkedin&fbclid=abc"
    u2 = "https://boards.greenhouse.io/acme/jobs/12345"
    assert normalize_job_url(u1) == normalize_job_url(u2)


def test_html_fixtures_extraction_and_validation() -> None:
    llm = DemoLLMClient()

    # 1. JSON-LD fixture
    html_jsonld = (FIXTURES / "jsonld_job.html").read_text(encoding="utf-8")
    method, fields, _, conf, reason = extract_job_from_html_fixture(html_jsonld, "https://acme.example/jobs/1", llm)
    assert method == "json_ld"
    assert reason is None
    assert fields.title == "Backend Software Engineer Intern"
    assert fields.company == "Acme Cloud"
    assert "FastAPI" in fields.required_skills
    assert conf >= 0.7

    # 2. Plain HTML fixture
    html_plain = (FIXTURES / "plain_job.html").read_text(encoding="utf-8")
    method_p, fields_p, _, conf_p, reason_p = extract_job_from_html_fixture(html_plain, "https://rivertech.example/jobs/2", llm)
    assert method_p == "http_html"
    assert reason_p is None
    assert "Python" in fields_p.required_skills
    assert len(fields_p.responsibilities) >= 2
    assert conf_p >= 0.5

    # 3. Greenhouse & Lever HTML fixtures
    html_gh = (FIXTURES / "greenhouse_job.html").read_text(encoding="utf-8")
    _, fields_gh, _, _, reason_gh = extract_job_from_html_fixture(html_gh, "https://boards.greenhouse.io/stripe/jobs/1", llm)
    assert reason_gh is None
    assert "Go" in fields_gh.required_skills

    html_lv = (FIXTURES / "lever_job.html").read_text(encoding="utf-8")
    _, fields_lv, _, _, reason_lv = extract_job_from_html_fixture(html_lv, "https://jobs.lever.co/notion/abc", llm)
    assert reason_lv is None
    assert "TypeScript" in fields_lv.required_skills

    # 4. Login wall MUST be rejected
    html_wall = (FIXTURES / "login_wall.html").read_text(encoding="utf-8")
    _, _, _, conf_wall, reason_wall = extract_job_from_html_fixture(html_wall, "https://example.com/private-job", llm)
    assert reason_wall is not None
    assert conf_wall == 0.0


def test_ats_url_detection_and_json_parsing() -> None:
    gh = detect_ats_url("https://boards.greenhouse.io/vercel/jobs/987654")
    assert gh is not None and gh.provider == "greenhouse"
    assert gh.api_url == "https://boards-api.greenhouse.io/v1/boards/vercel/jobs/987654"

    lv = detect_ats_url("https://jobs.lever.co/figma/1111-2222")
    assert lv is not None and lv.provider == "lever"

    ashby = detect_ats_url("https://jobs.ashbyhq.com/linear/abc-123")
    assert ashby is not None and ashby.provider == "ashby"

    fields, text = parse_ats_json_payload(
        "greenhouse",
        {
            "title": "Software Engineer",
            "location": {"name": "Remote"},
            "content": "<p>Build APIs with Python and PostgreSQL.</p>",
        },
        "vercel",
        "987654",
    )
    assert fields.title == "Software Engineer"
    assert "Python" in text


def test_job_endpoints_gate_cache_and_fallback(client: TestClient, monkeypatch) -> None:
    # Unkeyed user is blocked
    reg_no_key = client.post(
        "/api/v1/auth/register",
        json={"email": "nokey_job@example.com", "full_name": "No Key", "password": "StrongPassword123!"},
    )
    no_key_headers = {"Authorization": f"Bearer {reg_no_key.json()['access_token']}"}
    blocked = client.post(
        "/api/v1/jobs/extract-text",
        headers=no_key_headers,
        json={"raw_text": "Backend Engineer at TestCorp. Responsibilities: build Python APIs with FastAPI and SQL."},
    )
    assert blocked.status_code == 403

    headers = _setup_user_with_key(client, monkeypatch)

    # SSRF rejected by API
    ssrf_resp = client.post(
        "/api/v1/jobs/extract-url",
        headers=headers,
        json={"url": "http://127.0.0.1:8080/internal"},
    )
    assert ssrf_resp.status_code == 400

    # LinkedIn URL gracefully routes to fallback_required without aggressive scraping
    monkeypatch.setattr("app.services.jobs.extractor.safe_fetch_url", lambda *a, **kw: (a[0], "<html><body>Sign in</body></html>", 200))
    monkeypatch.setattr("app.services.jobs.extractor.validate_public_url", lambda u: u)
    li_resp = client.post(
        "/api/v1/jobs/extract-url",
        headers=headers,
        json={"url": "https://www.linkedin.com/jobs/view/123456789/"},
    )
    assert li_resp.status_code == 200
    assert li_resp.json()["status"] == "fallback_required"

    # Manual paste fallback extracts, saves, and can be edited/confirmed
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    paste_resp = client.post(
        "/api/v1/jobs/extract-text",
        headers=headers,
        json={
            "raw_text": (
                "Backend Software Engineer Intern at NorthStar\n"
                "Responsibilities:\n"
                "- Build REST APIs in Python and FastAPI\n"
                "- Write automated tests and manage PostgreSQL schemas\n"
                "Requirements:\n"
                "- Python, FastAPI, PostgreSQL, Docker, Git"
            ),
            "title_hint": "Backend Software Engineer Intern",
            "company_hint": "NorthStar",
        },
    )
    assert paste_resp.status_code == 200, paste_resp.text
    job = paste_resp.json()
    assert job["status"] == "extracted"
    assert job["title"] == "Backend Software Engineer Intern"
    assert "FastAPI" in job["required_skills"]

    # Update confirmation endpoint
    upd = client.put(
        f"/api/v1/jobs/{job['id']}",
        headers=headers,
        json={
            "title": "Backend Engineer Intern (Confirmed)",
            "company": "NorthStar",
            "location": "Remote",
            "experience_level": "Internship",
            "responsibilities": job["responsibilities"],
            "required_skills": ["Python", "FastAPI", "PostgreSQL"],
            "preferred_skills": ["Docker"],
            "keywords": ["Python", "FastAPI"],
        },
    )
    assert upd.status_code == 200
    assert upd.json()["title"] == "Backend Engineer Intern (Confirmed)"
    get_settings.cache_clear()
