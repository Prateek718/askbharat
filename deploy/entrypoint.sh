#!/bin/bash
set -e

echo "[askbharat] Starting embedded PostgreSQL 16..."
export PGDATA="${PGDATA:-/var/lib/postgresql/data}"
export PORT="${PORT:-7860}"
export HOST="${HOST:-0.0.0.0}"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://askbharat:askbharat@127.0.0.1:5432/askbharat}"

# Start local PostgreSQL in the background
pg_ctl -D "$PGDATA" -l /var/log/postgresql/server.log -o "-p 5432" start

# Wait for PostgreSQL to accept connections
echo "[askbharat] Waiting for database to be ready..."
until pg_isready -h 127.0.0.1 -p 5432 -U askbharat > /dev/null 2>&1; do
    sleep 0.2
done
echo "[askbharat] Database is ready."

# Launch FastAPI ASGI server
echo "[askbharat] Starting AskBharat web server on $HOST:$PORT..."
exec uvicorn askbharat.web.app:app --host "$HOST" --port "$PORT" --workers "${WEB_CONCURRENCY:-1}"
