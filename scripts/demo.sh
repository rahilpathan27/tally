#!/usr/bin/env bash
# `make demo`: dependencies, services with seeded data, the console, then the guided tour.
# Everything stays running afterwards so the console can be explored; `make demo-stop` ends it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/.data/demo"
mkdir -p "$LOGS"
cd "$ROOT"

echo "→ dependencies (PostgreSQL x3, Redis, Redpanda, object store)"
docker compose up -d --wait >/dev/null

if ! curl -fs http://127.0.0.1:8000/health/live >/dev/null 2>&1; then
  echo "→ services (core, ledger, vault, risk, recon, back office, simulators) with demo data"
  rm -f .data/dev-stack.json
  nohup uv run python -m scripts.dev_stack >"$LOGS/services.log" 2>&1 &
  echo $! >"$LOGS/services.pid"
  for _ in $(seq 1 120); do [[ -s .data/dev-stack.json ]] && break; sleep 2; done
  [[ -s .data/dev-stack.json ]] || { echo "services did not start; see $LOGS/services.log"; exit 1; }
fi

if ! curl -fs http://127.0.0.1:3000/login >/dev/null 2>&1; then
  echo "→ console (Next.js)"
  (cd web/console && [[ -d node_modules ]] || npm ci --silent)
  nohup sh -c "cd web/console && npm run dev" >"$LOGS/console.log" 2>&1 &
  echo $! >"$LOGS/console.pid"
  for _ in $(seq 1 90); do curl -fs http://127.0.0.1:3000/login >/dev/null 2>&1 && break; sleep 2; done
fi

uv run python -m scripts.demo
echo "Logs: $LOGS. Stop with: make demo-stop"
