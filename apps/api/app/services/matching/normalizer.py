import re

SKILL_ALIAS_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("python", ("python", "python3", "py")),
    ("typescript", ("typescript", "ts")),
    ("javascript", ("javascript", "js", "ecmascript", "es6")),
    ("postgresql", ("postgresql", "postgres", "psql", "pg")),
    ("mysql", ("mysql", "mariadb")),
    ("mongodb", ("mongodb", "mongo")),
    ("redis", ("redis",)),
    ("sqlite", ("sqlite", "sqlite3")),
    ("fastapi", ("fastapi",)),
    ("django", ("django", "drf", "django rest framework")),
    ("flask", ("flask",)),
    ("react", ("react", "react.js", "reactjs")),
    ("next.js", ("next.js", "nextjs", "next")),
    ("node.js", ("node.js", "nodejs", "node")),
    ("express", ("express", "express.js", "expressjs")),
    ("rest apis", ("rest apis", "rest api", "restful services", "restful apis", "rest")),
    ("graphql", ("graphql",)),
    ("docker", ("docker", "docker compose", "docker-compose", "containers", "containerization")),
    ("kubernetes", ("kubernetes", "k8s")),
    ("aws", ("aws", "amazon web services")),
    ("gcp", ("gcp", "google cloud", "google cloud platform")),
    ("azure", ("azure", "microsoft azure")),
    ("ci/cd", ("ci/cd", "cicd", "github actions", "continuous integration", "jenkins", "gitlab ci")),
    ("git", ("git", "github", "gitlab")),
    ("pytest", ("pytest", "unit testing", "automated testing")),
    ("playwright", ("playwright",)),
    ("pytorch", ("pytorch", "torch")),
    ("tensorflow", ("tensorflow", "tf")),
    ("sql", ("sql", "structured query language", "sql queries")),
    ("linux", ("linux", "unix", "bash", "shell")),
    ("c++", ("c++", "cpp")),
    ("c#", ("c#", "csharp", ".net")),
    ("go", ("go", "golang")),
    ("java", ("java", "jdk")),
    ("spring boot", ("spring boot", "springboot", "spring")),
    ("tailwind css", ("tailwind css", "tailwindcss", "tailwind")),
]

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in SKILL_ALIAS_GROUPS:
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical


def normalize_skill(skill: str) -> str:
    cleaned = re.sub(r"\s+", " ", (skill or "").strip().lower())
    cleaned = cleaned.strip(".,;:/()[]")
    return _ALIAS_TO_CANONICAL.get(cleaned, cleaned)


def skills_match_via_alias(skill_a: str, skill_b: str) -> bool:
    return normalize_skill(skill_a) == normalize_skill(skill_b)


def skill_in_text(skill: str, text: str) -> bool:
    """Check whether a skill or any of its known aliases appears as a token in text."""
    if not text or not skill:
        return False
    lower_text = text.lower()
    canonical = normalize_skill(skill)
    aliases = next((grp for can, grp in SKILL_ALIAS_GROUPS if can == canonical), (canonical,))
    for alias in aliases:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])")
        if pattern.search(lower_text):
            return True
    return False
