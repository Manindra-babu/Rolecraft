import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return _password_hasher.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        sub = payload.get("sub")
        return str(sub) if sub else None
    except jwt.PyJWTError:
        return None


def _get_fernet() -> Fernet:
    settings = get_settings()
    raw_key = settings.ENCRYPTION_KEY.encode("utf-8")
    try:
        return Fernet(raw_key)
    except Exception:
        # Derive a deterministic valid 32-byte Fernet key if ENCRYPTION_KEY is a passphrase
        digest = hashlib.sha256(raw_key).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(plaintext_key: str) -> str:
    """Encrypt a user's Gemini API key at rest using Fernet."""
    f = _get_fernet()
    return f.encrypt(plaintext_key.strip().encode("utf-8")).decode("utf-8")


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt a user's Gemini API key in memory only when needed."""
    f = _get_fernet()
    try:
        return f.decrypt(encrypted_key.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored API key") from exc


def mask_api_key(plaintext_key: str) -> str:
    """Return a safe masked preview (e.g., AIza••••Xy9Q) for UI display."""
    cleaned = plaintext_key.strip()
    if len(cleaned) <= 8:
        return "••••"
    return f"{cleaned[:4]}••••{cleaned[-4:]}"
