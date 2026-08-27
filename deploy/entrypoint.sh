#!/bin/bash
set -e

export PORT="${PORT:-8080}"
export HOST="${HOST:-0.0.0.0}"

# If DATABASE_URL is not set or points to localhost, start embedded PostgreSQL
if [ -z "$DATABASE_URL" ] || [[ "$DATABASE_URL" == *"localhost"* ]] || [[ "$DATABASE_URL" == *"127.0.0.1"* ]]; then
    export PGDATA="${PGDATA:-/var/lib/postgresql/data}"
    export DATABASE_URL="postgresql+psycopg://askbharat:askbharat@127.0.0.1:5432/askbharat"
    echo "[askbharat] Starting embedded PostgreSQL 16 on local loopback..."
    pg_ctl -D "$PGDATA" -l /var/log/postgresql/server.log -o "-p 5432" start
    until pg_isready -h 127.0.0.1 -p 5432 -U askbharat > /dev/null 2>&1; do
        sleep 0.2
    done
    echo "[askbharat] Local database is ready."
else
    echo "[askbharat] Using external database from DATABASE_URL."
fi

# Launch FastAPI ASGI server
echo "[askbharat] Starting AskBharat web server on $HOST:$PORT..."
exec uvicorn askbharat.web.app:app --host "$HOST" --port "$PORT" --workers "${WEB_CONCURRENCY:-1}"
