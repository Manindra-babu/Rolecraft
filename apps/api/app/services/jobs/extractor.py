import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import extruct
import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.models.job import JobPosting
from app.models.user import User
from app.schemas.job import JobPostingResponse, StructuredJobFields
from app.services.jobs.ats import fetch_ats_posting
from app.services.jobs.ssrf import SSRFError, normalize_job_url, safe_fetch_url, validate_public_url
from app.services.jobs.validator import validate_extracted_job
from app.services.llm.base import LLMClient
from app.services.llm.factory import get_llm_client_for_user

RESTRICTED_SOCIAL_JOB_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com")

COMMON_SKILLS_VOCAB = [
    "Python", "TypeScript", "JavaScript", "Java", "Go", "Rust", "C++", "C#", "SQL",
    "FastAPI", "Django", "Flask", "React", "Next.js", "Node.js", "Express", "Spring Boot",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite", "Docker", "Kubernetes",
    "AWS", "GCP", "Azure", "Terraform", "Git", "GitHub Actions", "CI/CD",
    "REST APIs", "GraphQL", "PyTorch", "TensorFlow", "Playwright", "pytest", "Linux",
]


def _is_restricted_job_board(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in RESTRICTED_SOCIAL_JOB_DOMAINS)


def _check_robots_allowed(url: str) -> bool:
    """Best-effort robots.txt check for plain HTML page fetches."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        _, body, status_code = safe_fetch_url(robots_url, timeout_seconds=4.0, max_bytes=200_000)
        if status_code != 200:
            return True
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return rp.can_fetch("ResumeIQBot", url)
    except Exception:
        return True


def _extract_jsonld_job(html: str, base_url: str) -> tuple[StructuredJobFields, str] | None:
    try:
        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
    except Exception:
        return None

    candidates: list[dict[str, Any]] = []
    for item in data.get("json-ld", []):
        if isinstance(item, dict):
            if "@graph" in item and isinstance(item["@graph"], list):
                candidates.extend(g for g in item["@graph"] if isinstance(g, dict))
            else:
                candidates.append(item)

    for item in candidates:
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if not any(str(t).lower().endswith("jobposting") for t in types if t):
            continue

        title = str(item.get("title") or item.get("name") or "").strip()
        org = item.get("hiringOrganization")
        company = ""
        if isinstance(org, dict):
            company = str(org.get("name", "")).strip()
        elif isinstance(org, str):
            company = org.strip()

        loc = ""
        job_loc = item.get("jobLocation")
        if isinstance(job_loc, list) and job_loc:
            job_loc = job_loc[0]
        if isinstance(job_loc, dict):
            addr = job_loc.get("address") or {}
            if isinstance(addr, dict):
                loc = ", ".join(
                    str(p)
                    for p in [addr.get("addressLocality", ""), addr.get("addressRegion", ""), addr.get("addressCountry", "")]
                    if p
                )

        desc_html = str(item.get("description") or "")
        raw_text = LexborHTMLParser(unescape(desc_html)).text(separator="\n").strip()
        skills_raw = item.get("skills") or []
        if isinstance(skills_raw, str):
            req_skills = _sanitize_raw_skill_items([skills_raw])
        elif isinstance(skills_raw, list):
            req_skills = _sanitize_raw_skill_items([str(s) for s in skills_raw if str(s).strip()])
        else:
            req_skills = []

        resp_raw = item.get("responsibilities") or []
        if isinstance(resp_raw, str):
            responsibilities = [s.strip() for s in resp_raw.splitlines() if s.strip()]
        elif isinstance(resp_raw, list):
            responsibilities = [str(s).strip() for s in resp_raw if str(s).strip()]
        else:
            responsibilities = []

        fields = StructuredJobFields(
            title=title,
            company=company,
            location=loc,
            experience_level=str(item.get("experienceRequirements") or ""),
            responsibilities=responsibilities,
            required_skills=req_skills,
        )
        fields = _enrich_fields_deterministically(fields, raw_text)
        return fields, raw_text
    return None


from app.schemas.job import SkillRequirementItem

ATOMIC_SKILL_CANONICAL_TERMS: tuple[str, ...] = (
    "HTTP", "HTML", "HTML5", "DOM", "AJAX", "JavaScript", "TypeScript", "CSS",
    "Node.js", "React", "Next.js", "Express", "MERN", "Python", "Java", "C++", "C#",
    "Go", "Rust", "SQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite",
    "FastAPI", "Django", "Flask", "Spring Boot", "GraphQL", "Web Services", "SOA",
    "REST APIs", "REST", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Linux",
    "Git", "CI/CD", "PyTorch", "TensorFlow", "Playwright", "pytest", "Tailwind CSS",
)

_CANONICAL_LOOKUP: dict[str, str] = {t.lower(): t for t in ATOMIC_SKILL_CANONICAL_TERMS}
_CANONICAL_LOOKUP.update({
    "html/dom": "HTML/DOM",
    "rest": "REST",
    "restful apis": "REST APIs",
    "restful services": "REST APIs",
    "node": "Node.js",
    "nodejs": "Node.js",
    "react.js": "React",
    "reactjs": "React",
    "express.js": "Express",
    "expressjs": "Express",
    "postgres": "PostgreSQL",
    "k8s": "Kubernetes",
    "tailwindcss": "Tailwind CSS",
    "tailwind": "Tailwind CSS",
    "html5": "HTML",
})

_NON_SKILL_PHRASES = (
    "building, breaking",
    "rebuilding",
    "strong applied experience",
    "graduating in",
    "bachelor",
    "accredited college",
    "university",
    "creative thinkers",
    "real-world products",
    "constant desire",
    "explore new things",
    "working knowledge",
    "familiarity with",
    "understanding of concepts",
    "partner closely",
    "triage production",
    "does not charge candidates",
)

_OR_GROUP_TRIGGER_RE = re.compile(
    r"(?:\bany\s+(?:one\s+)?(?:or\s+multiple\s+)?of\s+the\s+following\b|"
    r"\bone\s+or\s+more\s+of\s+the\s+following\b|"
    r"\b(?:either|experience\s+in\s+any\s+one)\b)",
    re.I,
)

_NOISE_TAIL_WORDS_RE = re.compile(
    r"\s+(?:applications|application|frameworks|framework|tools|tool|concepts|skills)\s*$",
    re.I,
)

_NOISE_LEAD_WORDS_RE = re.compile(
    r"^(?:such\s+as|like|including|concepts\s+like|e\.g\.|for\s+example|"
    r"working\s+knowledge\s+of|familiarity\s+with|understanding\s+of(?:\s+concepts\s+like)?|"
    r"experience\s+(?:with|in)|strong\s+applied\s+experience\s+in|"
    r"any\s+(?:one\s+)?(?:or\s+multiple\s+)?of\s+the\s+following\s*:?\s*)+",
    re.I,
)


def _split_atomic_slash_pair(tok: str) -> list[str]:
    low = tok.lower()
    if low in {"ci/cd", "tcp/ip", "ui/ux", "i/o"}:
        return [tok]
    if "/" in tok and not tok.startswith("http"):
        parts = [p.strip() for p in tok.split("/") if p.strip()]
        if all(1 <= len(p.split()) <= 2 for p in parts) and all(
            p.lower() in _CANONICAL_LOOKUP or len(p) <= 15 for p in parts
        ):
            return parts
    return [tok]


def _normalize_single_skill_token(raw_tok: str) -> list[str]:
    """Clean one extracted token into canonical atomic skill name(s), dropping prose fragments."""
    s = (raw_tok or "").strip()
    if not s:
        return []
    # Remove unbalanced paren clutter
    s = _NOISE_LEAD_WORDS_RE.sub("", s).strip()
    s = s.strip("()[]{}•-*–—,;:. ")
    if not s:
        return []
    s = _NOISE_TAIL_WORDS_RE.sub("", s).strip("()[]{}•-*–—,;:. ")
    if not s:
        return []

    low = s.lower()
    if any(bad in low for bad in _NON_SKILL_PHRASES):
        return []
    if low in {"building", "breaking", "rebuilding", "and rebuilding", "web technologies", "software applications", "following"}:
        return []

    out: list[str] = []
    for sub in _split_atomic_slash_pair(s):
        sub_clean = _NOISE_TAIL_WORDS_RE.sub("", _NOISE_LEAD_WORDS_RE.sub("", sub)).strip("()[]{}•-*–—,;:. ")
        if not sub_clean:
            continue
        sub_low = sub_clean.lower()
        # Reject long natural-language clauses unless an exact known term
        if sub_low in _CANONICAL_LOOKUP:
            canon = _CANONICAL_LOOKUP[sub_low]
            if canon not in out:
                out.append(canon)
            continue
        words = sub_clean.split()
        if len(words) > 4 or len(sub_clean) > 40:
            continue
        if any(bad in sub_low for bad in _NON_SKILL_PHRASES):
            continue
        if sub_low in {"and", "or", "with", "using", "via", "such", "as", "breaking", "building", "rebuilding"}:
            continue
        out.append(sub_clean)
    return out


def _extract_skills_from_segment(segment: str) -> list[str]:
    """
    Extract atomic skill names from a clause or parenthetical list without naively splitting prose sentences.
    Handles 'C++. Python', 'HTTP, HTML/DOM, JavaScript, CSS, AJAX', 'React/Node', etc.
    """
    text = segment.strip()
    if not text:
        return []
    # Split on commas, semicolons, 'and/or', and sentence-boundary dots like 'C++. Python' (keeping 'Node.js')
    raw_parts = re.split(r"\s*[,;]\s*|\s+(?:and/or|and|or)\s+|(?<=[a-zA-Z0-9+#])\.\s+(?=[A-Z])", text)
    results: list[str] = []
    seen: set[str] = set()
    for p in raw_parts:
        for tok in _normalize_single_skill_token(p):
            if tok.lower() not in seen:
                seen.add(tok.lower())
                results.append(tok)
    return results


def _sanitize_raw_skill_items(raw_items: list[str]) -> list[str]:
    """Convert raw skill strings (even comma-split or parenthetical prose) into atomic canonical skills."""
    results: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        k = tok.lower()
        if tok and k not in seen:
            seen.add(k)
            results.append(tok)

    for raw in raw_items:
        item = (raw or "").strip()
        if not item:
            continue
        # Pull parenthetical content like 'web technologies (such as HTTP, HTML/DOM, AJAX)' first
        paren_m = re.search(r"\(([^)]+)\)", item)
        if paren_m:
            for tok in _extract_skills_from_segment(paren_m.group(1)):
                _add(tok)
            item_wo_paren = (item[: paren_m.start()] + " " + item[paren_m.end() :]).strip()
            for tok in _extract_skills_from_segment(item_wo_paren):
                _add(tok)
            continue

        # Also extract known vocab hits if the string was a noisy sentence fragment
        if len(item.split()) > 4 or any(w in item.lower() for w in ("such as", "following", "knowledge of", "experience in")):
            tail_m = re.search(r"(?:such\s+as|concepts\s+like|following\s*:?)\s+(.+)$", item, re.I)
            if tail_m:
                for tok in _extract_skills_from_segment(tail_m.group(1)):
                    _add(tok)
            for term in ATOMIC_SKILL_CANONICAL_TERMS:
                if re.search(rf"(?<![A-Za-z0-9+#]){re.escape(term)}(?![A-Za-z0-9+#])", item, re.I if len(term) > 2 else 0):
                    _add(_CANONICAL_LOOKUP.get(term.lower(), term))
            continue

        for tok in _extract_skills_from_segment(item):
            _add(tok)

    return results


def _build_skill_items(
    req_skills: list[str],
    pref_skills: list[str],
    or_groups: list[list[str]],
) -> list[SkillRequirementItem]:
    group_map: dict[str, str] = {}
    for idx, grp in enumerate(or_groups):
        gname = f"or_group_{idx + 1}"
        for member in grp:
            group_map[member.lower()] = gname

    items: list[SkillRequirementItem] = []
    seen: set[str] = set()
    for sk in req_skills:
        k = sk.lower()
        if k not in seen:
            seen.add(k)
            items.append(SkillRequirementItem(name=sk, importance="required", or_group=group_map.get(k)))
    for sk in pref_skills:
        k = sk.lower()
        if k not in seen:
            seen.add(k)
            items.append(SkillRequirementItem(name=sk, importance="preferred", or_group=group_map.get(k)))
    return items


def _enrich_fields_deterministically(
    fields: StructuredJobFields, raw_text: str
) -> StructuredJobFields:
    """
    Extract clean atomic required/preferred skills, OR groups ('any one/multiple of the following'),
    and separate responsibilities/soft requirements deterministically from raw job text.
    """
    lines = [ln.strip(" •-*–\t") for ln in raw_text.splitlines() if ln.strip()]
    if not fields.title and lines:
        for candidate in lines[:5]:
            if 2 <= len(candidate.split()) <= 10 and len(candidate) <= 90:
                fields.title = candidate
                break

    if not fields.company:
        comp_m = re.search(r"\bAt\s+([A-Z][A-Za-z0-9&.\- ]{1,30}),", raw_text)
        if comp_m:
            fields.company = comp_m.group(1).strip()

    req_skills = _sanitize_raw_skill_items(fields.required_skills)
    pref_skills = _sanitize_raw_skill_items(fields.preferred_skills)
    or_groups: list[list[str]] = [
        _sanitize_raw_skill_items(g) for g in (fields.or_groups or []) if _sanitize_raw_skill_items(g)
    ]
    seen_req = {_norm_key_global(s) for s in req_skills} if False else {("rest" if s.lower() in {"rest", "rest apis", "restful apis"} else s.lower()) for s in req_skills}
    seen_pref = {("rest" if s.lower() in {"rest", "rest apis", "restful apis"} else s.lower()) for s in pref_skills}
    resp_list: list[str] = list(fields.responsibilities_or_soft_requirements or fields.responsibilities)
    seen_resp = {r.lower() for r in resp_list}

    # Section-aware line scan
    section_mode = "general"  # general | responsibilities | required | preferred | ignore
    for raw_ln in raw_text.splitlines():
        ln = raw_ln.strip(" •-*–\t\u202f\xa0")
        if not ln:
            continue
        low = ln.lower()
        if ln.startswith("#"):
            continue
        if any(h in low for h in ("our benefits", "does not charge candidates", "travel percent", "hybrid work model", "equal")):
            section_mode = "ignore"
            continue
        if any(h in low for h in ("additional responsibilities & preferred", "preferred qualifications", "nice to have", "bonus qualifications")):
            section_mode = "preferred"
            continue
        if any(h in low for h in ("what do you need to bring", "minimum qualifications", "requirements", "required qualifications")):
            section_mode = "required"
            continue
        if any(h in low for h in ("your way to impact", "your day-to-day", "responsibilities", "what you'll do", "what you will do", "your role")):
            section_mode = "responsibilities"
            continue
        if any(h in low for h in ("university graduate position information",)):
            section_mode = "soft_required"
            continue

        if section_mode == "ignore":
            continue

        target_bucket = pref_skills if section_mode == "preferred" else req_skills
        target_seen = seen_pref if section_mode == "preferred" else seen_req

        def _norm_key(sk: str) -> str:
            low_s = sk.lower().strip()
            if low_s in {"rest", "rest apis", "restful apis", "restful services"}:
                return "rest"
            return low_s

        def _add_skill(sk: str) -> None:
            k = _norm_key(sk)
            if section_mode == "preferred":
                if k not in seen_pref and k not in seen_req:
                    seen_pref.add(k)
                    pref_skills.append(sk)
            else:
                if k not in seen_req:
                    seen_req.add(k)
                    req_skills.append(sk)

        # Extract parenthetical enumerations (such as HTTP, HTML/DOM, JavaScript, CSS, AJAX)
        line_skills: list[str] = []
        if section_mode in {"required", "preferred", "responsibilities"}:
            for pm in re.finditer(r"\(([^)]+)\)", ln):
                inner = pm.group(1)
                extracted = _extract_skills_from_segment(inner)
                for sk in extracted:
                    if (
                        sk.lower() in _CANONICAL_LOOKUP
                        or any(sk.lower() == t.lower() for t in ATOMIC_SKILL_CANONICAL_TERMS)
                        or re.search(r"\b(?:such as|e\.g\.|like)\b", inner, re.I)
                    ):
                        line_skills.append(sk)

            # Extract colon / phrase-introduced enumerations ('following: Node.js applications, Java, C++. Python', 'concepts like Web Services, SOA, REST APIs', 'using React/Node ...')
            for em in re.finditer(
                r"(?:any\s+(?:one\s+)?(?:or\s+multiple\s+)?of\s+the\s+following\s*:?\s+|concepts\s+like\s+|such\s+as\s+)([^\n.;]+(?:\.\s+[A-Z][A-Za-z0-9+#.-]+)*)",
                ln,
                re.I,
            ):
                frag = em.group(1)
                frag = re.sub(r"\band/or\s+(?:build|develop|create|work|partner).*$", "", frag, flags=re.I)
                line_skills.extend(_extract_skills_from_segment(frag))

            for em in re.finditer(
                r"\busing\s+([A-Za-z0-9+#./-]+(?:\s*,\s*[A-Za-z0-9+#./-]+){0,6})",
                ln,
                re.I,
            ):
                for sk in _extract_skills_from_segment(em.group(1)):
                    if sk.lower() in _CANONICAL_LOOKUP or any(sk.lower() == t.lower() for t in ATOMIC_SKILL_CANONICAL_TERMS):
                        line_skills.append(sk)

        # Match any canonical tech terms explicitly appearing on this line
        if section_mode in {"required", "preferred", "responsibilities", "general"}:
            for term in ATOMIC_SKILL_CANONICAL_TERMS:
                flags = 0 if len(term) <= 2 else re.I
                if re.search(rf"(?<![A-Za-z0-9+#]){re.escape(term)}(?![A-Za-z0-9+#])", ln, flags):
                    canon = _CANONICAL_LOOKUP.get(term.lower(), term)
                    line_skills.append(canon)

        # Deduplicate REST / REST APIs (keep 'REST APIs' or 'REST' canonical once)
        if any(s.lower() == "rest apis" for s in line_skills):
            line_skills = [s for s in line_skills if s.lower() != "rest"]

        # Deduplicate preserving order
        unique_line_skills: list[str] = []
        u_seen: set[str] = set()
        for sk in line_skills:
            if sk.lower() not in u_seen:
                u_seen.add(sk.lower())
                unique_line_skills.append(sk)
                _add_skill(sk)

        # OR group detection ('any one of the following', 'any or multiple of the following')
        if _OR_GROUP_TRIGGER_RE.search(ln) and len(unique_line_skills) >= 2:
            if not any(set(s.lower() for s in g) == set(s.lower() for s in unique_line_skills) for g in or_groups):
                or_groups.append(unique_line_skills)

        # Responsibilities and soft/behavioral/education requirements stay in `responsibilities` ONLY
        is_resp_or_soft = (
            section_mode in {"responsibilities", "soft_required"}
            or (section_mode == "required" and not unique_line_skills and len(ln) > 20)
            or any(
                phrase in low
                for phrase in (
                    "built, broken, and rebuilt",
                    "building, breaking, and rebuilding",
                    "a constant desire to grow",
                    "graduating in",
                    "bachelor",
                    "partner closely",
                    "estimate engineering",
                    "code high-volume",
                )
            )
        )
        if is_resp_or_soft and 20 <= len(ln) <= 320 and " " in ln:
            if ln.lower() not in seen_resp:
                seen_resp.add(ln.lower())
                resp_list.append(ln)

    # Also ensure fallback responsibilities if empty
    if not resp_list:
        for ln in lines:
            if len(ln) > 25 and not ln.startswith("#") and "does not charge candidates" not in ln.lower():
                if any(v in ln.lower() for v in ("build", "design", "develop", "partner", "estimate", "graduat")):
                    if ln.lower() not in seen_resp:
                        seen_resp.add(ln.lower())
                        resp_list.append(ln)
            if len(resp_list) >= 8:
                break

    fields.required_skills = req_skills
    fields.preferred_skills = pref_skills
    fields.or_groups = or_groups
    fields.skills = _build_skill_items(req_skills, pref_skills, or_groups)
    fields.responsibilities = resp_list[:12]
    fields.responsibilities_or_soft_requirements = list(fields.responsibilities)
    fields.keywords = list(dict.fromkeys(req_skills + pref_skills))[:20]

    lower = raw_text.lower()
    if not fields.experience_level:
        if any(w in lower for w in ("intern", "internship", "co-op")):
            fields.experience_level = "Internship"
        elif any(w in lower for w in ("new grad", "entry level", "junior", "0-2 years")):
            fields.experience_level = "Entry Level"
        elif any(w in lower for w in ("senior", "staff", "principal", "5+ years")):
            fields.experience_level = "Senior"
        elif "years" in lower:
            fields.experience_level = "Mid-Level"

    return fields


def _extract_main_html_text(html: str) -> str:
    extracted = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    if len(extracted.strip()) >= 120:
        return extracted.strip()
    tree = LexborHTMLParser(html)
    for tag in tree.css("script, style, noscript, nav, footer"):
        tag.decompose()
    return tree.text(separator="\n").strip()


def _structure_with_llm(
    llm: LLMClient | None, raw_text: str, title_hint: str = "", company_hint: str = ""
) -> StructuredJobFields:
    fallback = _enrich_fields_deterministically(
        StructuredJobFields(title=title_hint, company=company_hint), raw_text
    )
    if llm is None:
        return fallback
    prompt = (
        "Extract structured job posting fields from the text below. Use ONLY facts present in the text.\n"
        "CRITICAL EXTRACTION RULES:\n"
        "1. `skills`, `required_skills`, and `preferred_skills` MUST contain ONLY atomic canonical "
        "technology/tool/concept names (e.g., 'HTTP', 'HTML', 'DOM', 'AJAX', 'JavaScript', 'CSS', 'Node.js', "
        "'Web Services', 'SOA', 'REST APIs'). NEVER split parentheticals at commas with prose fragments like "
        "'working knowledge of web technologies (such as HTTP' or 'AJAX)' or 'breaking'.\n"
        "2. Parse enumerations like 'web technologies (such as HTTP, HTML/DOM, AJAX)' into separate atomic skills: "
        "HTTP, HTML, DOM, AJAX.\n"
        "3. Put behavioral traits, soft requirements, degree/graduation rules, and engineering duties "
        "(e.g. 'building, breaking, and rebuilding software applications') ONLY in `responsibilities_or_soft_requirements` "
        "and `responsibilities`, NEVER in `required_skills` or `preferred_skills`.\n"
        "4. Put any 'any one of the following' / 'any or multiple of the following' skill alternatives as lists inside `or_groups`.\n\n"
        f"Title hint: {title_hint}\nCompany hint: {company_hint}\n\n"
        f"JOB TEXT:\n{raw_text[:12000]}"
    )
    try:
        structured = llm.generate_structured(
            prompt=prompt,
            schema=StructuredJobFields,
            operation="job_extraction",
            tier="fast",
            redact_pii=False,
        )
        # Always sanitize LLM outputs and merge with deterministic extraction to guarantee clean atomic skills
        if structured.skills and not structured.required_skills and not structured.preferred_skills:
            structured.required_skills = [s.name for s in structured.skills if s.importance == "required"]
            structured.preferred_skills = [s.name for s in structured.skills if s.importance == "preferred"]
        if structured.responsibilities_or_soft_requirements and not structured.responsibilities:
            structured.responsibilities = list(structured.responsibilities_or_soft_requirements)

        structured.required_skills = _sanitize_raw_skill_items(structured.required_skills)
        structured.preferred_skills = _sanitize_raw_skill_items(structured.preferred_skills)

        if not structured.title and fallback.title:
            structured.title = fallback.title
        if not structured.company and fallback.company:
            structured.company = fallback.company
        if not structured.required_skills and fallback.required_skills:
            structured.required_skills = fallback.required_skills
        else:
            seen_r = {s.lower() for s in structured.required_skills}
            seen_p = {s.lower() for s in structured.preferred_skills}
            for s in fallback.required_skills:
                if s.lower() not in seen_r and s.lower() not in seen_p:
                    seen_r.add(s.lower())
                    structured.required_skills.append(s)
        if not structured.or_groups and fallback.or_groups:
            structured.or_groups = fallback.or_groups
        if not structured.responsibilities and fallback.responsibilities:
            structured.responsibilities = fallback.responsibilities
        structured.responsibilities_or_soft_requirements = list(structured.responsibilities)
        structured.skills = _build_skill_items(
            structured.required_skills, structured.preferred_skills, structured.or_groups
        )
        if not structured.keywords:
            structured.keywords = fallback.keywords
        return structured
    except Exception:
        return fallback


def _render_with_playwright(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        validate_public_url(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


def _try_third_party_scraper(url: str) -> str | None:
    settings = get_settings()
    if not settings.THIRD_PARTY_SCRAPER_ENABLED or not settings.THIRD_PARTY_SCRAPER_URL:
        return None
    try:
        resp = httpx.post(
            settings.THIRD_PARTY_SCRAPER_URL,
            json={"url": url},
            headers={"Authorization": f"Bearer {settings.THIRD_PARTY_SCRAPER_KEY}"}
            if settings.THIRD_PARTY_SCRAPER_KEY
            else {},
            timeout=15.0,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        return None
    return None


def _save_job_posting(
    db: Session,
    user: User,
    *,
    url: str | None,
    normalized_url: str | None,
    method: str,
    confidence: float,
    fields: StructuredJobFields,
    raw_text: str,
) -> JobPostingResponse:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    job = JobPosting(
        user_id=user.id,
        url=url,
        normalized_url=normalized_url,
        extraction_method=method,
        confidence=confidence,
        title=fields.title[:300],
        company=fields.company[:300],
        location=fields.location[:200],
        experience_level=fields.experience_level[:100],
        responsibilities=fields.responsibilities,
        required_skills=fields.required_skills,
        preferred_skills=fields.preferred_skills,
        or_groups=fields.or_groups,
        keywords=fields.keywords,
        raw_text=raw_text,
        fetched_at=now,
        expires_at=now + timedelta(hours=settings.JOB_CACHE_TTL_HOURS),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return JobPostingResponse(
        id=job.id,
        status="extracted",
        url=job.url,
        normalized_url=job.normalized_url,
        extraction_method=job.extraction_method,
        confidence=job.confidence,
        title=job.title,
        company=job.company,
        location=job.location,
        experience_level=job.experience_level,
        responsibilities=job.responsibilities,
        responsibilities_or_soft_requirements=list(job.responsibilities),
        required_skills=job.required_skills,
        preferred_skills=job.preferred_skills,
        skills=_build_skill_items(job.required_skills, job.preferred_skills, job.or_groups or []),
        or_groups=job.or_groups or [],
        keywords=job.keywords,
        raw_text=job.raw_text,
        fetched_at=job.fetched_at,
        cached=False,
    )


def extract_job_from_html_fixture(
    html: str,
    url: str,
    llm: LLMClient | None = None,
) -> tuple[str, StructuredJobFields, str, float, str | None]:
    """
    Pure helper used both by live HTTP extraction and offline HTML fixture tests.
    Returns (method, fields, raw_text, confidence, rejection_reason).
    """
    jsonld = _extract_jsonld_job(html, url)
    if jsonld is not None:
        fields, raw_text = jsonld
        ok, conf, reason = validate_extracted_job(raw_text, fields)
        if ok:
            return "json_ld", fields, raw_text, conf, None
        return "json_ld", fields, raw_text, conf, reason

    main_text = _extract_main_html_text(html)
    fields = _structure_with_llm(llm, main_text)
    ok, conf, reason = validate_extracted_job(main_text, fields)
    return "http_html", fields, main_text, conf, None if ok else reason


def extract_job_from_url(
    url: str,
    *,
    db: Session,
    user: User,
) -> JobPostingResponse:
    # Enforce SSRF before anything else
    validate_public_url(url)
    norm_url = normalize_job_url(url)

    # Check cache first
    now = datetime.now(timezone.utc)
    cached = (
        db.query(JobPosting)
        .filter(JobPosting.normalized_url == norm_url)
        .order_by(JobPosting.fetched_at.desc())
        .first()
    )
    if cached and cached.expires_at:
        exp = cached.expires_at if cached.expires_at.tzinfo else cached.expires_at.replace(tzinfo=timezone.utc)
        if exp > now:
            return JobPostingResponse(
                id=cached.id,
                status="extracted",
                url=cached.url,
                normalized_url=cached.normalized_url,
                extraction_method=cached.extraction_method,
                confidence=cached.confidence,
                title=cached.title,
                company=cached.company,
                location=cached.location,
                experience_level=cached.experience_level,
                responsibilities=cached.responsibilities,
                responsibilities_or_soft_requirements=list(cached.responsibilities),
                required_skills=cached.required_skills,
                preferred_skills=cached.preferred_skills,
                skills=_build_skill_items(cached.required_skills, cached.preferred_skills, cached.or_groups or []),
                or_groups=cached.or_groups or [],
                keywords=cached.keywords,
                raw_text=cached.raw_text,
                fetched_at=cached.fetched_at,
                cached=True,
            )

    llm = get_llm_client_for_user(db, user)
    restricted_board = _is_restricted_job_board(url)

    # Step 2: Known ATS public APIs (skip HTTP HTML fetch if public JSON succeeds)
    if not restricted_board:
        ats_res = fetch_ats_posting(url)
        if ats_res is not None:
            provider, fields, raw_text = ats_res
            fields = _enrich_fields_deterministically(fields, raw_text)
            ok, conf, _ = validate_extracted_job(raw_text, fields)
            if ok:
                return _save_job_posting(
                    db,
                    user,
                    url=url,
                    normalized_url=norm_url,
                    method=f"ats_{provider}",
                    confidence=conf,
                    fields=fields,
                    raw_text=raw_text,
                )

    # Step 1 + Step 3: Plain HTTP fetch (for restricted boards, try public JSON-LD only)
    if restricted_board or _check_robots_allowed(url):
        try:
            final_url, html, status_code = safe_fetch_url(url, timeout_seconds=10.0)
            if status_code == 200 and html:
                if restricted_board:
                    jsonld = _extract_jsonld_job(html, final_url)
                    if jsonld is not None:
                        fields, raw_text = jsonld
                        ok, conf, _ = validate_extracted_job(raw_text, fields)
                        if ok:
                            return _save_job_posting(
                                db,
                                user,
                                url=url,
                                normalized_url=norm_url,
                                method="json_ld",
                                confidence=conf,
                                fields=fields,
                                raw_text=raw_text,
                            )
                else:
                    method, fields, raw_text, conf, rejection = extract_job_from_html_fixture(
                        html, final_url, llm
                    )
                    if rejection is None:
                        return _save_job_posting(
                            db,
                            user,
                            url=url,
                            normalized_url=norm_url,
                            method=method,
                            confidence=conf,
                            fields=fields,
                            raw_text=raw_text,
                        )
        except SSRFError:
            raise
        except Exception as exc:
            logger.info("Plain HTTP fetch failed (%s)", type(exc).__name__)

    if restricted_board:
        return JobPostingResponse(
            status="fallback_required",
            fallback_reason=(
                "This job site requires sign-in or blocks automated access. "
                "Paste the job text below, upload a screenshot, or use the ResumeIQ browser extension."
            ),
            url=url,
            normalized_url=norm_url,
            extraction_method="fallback_required",
        )

    # Step 4: Playwright headless render
    rendered_html = _render_with_playwright(url)
    if rendered_html:
        method, fields, raw_text, conf, rejection = extract_job_from_html_fixture(
            rendered_html, url, llm
        )
        if rejection is None:
            return _save_job_posting(
                db,
                user,
                url=url,
                normalized_url=norm_url,
                method=f"playwright_{method}",
                confidence=conf,
                fields=fields,
                raw_text=raw_text,
            )

    # Step 5: Optional third-party scraping service (off by default)
    third_party_html = _try_third_party_scraper(url)
    if third_party_html:
        method, fields, raw_text, conf, rejection = extract_job_from_html_fixture(
            third_party_html, url, llm
        )
        if rejection is None:
            return _save_job_posting(
                db,
                user,
                url=url,
                normalized_url=norm_url,
                method="third_party_scraper",
                confidence=conf,
                fields=fields,
                raw_text=raw_text,
            )

    # Step 6: Graceful Fallback UI
    return JobPostingResponse(
        status="fallback_required",
        fallback_reason=(
            "We could not automatically extract a complete job posting from this URL "
            "(it may be behind a login wall, captcha, or JavaScript app). "
            "Paste the job description text or upload a screenshot instead."
        ),
        url=url,
        normalized_url=norm_url,
        extraction_method="fallback_required",
    )


def extract_job_from_text(
    raw_text: str,
    *,
    db: Session,
    user: User,
    url: str | None = None,
    title_hint: str = "",
    company_hint: str = "",
    method: str = "manual_paste",
) -> JobPostingResponse:
    llm = get_llm_client_for_user(db, user)
    fields = _structure_with_llm(llm, raw_text, title_hint=title_hint, company_hint=company_hint)
    ok, conf, reason = validate_extracted_job(raw_text, fields)
    if not ok:
        return JobPostingResponse(
            status="fallback_required",
            fallback_reason=reason or "Text does not look like a complete job posting.",
            url=url,
            extraction_method=method,
            confidence=conf,
        )
    norm_url = normalize_job_url(url) if url else None
    return _save_job_posting(
        db,
        user,
        url=url,
        normalized_url=norm_url,
        method=method,
        confidence=conf,
        fields=fields,
        raw_text=raw_text,
    )
