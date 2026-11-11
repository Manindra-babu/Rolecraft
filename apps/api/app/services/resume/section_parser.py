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

    # Merge explicit link metadata attached to elements (e.g. PDF annotations / DOCX hyperlinks)
    for el in elements:
        for link in el.get("links") or []:
            l_url = (link.get("url") or "").strip()
            if not l_url or l_url.startswith("/") or "@" in l_url and not l_url.startswith("http"):
                continue
            if l_url not in seen_urls:
                seen_urls.add(l_url)
                low_u = l_url.lower()
                label = link.get("label") or (
                    "GitHub" if "github.com" in low_u else (
                        "LinkedIn" if "linkedin.com" in low_u else "Website"
                    )
                )
                structure.links.append({"label": label, "url": l_url})
                # Also extract GitHub profile link when a repo URL is present
                if "github.com/" in low_u:
                    gh_m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)", l_url, re.I)
                    if gh_m:
                        prof_url = f"https://github.com/{gh_m.group(1)}"
                        if prof_url not in seen_urls:
                            seen_urls.add(prof_url)
                            structure.links.append({"label": "GitHub", "url": prof_url})

    def _clean_glyphs(text: str) -> str:
        # Remove Unicode Private Use Area chars (e.g. U+F0xx Wingdings/Symbol bullets) & replacement boxes
        cleaned = re.sub(r"[\uE000-\uF8FF\ufffd\u25af]+", "", text or "")
        cleaned = cleaned.replace("\u202f", " ").replace("\xa0", " ")
        return re.sub(r"\s{2,}", " ", cleaned).strip()

    def _clean_skill_token(tok: str) -> str:
        s = _clean_glyphs(tok).strip(" •-–—*▪◦○▸►●,;:")
        # Strip trailing period from skill names (e.g. 'Python.' -> 'Python') without touching '.net'
        while s.endswith(".") and not s.lower().endswith(".."):
            s = s[:-1].strip()
        return s

    # Clean phone duplicates, spaced-caps name recovery, multi-line name merge, unheaded summary recovery
    if structure.full_name and len(header_lines) >= 2:
        second = _clean_glyphs(header_lines[1].get("text", ""))
        if (
            second
            and second.lower() not in structure.full_name.lower()
            and "@" not in second
            and not re.search(r"\d", second)
            and "," not in second
            and "linkedin" not in second.lower()
            and "http" not in second.lower()
            and 1 <= len(second.split()) <= 3
        ):
            structure.full_name = f"{structure.full_name} {second}".strip()

    if not structure.full_name and header_lines:
        name_parts: list[str] = []
        for el in header_lines[:3]:
            t = _clean_glyphs(el.get("text", ""))
            if not t or "@" in t or re.search(r"\d{5,}", t) or "linkedin" in t.lower() or "http" in t.lower():
                continue
            if len(t.split()) >= 12 or len(t) > 65:
                continue
            # Collapse spaced uppercase letters like 'C H A P PI DI V E N K ATA' -> 'CHAPPIDI VENKATA'
            if re.search(r"\b(?:[A-Z]\s+){2,}[A-Z]", t):
                # Re-join single-letter tokens while preserving multi-letter word boundaries
                chunks = re.split(r"\s{2,}", t.strip())
                t = " ".join(re.sub(r"\b([A-Z])\s+(?=[A-Z]\b)", r"\1", c).replace(" ", "") if len(c.replace(" ", "")) <= 16 and re.search(r"(?:[A-Z]\s){2,}", c) else c for c in chunks)
                t = re.sub(r"(?<=[A-Z])\s+(?=[A-Z](?:\s|$))", "", t)
            name_parts.append(t)
        if name_parts:
            candidate_name = " ".join(name_parts).strip()
            # Format overly ALL CAPS names gently to Title Case while preserving initials
            if candidate_name.isupper():
                candidate_name = candidate_name.title()
            structure.full_name = candidate_name

    # Re-extract location without duplicating phone/email/links, and capture unheaded summary paragraph
    structure.location = ""
    extra_summary_parts: list[str] = []
    for el in header_lines:
        line = _clean_glyphs(el.get("text", ""))
        if not line:
            continue
        if len(line) >= 55 and "@" not in line and not re.search(r"\+?\d[\d\s\-]{7,}", line):
            extra_summary_parts.append(line)
            el["section"] = "summary"
            continue
        # Remove phone, email, and link tokens before checking location
        cleaned_loc = line
        if structure.phone:
            cleaned_loc = cleaned_loc.replace(structure.phone, "")
        cleaned_loc = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", cleaned_loc)
        cleaned_loc = re.sub(r"\b(?:LinkedIn|GitHub|Portfolio|Website)\b", "", cleaned_loc, flags=re.I)
        cleaned_loc = re.sub(r"\+?\d[\d\s\-().]{7,}\d", "", cleaned_loc)
        segments = [seg.strip(" |•·,-") for seg in re.split(r"[|•·]", cleaned_loc) if seg.strip(" |•·,-")]
        for seg in segments:
            if "," in seg and len(seg) < 55 and not structure.location:
                if not structure.full_name or seg.lower() not in structure.full_name.lower():
                    structure.location = seg

    # Summary
    summary_texts = [_clean_glyphs(el["text"]) for el in section_buckets["summary"] if _clean_glyphs(el["text"])]
    if not summary_texts and extra_summary_parts:
        summary_texts = extra_summary_parts
    structure.summary = " ".join(summary_texts).strip()
    if structure.summary and "summary" not in structure.sections_order:
        structure.sections_order.insert(0, "summary")

    # Helper: detect role/company/date lines vs description bullets in experience/projects/education
    DATE_RANGE_RE = re.compile(
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\b20\d{2}\b|\b19\d{2}\b|present|current)",
        re.I,
    )

    def _looks_like_subheading(el: dict[str, Any], raw: str) -> bool:
        if el.get("is_heading_sub"):
            return True
        words = raw.split()
        if "|" in raw and (DATE_RANGE_RE.search(raw) or len(words) <= 10):
            return True
        if raw.isupper() and 2 <= len(words) <= 8 and not raw.endswith("."):
            return True
        if len(words) <= 8 and not raw.endswith(".") and not re.match(r"^(Architecture|Optimization|Deployment|Built|Developed|Collaborated|Engineered|Implemented|Designed|Created|Participated|Top\s+\d+|Gained)\b", raw, re.I):
            return True
        return False

    # Experience
    current_exp: ParsedExperienceItem | None = None
    for el in section_buckets["experience"]:
        raw = _clean_glyphs(el["text"])
        if not raw:
            continue
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if is_bullet and current_exp is not None:
            cleaned_b = strip_bullet_prefix(_clean_glyphs(raw))
            if cleaned_b:
                current_exp.bullets.append(cleaned_b)
                current_exp.element_ids.append(el["id"])
        elif current_exp is not None and (not current_exp.dates and DATE_RANGE_RE.search(raw) and "|" in raw):
            # Line immediately following role title: Company | Location Dates
            comp_raw = DATE_RANGE_RE.sub("", raw)
            title2, company2, dates2 = _split_title_company(raw)
            current_exp.dates = dates2 or current_exp.dates
            if "|" in raw:
                left_side = re.sub(r"\s+(?:May|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4}).*$", "", raw, flags=re.I).strip()
                parts = [p.strip() for p in left_side.split("|") if p.strip()]
                if parts:
                    current_exp.company = parts[0]
                if len(parts) > 1:
                    current_exp.location = parts[1]
            else:
                current_exp.company = current_exp.company or title2 or company2
            current_exp.element_ids.append(el["id"])
        elif current_exp is not None and not _looks_like_subheading(el, raw):
            cleaned_b = strip_bullet_prefix(raw)
            if cleaned_b:
                current_exp.bullets.append(cleaned_b)
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
        raw = _clean_glyphs(el["text"])
        if not raw:
            continue
        el_links = [lk.get("url", "") for lk in (el.get("links") or []) if "github.com" in (lk.get("url") or "").lower()]
        has_colon_prefix = bool(re.match(r"^[A-Z][A-Za-z &/+]{1,28}:\s", raw))
        is_proj_header = (
            not has_colon_prefix
            and bool(
                el_links
                or re.search(r"[-–]\s*github\b", raw, re.I)
                or (el.get("is_bold") and len(raw.split()) <= 10)
                or (raw.isupper() and len(raw.split()) <= 10)
            )
        )
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if (is_bullet or (current_proj is not None and not is_proj_header)) and current_proj is not None:
            cleaned_b = strip_bullet_prefix(raw)
            if cleaned_b:
                current_proj.bullets.append(cleaned_b)
                current_proj.element_ids.append(el["id"])
        else:
            if current_proj is not None and current_proj.name:
                structure.projects.append(current_proj)
            url_m = URL_RE.search(raw)
            proj_url = el_links[0] if el_links else (url_m.group(0) if url_m else "")
            cleaned = URL_RE.sub("", raw)
            cleaned = re.sub(r"\s*[-–|]?\s*github\s*$", "", cleaned, flags=re.I).strip(" |•-–—")
            name_part = cleaned
            tech_stack: list[str] = []
            if "|" in cleaned:
                parts = [p.strip() for p in cleaned.split("|", 1)]
                name_part = parts[0]
                tech_stack = [_clean_skill_token(t) for t in parts[1].split(",") if _clean_skill_token(t)]
            elif "(" in cleaned and cleaned.endswith(")"):
                idx = cleaned.rfind("(")
                name_part = cleaned[:idx].strip()
                tech_stack = [_clean_skill_token(t) for t in cleaned[idx + 1 : -1].split(",") if _clean_skill_token(t)]
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
        raw = _clean_glyphs(el["text"])
        if not raw:
            continue
        is_bullet = bool(el.get("is_bullet")) or raw.startswith(("•", "-", "–", "*"))
        if is_bullet and current_edu is not None:
            cleaned_d = strip_bullet_prefix(raw)
            if cleaned_d:
                current_edu.details.append(cleaned_d)
                current_edu.element_ids.append(el["id"])
        elif current_edu is not None and not current_edu.institution and ("|" in raw or DATE_RANGE_RE.search(raw)):
            inst2, deg2, dates2 = _split_title_company(raw)
            current_edu.institution = inst2 or deg2
            if deg2:
                current_edu.details.append(deg2)
            if dates2:
                current_edu.dates = dates2
            current_edu.element_ids.append(el["id"])
        elif current_edu is not None and (not _looks_like_subheading(el, raw) or current_edu.institution):
            # Lines below degree/institution line inside Education become details in original reading order
            cleaned_d = strip_bullet_prefix(raw)
            if cleaned_d:
                current_edu.details.append(cleaned_d)
                current_edu.element_ids.append(el["id"])
        elif current_edu is not None and re.match(r"^(?:Top\s+\d+|Participated|Gained|Cleared|Awarded)\b", raw, re.I):
            cleaned_d = strip_bullet_prefix(raw)
            if cleaned_d:
                current_edu.details.append(cleaned_d)
                current_edu.element_ids.append(el["id"])
        else:
            if current_edu is not None and (current_edu.institution or current_edu.degree):
                structure.education.append(current_edu)
            if re.match(r"^(?:Top\s+\d+|Participated|Gained|Cleared|Awarded)\b", raw, re.I):
                current_edu = ParsedEducationItem(
                    institution="",
                    degree="",
                    dates="",
                    details=[strip_bullet_prefix(raw)],
                    element_ids=[el["id"]],
                )
                continue
            inst, deg, dates = _split_title_company(raw)
            # If first line looks like a degree (e.g., Bachelor of Technology...), store in degree
            if re.search(r"\b(bachelor|master|b\.?tech|m\.?tech|b\.?s\.?|m\.?s\.?|ph\.?d|diploma)\b", raw, re.I) and not dates:
                current_edu = ParsedEducationItem(
                    institution="",
                    degree=raw,
                    dates="",
                    element_ids=[el["id"]],
                )
            else:
                current_edu = ParsedEducationItem(
                    institution=inst,
                    degree=deg,
                    dates=dates,
                    element_ids=[el["id"]],
                )
    if current_edu is not None and (current_edu.institution or current_edu.degree):
        structure.education.append(current_edu)

    # Skills (preserve category labels & strip PUA glyphs and trailing periods)
    skills_seen: set[str] = set()
    for el in section_buckets["skills"]:
        raw = strip_bullet_prefix(_clean_glyphs(el["text"]))
        if not raw:
            continue
        cat_label: str | None = None
        items_str = raw
        if ":" in raw:
            left, right = raw.split(":", 1)
            if 1 <= len(left.split()) <= 5 and right.strip():
                cat_label = left.strip(" •-–—")
                items_str = right.strip()
        tokens: list[str] = []
        for token in re.split(r"[,|•;/]+", items_str):
            skill = _clean_skill_token(token)
            if skill and len(skill) <= 50:
                tokens.append(skill)
                if skill.lower() not in skills_seen:
                    skills_seen.add(skill.lower())
                    structure.skills.append(skill)
        if cat_label and tokens:
            existing = structure.skill_categories.setdefault(cat_label, [])
            for t in tokens:
                if t.lower() not in {x.lower() for x in existing}:
                    existing.append(t)

    # Achievements & Certifications (preserve links metadata text cleanly)
    for el in section_buckets["achievements"]:
        cleaned = strip_bullet_prefix(_clean_glyphs(el["text"]))
        if cleaned:
            structure.achievements.append(cleaned)

    for el in section_buckets["certifications"]:
        cleaned = strip_bullet_prefix(_clean_glyphs(el["text"]))
        if cleaned:
            structure.certifications.append(cleaned)

    # Content-completeness check across source sections
    warnings: list[str] = []
    if section_buckets["summary"] and not structure.summary:
        warnings.append("Source Summary section produced empty parsed summary.")
    if section_buckets["experience"] and not structure.experience:
        warnings.append("Source Experience section could not be parsed.")
    if section_buckets["projects"] and not structure.projects:
        warnings.append("Source Projects section could not be parsed.")
    if section_buckets["education"] and not structure.education:
        warnings.append("Source Education section could not be parsed.")
    if section_buckets["skills"] and not structure.skills:
        warnings.append("Source Skills section could not be parsed.")
    if section_buckets["certifications"] and not structure.certifications:
        warnings.append("Source Certifications section could not be parsed.")
    structure.completeness_warnings = warnings

    return structure
