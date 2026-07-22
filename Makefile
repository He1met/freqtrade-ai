.PHONY: help bootstrap doctor demo-up dev-up status down logs verify backend-install backend-dev worker-dev frontend-install frontend-dev db-up db-down db-backup db-init db-verify test

MODE ?= demo

help:
	@python3 scripts/local_runtime.py doctor --mode demo --json >/dev/null || true
	@printf '%s\n' 'Local runtime: make bootstrap | doctor | demo-up | dev-up | status | logs | verify | down'
	@printf '%s\n' 'demo-up/dev-up manage backend, DB-backed worker, and frontend together.'
	@printf '%s\n' 'demo uses .freqtrade-ai/runtime/demo.sqlite3; dev requires FREQTRADE_AI_DEV_DATABASE_URL.'

bootstrap:
	python3 scripts/local_runtime.py bootstrap

doctor:
	python3 scripts/local_runtime.py doctor --mode "$(MODE)"

demo-up:
	python3 scripts/local_runtime.py demo-up

dev-up:
	python3 scripts/local_runtime.py dev-up --mode dev

status:
	python3 scripts/local_runtime.py status --mode "$(MODE)"

down:
	python3 scripts/local_runtime.py down

logs:
	python3 scripts/local_runtime.py logs

verify:
	python3 scripts/local_runtime.py verify --mode "$(MODE)"

backend-install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

backend-dev:
	cd backend && . .venv/bin/activate && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

worker-dev:
	cd backend && . .venv/bin/activate && python -m app.workers.deepseek_backtest_worker

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
