from fastapi.testclient import TestClient

from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals
from app.services.github.scorer import (
    aggregate_github_skills,
    extract_github_username,
    infer_repo_skills,
    parse_dependencies_from_manifest,
    rank_repos_for_job,
    score_repo_evidence,
)


def test_github_username_extraction() -> None:
    assert extract_github_username("https://github.com/octocat/") == "octocat"
    assert extract_github_username("github.com/priya-dev") == "priya-dev"
    assert extract_github_username("@alex") == "alex"
    assert extract_github_username("") == ""


def test_evidence_scoring_filters_forks_empty_and_tutorials() -> None:
    # Strong original repo
    strong_sig = RepoEvidenceSignals(
        has_tests=True,
        has_ci=True,
        has_dockerfile=True,
        has_readme=True,
        readme_length=800,
        file_count=18,
        total_commits=24,
        user_commits=24,
        user_commit_share=1.0,
    )
    score, notes, excluded = score_repo_evidence(
        name="fastapi-billing",
        description="Subscription invoice service",
        readme_text="Architecture and API endpoints...",
        signals=strong_sig,
    )
    assert excluded is None
    assert score >= 80.0
    assert "Automated tests present" in notes

    # Empty repo
    empty_sig = RepoEvidenceSignals(is_empty=True, file_count=0)
    e_score, _, e_excl = score_repo_evidence(
        name="empty-repo", description="", readme_text="", signals=empty_sig
    )
    assert e_excl is not None
    assert e_score <= 10.0

    # Untouched fork
    fork_sig = RepoEvidenceSignals(is_fork=True, file_count=40, total_commits=200, user_commits=0)
    f_score, _, f_excl = score_repo_evidence(
        name="django", description="Forked framework", readme_text="", signals=fork_sig
    )
    assert f_excl is not None
    assert f_score <= 15.0

    # Tutorial clone
    tut_sig = RepoEvidenceSignals(file_count=10, total_commits=2, user_commits=2, user_commit_share=1.0)
    t_score, _, t_excl = score_repo_evidence(
        name="react-todo-app-tutorial",
        description="Follow along freecodecamp clone",
        readme_text="Create React App",
        signals=tut_sig,
    )
    assert t_excl is not None
    assert t_score <= 30.0


def test_manifest_skill_inference_and_job_ranking() -> None:
    reqs = "fastapi==0.115.0\npsycopg[binary]>=3.2\npytest\nredis\n"
    deps = parse_dependencies_from_manifest("requirements.txt", reqs)
    assert "fastapi" in deps
    assert "redis" in deps

    sig = RepoEvidenceSignals(
        has_tests=True, has_dockerfile=True, has_readme=True, readme_length=500, file_count=15, user_commits=18, total_commits=18, user_commit_share=1.0
    )
    skills = infer_repo_skills({"Python": 12000}, ["rest-api"], deps, sig)
    assert "FastAPI" in skills
    assert "PostgreSQL" in skills
    assert "Docker" in skills

    repo_a = AnalyzedRepo(
        name="api-gateway",
        full_name="dev/api-gateway",
        html_url="https://github.com/dev/api-gateway",
        description="FastAPI and PostgreSQL billing gateway",
        primary_language="Python",
        inferred_skills=skills,
        signals=sig,
        quality_score=88.0,
        quality_notes=["18 personal commits", "Automated tests present"],
    )
    repo_b = AnalyzedRepo(
        name="hello-tutorial",
        full_name="dev/hello-tutorial",
        html_url="https://github.com/dev/hello-tutorial",
        primary_language="JavaScript",
        inferred_skills=["JavaScript"],
        quality_score=15.0,
        excluded_reason="Tutorial clone",
    )

    agg = aggregate_github_skills([repo_a, repo_b])
    assert any(s.skill == "Python" and "1 repo" in s.evidence_level for s in agg)

    recs = rank_repos_for_job(
        [repo_a, repo_b],
        job_skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
        job_keywords=["REST APIs"],
        resume_text="Built other unrelated projects",
    )
    assert len(recs) == 1
    assert recs[0].repo_name == "api-gateway"
    assert recs[0].already_in_resume is False
    assert "FastAPI" in recs[0].matched_skills
    assert len(recs[0].draft_bullets) >= 1


def test_github_optional_skip_and_mocked_endpoint(client: TestClient, monkeypatch) -> None:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "gh_user@example.com", "full_name": "GH User", "password": "StrongPassword123!"},
    )
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    monkeypatch.setattr("app.api.profile.validate_gemini_key", lambda k: (True, None))
    client.put("/api/v1/profile/api-key", headers=headers, json={"api_key": "AIzaSyValidTestKey1234567890"})

    # Empty GitHub URL skips without error and stores plain links
    skip_resp = client.post(
        "/api/v1/github/analyze",
        headers=headers,
        json={"profile_url_or_username": "", "other_links": ["https://kaggle.com/ghuser"]},
    )
    assert skip_resp.status_code == 200
    assert skip_resp.json()["skipped"] is True
    assert skip_resp.json()["other_links"] == ["https://kaggle.com/ghuser"]

    # Mock GitHubClient for real username flow
    sample_repo = AnalyzedRepo(
        name="resume-api",
        full_name="ghuser/resume-api",
        html_url="https://github.com/ghuser/resume-api",
        description="Backend API in FastAPI and PostgreSQL",
        primary_language="Python",
        inferred_skills=["Python", "FastAPI", "PostgreSQL"],
        quality_score=85.0,
        quality_notes=["15 personal commits", "Automated tests present"],
    )
    monkeypatch.setattr(
        "app.services.github.client.GitHubClient.fetch_and_analyze_user",
        lambda self, username, cached_etag=None: ([sample_repo], aggregate_github_skills([sample_repo]), "etag1", False),
    )

    ok_resp = client.post(
        "/api/v1/github/analyze",
        headers=headers,
        json={"profile_url_or_username": "https://github.com/ghuser", "other_links": []},
    )
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["skipped"] is False
    assert body["github_username"] == "ghuser"
    assert len(body["repos"]) == 1
