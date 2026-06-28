.PHONY: backend-install backend-dev frontend-install frontend-dev db-up db-down db-init test

backend-install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

backend-dev:
	cd backend && . .venv/bin/activate && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

db-init:
	psql "$${DATABASE_URL:-postgresql://freqtrade:change_me@localhost:5432/freqtrade_ai}" -f db/migrations/001_init.sql

test:
	cd backend && . .venv/bin/activate && pytest
