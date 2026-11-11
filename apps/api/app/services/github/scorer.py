import json
import re
from collections import defaultdict
from typing import Any

from app.schemas.github import (
    AnalyzedRepo,
    DetectedGitHubSkill,
    RepoEvidenceSignals,
    RepoRecommendation,
)

DEPENDENCY_SKILL_MAP: dict[str, str] = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "sqlalchemy": "SQLAlchemy",
    "alembic": "Alembic",
    "pydantic": "Pydantic",
    "psycopg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "asyncpg": "PostgreSQL",
    "redis": "Redis",
    "celery": "Celery",
    "rq": "RQ",
    "pytest": "pytest",
    "playwright": "Playwright",
    "httpx": "httpx",
    "torch": "PyTorch",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "scikit-learn": "scikit-learn",
    "pandas": "pandas",
    "numpy": "NumPy",
    "react": "React",
    "next": "Next.js",
    "typescript": "TypeScript",
    "tailwindcss": "Tailwind CSS",
    "express": "Express",
    "prisma": "Prisma",
    "zod": "Zod",
    "vitest": "Vitest",
    "jest": "Jest",
    "spring-boot": "Spring Boot",
    "gin-gonic": "Go",
    "docker": "Docker",
}

TUTORIAL_KEYWORDS = (
    "tutorial",
    "todo-app",
    "hello-world",
    "boilerplate",
    "starter-template",
    "udemy",
    "coursera",
    "freecodecamp",
    "clone",
    "practice-repo",
)


def extract_github_username(profile_url_or_username: str) -> str:
    raw = (profile_url_or_username or "").strip().rstrip("/")
    if not raw:
        return ""
    if "github.com/" in raw.lower():
        part = raw.split("github.com/", 1)[1].split("/")[0].split("?")[0]
        return part.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}", raw):
        return raw
    return ""


def parse_dependencies_from_manifest(filename: str, content: str) -> list[str]:
    lower_name = filename.lower()
    deps: list[str] = []
    if lower_name.endswith("requirements.txt"):
        for line in content.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            pkg = re.split(r"[=<>!~\[;\s]", clean, maxsplit=1)[0].strip().lower()
            if pkg:
                deps.append(pkg)
    elif lower_name.endswith("package.json"):
        try:
            data = json.loads(content)
            for key in ("dependencies", "devDependencies"):
                if isinstance(data.get(key), dict):
                    deps.extend(str(k).lower() for k in data[key].keys())
        except Exception:
            pass
    elif lower_name.endswith("pyproject.toml"):
        for m in re.finditer(r'["\']([a-zA-Z0-9_.-]+)(?:[=<>!~\[].*)?["\']', content):
            deps.append(m.group(1).lower())
    elif lower_name.endswith("pom.xml"):
        for m in re.finditer(r"<artifactId>([^<]+)</artifactId>", content):
            deps.append(m.group(1).strip().lower())
    elif lower_name.endswith("go.mod"):
        for line in content.splitlines():
            parts = line.strip().split()
            if parts and "/" in parts[0] and not parts[0].startswith("module"):
                deps.append(parts[0].lower())
    return list(dict.fromkeys(deps))[:50]


def infer_repo_skills(
    languages: dict[str, int],
    topics: list[str],
    dependencies: list[str],
    signals: RepoEvidenceSignals,
) -> list[str]:
    skills: list[str] = []
    seen: set[str] = set()

    def add(skill: str) -> None:
        if skill and skill.lower() not in seen:
            seen.add(skill.lower())
            skills.append(skill)

    for lang in languages.keys():
        add(lang)
    for dep in dependencies:
        for key, canonical in DEPENDENCY_SKILL_MAP.items():
            if key in dep:
                add(canonical)
    for topic in topics:
        if len(topic) <= 30:
            add(topic.replace("-", " ").title() if "-" in topic else topic)
    if signals.has_dockerfile:
        add("Docker")
    if signals.has_ci:
        add("CI/CD")
    return skills


def score_repo_evidence(
    *,
    name: str,
    description: str,
    readme_text: str,
    signals: RepoEvidenceSignals,
) -> tuple[float, list[str], str | None]:
    """
    Score repository authenticity and engineering evidence (0 to 100).
    Heavily down-weights empty repos, untouched forks, and tutorial clones.
    """
    notes: list[str] = []
    excluded_reason: str | None = None

    if signals.is_empty or signals.file_count <= 2:
        return 5.0, ["Repository is empty or has almost no source files"], "Empty or minimal repository"

    if signals.is_fork and signals.user_commits <= 2:
        return 10.0, ["Fork with minimal personal commits"], "Fork with no substantial personal contributions"

    lower_blob = f"{name} {description} {readme_text[:600]}".lower()
    is_tutorial = any(k in lower_blob for k in TUTORIAL_KEYWORDS) and signals.total_commits <= 5
    if is_tutorial:
        signals.is_tutorial_like = True

    score = 20.0

    # Personal commit depth
    if signals.user_commits >= 15:
        score += 25.0
        notes.append(f"{signals.user_commits} personal commits")
    elif signals.user_commits >= 5:
        score += 15.0
        notes.append(f"{signals.user_commits} personal commits")
    elif signals.user_commits >= 2:
        score += 6.0

    if signals.user_commit_share >= 0.6:
        score += 10.0

    # Architecture & file count
    if signals.file_count >= 12:
        score += 15.0
        notes.append(f"Multi-file architecture ({signals.file_count} files)")
    elif signals.file_count >= 6:
        score += 8.0

    # Tests
    if signals.has_tests:
        score += 15.0
        notes.append("Automated tests present")

    # CI / Deployment
    if signals.has_ci or signals.has_dockerfile:
        score += 10.0
        notes.append("CI/Docker configuration present")

    # Documentation
    if signals.has_readme and signals.readme_length >= 300:
        score += 10.0
        notes.append("Detailed README documentation")
    elif signals.has_readme:
        score += 4.0

    if signals.is_tutorial_like:
        score = min(score, 28.0)
        notes.append("Down-weighted: resembles a tutorial or starter clone")
        excluded_reason = "Tutorial or starter clone with low original commit history"

    if signals.is_fork and signals.user_commits > 2:
        score *= 0.75
        notes.append("Fork with personal modifications")

    score = round(max(0.0, min(100.0, score)), 1)
    if score < 30.0 and not excluded_reason:
        excluded_reason = "Low engineering evidence score"
    return score, notes, excluded_reason


def aggregate_github_skills(repos: list[AnalyzedRepo]) -> list[DetectedGitHubSkill]:
    active_repos = [r for r in repos if r.excluded_reason is None]
    skill_repos: dict[str, list[str]] = defaultdict(list)
    main_lang_counts: dict[str, int] = defaultdict(int)
    canonical_names: dict[str, str] = {}

    for repo in active_repos:
        if repo.primary_language:
            main_lang_counts[repo.primary_language.lower()] += 1
        for skill in repo.inferred_skills:
            key = skill.lower()
            canonical_names.setdefault(key, skill)
            if repo.name not in skill_repos[key]:
                skill_repos[key].append(repo.name)

    results: list[DetectedGitHubSkill] = []
    for key, repo_names in skill_repos.items():
        r_count = len(repo_names)
        m_count = main_lang_counts.get(key, 0)
        parts = [f"used in {r_count} {'repo' if r_count == 1 else 'repos'}"]
        if m_count:
            parts.append(f"main language in {m_count}")
        results.append(
            DetectedGitHubSkill(
                skill=canonical_names[key],
                repo_count=r_count,
                main_language_count=m_count,
                evidence_level=", ".join(parts),
                repos=repo_names,
            )
        )
    results.sort(key=lambda s: (-s.repo_count, -s.main_language_count, s.skill.lower()))
    return results


def rank_repos_for_job(
    repos: list[AnalyzedRepo],
    *,
    job_skills: list[str],
    job_keywords: list[str],
    resume_text: str,
) -> list[RepoRecommendation]:
    """Rank analyzed repos by relevance to target job + engineering evidence quality."""
    target_terms = {t.lower().strip() for t in (job_skills + job_keywords) if t.strip()}
    resume_lower = resume_text.lower()
    recs: list[RepoRecommendation] = []

    for repo in repos:
        if repo.excluded_reason is not None:
            continue
        repo_skills_lower = {s.lower(): s for s in repo.inferred_skills}
        matched = [
            repo_skills_lower[t]
            for t in target_terms
            if t in repo_skills_lower
        ]
        # Also check partial mentions in README/description
        blob = f"{repo.description} {repo.readme_excerpt}".lower()
        for t in target_terms:
            if t not in {m.lower() for m in matched} and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", blob):
                matched.append(t.title() if len(t) > 3 else t.upper())

        skill_overlap = (len(matched) / max(1, min(len(target_terms), 6))) * 100.0
        relevance = round(min(100.0, 0.65 * skill_overlap + 0.35 * repo.quality_score), 1)
        already_in = repo.name.lower() in resume_lower or repo.html_url.lower() in resume_lower

        if matched:
            reason = (
                f"Matches required {', '.join(matched[:4])} "
                f"(quality score {repo.quality_score:.0f}/100; "
                f"{', '.join(repo.quality_notes[:2]) or 'verified repo code'})."
            )
        else:
            reason = (
                f"Demonstrates {repo.primary_language or 'software'} engineering "
                f"(quality score {repo.quality_score:.0f}/100)."
            )

        draft_bullets = build_factual_repo_bullets(repo)
        recs.append(
            RepoRecommendation(
                repo_name=repo.name,
                full_name=repo.full_name,
                html_url=repo.html_url,
                quality_score=repo.quality_score,
                relevance_score=relevance,
                matched_skills=matched,
                reason=reason,
                already_in_resume=already_in,
                draft_bullets=draft_bullets,
            )
        )

    recs.sort(key=lambda r: (-r.relevance_score, -r.quality_score))
    return recs


def build_factual_repo_bullets(repo: AnalyzedRepo) -> list[str]:
    """Generate factual, evidence-only starter bullets directly from repo metadata."""
    bullets: list[str] = []
    techs = ", ".join(repo.inferred_skills[:5]) or repo.primary_language or "software components"
    if repo.description:
        bullets.append(f"Built {repo.name} ({techs}): {repo.description.rstrip('.')}.")
    else:
        bullets.append(f"Developed {repo.name} using {techs} across {repo.signals.file_count} source files.")

    if repo.signals.has_tests and repo.signals.has_ci:
        bullets.append("Implemented automated tests and CI workflows to validate code changes.")
    elif repo.signals.has_tests:
        bullets.append("Added automated test coverage to verify core module behavior.")
    elif repo.signals.has_dockerfile:
        bullets.append("Configured Docker containerization for reproducible local and deployment environments.")
    return bullets
