# ResumeIQ Setup Checklist

## 1. Environment Variables (`.env`)

Copy `.env.example` to `.env` at the repository root and set:

| Variable | Required | Where to get / generate it |
| --- | --- | --- |
| `SECRET_KEY` | **Yes** | Run `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ENCRYPTION_KEY` | **Yes** | Run `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | **Yes** | Use `postgresql+psycopg://resumeiq:resumeiq@postgres:5432/resumeiq` for Docker Compose, or `sqlite:///./resumeiq_dev.db` for local dev |
| `GITHUB_TOKEN` | Recommended | Create a read-only personal access token (no private scopes) at https://github.com/settings/tokens for 5,000 req/hr public repo analysis |
| `REDIS_URL` | Optional (Docker: Yes) | `redis://redis:6379/0` in Docker Compose; leave empty locally to use inline task execution |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional | Leave blank to disable Google OAuth (email + password is active by default) |

> **Note:** No global `GEMINI_API_KEY` is required on the server. Each user adds and verifies their own Gemini API key in **Profile → API Key**. Free-tier keys may be subject to Google's data-use policy; use test data or a paid-tier Gemini key for sensitive resumes.

## 2. Start with Docker Compose

```bash
docker compose up --build
```

- Frontend: `http://localhost:3000`
- Backend API & Swagger docs: `http://localhost:8000/docs`

## 3. Local Development (without Docker)

```bash
make setup
make migrate
# Terminal 1:
make dev-api
# Terminal 2:
make dev-web
```
