# Local Seeded Backend API Debugging

This workflow verifies that the frontend is reading backend API responses
instead of silently rendering controlled fallback data.

It is local-only debug data. It does not run Freqtrade, start dry-run trading,
start live trading, connect to an exchange, download K-lines, deploy services,
or place orders.

## Seed A Temporary SQLite Database

```bash
python3 scripts/seed_debug_mvp_data.py \
  --database-url sqlite+pysqlite:////tmp/freqtrade-ai-debug.sqlite
```

The script creates or updates the `debug_mvp_seed_payloads` table and inserts
deterministic fake payloads for the frontend MVP endpoints.

## Start Backend With The Same Database

```bash
cd backend
. .venv/bin/activate
DATABASE_URL=sqlite+pysqlite:////tmp/freqtrade-ai-debug.sqlite \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check one seeded endpoint:

```bash
curl http://127.0.0.1:8000/api/strategies
```

The response should contain `SeededBackendRsi001`.

## Start Frontend

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5174
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`.

Open `http://127.0.0.1:5174/freq-ui` or another frontend page. A successful
debug run shows the page source as `backend API`, hides the fallback notice, and
renders seeded markers such as `SeededBackendRsi001`,
`seeded-backend-api`, or `backend-seeded-sqlite-debug`.

## Failure Modes

- `404 Seeded frontend debug data is missing`: run the seed script or start the
  backend with the same `DATABASE_URL` used by the seed script.
- Frontend still shows fallback data: confirm the backend is running on port
  `8000` and the Vite server was restarted after the proxy config changed.
- Backend database connection error: use the temporary SQLite URL above, or make
  sure the configured `DATABASE_URL` is reachable.

## Safety Boundary

- No real API key, secret, or passphrase is required.
- Do not write real credentials into the seed database, config files, logs,
  reports, screenshots, docs, or tests.
- Seeded rows are fake display data only and are not trading evidence.
