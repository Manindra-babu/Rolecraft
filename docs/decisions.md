# Architectural & Ambiguity Decisions

This document records conservative, user-protective design decisions made during the development of ResumeIQ.

## Phase 0: Foundation & Security

1. **Bring-Your-Own Gemini Key (Addendum 2):**
   - Users must supply and validate their own Google Gemini API key in Profile -> API Key before running analysis or generation.
   - Keys are encrypted at rest with Fernet (`ENCRYPTION_KEY`) and decrypted strictly in memory when calling Gemini. Background workers receive only `user_id` and fetch/decrypt the key on demand.
   - Keys are masked (`AIza••••Xy9Q`) in all API responses and scrubbed from all logs and exception tracebacks.
   - No global `GEMINI_API_KEY` is required on the server. When `DEMO_MODE=true`, deterministic mock LLM responses can be used for offline evaluation/testing without external API calls.

2. **Model Naming & Configuration:**
   - Default model IDs (`GEMINI_FAST_MODEL=gemini-3.8-flash`, `GEMINI_QUALITY_MODEL=gemini-3.8-flash`, `GEMINI_EMBEDDING_MODEL=gemini-embedding-001`) are externalized in `.env` so users or operators can switch models without code edits.

3. **Auth & Optional OAuth:**
   - Email + password uses `argon2-cffi` for password hashing and HS256 JWT bearer tokens (plus HttpOnly-ready auth support).
   - Google OAuth is disabled unless `GOOGLE_CLIENT_ID` is configured.
   - GitHub OAuth and private repo access are not enabled; public GitHub repositories are analyzed using the server's `GITHUB_TOKEN`.

4. **Database & Queue Portability:**
   - Docker Compose runs PostgreSQL 16 + Redis 7 + RQ worker + LibreOffice headless.
   - Local development and unit test suites also support SQLite (`sqlite:///...`) and an inline task runner (`TASK_RUNNER=inline`) so every feature can be tested locally even when Docker is unavailable.

5. **PII Redaction Before LLM Calls:**
   - Phone numbers and street addresses are redacted via regex placeholders before sending resume text to external LLM providers.
