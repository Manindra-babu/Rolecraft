from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import decrypt_api_key
from app.models.user import User
from app.services.llm.anthropic_client import AnthropicLLMClient
from app.services.llm.base import EmbeddingClient, LLMClient
from app.services.llm.demo_client import DemoEmbeddingClient, DemoLLMClient
from app.services.llm.gemini_client import GeminiEmbeddingClient, GeminiLLMClient


def get_llm_client_for_user(db: Session, user: User) -> LLMClient:
    """Resolve the active LLMClient for a specific user, enforcing BYO Gemini key gating."""
    settings = get_settings()
    if settings.DEMO_MODE:
        return DemoLLMClient()

    if settings.LLM_PROVIDER == "anthropic":
        return AnthropicLLMClient()

    if not user.encrypted_gemini_key or user.gemini_key_status != "valid":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GEMINI_KEY_REQUIRED",
                "message": "Add a valid Gemini API key in Profile -> API Key to run analysis or generation.",
            },
        )

    decrypted_key = decrypt_api_key(user.encrypted_gemini_key)
    return GeminiLLMClient(api_key=decrypted_key, db=db, user=user)


def get_embedding_client_for_user(db: Session, user: User) -> EmbeddingClient:
    """Resolve the EmbeddingClient for a specific user, enforcing BYO Gemini key gating."""
    settings = get_settings()
    if settings.DEMO_MODE:
        return DemoEmbeddingClient()

    if not user.encrypted_gemini_key or user.gemini_key_status != "valid":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GEMINI_KEY_REQUIRED",
                "message": "Add a valid Gemini API key in Profile -> API Key to run semantic matching.",
            },
        )

    decrypted_key = decrypt_api_key(user.encrypted_gemini_key)
    return GeminiEmbeddingClient(api_key=decrypted_key, db=db, user=user)
