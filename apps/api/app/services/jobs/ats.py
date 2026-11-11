import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from app.schemas.job import StructuredJobFields
from app.services.jobs.ssrf import safe_fetch_url


@dataclass
class ATSMatch:
    provider: str
    company: str
    job_id: str
    api_url: str


def _html_to_text(html_str: str) -> str:
    if not html_str:
        return ""
    tree = LexborHTMLParser(unescape(html_str))
    text = tree.text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def detect_ats_url(url: str) -> ATSMatch | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    # Greenhouse: boards.greenhouse.io/{company}/jobs/{id} or job-boards.greenhouse.io/{company}/jobs/{id}
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and len(parts) >= 3 and parts[1] == "jobs":
        company, job_id = parts[0], parts[2]
        return ATSMatch(
            provider="greenhouse",
            company=company,
            job_id=job_id,
            api_url=f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}",
        )

    # Lever: jobs.lever.co/{company}/{id}
    if host == "jobs.lever.co" and len(parts) >= 2:
        company, job_id = parts[0], parts[1]
        return ATSMatch(
            provider="lever",
            company=company,
            job_id=job_id,
            api_url=f"https://api.lever.co/v0/postings/{company}/{job_id}",
        )

    # Ashby: jobs.ashbyhq.com/{company}/{id}
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        company, job_id = parts[0], parts[1]
        return ATSMatch(
            provider="ashby",
            company=company,
            job_id=job_id,
            api_url=f"https://api.ashbyhq.com/posting-api/job-board/{company}",
        )

    # SmartRecruiters: jobs.smartrecruiters.com/{company}/{id}
    if host == "jobs.smartrecruiters.com" and len(parts) >= 2:
        company, job_id = parts[0], parts[1].split("-")[0]
        return ATSMatch(
            provider="smartrecruiters",
            company=company,
            job_id=job_id,
            api_url=f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}",
        )

    # Workable: apply.workable.com/{company}/j/{shortcode}
    if host == "apply.workable.com" and len(parts) >= 3 and parts[1] == "j":
        company, job_id = parts[0], parts[2]
        return ATSMatch(
            provider="workable",
            company=company,
            job_id=job_id,
            api_url=f"https://apply.workable.com/api/v3/accounts/{company}/jobs/{job_id}",
        )

    # Workday best-effort: {company}.wd{n}.myworkdayjobs.com/...
    if host.endswith(".myworkdayjobs.com"):
        return ATSMatch(
            provider="workday",
            company=host.split(".")[0],
            job_id=parts[-1] if parts else "",
            api_url=url,
        )

    return None


def parse_ats_json_payload(
    provider: str, payload: dict[str, Any], company_hint: str = "", job_id_hint: str = ""
) -> tuple[StructuredJobFields, str]:
    """Parse JSON from a known ATS public API into StructuredJobFields + raw_text."""
    if provider == "greenhouse":
        title = str(payload.get("title", "")).strip()
        company = (
            payload.get("company", {}).get("name")
            if isinstance(payload.get("company"), dict)
            else None
        ) or company_hint.replace("-", " ").title()
        loc = (
            payload.get("location", {}).get("name", "")
            if isinstance(payload.get("location"), dict)
            else ""
        )
        raw_text = _html_to_text(str(payload.get("content", "")))
        return StructuredJobFields(title=title, company=str(company), location=str(loc)), raw_text

    if provider == "lever":
        title = str(payload.get("text", "")).strip()
        cats = payload.get("categories") or {}
        loc = str(cats.get("location", "")) if isinstance(cats, dict) else ""
        lists_text = "\n".join(
            f"{item.get('text', '')}\n{_html_to_text(item.get('content', ''))}"
            for item in (payload.get("lists") or [])
            if isinstance(item, dict)
        )
        raw_text = "\n".join(
            p for p in [str(payload.get("descriptionPlain", "")), lists_text] if p
        ).strip()
        return (
            StructuredJobFields(
                title=title,
                company=company_hint.replace("-", " ").title(),
                location=loc,
            ),
            raw_text,
        )

    if provider == "ashby":
        jobs = payload.get("jobs") or []
        matched = next(
            (j for j in jobs if str(j.get("id", "")) == job_id_hint or job_id_hint in str(j.get("jobUrl", ""))),
            jobs[0] if jobs else None,
        )
        if isinstance(matched, dict):
            title = str(matched.get("title", "")).strip()
            loc = str(matched.get("location", "")).strip()
            raw_text = _html_to_text(str(matched.get("descriptionHtml", ""))) or str(
                matched.get("descriptionPlain", "")
            )
            return (
                StructuredJobFields(
                    title=title,
                    company=company_hint.replace("-", " ").title(),
                    location=loc,
                ),
                raw_text,
            )

    if provider == "smartrecruiters":
        title = str(payload.get("name", "")).strip()
        company = (
            (payload.get("company") or {}).get("name")
            if isinstance(payload.get("company"), dict)
            else company_hint
        ) or company_hint
        loc_obj = payload.get("location") or {}
        loc = ", ".join(p for p in [loc_obj.get("city", ""), loc_obj.get("country", "")] if p)
        sections = (payload.get("jobAd") or {}).get("sections") or {}
        raw_parts = [
            _html_to_text(str(sec.get("text", "")))
            for sec in sections.values()
            if isinstance(sec, dict)
        ]
        return (
            StructuredJobFields(title=title, company=str(company), location=loc),
            "\n".join(raw_parts).strip(),
        )

    if provider == "workable":
        title = str(payload.get("title", "")).strip()
        loc_obj = payload.get("location") or {}
        loc = str(loc_obj.get("city", "") or loc_obj.get("country", ""))
        raw_text = "\n".join(
            _html_to_text(str(payload.get(k, "")))
            for k in ("description", "requirements", "benefits")
            if payload.get(k)
        ).strip()
        return (
            StructuredJobFields(
                title=title,
                company=company_hint.replace("-", " ").title(),
                location=loc,
            ),
            raw_text,
        )

    return StructuredJobFields(company=company_hint), ""


def fetch_ats_posting(url: str) -> tuple[str, StructuredJobFields, str] | None:
    """Try public ATS API for a recognized URL. Returns (provider, fields, raw_text) or None."""
    match = detect_ats_url(url)
    if not match or match.provider == "workday":
        return None
    try:
        _, body, status_code = safe_fetch_url(match.api_url, timeout_seconds=10.0)
        if status_code != 200:
            return None
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        fields, raw_text = parse_ats_json_payload(
            match.provider, payload, match.company, match.job_id
        )
        if fields.title or raw_text:
            return match.provider, fields, raw_text
    except Exception:
        return None
    return None
