# ResumeIQ

**ResumeIQ** is an evidence-based AI resume intelligence platform for students, fresh graduates, and job seekers. Upload your `.docx` resume, optionally connect a public GitHub profile, and paste a job posting URL to get an explainable compatibility breakdown, five-category skill gap roadmap, verified GitHub project recommendations, and an in-place edited `.docx` + `.pdf` export that preserves your original formatting.

---

## Core Principles

1. **Truthfulness first:** An automated claim validator inspects every generated bullet and project description against evidence from your resume, Master Profile, analyzed GitHub repositories, and explicitly confirmed learning. Unverified metrics, tools, or inflated leadership verbs are blocked or flagged before review.
2. **User control:** Every edit is reviewed side-by-side (Accept / Reject / Edit). Bulk-accept is restricted to low-risk reordering.
3. **Format preservation:** Edits happen in place at the `python-docx` / OOXML paragraph and run level (`rPr`, `pPr`, `numPr`, tables).
4. **Bring-your-own Gemini key:** Each user adds and verifies their own Google AI Studio Gemini API key in **Profile → API Key**. Keys are encrypted at rest with Fernet (`ENCRYPTION_KEY`) and scrubbed from all logs.
5. **Privacy by default:** Phone numbers and street addresses are redacted before LLM rewrite calls, and you can export or permanently delete all your data from **Settings**.

> **Gemini Free-Tier Notice:** Your key is stored encrypted and used only to process your own resumes. Requests go to Google under your own Gemini account and its terms, so free-tier keys may be subject to Google's data-use policy. Use test data with free-tier keys or switch to a paid-tier Gemini key for sensitive resumes.

---

## Architecture

```mermaid
flowchart LR
  Web["Next.js App Router (apps/web)"] -->|"REST + JWT"| API["FastAPI Backend (apps/api)"]
  Ext["Browser Extension MV3 (apps/extension)"] -->|"Capture Job Page"| API
  API --> DB["PostgreSQL / SQLite"]
  API --> Redis["Redis + RQ Worker"]
  API -->|"In-place OOXML + LibreOffice"| Docx["DOCX / PDF Engine"]
  API -->|"User's Decrypted Gemini Key"| Gemini["Google Gemini API"]
  API -->|"Server GITHUB_TOKEN + ETag"| GitHub["GitHub REST API"]
```

---

## Monorepo Layout

```text
ResumeBuilder/
  apps/
    web/            # Next.js (App Router, TypeScript, Tailwind CSS, TanStack Query, RHF + Zod)
    api/            # FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, python-docx, PyMuPDF, extruct, trafilatura
    extension/      # Chrome/Edge Manifest V3 extension ("Add to ResumeIQ")
  docs/
    decisions.md
    setup-checklist.md
    supported-layouts.md
  docker-compose.yml
  .env.example
  Makefile
```

---

## Quick Start (Docker Compose)

1. Copy `.env.example` to `.env` and fill in:
   - `SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
   - `ENCRYPTION_KEY` — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `DATABASE_URL` — `postgresql+psycopg://resumeiq:resumeiq@postgres:5432/resumeiq` (automatically set inside `docker-compose.yml`)
   - `GITHUB_TOKEN` — optional read-only GitHub personal access token (no private scopes) for 5,000 req/hr public repo analysis
   - *(No global `GEMINI_API_KEY` is required on the server.)*

2. Start the entire stack:
   ```bash
   docker compose up --build
   ```

3. Open `http://localhost:3000`, register an account, and add your Gemini API key in **Profile → API Key** (or set `DEMO_MODE=true` in `.env` for offline testing).

---

## Local Development (Without Docker)

```bash
make setup
make migrate
# Terminal 1:
make dev-api
# Terminal 2:
make dev-web
```

Run tests:
```bash
make test
```

---

## Supported Resume Layouts & Known Limitations

See [`docs/supported-layouts.md`](docs/supported-layouts.md) for the full matrix:
- **Single-column & Table-based `.docx`:** Full in-place editing preserving fonts, colors, sizes, bullet numbering, and tables.
- **Two-column & Text-box `.docx`:** Partial support; verify page/column breaks in the rendered PDF preview.
- **PDF uploads:** Analysis only. Exact-format editing requires `.docx`; ResumeIQ offers a clean ATS-friendly `.docx` export approximating your PDF content.
- **LibreOffice headless:** Included in the backend Docker image (`apps/api/Dockerfile`). If LibreOffice is absent on a local host machine, PDF preview falls back to PyMuPDF rendering.
