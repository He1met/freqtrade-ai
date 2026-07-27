.PHONY: help bootstrap doctor up status down logs verify okx-demo-pin-account okx-demo-preflight okx-demo-canary okx-demo-e2e-offline okx-demo-e2e-controlled autostart-install autostart-status autostart-logs autostart-restart autostart-uninstall db-backup db-init db-verify db-attestation-harden test

DATABASE_URL ?= postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai

help:
	@backend/.venv/bin/python scripts/local_runtime.py doctor --json >/dev/null || true
	@printf '%s\n' 'Local runtime: make bootstrap | doctor | up | status | logs | verify | down'
	@printf '%s\n' 'OKX Demo onboarding: make okx-demo-pin-account (one-time; refuses overwrite)'
	@printf '%s\n' 'OKX Demo: make okx-demo-preflight (authenticated read-only; never submits orders)'
	@printf '%s\n' 'OKX Demo canary: make okx-demo-canary CANARY_FLAGS=--allow-demo-order'
	@printf '%s\n' 'OKX Demo E2E: make okx-demo-e2e-offline (controlled mode stays blocked until #449/#450 integration)'
	@printf '%s\n' 'macOS autostart: make autostart-install | autostart-status | autostart-logs | autostart-restart | autostart-uninstall'
	@printf '%s\n' 'The managed runtime uses only local PostgreSQL database freqtrade_ai.'
	@printf '%s\n' 'One-time peer-admin attestation ACL: make db-attestation-harden'

bootstrap:
	backend/.venv/bin/python scripts/local_runtime.py bootstrap

doctor:
	backend/.venv/bin/python scripts/local_runtime.py doctor

up:
	backend/.venv/bin/python scripts/local_runtime.py up

status:
	backend/.venv/bin/python scripts/local_runtime.py status

down:
	backend/.venv/bin/python scripts/local_runtime.py down

logs:
	backend/.venv/bin/python scripts/local_runtime.py logs

verify:
	backend/.venv/bin/python scripts/local_runtime.py verify

okx-demo-pin-account:
	backend/.venv/bin/python scripts/local_runtime.py okx-pin-account

okx-demo-preflight:
	backend/.venv/bin/python scripts/local_runtime.py okx-preflight

okx-demo-canary:
	backend/.venv/bin/python scripts/local_runtime.py okx-demo-canary $(CANARY_FLAGS)

okx-demo-e2e-offline:
	python3 scripts/okx_demo_e2e.py --mode offline-ci

okx-demo-e2e-controlled:
	python3 scripts/okx_demo_e2e.py --mode controlled-real $(E2E_FLAGS)

autostart-install:
	backend/.venv/bin/python scripts/macos_launch_agent.py install

autostart-status:
	backend/.venv/bin/python scripts/macos_launch_agent.py status

autostart-logs:
	backend/.venv/bin/python scripts/macos_launch_agent.py logs

autostart-restart:
	backend/.venv/bin/python scripts/macos_launch_agent.py restart

autostart-uninstall:
	backend/.venv/bin/python scripts/macos_launch_agent.py uninstall

db-backup:
	mkdir -p .freqtrade-ai/backups
	cd backend && . .venv/bin/activate && export DATABASE_URL="$(DATABASE_URL)"; psql_url=$$(python -c 'import os; from app.db.migrations import psql_database_url; print(psql_database_url(os.environ["DATABASE_URL"]))'); backup="../.freqtrade-ai/backups/freqtrade-ai-$$(date +%Y%m%d%H%M%S).sql"; pg_dump "$$psql_url" > "$$backup.tmp" && mv "$$backup.tmp" "$$backup"

db-init:
	cd backend && . .venv/bin/activate && python -m app.db.migrate upgrade --database-url "$(DATABASE_URL)"

db-verify:
	cd backend && . .venv/bin/activate && python -m app.db.migrate verify --database-url "$(DATABASE_URL)"

db-attestation-harden:
	cd backend && . .venv/bin/activate && DATABASE_URL="$(DATABASE_URL)" python ../scripts/harden_okx_demo_attestation.py

test:
	cd backend && . .venv/bin/activate && pytest
