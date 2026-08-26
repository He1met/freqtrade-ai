.PHONY: help bootstrap doctor up status down logs verify operator-token-init operator-token-status okx-demo-pin-account okx-demo-preflight okx-demo-compatibility okx-demo-canary okx-demo-e2e-offline okx-demo-e2e-controlled natural-chain-preflight evaluator-receipt-preflight natural-risk-budget-preflight autostart-install autostart-status autostart-logs autostart-restart autostart-uninstall db-backup db-init db-verify db-attestation-harden db-reconciliation-compact-plan db-reconciliation-compact-apply db-reconciliation-compact-verify test-dev test-subsystem test-pg test-frontend test-milestone test

DATABASE_URL ?= postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai
CANONICAL_REPO ?= $(CURDIR)
BACKEND_PYTHON ?= $(CURDIR)/backend/.venv/bin/python

help:
	@backend/.venv/bin/python scripts/local_runtime.py doctor --json >/dev/null || true
	@printf '%s\n' 'Local runtime: make bootstrap | doctor | up | status | logs | verify | down'
	@printf '%s\n' 'Local authorization: make operator-token-init (one-time interactive Keychain prompt) | operator-token-status'
	@printf '%s\n' 'OKX Demo onboarding: make okx-demo-pin-account (one-time; refuses overwrite)'
	@printf '%s\n' 'OKX Demo: make okx-demo-preflight (authenticated read-only; never submits orders)'
	@printf '%s\n' 'OKX Demo compatibility: make okx-demo-compatibility (offline-first; uses the shared FREQTRADE_BINARY resolver)'
	@printf '%s\n' 'OKX Demo direct canary: permanently BLOCKED; canonical runtime one-shot grant owns any controlled canary'
	@printf '%s\n' 'OKX Demo E2E: make okx-demo-e2e-offline (controlled mode stays blocked until #449/#450 integration)'
	@printf '%s\n' 'Natural chain: make natural-chain-preflight (disposable real PostgreSQL; zero exchange/order access)'
	@printf '%s\n' 'Evaluator receipt: POSTGRES_WORKER_URL=... make evaluator-receipt-preflight (real PostgreSQL; zero order submission)'
	@printf '%s\n' 'Natural risk budget: POSTGRES_WORKER_URL=... make natural-risk-budget-preflight (real PostgreSQL; zero order submission)'
	@printf '%s\n' 'macOS autostart: make autostart-install | autostart-status | autostart-logs | autostart-restart | autostart-uninstall'
	@printf '%s\n' 'The managed runtime uses only local PostgreSQL database freqtrade_ai.'
	@printf '%s\n' 'One-time peer-admin attestation ACL: make db-attestation-harden'
	@printf '%s\n' 'Reconciliation maintenance: make down; make db-reconciliation-compact-plan; review; make db-reconciliation-compact-apply; make up; make db-reconciliation-compact-verify'
	@printf '%s\n' 'Tests: make test-dev TEST=... | test-subsystem TESTS="..." | test-pg TEST=... POSTGRES_TEST_URL=... | test-frontend | test-milestone'

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

operator-token-init:
	backend/.venv/bin/python scripts/local_runtime.py operator-token-init

operator-token-status:
	backend/.venv/bin/python scripts/local_runtime.py operator-token-status

okx-demo-pin-account:
	backend/.venv/bin/python scripts/local_runtime.py okx-pin-account

okx-demo-preflight:
	backend/.venv/bin/python scripts/local_runtime.py okx-preflight

okx-demo-compatibility:
	backend/.venv/bin/python scripts/okx_demo_compatibility.py

okx-demo-canary:
	backend/.venv/bin/python scripts/local_runtime.py okx-demo-canary $(CANARY_FLAGS)

okx-demo-e2e-offline:
	backend/.venv/bin/python scripts/okx_demo_e2e.py --mode offline-ci

okx-demo-e2e-controlled:
	backend/.venv/bin/python scripts/okx_demo_e2e.py --mode controlled-real $(E2E_FLAGS)

natural-chain-preflight:
	backend/.venv/bin/python scripts/okx_demo_natural_chain_preflight.py

evaluator-receipt-preflight:
	@test -n "$(POSTGRES_WORKER_URL)" || (printf '%s\n' 'POSTGRES_WORKER_URL is required' >&2; exit 2)
	@"$(CANONICAL_REPO)/backend/.venv/bin/python" "$(CANONICAL_REPO)/scripts/local_runtime.py" verify
	@cd backend && PYTHONPATH=. POSTGRES_WORKER_URL="$(POSTGRES_WORKER_URL)" \
		"$(CANONICAL_REPO)/backend/.venv/bin/python" -m pytest -q \
		tests/test_risk_chain_postgresql.py::test_postgresql_v44_owner_initializes_missing_natural_budget_once \
		tests/test_risk_chain_postgresql.py::test_postgresql_v44_reinstalls_repository_datetime_digest_contract \
		tests/test_okx_demo_execution_orchestrator.py::test_actionable_signal_completes_signal_then_risk_and_evaluation \
		tests/test_okx_demo_execution_orchestrator.py::test_signal_checkpoint_survives_expiry_and_new_fence_without_recapture \
		tests/test_strategy_deployment_repository.py::test_leased_signal_checkpoint_survives_expiry_and_is_immutable

natural-risk-budget-preflight:
	@test -n "$(POSTGRES_WORKER_URL)" || (printf '%s\n' 'POSTGRES_WORKER_URL is required' >&2; exit 2)
	@"$(CANONICAL_REPO)/backend/.venv/bin/python" "$(CANONICAL_REPO)/scripts/local_runtime.py" verify
	@cd backend && PYTHONPATH=. POSTGRES_WORKER_URL="$(POSTGRES_WORKER_URL)" \
		"$(CANONICAL_REPO)/backend/.venv/bin/python" -m pytest -q \
		tests/test_schema_migrations.py::test_schema_version_is_explicit_and_stable \
		tests/test_risk_chain_postgresql.py::test_postgresql_v44_owner_initializes_missing_natural_budget_once \
		tests/test_risk_chain_postgresql.py::test_postgresql_v43_upgrades_budget_initializer_and_acl_idempotently \
		tests/test_okx_demo_writer_postgresql.py::test_postgresql_v39_natural_risk_function_is_execute_only_and_fail_closed \
		tests/test_okx_demo_execution_orchestrator.py::test_actionable_signal_completes_signal_then_risk_and_evaluation

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
	DATABASE_URL="$(DATABASE_URL)" backend/.venv/bin/python scripts/postgres_backup.py

db-init:
	cd backend && . .venv/bin/activate && python -m app.db.migrate upgrade --database-url "$(DATABASE_URL)"

db-verify:
	cd backend && . .venv/bin/activate && python -m app.db.migrate verify --database-url "$(DATABASE_URL)"

db-attestation-harden:
	cd backend && . .venv/bin/activate && DATABASE_URL="$(DATABASE_URL)" python ../scripts/harden_okx_demo_attestation.py

db-reconciliation-compact-plan:
	backend/.venv/bin/python scripts/compact_okx_demo_reconciliation.py --database-url "$(DATABASE_URL)"

db-reconciliation-compact-apply:
	backend/.venv/bin/python scripts/compact_okx_demo_reconciliation.py --database-url "$(DATABASE_URL)" --apply --maintenance-stopped

db-reconciliation-compact-verify:
	backend/.venv/bin/python scripts/compact_okx_demo_reconciliation.py --database-url "$(DATABASE_URL)" --verify

test-dev:
	@test -n "$(TEST)" || (printf '%s\n' 'TEST is required, for example TEST=tests/test_health.py' >&2; exit 2)
	@test -x "$(BACKEND_PYTHON)" || (printf '%s\n' 'BACKEND_PYTHON is not executable; run make bootstrap or point it at a compatible venv' >&2; exit 2)
	cd backend && "$(BACKEND_PYTHON)" -m pytest -q $(TEST)
	git diff --check

test-subsystem:
	@test -n "$(TESTS)" || (printf '%s\n' 'TESTS is required, for example TESTS="tests/test_health.py tests/test_runtime_contract.py"' >&2; exit 2)
	@test -x "$(BACKEND_PYTHON)" || (printf '%s\n' 'BACKEND_PYTHON is not executable; run make bootstrap or point it at a compatible venv' >&2; exit 2)
	cd backend && "$(BACKEND_PYTHON)" -m pytest -q $(TESTS)
	git diff --check

test-pg:
	@test -n "$(TEST)" || (printf '%s\n' 'TEST is required' >&2; exit 2)
	@test -n "$(POSTGRES_TEST_URL)" || (printf '%s\n' 'POSTGRES_TEST_URL is required and must name an isolated temporary database' >&2; exit 2)
	@test -x "$(BACKEND_PYTHON)" || (printf '%s\n' 'BACKEND_PYTHON is not executable; run make bootstrap or point it at a compatible venv' >&2; exit 2)
	@case "$(POSTGRES_TEST_URL)" in *'/freqtrade_ai_v13'|*'/freqtrade_ai') printf '%s\n' 'Refusing a shared/runtime database; use an isolated temporary database' >&2; exit 2;; esac
	cd backend && POSTGRES_WORKER_URL="$(POSTGRES_TEST_URL)" CANONICAL_V13_POSTGRES_URL="$(POSTGRES_TEST_URL)" "$(BACKEND_PYTHON)" -m pytest -q $(TEST)
	git diff --check

test-frontend:
	cd frontend && npm test
	git diff --check

test-milestone:
	@test -x "$(BACKEND_PYTHON)" || (printf '%s\n' 'BACKEND_PYTHON is not executable; run make bootstrap or point it at a compatible venv' >&2; exit 2)
	cd backend && "$(BACKEND_PYTHON)" -m pytest
	python3 -m compileall backend/app backend/tests scripts
	cd frontend && npm test && npm run build
	git diff --check
	python3 scripts/scan_secrets.py

test: test-milestone
