.PHONY: backend-install backend-dev frontend-install frontend-dev db-up db-down db-backup db-init db-verify test

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

db-backup:
	mkdir -p tmp/db-backups
	cd backend && . .venv/bin/activate && psql_url=$$(python -c 'import os; from app.db.migrations import psql_database_url; print(psql_database_url(os.environ["DATABASE_URL"]))'); pg_dump "$$psql_url" > "../tmp/db-backups/freqtrade-ai-$$(date +%Y%m%d%H%M%S).sql"

db-init:
	cd backend && . .venv/bin/activate && python -m app.db.migrate upgrade --database-url "$${DATABASE_URL}"

db-verify:
	cd backend && . .venv/bin/activate && python -m app.db.migrate verify --database-url "$${DATABASE_URL}"

test:
	cd backend && . .venv/bin/activate && pytest
