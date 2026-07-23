.PHONY: help bootstrap doctor up status down logs verify autostart-install autostart-status autostart-logs autostart-restart autostart-uninstall db-backup db-init db-verify test

DATABASE_URL ?= postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai

help:
	@python3 scripts/local_runtime.py doctor --json >/dev/null || true
	@printf '%s\n' 'Local runtime: make bootstrap | doctor | up | status | logs | verify | down'
	@printf '%s\n' 'macOS autostart: make autostart-install | autostart-status | autostart-logs | autostart-restart | autostart-uninstall'
	@printf '%s\n' 'The managed runtime uses only local PostgreSQL database freqtrade_ai.'

bootstrap:
	python3 scripts/local_runtime.py bootstrap

doctor:
	python3 scripts/local_runtime.py doctor

up:
	python3 scripts/local_runtime.py up

status:
	python3 scripts/local_runtime.py status

down:
	python3 scripts/local_runtime.py down

logs:
	python3 scripts/local_runtime.py logs

verify:
	python3 scripts/local_runtime.py verify

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

test:
	cd backend && . .venv/bin/activate && pytest
