import re

SKILL_ALIAS_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("python", ("python", "python3", "py")),
    ("typescript", ("typescript", "ts")),
    ("javascript", ("javascript", "js", "ecmascript", "es6")),
    ("html", ("html", "html5")),
    ("css", ("css", "css3")),
    ("dom", ("dom", "document object model")),
    ("http", ("http", "https")),
    ("ajax", ("ajax", "asynchronous javascript and xml", "fetch api")),
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
    ("mern", ("mern", "mern stack")),
    ("rest", ("rest", "rest apis", "rest api", "restful services", "restful apis", "restful api", "restful")),
    ("web services", ("web services", "web service")),
    ("soa", ("soa", "service-oriented architecture", "service oriented architecture")),
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
    ("c", ("c", "c programming", "c essentials")),
    ("c++", ("c++", "cpp")),
    ("c#", ("c#", "csharp", ".net")),
    ("go", ("go", "golang")),
    ("java", ("java", "jdk")),
    ("spring boot", ("spring boot", "springboot", "spring")),
    ("tailwind css", ("tailwind css", "tailwindcss", "tailwind")),
    ("rag", ("rag", "retrieval-augmented generation", "retrieval augmented generation")),
    ("llms", ("llm", "llms", "large language models", "large language model")),
]

# Explicit implication graph: canonical_trigger -> list of implied canonical skills
SKILL_IMPLICATIONS: dict[str, tuple[str, ...]] = {
    "mern": ("mongodb", "express", "react", "node.js", "javascript"),
    "pytorch": ("python",),
    "tensorflow": ("python",),
    "fastapi": ("python", "rest"),
    "django": ("python",),
    "flask": ("python",),
    "tailwind css": ("css",),
    "react": ("javascript", "html"),
    "next.js": ("react", "javascript"),
    "express": ("node.js", "javascript"),
    "node.js": ("javascript",),
    "rest": ("rest", "http"),
    "html": ("html",),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
_CANONICAL_TO_ALIASES: dict[str, tuple[str, ...]] = {}
for canonical, aliases in SKILL_ALIAS_GROUPS:
    _CANONICAL_TO_ALIASES[canonical] = aliases
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
    canonical = normalize_skill(skill)
    aliases = _CANONICAL_TO_ALIASES.get(canonical, (canonical,))
    for alias in aliases:
        if alias == "go":
            # Avoid matching the English verb 'go' as the Go programming language
            pattern = re.compile(r"(?<![a-zA-Z0-9+#])(?:Go|golang)(?![a-zA-Z0-9+#])")
            if pattern.search(text):
                return True
            continue
        if alias == "c":
            # Match uppercase 'C' cleanly (avoid 'C++', 'C#', or English initials mid-hyphen)
            pattern = re.compile(r"(?<![A-Za-z0-9+#.])C(?![A-Za-z0-9+#])")
            if pattern.search(text):
                return True
            continue
        pattern = re.compile(rf"(?<![a-z0-9+#]){re.escape(alias)}(?![a-z0-9+#])")
        if pattern.search(text.lower()):
            return True
    return False


def find_direct_and_implied_skills(text: str) -> dict[str, list[str]]:
    """
    Return {canonical_skill: [evidence_phrases]} from direct matches AND 1-2 hop implications
    in `SKILL_IMPLICATIONS` (e.g. MERN -> MongoDB, Express, React, Node.js, JavaScript -> HTML).
    """
    if not text or not text.strip():
        return {}

    evidence: dict[str, list[str]] = {}
    direct_canonicals: set[str] = set()

    for canonical, _aliases in SKILL_ALIAS_GROUPS:
        if skill_in_text(canonical, text):
            direct_canonicals.add(canonical)
            evidence.setdefault(canonical, []).append(f"Direct '{canonical}'")

    # Expand 2 hops along SKILL_IMPLICATIONS
    frontier = list(direct_canonicals)
    visited = set(direct_canonicals)
    for _depth in range(2):
        next_frontier: list[str] = []
        for src in frontier:
            for implied in SKILL_IMPLICATIONS.get(src, ()):
                norm_imp = normalize_skill(implied)
                evidence.setdefault(norm_imp, []).append(f"Implied via {src}")
                if norm_imp not in visited:
                    visited.add(norm_imp)
                    next_frontier.append(norm_imp)
        frontier = next_frontier

    return evidence
