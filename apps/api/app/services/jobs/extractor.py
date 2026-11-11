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
            req_skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
        elif isinstance(skills_raw, list):
            req_skills = [str(s).strip() for s in skills_raw if str(s).strip()]
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


def _enrich_fields_deterministically(
    fields: StructuredJobFields, raw_text: str
) -> StructuredJobFields:
    """Fill missing skills, responsibilities, or keywords deterministically from job text."""
    lines = [ln.strip(" •-*–\t") for ln in raw_text.splitlines() if ln.strip()]
    if not fields.title and lines:
        for candidate in lines[:5]:
            if 2 <= len(candidate.split()) <= 10 and len(candidate) <= 90:
                fields.title = candidate
                break

    lower = raw_text.lower()
    detected_skills = list(fields.required_skills)
    seen = {s.lower() for s in detected_skills}
    for skill in COMMON_SKILLS_VOCAB:
        pattern = re.compile(rf"(?<!\w){re.escape(skill.lower())}(?!\w)")
        if pattern.search(lower) and skill.lower() not in seen:
            seen.add(skill.lower())
            detected_skills.append(skill)
    fields.required_skills = detected_skills

    if not fields.responsibilities:
        resp_lines: list[str] = []
        in_resp = False
        for line in raw_text.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if any(h in low for h in ("responsibilities", "what you'll do", "what you will do", "your role")):
                in_resp = True
                continue
            if any(h in low for h in ("requirements", "qualifications", "preferred", "benefits", "about us")):
                in_resp = False
            if (in_resp or stripped.startswith(("•", "-", "*"))) and len(stripped) > 20:
                resp_lines.append(stripped.lstrip("•-*– ").strip())
            if len(resp_lines) >= 8:
                break
        fields.responsibilities = resp_lines

    if not fields.keywords:
        fields.keywords = list(dict.fromkeys(fields.required_skills + fields.preferred_skills))[:15]

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
        "Extract structured job posting fields from the text below. "
        "Use ONLY facts present in the text; do not invent requirements.\n\n"
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
        # Merge deterministic skills if demo mode or sparse LLM output
        if not structured.title and fallback.title:
            structured.title = fallback.title
        if not structured.required_skills and fallback.required_skills:
            structured.required_skills = fallback.required_skills
        if not structured.responsibilities and fallback.responsibilities:
            structured.responsibilities = fallback.responsibilities
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
        required_skills=job.required_skills,
        preferred_skills=job.preferred_skills,
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
                required_skills=cached.required_skills,
                preferred_skills=cached.preferred_skills,
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
