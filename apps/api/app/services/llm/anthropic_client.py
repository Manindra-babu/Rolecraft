from typing import Optional, Type, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import scrub_secrets
from app.services.llm.base import LLMClient, LLMServiceError, PIIRedactor

T = TypeVar("T", bound=BaseModel)


class AnthropicLLMClient(LLMClient):
    """Optional Anthropic provider selected via LLM_PROVIDER=anthropic."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        settings = get_settings()
        key = api_key or settings.ANTHROPIC_API_KEY
        if not key:
            raise LLMServiceError("ANTHROPIC_API_KEY is not configured.")
        self._key = key
        self._model = settings.ANTHROPIC_MODEL
        self._client = Anthropic(api_key=key)

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
        safe_prompt = PIIRedactor.redact(prompt) if redact_pii else prompt
        schema_json = schema.model_json_schema()
        full_prompt = (
            f"{safe_prompt}\n\n"
            f"Respond ONLY with valid JSON matching this JSON schema:\n{schema_json}"
        )
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system_instruction or "Return only valid JSON.",
                messages=[{"role": "user", "content": full_prompt}],
            )
            text = "".join(block.text for block in msg.content if hasattr(block, "text")).strip()
            return schema.model_validate_json(text)
        except Exception as exc:
            raise LLMServiceError(
                f"Anthropic generation failed: {scrub_secrets(str(exc), [self._key])[:200]}"
            ) from None
