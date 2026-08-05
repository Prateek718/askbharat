#!/usr/bin/env bash
# Start the site on 127.0.0.1:8077, replacing any previous instance.
# Kept as a script because a `pkill -f uvicorn ...` typed inline matches the
# invoking shell's own command line and kills the caller.
cd "$(dirname "$0")" || exit 1
pkill -f 'uvicorn[ ]askbharat' 2>/dev/null
sleep 1
exec .venv/bin/uvicorn askbharat.web.app:app --host 127.0.0.1 --port "${PORT:-8077}" "$@"
