import json
import re
from collections import defaultdict
from typing import Any

from app.schemas.github import (
    AnalyzedRepo,
    BulletEvidenceItem,
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

        draft_bullets, bullet_evidence = build_factual_repo_bullets_with_evidence(repo)
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
                bullet_evidence=bullet_evidence,
            )
        )

    recs.sort(key=lambda r: (-r.relevance_score, -r.quality_score))
    for idx, r in enumerate(recs):
        if idx >= 1:
            r.omitted_for_space = True
            r.omission_reason = (
                f"Omitted due to page-fit budget (1-page ceiling). "
                f"Available as an alternative project option in page-fit selector (relevance: {r.relevance_score:.0f}/100)."
            )
    return recs


_NOISY_LANG_TOKENS = {
    "powershell", "batchfile", "makefile", "shell", "dockerfile", "html", "css", "scss",
}
_TRIVIAL_COMMIT_RE = re.compile(
    r"^(?:initial commit|init|update(?: readme)?|wip|fix(?: typo)?|merge (?:branch|pull request)|cleanup|minor)\b",
    re.IGNORECASE,
)
_README_BOILERPLATE_RE = re.compile(
    r"(?:npm install|pip install|uvicorn|docker-compose|git clone|mit license|apache license|table of contents|getting started|prerequisites|environment variables)",
    re.IGNORECASE,
)


def _clean_readme_sentences(readme_excerpt: str, repo_name: str) -> list[str]:
    """Extract clean, human-readable project summary sentences from a README excerpt."""
    if not readme_excerpt.strip():
        return []
    text = re.sub(r"```[\s\S]*?```", " ", readme_excerpt)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    out: list[str] = []
    repo_clean = repo_name.lower().replace("-", " ").replace("_", " ")
    for raw_line in text.splitlines():
        line = re.sub(r"^[\s#>*\-•]+", "", raw_line).strip()
        line = line.replace("`", "")
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 24 or len(line) > 200:
            continue
        if line.lower().replace("-", " ").replace("_", " ") == repo_clean:
            continue
        if _README_BOILERPLATE_RE.search(line) or "http://" in line or "https://" in line:
            continue
        # Strip trailing colon headings
        if line.endswith(":"):
            continue
        out.append(line.rstrip("."))
    return out


_FRONTEND_HINTS = {
    "react": "React",
    "next": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "vue": "Vue.js",
    "angular": "Angular",
    "svelte": "Svelte",
    "tailwindcss": "Tailwind CSS",
    "tailwind": "Tailwind CSS",
    "vite": "React/Vite",
}


def _detect_frontend_label(repo: AnalyzedRepo) -> str | None:
    """Identify the repo's frontend framework/UI layer if code/dependencies show one."""
    deps_lower = [d.lower().strip() for d in repo.dependencies]
    skills_lower = [s.lower().strip() for s in repo.inferred_skills]
    for k, label in _FRONTEND_HINTS.items():
        if k in deps_lower or k in skills_lower:
            return label
    has_ui_files = any(
        f.lower().endswith((".tsx", ".jsx", ".html", ".css", ".vue", ".svelte"))
        or f.lower().startswith(("frontend/", "client/", "web/", "app/src/"))
        for f in repo.key_files
    )
    if has_ui_files or ("HTML" in repo.languages and "CSS" in repo.languages):
        if "TypeScript" in repo.languages:
            return "TypeScript UI"
        return "HTML/CSS frontend"
    return None


def _select_stack_highlights(repo: AnalyzedRepo) -> list[str]:
    """Select concise key technologies (frontend + backend/AI frameworks), avoiding noisy file-type dumps."""
    preferred: list[str] = []
    seen_lower: set[str] = set()
    # Detect frameworks/AI tools strictly from verified dependencies, inferred code/import skills,
    # and repo metadata — never substring-match prose like "node canvas" as "Node.js".
    dep_tokens = [str(d).lower().strip() for d in (repo.dependencies or []) if str(d).strip()]
    dep_set = set(dep_tokens)
    skill_set = {str(s).lower().strip() for s in (repo.inferred_skills or []) if str(s).strip()}

    def _has_dep(*names: str) -> bool:
        for n in names:
            if n in dep_set or any(d.startswith(n + "-") or d.endswith("/" + n) for d in dep_tokens):
                return True
        return False

    evidence_checks: tuple[tuple[str, bool], ...] = (
        ("React", _has_dep("react", "reactflow", "xyflow") or "react" in skill_set or "react.js" in skill_set),
        ("Next.js", _has_dep("next", "nextjs") or "next.js" in skill_set or "nextjs" in skill_set),
        ("FastAPI", _has_dep("fastapi", "uvicorn") or "fastapi" in skill_set),
        ("LangGraph", _has_dep("langgraph") or "langgraph" in skill_set),
        (
            "Gemini",
            _has_dep("google-generativeai", "google-genai", "generative-ai", "gemini")
            or "gemini" in skill_set,
        ),
        (
            "Node.js",
            _has_dep("express", "fastify", "nestjs", "koa", "hono", "nodejs")
            or "node.js" in skill_set
            or "nodejs" in skill_set
            or "node" in skill_set,
        ),
        ("Express.js", _has_dep("express") or "express" in skill_set or "express.js" in skill_set),
    )
    for label, matched in evidence_checks:
        if matched and label.lower() not in seen_lower:
            preferred.append(label)
            seen_lower.add(label.lower())
    # Then prioritized mapped dependencies (excluding noise like uvicorn/pytest)
    for dep in repo.dependencies:
        mapped = DEPENDENCY_SKILL_MAP.get(dep.lower().strip())
        cand = mapped or (dep.strip() if len(dep.strip()) <= 20 else "")
        if cand and cand.lower() not in seen_lower and cand.lower() not in {"pytest", "uvicorn"}:
            preferred.append(cand)
            seen_lower.add(cand.lower())
    # Then inferred skills excluding shell/markup noise
    for sk in repo.inferred_skills:
        if sk.lower() in _NOISY_LANG_TOKENS or sk.lower() in seen_lower:
            continue
        preferred.append(sk)
        seen_lower.add(sk.lower())
    if not preferred and repo.primary_language:
        preferred.append(repo.primary_language)
    elif not preferred:
        for sk in repo.inferred_skills[:3]:
            if sk.lower() not in seen_lower:
                preferred.append(sk)
                seen_lower.add(sk.lower())
    return preferred[:5]


def _extract_test_coverage_bullet(repo: AnalyzedRepo) -> tuple[str, str] | None:
    """Return a concrete test coverage bullet naming real test files/modules, or None."""
    if not repo.signals.has_tests:
        return None
    test_paths = [
        p
        for p in (repo.test_files or repo.key_files)
        if "test" in p.lower() or ".spec." in p.lower()
    ]
    # Keep distinct code test files (skip cache/snapshot junk)
    clean_paths = [
        p
        for p in dict.fromkeys(test_paths)
        if not any(skip in p.lower() for skip in ("__pycache__", "node_modules", ".snap"))
    ]
    if not clean_paths:
        return None

    topics: list[str] = []
    for path in clean_paths:
        stem = path.rsplit("/", 1)[-1].split(".")[0]
        stem = re.sub(r"^(?:test_|spec_)", "", stem, flags=re.IGNORECASE)
        stem = re.sub(r"(?:_test|\.test|_spec|\.spec)$", "", stem, flags=re.IGNORECASE)
        topic = stem.replace("_", " ").replace("-", " ").strip()
        if topic and topic.lower() not in {"all", "app", "main", "index", "unit", "integration", "tests", "test"}:
            if topic.lower() not in {t.lower() for t in topics}:
                topics.append(topic)

    cited = ", ".join(clean_paths[:2])
    if topics:
        topic_str = ", ".join(topics[:3])
        return (
            f"Wrote automated tests covering {topic_str} ({cited}).",
            f"Test files: {cited}",
        )
    return (
        f"Implemented automated tests verified in {cited}.",
        f"Test files: {cited}",
    )


def _format_commit_contribution(repo: AnalyzedRepo, stack: list[str]) -> tuple[str, str] | None:
    """Turn non-trivial commit messages into one concrete engineering contribution bullet."""
    highlights: list[str] = []
    for msg in repo.commit_messages:
        clean = re.sub(r"^(?:feat|fix|refactor|perf|chore|docs)(?:\([^)]*\))?:\s*", "", msg.strip(), flags=re.I)
        clean = re.sub(r"\s+", " ", clean).strip().rstrip(".")
        if len(clean) < 15 or _TRIVIAL_COMMIT_RE.match(clean):
            continue
        if clean.lower() not in {h.lower() for h in highlights}:
            highlights.append(clean)
        if len(highlights) >= 2:
            break
    if not highlights:
        return None
    if len(highlights) == 1:
        summary = highlights[0]
        summary = summary[0].lower() + summary[1:] if len(summary) > 1 and summary[1].islower() else summary
        bullet = f"Implemented repository enhancements including {summary}."
    else:
        bullet = f"Engineered key changes including {highlights[0]} and {highlights[1]}."
    return bullet, f"Recent commits ({len(highlights)} highlighted)"


def build_factual_repo_bullets_with_evidence(
    repo: AnalyzedRepo,
) -> tuple[list[str], list[BulletEvidenceItem]]:
    """
    Generate honest, resume-style project bullets strictly grounded in README, code/dependencies,
    commit history, and real test files (never emitting file counts, bare language dumps, or generic test claims).
    """
    bullets: list[str] = []
    evidence: list[BulletEvidenceItem] = []

    stack = _select_stack_highlights(repo)
    frontend_label = _detect_frontend_label(repo)
    if frontend_label and frontend_label.lower() not in {s.lower() for s in stack}:
        stack.insert(0, frontend_label)
    stack_phrase = " and ".join(stack[:3]) if len(stack[:3]) <= 2 else ", ".join(stack[:2]) + f", and {stack[2]}"
    readme_sentences = _clean_readme_sentences(repo.readme_excerpt, repo.name)
    desc = re.sub(r"\s+", " ", (repo.description or "").strip()).rstrip(".")

    def _fit_complete_bullet(text: str, max_len: int = 178) -> str:
        t = re.sub(r"\s+", " ", text).strip()
        if not t.endswith((".", "!", "?")):
            t += "."
        if len(t) <= max_len:
            return t
        # Preserve trailing "Uses ..." / "Built with ..." stack sentence when shortening the first sentence
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
        if len(sents) >= 2:
            head_s = sents[0]
            tail_s = " ".join(sents[1:])
            budget_head = max(48, max_len - len(tail_s) - 1)
            for sep in ("; ", " connecting ", " featuring ", " powered by ", ", "):
                parts = head_s[:-1].split(sep)
                if len(parts) > 1:
                    cand_h = parts[0].rstrip(" .,;:-") + "."
                    if len(cand_h) <= budget_head and len(cand_h.split()) >= 5:
                        return f"{cand_h} {tail_s}"
        for sep in ("; ", " connecting ", " featuring ", " including ", ", "):
            parts = t[:-1].split(sep)
            if len(parts) > 1:
                for k in range(len(parts) - 1, 0, -1):
                    cand = sep.join(parts[:k]).rstrip(" .,;:-") + "."
                    if len(cand) <= max_len and len(cand.split()) >= 6:
                        return cand
        return t

    def _with_indefinite_article(phrase: str) -> str:
        p = phrase.strip()
        if not p or re.match(r"^(?:a|an|the)\b", p, re.I):
            return p
        # Use 'an' before vowel sounds / AI-ML acronyms, 'a' otherwise
        first = p.split()[0].strip(",.-").lower()
        if first.startswith(("ai", "ml", "api", "html", "sql", "nlp", "http", "a", "e", "i", "o", "u")):
            return f"an {p}"
        return f"a {p}"

    def _join_clean_stack(items: list[str]) -> str:
        clean = [s.strip() for s in items if s.strip()]
        if not clean:
            return ""
        if len(clean) == 1:
            return clean[0]
        if len(clean) == 2:
            return f"{clean[0]} and {clean[1]}"
        return f"{', '.join(clean[:-1])}, and {clean[-1]}"

    # Bullet 1: What the project does + core stack + frontend when present (grammatical sentences, no 'A / B' slashes)
    raw_summary = desc or (readme_sentences[0] if readme_sentences else "")
    summary_source = "Repository description + dependencies" if desc else "README + dependencies"
    if raw_summary:
        # Strip marketing hype / unproven buzzwords not backed by benchmark evidence
        summary_text = re.sub(
            r"\b(?:high-throughput|ultra-fast|blazing(?:ly)?\s+fast|scalable|industry-leading|world-class)\s+",
            "",
            raw_summary,
            flags=re.IGNORECASE,
        ).strip()
        summary_text = re.sub(r"\s+", " ", summary_text).strip()

        # Fix repetition: "Built X, a ... built with Y" -> "Built X, a ... using Y"
        if re.search(r"\bbuilt with\b", summary_text, re.I):
            summary_text = re.sub(r"\bbuilt with\b", "using", summary_text, count=1, flags=re.I)

        normalized_summary = summary_text
        if normalized_summary and not normalized_summary.startswith((repo.name, "API", "AI", "ML", "UI")):
            if len(normalized_summary) > 1 and normalized_summary[0].isupper() and normalized_summary[1].islower():
                normalized_summary = normalized_summary[0].lower() + normalized_summary[1:]
        grammatical_summary = _with_indefinite_article(normalized_summary)

        # Strict evidence check: never claim Redis or Docker unless present in repo dependencies, languages, or inferred skills
        verified_tech_names = {
            *(s.lower() for s in (repo.dependencies or [])),
            *(s.lower() for s in (repo.inferred_skills or [])),
            *(s.lower() for s in (repo.languages or {}).keys()),
        }
        filtered_stack = [
            s for s in stack
            if s.lower() in verified_tech_names
            or any(v in s.lower() or s.lower() in v for v in verified_tech_names)
        ]
        missing_stack = [s for s in filtered_stack[:4] if s.lower() not in summary_text.lower()]
        if frontend_label and frontend_label.lower() in {m.lower() for m in missing_stack}:
            other_stack = [m for m in missing_stack if m.lower() != frontend_label.lower()][:3]
            if other_stack:
                tech_sent = f"Uses {_with_indefinite_article(frontend_label)} frontend with {_join_clean_stack(other_stack)}."
            else:
                tech_sent = f"Uses {_with_indefinite_article(frontend_label)} frontend."
            b1 = _fit_complete_bullet(f"Built {repo.name}, {grammatical_summary}. {tech_sent}")
        elif missing_stack:
            b1 = _fit_complete_bullet(f"Built {repo.name}, {grammatical_summary}. Uses {_join_clean_stack(missing_stack[:4])}.")
        else:
            b1 = _fit_complete_bullet(f"Built {repo.name}, {grammatical_summary}.")
        bullets.append(b1)
        tech_candidates: list[str] = []
        seen_tc: set[str] = set()
        for t in [*stack, *(repo.inferred_skills or []), *list((repo.languages or {}).keys())]:
            ts = str(t).strip()
            if ts and ts.lower() not in seen_tc:
                seen_tc.add(ts.lower())
                tech_candidates.append(ts)
        listed_techs = [
            t
            for t in tech_candidates
            if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", b1, re.IGNORECASE)
        ]
        # Include stack highlights if fewer than 2 matched directly inside prose
        for t in stack:
            if t not in listed_techs:
                listed_techs.append(t)

        dep_list = [str(d).strip() for d in (repo.dependencies or []) if str(d).strip()]
        lang_map = {str(k).lower(): (str(k), int(v)) for k, v in (repo.languages or {}).items()}
        key_files_list = [str(f).strip() for f in (repo.key_files or []) if str(f).strip()]
        per_tech_ev: list[str] = []
        _dep_aliases = {
            "react": ("react", "react-dom", "reactflow", "@xyflow/react"),
            "next.js": ("next", "nextjs"),
            "fastapi": ("fastapi", "uvicorn"),
            "langgraph": ("langgraph", "@langchain/langgraph"),
            "gemini": ("google-generativeai", "@google/generative-ai", "google-genai", "gemini"),
            "node.js": ("express", "fastify", "nestjs", "@nestjs/core", "nodejs"),
            "express.js": ("express",),
            "pytorch": ("torch", "pytorch"),
            "tailwind css": ("tailwindcss",),
        }
        for tech in listed_techs:
            tl = tech.lower()
            parts: list[str] = []
            aliases = _dep_aliases.get(tl, (tl,))
            matched_deps = [
                d for d in dep_list if d.lower() in aliases or any(al in d.lower() for al in aliases)
            ]
            if matched_deps:
                parts.append(f"dependencies ({', '.join(matched_deps[:3])})")
            if tl in lang_map:
                orig_k, bcount = lang_map[tl]
                parts.append(f"language stats ({orig_k}: {bcount:,} B)")
            elif tl in {"fastapi", "langgraph"} and "python" in lang_map and not matched_deps:
                parts.append("Python repository code")
            elif tl in {"react", "next.js"} and any(k in lang_map for k in ("javascript", "typescript")) and not matched_deps:
                parts.append("JS/TS repository code")
            if not parts and key_files_list:
                parts.append(f"repo files ({', '.join(key_files_list[:2])})")
            if not parts and any(s.lower() == tl for s in (repo.inferred_skills or [])):
                parts.append("inferred from repository imports/code")
            per_tech_ev.append(f"{tech}: {', '.join(parts) if parts else summary_source}")

        detail_str = f"{summary_source} | Tech evidence — {'; '.join(per_tech_ev)}" if per_tech_ev else f"{summary_source} ({repo.primary_language or 'repository'})"
        evidence.append(
            BulletEvidenceItem(
                bullet=b1,
                source=summary_source,
                detail=detail_str,
            )
        )
    else:
        modules = [f.split("/")[0] for f in repo.key_files if "/" in f]
        top_modules = list(dict.fromkeys(m for m in modules if not m.startswith(".")))[:2]
        mod_note = f" across {' and '.join(top_modules)} modules" if top_modules else ""
        fe_note = f" with a {frontend_label} frontend" if frontend_label else ""
        b1 = _fit_complete_bullet(
            f"Developed {repo.name} using {stack_phrase or 'modular software architecture'}{fe_note}{mod_note}."
        )
        bullets.append(b1)
        evidence.append(
            BulletEvidenceItem(
                bullet=b1,
                source="Code structure + dependencies",
                detail=f"Inferred from {', '.join(repo.key_files[:3]) or repo.full_name}",
            )
        )

    # Bullet 2: User contribution from second README detail, real commit history, or Docker/CI/tests
    second_readme = ""
    for sent in readme_sentences:
        if sent.lower() != summary_text.lower():
            second_readme = sent
            break

    commit_pair = _format_commit_contribution(repo, stack)
    test_pair = _extract_test_coverage_bullet(repo)

    if commit_pair:
        b2, detail2 = commit_pair
        bullets.append(b2)
        evidence.append(BulletEvidenceItem(bullet=b2, source="Commit history", detail=detail2))
    elif second_readme:
        b2 = f"{second_readme}."
        bullets.append(b2)
        evidence.append(
            BulletEvidenceItem(bullet=b2, source="README", detail=f"README feature documentation ({repo.full_name})")
        )
    elif repo.signals.has_dockerfile and repo.signals.has_ci:
        b2 = "Configured Docker containerization and GitHub Actions CI workflows for reproducible builds."
        bullets.append(b2)
        evidence.append(
            BulletEvidenceItem(
                bullet=b2,
                source="Repository config",
                detail="Dockerfile & .github/workflows present in repo tree",
            )
        )
    elif repo.signals.has_dockerfile:
        b2 = "Containerized the application with Docker for consistent local and deployment runs."
        bullets.append(b2)
        evidence.append(
            BulletEvidenceItem(bullet=b2, source="Repository config", detail="Dockerfile present in repo tree")
        )

    if test_pair and len(bullets) < 3:
        bt, dt = test_pair
        if bt not in bullets:
            bullets.append(bt)
            evidence.append(BulletEvidenceItem(bullet=bt, source="Automated tests", detail=dt))

    return bullets, evidence


def build_factual_repo_bullets(repo: AnalyzedRepo) -> list[str]:
    """Generate factual, evidence-only starter bullets directly from repo README, code, commits, and tests."""
    bullets, _ = build_factual_repo_bullets_with_evidence(repo)
    return bullets
