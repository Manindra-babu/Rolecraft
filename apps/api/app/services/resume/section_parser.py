import re
from typing import Any

from app.schemas.resume import (
    ParsedEducationItem,
    ParsedExperienceItem,
    ParsedProjectItem,
    ParsedResumeStructure,
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:github\.com|linkedin\.com|[a-z0-9-]+\.[a-z]{2,})/\S+", re.IGNORECASE)
DATE_RANGE_RE = re.compile(
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\b\d{4})\s*[-–—to]+\s*(?:Present|Current|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\d{4})",
    re.IGNORECASE,
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "profile", "objective", "about me", "professional summary"),
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "internships",
    ),
    "projects": ("projects", "personal projects", "academic projects", "selected projects"),
    "education": ("education", "academic background", "academics"),
    "skills": ("skills", "technical skills", "core competencies", "technologies", "tools"),
    "achievements": ("achievements", "awards", "honors", "accomplishments"),
    "certifications": ("certifications", "licenses", "certificates", "credentials"),
}


def detect_section_heading(text: str) -> str | None:
    cleaned = re.sub(r"[:|\-–—•]+$", "", text.strip()).strip().lower()
    if not cleaned or len(cleaned.split()) > 5:
        return None
    for section, aliases in SECTION_ALIASES.items():
        if cleaned in aliases:
            return section
    return None


def strip_bullet_prefix(text: str) -> str:
    return re.sub(r"^[•\-–—*▪◦·]+\s*", "", text.strip()).strip()


def _split_title_company(line: str) -> tuple[str, str, str]:
    dates_match = DATE_RANGE_RE.search(line)
    dates = dates_match.group(0).strip() if dates_match else ""
    without_dates = DATE_RANGE_RE.sub("", line).strip(" |•-–—,\t")
    if " | " in without_dates:
        parts = [p.strip() for p in without_dates.split("|", 1)]
        return parts[0], parts[1], dates
    if " at " in without_dates.lower():
        parts = re.split(r"\s+at\s+", without_dates, maxsplit=1, flags=re.IGNORECASE)
        return parts[0].strip(), parts[1].strip(), dates
    if " - " in without_dates or " — " in without_dates or " – " in without_dates:
        parts = re.split(r"\s+[-–—]\s+", without_dates, maxsplit=1)
        return parts[0].strip(), parts[1].strip(), dates
    return without_dates, "", dates


def parse_lines_to_structure(
    elements: list[dict[str, Any]],
) -> ParsedResumeStructure:
    """
    Deterministically map indexed resume elements into ParsedResumeStructure
    and annotate each element with its detected section.
    """
    structure = ParsedResumeStructure()
    current_section = "header"
    header_lines: list[dict[str, Any]] = []
    section_buckets: dict[str, list[dict[str, Any]]] = {
        "summary": [],
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
        "achievements": [],
        "certifications": [],
    }

    for el in elements:
        text = el["text"].strip()
        if not text:
            continue
        heading = detect_section_heading(text)
        if heading:
            current_section = heading
            el["section"] = heading
            el["is_heading"] = True
            if heading not in structure.sections_order:
                structure.sections_order.append(heading)
            continue

        el["section"] = current_section
        el["is_heading"] = False
        if current_section == "header":
            header_lines.append(el)
        elif current_section in section_buckets:
            section_buckets[current_section].append(el)

    # Parse header (contact info)
    full_header_text = "\n".join(el["text"] for el in header_lines)
    if header_lines:
        first_line = header_lines[0]["text"].strip()
        if not EMAIL_RE.search(first_line) and not URL_RE.search(first_line) and len(first_line.split()) <= 5:
            structure.full_name = first_line
    email_m = EMAIL_RE.search(full_header_text)
    if email_m:
        structure.email = email_m.group(0)
    phone_m = PHONE_RE.search(full_header_text)
    if phone_m:
        structure.phone = phone_m.group(0).strip()

    seen_urls: set[str] = set()
    for url_m in URL_RE.finditer(full_header_text):
        raw_url = url_m.group(0).rstrip(".,;)")
        if raw_url not in seen_urls:
            seen_urls.add(raw_url)
            label = "GitHub" if "github.com" in raw_url.lower() else (
                "LinkedIn" if "linkedin.com" in raw_url.lower() else "Website"
            )
            full_url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
            structure.links.append({"label": label, "url": full_url})

    for el in header_lines[1:]:
        line = el["text"]
        if "@" not in line and "http" not in line.lower() and "github.com" not in line.lower():
            if "," in line and len(line) < 60 and not structure.location:
                structure.location = line.strip(" |•")

    # Summary
    structure.summary = " ".join(el["text"].strip() for el in section_buckets["summary"]).strip()

    # Experience
    current_exp: ParsedExperienceItem | None = None
    for el in section_buckets["experience"]:
        raw = el["text"].strip()
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if is_bullet and current_exp is not None:
            current_exp.bullets.append(strip_bullet_prefix(raw))
            current_exp.element_ids.append(el["id"])
        else:
            if current_exp is not None and (current_exp.title or current_exp.company):
                structure.experience.append(current_exp)
            title, company, dates = _split_title_company(raw)
            current_exp = ParsedExperienceItem(
                title=title,
                company=company,
                dates=dates,
                element_ids=[el["id"]],
            )
    if current_exp is not None and (current_exp.title or current_exp.company):
        structure.experience.append(current_exp)

    # Projects
    current_proj: ParsedProjectItem | None = None
    for el in section_buckets["projects"]:
        raw = el["text"].strip()
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if is_bullet and current_proj is not None:
            current_proj.bullets.append(strip_bullet_prefix(raw))
            current_proj.element_ids.append(el["id"])
        else:
            if current_proj is not None and current_proj.name:
                structure.projects.append(current_proj)
            url_m = URL_RE.search(raw)
            proj_url = url_m.group(0) if url_m else ""
            cleaned = URL_RE.sub("", raw).strip(" |•-–—")
            name_part = cleaned
            tech_stack: list[str] = []
            if "|" in cleaned:
                parts = [p.strip() for p in cleaned.split("|", 1)]
                name_part = parts[0]
                tech_stack = [t.strip() for t in parts[1].split(",") if t.strip()]
            elif "(" in cleaned and cleaned.endswith(")"):
                idx = cleaned.rfind("(")
                name_part = cleaned[:idx].strip()
                tech_stack = [t.strip() for t in cleaned[idx + 1 : -1].split(",") if t.strip()]
            current_proj = ParsedProjectItem(
                name=name_part,
                tech_stack=tech_stack,
                url=proj_url,
                element_ids=[el["id"]],
            )
    if current_proj is not None and current_proj.name:
        structure.projects.append(current_proj)

    # Education
    current_edu: ParsedEducationItem | None = None
    for el in section_buckets["education"]:
        raw = el["text"].strip()
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if is_bullet and current_edu is not None:
            current_edu.details.append(strip_bullet_prefix(raw))
            current_edu.element_ids.append(el["id"])
        else:
            if current_edu is not None and (current_edu.institution or current_edu.degree):
                structure.education.append(current_edu)
            inst, deg, dates = _split_title_company(raw)
            current_edu = ParsedEducationItem(
                institution=inst,
                degree=deg,
                dates=dates,
                element_ids=[el["id"]],
            )
    if current_edu is not None and (current_edu.institution or current_edu.degree):
        structure.education.append(current_edu)

    # Skills
    skills_seen: set[str] = set()
    for el in section_buckets["skills"]:
        raw = strip_bullet_prefix(el["text"])
        if ":" in raw:
            raw = raw.split(":", 1)[1]
        for token in re.split(r"[,|•;/]+", raw):
            skill = token.strip()
            if skill and len(skill) <= 50 and skill.lower() not in skills_seen:
                skills_seen.add(skill.lower())
                structure.skills.append(skill)

    # Achievements & Certifications
    for el in section_buckets["achievements"]:
        cleaned = strip_bullet_prefix(el["text"])
        if cleaned:
            structure.achievements.append(cleaned)

    for el in section_buckets["certifications"]:
        cleaned = strip_bullet_prefix(el["text"])
        if cleaned:
            structure.certifications.append(cleaned)

    return structure
