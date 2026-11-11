.PHONY: setup test test-api test-web migrate reset-db dev-api dev-web

setup:
	cd apps/api && pip install -r requirements.txt
	cd apps/web && npm install
	@echo "Setup complete. Copy .env.example to .env and set SECRET_KEY and ENCRYPTION_KEY."

test: test-api test-web

test-api:
	cd apps/api && pytest -v

test-web:
	cd apps/web && npm test

migrate:
	cd apps/api && alembic upgrade head

reset-db:
	cd apps/api && alembic downgrade base && alembic upgrade head

dev-api:
	cd apps/api && uvicorn app.main:app --reload --port 8000

dev-web:
	cd apps/web && npm run dev
