import re
from dataclasses import dataclass, field
from typing import Any

from app.schemas.github import AnalyzedRepo
from app.schemas.resume import ParsedResumeStructure
from app.services.matching.normalizer import (
    find_direct_and_implied_skills,
    normalize_skill,
    skill_in_text,
)

KNOWN_TOOLS = [
    "Python", "TypeScript", "JavaScript", "Java", "Go", "Rust", "C", "C++", "C#", "SQL",
    "HTML", "HTML5", "CSS", "Tailwind CSS", "DOM", "AJAX", "HTTP", "SOA", "Web Services",
    "FastAPI", "Django", "Flask", "React", "Next.js", "Node.js", "Express", "MERN", "Spring Boot",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite", "Docker", "Kubernetes",
    "AWS", "GCP", "Azure", "Terraform", "Git", "GitHub", "GitHub Actions", "CI/CD",
    "REST", "REST APIs", "GraphQL", "PyTorch", "TensorFlow", "Playwright", "pytest", "Linux",
    "Kafka", "Spark", "Snowflake", "Airflow", "Celery", "RabbitMQ", "Elasticsearch",
    "Firebase", "ChromaDB", "Groq", "Linux",
]

BAD_GLYPH_RE = re.compile(r"[\uE000-\uF8FF\ufffd\u25af]")

NUMBER_METRIC_RE = re.compile(
    r"(?:\$?\d+(?:[.,]\d+)?(?:\s?[%kKmMbBxX+]|ms|s|hrs?|hours?|days?|weeks?|months?|years?)?)"
)

INFLATED_LEADERSHIP_VERBS: dict[str, str] = {
    "led": "implemented",
    "architected": "built",
    "managed": "contributed to",
    "directed": "developed",
    "spearheaded": "built",
    "orchestrated": "automated",
    "pioneered": "created",
}

INDIVIDUAL_EVIDENCE_VERBS = {
    "built", "implemented", "wrote", "developed", "created", "automated",
    "trained", "indexed", "configured", "tested", "contributed", "assisted",
}


@dataclass
class ValidationResult:
    status: str  # passed | flagged | blocked
    warnings: list[str] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)
    unverified_tools: list[str] = field(default_factory=list)
    inflated_verbs: list[str] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


def _normalize_number_token(tok: str) -> str:
    return re.sub(r"\s+", "", tok.strip().lower())


def build_evidence_corpus(
    *,
    resume: ParsedResumeStructure,
    raw_resume_text: str,
    github_repos: list[AnalyzedRepo] | None = None,
    confirmed_learning: list[dict[str, Any]] | None = None,
) -> tuple[str, set[str], set[str], list[dict[str, Any]]]:
    parts: list[str] = [
        raw_resume_text,
        resume.summary,
        " ".join(resume.skills),
        " ".join(resume.achievements),
        " ".join(resume.certifications),
    ]
    for exp in resume.experience:
        parts.extend([exp.company, exp.title, exp.dates, *exp.bullets])
    for proj in resume.projects:
        parts.extend([proj.name, *proj.tech_stack, *proj.bullets])
    for edu in resume.education:
        parts.extend([edu.institution, edu.degree, edu.dates, *edu.details])

    refs: list[dict[str, Any]] = []
    if raw_resume_text.strip():
        refs.append({"source": "resume", "detail": "Original resume text and Master Profile"})

    for repo in github_repos or []:
        if repo.excluded_reason is not None:
            continue
        parts.extend(
            [
                repo.name,
                repo.description,
                repo.primary_language,
                *repo.inferred_skills,
                *repo.dependencies,
                repo.readme_excerpt,
            ]
        )
        refs.append({"source": "github", "detail": f"GitHub repo {repo.full_name}"})

    for item in confirmed_learning or []:
        parts.append(str(item.get("label", "")))
        parts.append(str(item.get("skill", "")))
        refs.append({"source": "user_confirmed", "detail": str(item.get("label", "Confirmed learning"))})

    corpus = "\n".join(p for p in parts if p)
    allowed_numbers = {_normalize_number_token(m.group(0)) for m in NUMBER_METRIC_RE.finditer(corpus)}

    allowed_tools: set[str] = set()
    for tool in KNOWN_TOOLS:
        if skill_in_text(tool, corpus):
            allowed_tools.add(normalize_skill(tool))
    for implied_norm in find_direct_and_implied_skills(corpus).keys():
        allowed_tools.add(implied_norm)
    for sk in resume.skills:
        allowed_tools.add(normalize_skill(sk))
        for implied_norm in find_direct_and_implied_skills(sk).keys():
            allowed_tools.add(implied_norm)
    for repo in github_repos or []:
        if repo.excluded_reason is None:
            for sk in repo.inferred_skills:
                allowed_tools.add(normalize_skill(sk))

    return corpus, allowed_numbers, allowed_tools, refs


def validate_claim_against_evidence(
    *,
    new_text: str,
    original_text: str = "",
    resume: ParsedResumeStructure,
    raw_resume_text: str,
    github_repos: list[AnalyzedRepo] | None = None,
    confirmed_learning: list[dict[str, Any]] | None = None,
    section: str = "",
) -> ValidationResult:
    """
    Deterministic truthfulness + structural integrity validation layer.
    Extracts numbers/metrics, tools, leadership scope verbs, and checks
    integrity rules (no empty items, no stray glyphs/periods, no duplicated
    tokens, skill categories preserved).
    """
    integrity_errors: list[str] = []
    stripped_new = (new_text or "").strip()
    if not stripped_new:
        integrity_errors.append("Integrity check failed: proposed item cannot be empty.")

    if BAD_GLYPH_RE.search(stripped_new):
        integrity_errors.append("Integrity check failed: stray private-use or symbol glyph detected.")

    # No duplicated prefix/word typos introduced or left behind (e.g. 'Detail- Detail-oriented')
    if re.search(r"\b([A-Za-z]{3,}-)\s+\1", stripped_new, re.I):
        integrity_errors.append("Integrity check failed: duplicated hyphenated word detected.")

    # Skill category & trailing period check
    if section == "skills":
        orig_cat_m = re.match(r"^\s*([A-Za-z][A-Za-z &/+]{1,35}):\s*", original_text or "")
        if orig_cat_m:
            expected_label = orig_cat_m.group(1).strip()
            if not stripped_new.lower().startswith(f"{expected_label.lower()}:"):
                integrity_errors.append(
                    f"Integrity check failed: skill category label '{expected_label}:' must be preserved."
                )
        skills_body = stripped_new.split(":", 1)[1] if ":" in stripped_new else stripped_new
        raw_parts = [p.strip() for p in re.split(r"[,|;]+", skills_body) if p.strip()]
        if not raw_parts:
            integrity_errors.append("Integrity check failed: skills line contains no skill items.")
        for sp in raw_parts:
            if sp.endswith("."):
                integrity_errors.append(
                    f"Integrity check failed: trailing period in skill '{sp}'."
                )
                break

    # Balanced parenthesis check (don't introduce stray '(' or ')' absent from original)
    if stripped_new.count("(") != stripped_new.count(")") and (
        (original_text or "").count("(") == (original_text or "").count(")")
    ):
        integrity_errors.append("Integrity check failed: unbalanced parenthesis in text.")

    corpus, allowed_numbers, allowed_tools, refs = build_evidence_corpus(
        resume=resume,
        raw_resume_text=raw_resume_text,
        github_repos=github_repos,
        confirmed_learning=confirmed_learning,
    )

    unverified_numbers: list[str] = []
    for m in NUMBER_METRIC_RE.finditer(new_text):
        raw_num = m.group(0).strip()
        if not raw_num or not any(ch.isdigit() for ch in raw_num):
            continue
        norm_num = _normalize_number_token(raw_num)
        # Allow version-like single digits inside known names like Python 3 or SQLite3
        if norm_num not in allowed_numbers:
            unverified_numbers.append(raw_num)

    unverified_tools: list[str] = []
    for tool in KNOWN_TOOLS:
        if skill_in_text(tool, new_text):
            if normalize_skill(tool) not in allowed_tools:
                unverified_tools.append(tool)

    inflated_verbs: list[str] = []
    lower_new_words = re.findall(r"\b[a-z]+\b", new_text.lower())
    lower_corpus_words = set(re.findall(r"\b[a-z]+\b", corpus.lower()))
    lower_orig_words = set(re.findall(r"\b[a-z]+\b", original_text.lower()))
    orig_is_individual = bool(lower_orig_words & INDIVIDUAL_EVIDENCE_VERBS)

    for verb, honest_alt in INFLATED_LEADERSHIP_VERBS.items():
        if verb in lower_new_words and verb not in lower_corpus_words:
            if orig_is_individual or not original_text:
                inflated_verbs.append(
                    f"Inflated verb '{verb}' has no leadership evidence; consider '{honest_alt}'."
                )

    warnings: list[str] = list(integrity_errors)
    if unverified_numbers:
        warnings.append(
            f"Blocked unverified metric(s)/number(s) absent from evidence: {', '.join(unverified_numbers)}"
        )
    if unverified_tools:
        warnings.append(
            f"Blocked tool(s) absent from resume or GitHub evidence: {', '.join(unverified_tools)}"
        )
    warnings.extend(inflated_verbs)

    if integrity_errors or unverified_numbers or unverified_tools:
        status = "blocked"
    elif inflated_verbs:
        status = "flagged"
    else:
        status = "passed"

    return ValidationResult(
        status=status,
        warnings=warnings,
        unverified_numbers=unverified_numbers,
        unverified_tools=unverified_tools,
        inflated_verbs=inflated_verbs,
        evidence_refs=refs[:5],
    )
