import re
from app.schemas.job import StructuredJobFields

REJECT_PATTERNS = [
    (re.compile(r"sign in to (?:continue|view|apply)|log in to view", re.I), "Login wall detected"),
    (re.compile(r"verify you are human|cf-browser-verification|g-recaptcha|hcaptcha|turnstile", re.I), "Captcha or anti-bot challenge detected"),
    (re.compile(r"job (?:is )?no longer available|position has been (?:filled|closed)|this job has expired", re.I), "Job posting is expired or no longer available"),
    (re.compile(r"access denied|403 forbidden|enable javascript and cookies to continue", re.I), "Access blocked by site"),
]

JOB_SIGNAL_WORDS = (
    "responsibilities",
    "requirements",
    "qualifications",
    "experience",
    "skills",
    "engineering",
    "developer",
    "intern",
    "software",
    "apply",
    "role",
    "team",
)


def validate_extracted_job(
    raw_text: str,
    fields: StructuredJobFields,
) -> tuple[bool, float, str | None]:
    """
    Validate that extracted content represents a real, active job posting.
    Returns (is_valid, confidence_score, rejection_reason).
    """
    cleaned_text = (raw_text or "").strip()
    lower = cleaned_text.lower()

    for pattern, reason in REJECT_PATTERNS:
        if pattern.search(cleaned_text):
            # Only reject if it dominates or there is very little job substance
            if len(cleaned_text) < 1200 or not (fields.required_skills or fields.responsibilities):
                return False, 0.0, reason

    if len(cleaned_text) < 80 and not (fields.title and (fields.required_skills or fields.responsibilities)):
        return False, 0.1, "Insufficient job description text extracted."

    signal_hits = sum(1 for w in JOB_SIGNAL_WORDS if w in lower)
    score = 0.0
    if fields.title:
        score += 0.3
    if fields.company:
        score += 0.15
    if fields.required_skills:
        score += 0.25
    if fields.responsibilities:
        score += 0.2
    if signal_hits >= 3:
        score += 0.1
    score = round(min(1.0, score), 2)

    if not fields.title and signal_hits < 2:
        return False, score, "Page does not appear to contain a job posting."
    if score < 0.35:
        return False, score, "Extracted content lacks clear job title, skills, or responsibilities."

    return True, score, None
