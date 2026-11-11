import hashlib
import re
from abc import ABC, abstractmethod
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMServiceError(Exception):
    """Base error for LLM client failures (scrubbed of secrets)."""


class LLMKeyInvalidError(LLMServiceError):
    """Raised when the user's API key is invalid, revoked, or quota-blocked."""


class LLMRateLimitError(LLMServiceError):
    """Raised when rate limits persist after exponential backoff."""


class PIIRedactor:
    """Redacts phone numbers and street addresses before sending text to external LLMs."""

    PHONE_PATTERN = re.compile(
        r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}"
    )
    ADDRESS_PATTERN = re.compile(
        r"\b\d{1,5}\s+[A-Za-z0-9.\-\s]{2,40}\s+"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Circle|Cir)\.?\b",
        re.IGNORECASE,
    )

    @classmethod
    def redact(cls, text: str) -> str:
        if not text:
            return text
        redacted = cls.PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
        redacted = cls.ADDRESS_PATTERN.sub("[REDACTED_ADDRESS]", redacted)
        return redacted


def compute_content_hash(model_name: str, operation: str, prompt: str, schema_name: str) -> str:
    payload = f"{model_name}:{operation}:{schema_name}:{prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LLMClient(ABC):
    """Provider-agnostic structured LLM interface."""

    @abstractmethod
    def generate_structured(
        self,
        *,
        prompt: str,
        schema: Type[T],
        operation: str,
        tier: str = "fast",
        system_instruction: Optional[str] = None,
        redact_pii: bool = True,
    ) -> T:
        """Generate structured JSON validated against a Pydantic schema."""


class EmbeddingClient(ABC):
    """Provider-agnostic text embedding interface."""

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for the given texts."""
