import io
import logging

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import SecretScrubbingFilter, scrub_secrets
from app.core.security import decrypt_api_key
from app.models.user import User
from app.services.llm.base import PIIRedactor
from app.services.llm.factory import get_llm_client_for_user


def _register_user(client: TestClient, email: str, name: str = "Test User") -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "full_name": name, "password": "StrongPassword123!"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_register_login_and_me(client: TestClient) -> None:
    reg = _register_user(client, "alice@example.com", "Alice Dev")
    token = reg["access_token"]
    assert reg["user"]["email"] == "alice@example.com"
    assert reg["user"]["gemini_key_status"] == "missing"

    # Duplicate email rejected
    dup = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "full_name": "Alice", "password": "StrongPassword123!"},
    )
    assert dup.status_code == 409

    # Login succeeds
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "StrongPassword123!"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]

    # /me returns profile
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["full_name"] == "Alice Dev"


def test_gemini_key_encrypted_at_rest_masked_and_gated(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_key = "AIzaSyFakeKeyForTesting1234567890Xy9Q"
    reg = _register_user(client, "bob@example.com", "Bob Builder")
    token = reg["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Gate blocks analysis before key is saved
    gate_blocked = client.get("/api/v1/profile/analysis-gate-check", headers=headers)
    assert gate_blocked.status_code == 403
    assert gate_blocked.json()["detail"]["code"] == "GEMINI_KEY_REQUIRED"

    # Invalid key is rejected and NOT saved
    monkeypatch.setattr(
        "app.api.profile.validate_gemini_key",
        lambda k: (False, "This key was rejected by Google. Check that you copied it fully and that the Gemini API is enabled."),
    )
    bad_save = client.put("/api/v1/profile/api-key", headers=headers, json={"api_key": "AIzaBadKey"})
    assert bad_save.status_code == 400
    assert "rejected by Google" in bad_save.json()["detail"]

    # Valid key succeeds, is encrypted in DB, and never returned in full
    monkeypatch.setattr("app.api.profile.validate_gemini_key", lambda k: (True, None))
    ok_save = client.put("/api/v1/profile/api-key", headers=headers, json={"api_key": fake_key})
    assert ok_save.status_code == 200
    body = ok_save.json()
    assert body["has_key"] is True
    assert body["status"] == "valid"
    assert body["masked_key"] == "AIza••••Xy9Q"
    assert fake_key not in str(body)

    # Verify DB row has encrypted ciphertext, NOT plaintext
    user = db_session.query(User).filter(User.email == "bob@example.com").first()
    assert user is not None
    assert user.encrypted_gemini_key is not None
    assert fake_key not in user.encrypted_gemini_key
    assert decrypt_api_key(user.encrypted_gemini_key) == fake_key

    # Status endpoint never returns full key
    status_resp = client.get("/api/v1/profile/api-key", headers=headers)
    assert status_resp.status_code == 200
    assert fake_key not in status_resp.text

    # Gate now passes
    gate_ok = client.get("/api/v1/profile/analysis-gate-check", headers=headers)
    assert gate_ok.status_code == 200
    assert gate_ok.json()["allowed"] is True

    # Delete key re-locks gate
    del_resp = client.delete("/api/v1/profile/api-key", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["has_key"] is False
    gate_relocked = client.get("/api/v1/profile/analysis-gate-check", headers=headers)
    assert gate_relocked.status_code == 403


def test_user_key_isolation(client: TestClient, db_session: Session, monkeypatch) -> None:
    key_a = "AIzaSyUserAKey11111111111111111111AAAA"
    reg_a = _register_user(client, "usera@example.com", "User A")
    reg_b = _register_user(client, "userb@example.com", "User B")

    monkeypatch.setattr("app.api.profile.validate_gemini_key", lambda k: (True, None))
    save_a = client.put(
        "/api/v1/profile/api-key",
        headers={"Authorization": f"Bearer {reg_a['access_token']}"},
        json={"api_key": key_a},
    )
    assert save_a.status_code == 200

    # User B has no key — User B's gate and LLM factory must reject, never using User A's key
    gate_b = client.get(
        "/api/v1/profile/analysis-gate-check",
        headers={"Authorization": f"Bearer {reg_b['access_token']}"},
    )
    assert gate_b.status_code == 403

    user_a = db_session.query(User).filter(User.email == "usera@example.com").first()
    user_b = db_session.query(User).filter(User.email == "userb@example.com").first()
    assert user_a and user_b

    client_a = get_llm_client_for_user(db_session, user_a)
    assert getattr(client_a, "_api_key", None) == key_a

    try:
        get_llm_client_for_user(db_session, user_b)
        assert False, "Expected 403 for user_b without key"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403


def test_logs_never_contain_keys_and_pii_redacted() -> None:
    secret_key = "AIzaSySecretKeyNeverLogMe1234567890"
    scrubbed = scrub_secrets(f"Failed request with key={secret_key} and Bearer mysecrettoken123")
    assert secret_key not in scrubbed
    assert "mysecrettoken123" not in scrubbed

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretScrubbingFilter())
    test_logger = logging.getLogger("test_scrub")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.info("Error calling Gemini with %s", secret_key)
    assert secret_key not in stream.getvalue()

    redacted = PIIRedactor.redact("Call me at +1 (555) 234-5678 or visit 742 Evergreen Terrace Street.")
    assert "555" not in redacted
    assert "742 Evergreen" not in redacted
    assert "[REDACTED_PHONE]" in redacted


def test_demo_mode_bypasses_key_gate(client: TestClient, monkeypatch) -> None:
    reg = _register_user(client, "demo@example.com", "Demo User")
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    gate_ok = client.get(
        "/api/v1/profile/analysis-gate-check",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert gate_ok.status_code == 200
    assert gate_ok.json()["allowed"] is True
    get_settings.cache_clear()
