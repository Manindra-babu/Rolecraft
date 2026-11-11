import json
import time
from datetime import datetime, timezone
from typing import Optional, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger, scrub_secrets
from app.models.llm_cache import LLMCache
from app.models.user import User
from app.services.llm.base import (
    EmbeddingClient,
    LLMClient,
    LLMKeyInvalidError,
    LLMRateLimitError,
    LLMServiceError,
    PIIRedactor,
    compute_content_hash,
)

T = TypeVar("T", bound=BaseModel)


def validate_gemini_key(api_key: str) -> tuple[bool, Optional[str]]:
    """
    Make one lightweight call to Gemini to verify the key works and Gemini API is enabled.
    Returns (is_valid, error_message). Never logs or includes the raw key in errors.
    """
    settings = get_settings()
    cleaned = api_key.strip()
    if not cleaned or len(cleaned) < 15:
        return (
            False,
            "This key was rejected by Google. Check that you copied it fully and that the Gemini API is enabled.",
        )
    try:
        client = genai.Client(api_key=cleaned)
        client.models.generate_content(
            model=settings.GEMINI_FAST_MODEL,
            contents="Reply with OK.",
            config=types.GenerateContentConfig(max_output_tokens=4, temperature=0.0),
        )
        return True, None
    except Exception as exc:
        safe_msg = scrub_secrets(str(exc), extra_secrets=[cleaned])
        logger.warning("Gemini key validation failed: %s", safe_msg[:200])
        return (
            False,
            "This key was rejected by Google. Check that you copied it fully and that the Gemini API is enabled.",
        )


def _record_user_call(db: Session, user: User) -> None:
    now = datetime.now(timezone.utc)
    month_key = now.strftime("%Y-%m")
    day_key = now.strftime("%Y-%m-%d")
    if user.gemini_calls_month_key != month_key:
        user.gemini_calls_month = 0
        user.gemini_calls_month_key = month_key
    if user.gemini_calls_day_key != day_key:
        user.gemini_calls_today = 0
        user.gemini_calls_day_key = day_key
    user.gemini_calls_month += 1
    user.gemini_calls_today += 1
    db.add(user)
    db.commit()


def _check_daily_limit(user: User, daily_limit: int) -> None:
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls_today = user.gemini_calls_today if user.gemini_calls_day_key == today_key else 0
    if calls_today >= daily_limit:
        raise LLMRateLimitError(
            f"Daily Gemini usage limit ({daily_limit} calls) reached for your account. Try again tomorrow."
        )


class GeminiLLMClient(LLMClient):
    """Gemini client using the user's decrypted API key with caching, backoff, and Pydantic validation."""

    def __init__(self, *, api_key: str, db: Session, user: User) -> None:
        self._api_key = api_key
        self._db = db
        self._user = user
        self._settings = get_settings()
        self._client = genai.Client(api_key=api_key)

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
        if len(safe_prompt) > self._settings.LLM_MAX_INPUT_CHARS:
            safe_prompt = safe_prompt[: self._settings.LLM_MAX_INPUT_CHARS]

        model_name = (
            self._settings.GEMINI_QUALITY_MODEL
            if tier == "quality"
            else self._settings.GEMINI_FAST_MODEL
        )

        cache_key = compute_content_hash(
            model_name, operation, f"{system_instruction or ''}\n{safe_prompt}", schema.__name__
        )
        cached = (
            self._db.query(LLMCache).filter(LLMCache.cache_key == cache_key).first()
        )
        if cached is not None:
            try:
                return schema.model_validate(cached.response_json)
            except ValidationError:
                pass

        _check_daily_limit(self._user, self._settings.PER_USER_DAILY_LLM_LIMIT)

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                cfg_kwargs: dict = {
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                    "temperature": 0.2 if tier == "fast" else 0.35,
                }
                if system_instruction:
                    cfg_kwargs["system_instruction"] = system_instruction

                resp = self._client.models.generate_content(
                    model=model_name,
                    contents=safe_prompt,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                raw_text = (resp.text or "").strip()
                parsed = schema.model_validate_json(raw_text)

                _record_user_call(self._db, self._user)
                est_tokens = max(1, (len(safe_prompt) + len(raw_text)) // 4)
                logger.info(
                    "LLM call succeeded operation=%s model=%s est_tokens=%d",
                    operation,
                    model_name,
                    est_tokens,
                )

                cache_row = LLMCache(
                    cache_key=cache_key,
                    model_name=model_name,
                    operation=operation,
                    response_json=parsed.model_dump(mode="json"),
                    estimated_tokens=est_tokens,
                )
                self._db.merge(cache_row)
                self._db.commit()
                return parsed

            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning("Structured output validation retry (%d) for %s", attempt + 1, operation)
                safe_prompt = (
                    safe_prompt
                    + "\n\nIMPORTANT: Return ONLY valid JSON matching the requested schema."
                )
            except Exception as exc:
                msg = scrub_secrets(str(exc), extra_secrets=[self._api_key])
                lower_msg = msg.lower()
                if any(tok in lower_msg for tok in ("401", "403", "api key", "permission_denied", "invalid")):
                    self._user.gemini_key_status = "needs_attention"
                    self._db.add(self._user)
                    self._db.commit()
                    raise LLMKeyInvalidError(
                        "Your Gemini key needs attention. Please verify it in Profile -> API Key."
                    ) from None
                if "429" in lower_msg or "resource_exhausted" in lower_msg or "quota" in lower_msg:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(1.5 * (2**attempt))
                        continue
                    raise LLMRateLimitError(
                        "High demand on your Gemini key, retrying in a moment. Please try again shortly."
                    ) from None
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue

        safe_err = scrub_secrets(str(last_error or "Unknown LLM error"), extra_secrets=[self._api_key])
        raise LLMServiceError(f"LLM generation failed for {operation}: {safe_err[:200]}")


class GeminiEmbeddingClient(EmbeddingClient):
    """Gemini embedding client using the user's decrypted API key."""

    def __init__(self, *, api_key: str, db: Session, user: User) -> None:
        self._api_key = api_key
        self._db = db
        self._user = user
        self._settings = get_settings()
        self._client = genai.Client(api_key=api_key)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        _check_daily_limit(self._user, self._settings.PER_USER_DAILY_LLM_LIMIT)
        try:
            resp = self._client.models.embed_content(
                model=self._settings.GEMINI_EMBEDDING_MODEL,
                contents=texts,
            )
            _record_user_call(self._db, self._user)
            return [list(emb.values) for emb in resp.embeddings]
        except Exception as exc:
            msg = scrub_secrets(str(exc), extra_secrets=[self._api_key])
            if any(tok in msg.lower() for tok in ("401", "403", "api key", "permission_denied")):
                self._user.gemini_key_status = "needs_attention"
                self._db.add(self._user)
                self._db.commit()
                raise LLMKeyInvalidError(
                    "Your Gemini key needs attention. Please verify it in Profile -> API Key."
                ) from None
            raise LLMServiceError(f"Embedding generation failed: {msg[:200]}") from None
