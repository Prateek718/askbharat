#!/usr/bin/env bash
# Work the extraction queue to completion across several days.
#
# One day's quota (1000 requests, ~750 pages at the measured 1.33 requests per
# page) is spent in roughly three hours. The rest of the day is waiting for the
# window to roll at UTC midnight. This loop does that waiting, so the whole run
# is unattended: it survives quota exhaustion, and `extraction_queue` survives
# a reboot, so nothing is lost if the box goes down mid-run.
#
#   ./run_extraction.sh            # run until the queue is empty
#   tail -f data/extraction.log    # watch
set -u
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python
LOG=data/extraction.log

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG"; }

# A crash leaves rows in in_flight; they belong back in the queue before we
# start, or those pages are silently skipped for the rest of the run.
$PY -m askbharat.scripts.extract --reclaim >>"$LOG" 2>&1

while true; do
  pending=$($PY - <<'EOF' 2>/dev/null
from sqlalchemy import text
from askbharat.db.session import session_scope
with session_scope() as s:
    print(s.execute(text(
        "SELECT count(*) FROM extraction_queue WHERE status='pending'"
    )).scalar_one())
EOF
)
  if [ -z "${pending:-}" ]; then
    log "cannot reach the database — retrying in 5m"
    sleep 300
    continue
  fi
  if [ "$pending" -eq 0 ]; then
    log "queue empty — extraction complete"
    break
  fi

  log "starting pass — $pending pending"
  $PY -m askbharat.scripts.extract --workers 6 --batch 12 >>"$LOG" 2>&1
  log "pass finished"

  # The pass stopped because the daily cap is spent. Wait for the window to
  # roll over — but poll for it rather than sleeping the whole way in one call.
  #
  # A single `sleep 47812` looked equivalent and was not: `sleep` counts
  # CLOCK_MONOTONIC, which does not advance while the machine is suspended.
  # This box suspended for ~4 hours overnight, so a sleep due to end at 00:02
  # UTC was still running at 04:13 with a full day's quota sitting unused.
  # Polling asks the quota itself, so a suspend, a clock change or a manual
  # reset all resolve on the next check instead of hours later.
  log "daily cap spent — waiting for the quota window to roll over"
  while true; do
    remaining=$($PY - <<'EOF' 2>/dev/null
from askbharat.llm.limiter import DailyQuota
print(DailyQuota(cap=900).remaining)
EOF
)
    if [ "${remaining:-0}" -gt 20 ]; then
      log "quota available again (${remaining} requests) — resuming"
      break
    fi
    sleep 300
  done
done
