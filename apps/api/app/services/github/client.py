import base64
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.models.github import GitHubAnalysis
from app.models.user import MasterProfile, User
from app.schemas.github import (
    AnalyzedRepo,
    DetectedGitHubSkill,
    GitHubAnalysisResponse,
    RepoEvidenceSignals,
)
from app.services.github.scorer import (
    aggregate_github_skills,
    build_factual_repo_bullets,
    extract_github_username,
    infer_repo_skills,
    parse_dependencies_from_manifest,
    score_repo_evidence,
)
from app.services.llm.factory import get_llm_client_for_user

GITHUB_API_BASE = "https://api.github.com"
MANIFEST_FILES = ("requirements.txt", "pyproject.toml", "package.json", "pom.xml", "go.mod")


class RepoBulletDraftSchema(BaseModel):
    bullets: list[str] = Field(default_factory=list)


class GitHubClient:
    """GitHub REST API client using server GITHUB_TOKEN with ETag and rate-limit handling."""

    def __init__(self) -> None:
        settings = get_settings()
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ResumeIQ/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.GITHUB_TOKEN:
            self._headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    def _get(
        self, client: httpx.Client, path: str, etag: str | None = None
    ) -> httpx.Response:
        headers = dict(self._headers)
        if etag:
            headers["If-None-Match"] = etag
        resp = client.get(f"{GITHUB_API_BASE}{path}", headers=headers)
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if resp.status_code in (403, 429) and remaining == "0":
            raise RuntimeError("GitHub API rate limit reached. Try again later or add GITHUB_TOKEN.")
        return resp

    def fetch_and_analyze_user(
        self, username: str, *, cached_etag: str | None = None
    ) -> tuple[list[AnalyzedRepo], list[DetectedGitHubSkill], str | None, bool]:
        """
        Fetch public repos for `username` and compute evidence scores + skills.
        Returns (repos, detected_skills, new_etag, not_modified).
        """
        with httpx.Client(timeout=12.0) as client:
            repos_resp = self._get(
                client,
                f"/users/{username}/repos?type=owner&sort=pushed&per_page=12",
                etag=cached_etag,
            )
            if repos_resp.status_code == 304:
                return [], [], cached_etag, True
            if repos_resp.status_code == 404:
                raise ValueError(f"GitHub user '{username}' was not found.")
            if repos_resp.status_code != 200:
                raise RuntimeError(f"GitHub API returned status {repos_resp.status_code}.")

            new_etag = repos_resp.headers.get("ETag")
            raw_repos = repos_resp.json()
            analyzed: list[AnalyzedRepo] = []

            for raw in raw_repos[:10]:
                if raw.get("archived"):
                    continue
                analyzed.append(self._analyze_single_repo(client, username, raw))

        detected_skills = aggregate_github_skills(analyzed)
        return analyzed, detected_skills, new_etag, False

    def _analyze_single_repo(
        self, client: httpx.Client, username: str, raw: dict[str, Any]
    ) -> AnalyzedRepo:
        repo_name = str(raw.get("name", ""))
        full_name = str(raw.get("full_name", f"{username}/{repo_name}"))
        default_branch = str(raw.get("default_branch", "main"))
        is_fork = bool(raw.get("fork", False))
        is_empty = int(raw.get("size", 0)) == 0

        # Languages
        languages: dict[str, int] = {}
        lang_resp = self._get(client, f"/repos/{full_name}/languages")
        if lang_resp.status_code == 200 and isinstance(lang_resp.json(), dict):
            languages = lang_resp.json()

        # Tree (bounded file structure inspection)
        key_files: list[str] = []
        has_tests = False
        has_ci = False
        has_dockerfile = False
        file_count = 0
        if not is_empty:
            tree_resp = self._get(
                client, f"/repos/{full_name}/git/trees/{default_branch}?recursive=1"
            )
            if tree_resp.status_code == 200:
                tree_items = tree_resp.json().get("tree") or []
                blobs = [item["path"] for item in tree_items if item.get("type") == "blob"]
                file_count = len(blobs)
                key_files = blobs[:30]
                lower_paths = [p.lower() for p in blobs]
                has_tests = any(
                    "test" in p or p.startswith("tests/") or "__tests__" in p
                    for p in lower_paths
                )
                has_ci = any(p.startswith(".github/workflows/") for p in lower_paths)
                has_dockerfile = any(
                    p.endswith("dockerfile") or p.endswith("docker-compose.yml")
                    for p in lower_paths
                )

        # README (bounded excerpt)
        readme_text = ""
        readme_resp = self._get(client, f"/repos/{full_name}/readme")
        if readme_resp.status_code == 200:
            content_b64 = readme_resp.json().get("content", "")
            try:
                readme_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                readme_text = ""

        # Manifest dependencies (bounded)
        dependencies: list[str] = []
        for manifest in MANIFEST_FILES:
            matching_paths = [p for p in key_files if p.lower().endswith(manifest)]
            for path in matching_paths[:2]:
                c_resp = self._get(client, f"/repos/{full_name}/contents/{path}")
                if c_resp.status_code == 200 and c_resp.json().get("content"):
                    try:
                        text = base64.b64decode(c_resp.json()["content"]).decode(
                            "utf-8", errors="replace"
                        )
                        dependencies.extend(parse_dependencies_from_manifest(path, text))
                    except Exception:
                        pass

        # Commit counts (author share)
        total_commits = 0
        user_commits = 0
        commits_resp = self._get(client, f"/repos/{full_name}/commits?per_page=30")
        if commits_resp.status_code == 200 and isinstance(commits_resp.json(), list):
            commits = commits_resp.json()
            total_commits = len(commits)
            for c in commits:
                author = (c.get("author") or {}).get("login", "")
                if author.lower() == username.lower():
                    user_commits += 1
            if user_commits == 0 and not is_fork:
                # Owner commits authored under local git email not linked to login
                user_commits = total_commits

        signals = RepoEvidenceSignals(
            has_tests=has_tests,
            has_ci=has_ci,
            has_dockerfile=has_dockerfile,
            has_readme=bool(readme_text.strip()),
            readme_length=len(readme_text),
            file_count=file_count,
            total_commits=total_commits,
            user_commits=user_commits,
            user_commit_share=round(user_commits / max(1, total_commits), 2),
            is_fork=is_fork,
            is_empty=is_empty or file_count == 0,
        )

        primary_language = str(raw.get("language") or (next(iter(languages.keys()), "")))
        topics = [str(t) for t in (raw.get("topics") or [])]
        inferred = infer_repo_skills(languages, topics, dependencies, signals)
        q_score, q_notes, excluded = score_repo_evidence(
            name=repo_name,
            description=str(raw.get("description") or ""),
            readme_text=readme_text,
            signals=signals,
        )

        return AnalyzedRepo(
            name=repo_name,
            full_name=full_name,
            html_url=str(raw.get("html_url", f"https://github.com/{full_name}")),
            description=str(raw.get("description") or ""),
            primary_language=primary_language,
            languages=languages,
            topics=topics,
            dependencies=list(dict.fromkeys(dependencies))[:30],
            inferred_skills=inferred,
            key_files=key_files[:20],
            readme_excerpt=readme_text[:2000],
            stars=int(raw.get("stargazers_count", 0)),
            pushed_at=raw.get("pushed_at"),
            signals=signals,
            quality_score=q_score,
            quality_notes=q_notes,
            excluded_reason=excluded,
        )


def analyze_github_profile(
    profile_url_or_username: str,
    *,
    other_links: list[str],
    db: Session,
    user: User,
) -> GitHubAnalysisResponse:
    # Store optional other evidence links in MasterProfile (plain links, never analyzed)
    if other_links:
        profile = db.query(MasterProfile).filter(MasterProfile.user_id == user.id).first()
        if profile:
            existing_urls = {str(item.get("url", "")).lower() for item in (profile.links or [])}
            updated_links = list(profile.links or [])
            for link in other_links:
                cleaned = link.strip()
                if cleaned and cleaned.lower() not in existing_urls:
                    label = "Kaggle" if "kaggle.com" in cleaned.lower() else "Portfolio"
                    updated_links.append({"label": label, "url": cleaned})
            profile.links = updated_links
            db.add(profile)
            db.commit()

    username = extract_github_username(profile_url_or_username)
    if not username:
        return GitHubAnalysisResponse(
            skipped=True,
            other_links=other_links,
        )

    # Check 6-hour cache in DB
    existing = (
        db.query(GitHubAnalysis)
        .filter(
            GitHubAnalysis.user_id == user.id,
            GitHubAnalysis.github_username == username,
        )
        .order_by(GitHubAnalysis.fetched_at.desc())
        .first()
    )
    now = datetime.now(timezone.utc)
    if existing and existing.fetched_at:
        fetched = (
            existing.fetched_at
            if existing.fetched_at.tzinfo
            else existing.fetched_at.replace(tzinfo=timezone.utc)
        )
        if fetched > now - timedelta(hours=6):
            return GitHubAnalysisResponse(
                id=existing.id,
                skipped=False,
                github_username=existing.github_username,
                profile_url=existing.profile_url,
                repos=[AnalyzedRepo.model_validate(r) for r in existing.repos_data],
                detected_skills=[
                    DetectedGitHubSkill.model_validate(s) for s in existing.detected_skills
                ],
                other_links=other_links,
                fetched_at=existing.fetched_at,
            )

    gh_client = GitHubClient()
    repos, detected_skills, new_etag, not_modified = gh_client.fetch_and_analyze_user(
        username, cached_etag=existing.etag if existing else None
    )
    if not_modified and existing is not None:
        existing.fetched_at = now
        db.add(existing)
        db.commit()
        return GitHubAnalysisResponse(
            id=existing.id,
            skipped=False,
            github_username=existing.github_username,
            profile_url=existing.profile_url,
            repos=[AnalyzedRepo.model_validate(r) for r in existing.repos_data],
            detected_skills=[
                DetectedGitHubSkill.model_validate(s) for s in existing.detected_skills
            ],
            other_links=other_links,
            fetched_at=existing.fetched_at,
        )

    row = GitHubAnalysis(
        user_id=user.id,
        github_username=username,
        profile_url=f"https://github.com/{username}",
        repos_data=[r.model_dump(mode="json") for r in repos],
        detected_skills=[s.model_dump(mode="json") for s in detected_skills],
        etag=new_etag,
        fetched_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Analyzed GitHub profile username=%s repos=%d", username, len(repos))

    return GitHubAnalysisResponse(
        id=row.id,
        skipped=False,
        github_username=row.github_username,
        profile_url=row.profile_url,
        repos=repos,
        detected_skills=detected_skills,
        other_links=other_links,
        fetched_at=row.fetched_at,
    )


def generate_repo_bullets(
    repo: AnalyzedRepo, *, db: Session, user: User
) -> list[str]:
    """Generate 2-3 honest resume bullets strictly from bounded repo content."""
    fallback = build_factual_repo_bullets(repo)
    try:
        llm = get_llm_client_for_user(db, user)
    except Exception:
        return fallback

    prompt = (
        "Write 2 concise, strong resume bullet points for this GitHub project using ONLY the provided facts. "
        "Do NOT invent any metrics, user counts, latency numbers, or tools not listed below.\n\n"
        f"Project: {repo.name}\n"
        f"Description: {repo.description}\n"
        f"Inferred skills/dependencies: {', '.join(repo.inferred_skills)}\n"
        f"Key files: {', '.join(repo.key_files[:12])}\n"
        f"Has tests: {repo.signals.has_tests}, Has CI: {repo.signals.has_ci}, Has Docker: {repo.signals.has_dockerfile}\n"
        f"README excerpt:\n{repo.readme_excerpt[:1200]}"
    )
    try:
        res = llm.generate_structured(
            prompt=prompt,
            schema=RepoBulletDraftSchema,
            operation="github_project_draft",
            tier="fast",
            redact_pii=False,
        )
        cleaned = [b.strip() for b in res.bullets if b.strip()]
        return cleaned[:3] if cleaned else fallback
    except Exception:
        return fallback
