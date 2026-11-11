import logging
import re
from typing import Iterable

SECRET_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_GEMINI_KEY]"),
    (re.compile(r"sk-ant-[0-9A-Za-z\-_]{16,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)bearer\s+[0-9A-Za-z\-._~+/]+=*"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?[^\s'\",;]{8,}['\"]?"), r"\1=[REDACTED]"),
]


def scrub_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Remove API keys, tokens, and credentials from any log or error string."""
    if not text:
        return text
    cleaned = text
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for pattern, replacement in SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


class SecretScrubbingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    scrub_secrets(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: scrub_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("resumeiq")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
        )
        handler.setFormatter(formatter)
        handler.addFilter(SecretScrubbingFilter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


logger = configure_logging()
