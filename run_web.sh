#!/usr/bin/env bash
# Start the site, replacing any previous instance.
#
# Kept as a script because a `pkill -f uvicorn ...` typed inline matches the
# invoking shell's own command line and kills the caller.
#
# HOST, PORT and WEB_CONCURRENCY come from the environment so this script and
# the container agree on one set of names. Defaults stay on loopback: a dev run
# should not become reachable from the network just because someone ran the
# convenience script.
cd "$(dirname "$0")" || exit 1
pkill -f 'uvicorn[ ]askbharat' 2>/dev/null
sleep 1
exec .venv/bin/uvicorn askbharat.web.app:app \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8077}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  "$@"
