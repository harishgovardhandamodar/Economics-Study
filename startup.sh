#!/usr/bin/env bash
# Start the Economics Cross-Domain Mapper.
#
#   ./startup.sh              # http://localhost:8304
#   PORT=9000 ./startup.sh    # somewhere else
#
# Binds to localhost on purpose: the API has no authentication, so exposing it
# beyond the loopback interface exposes the entire database.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8304}"
HOST="${HOST:-127.0.0.1}"
PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "No virtualenv. Run:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

"$PY" -c "import fastapi, sqlalchemy" 2>/dev/null || {
  echo "Installing dependencies…"
  .venv/bin/pip install -q -r requirements.txt
}

# Apply the additive schema migrations, and report whether there is anything to
# look at. Cheap, and it means a schema problem surfaces at startup rather than
# on the first request that happens to touch the new column.
"$PY" -c "from app.database import init_db; init_db(); print('schema ready')"

if [ "$#" -gt 0 ]; then
  exec "$PY" scripts/seed.py "$@"
fi

echo
echo "  GUI      http://localhost:${PORT}"
echo "  API docs http://localhost:${PORT}/openapi.json"
echo
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"