#!/usr/bin/env python3
"""Run the outstanding harvests one after another, never side by side.

This exists because of a specific failure. On 27 July the static harvest and the
myScheme browser harvest were started separately and ended up overlapping. Each
is comfortable alone on a 7 GB machine; together, with Chromium on one side and
twelve PDF-parsing threads on the other, they exhausted RAM and took the editor
down with them. Both harvests are resumable, so nothing was lost but the hours.

Sequential is not a compromise here — it is very nearly free. The two jobs are
bound by different things: the static harvest waits on dead hosts timing out,
myScheme waits on a site that throttles by serving empty pages. Neither is
CPU-bound, so running them together buys little even when it works.

Order is deliberate: the static harvest finishes in well under an hour, so the
long browser run starts from a quiet machine rather than sharing one.

Usage:
    python -m askbharat.scripts.harvest_all
    python -m askbharat.scripts.harvest_all --only myscheme
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from askbharat.config import DATA_DIR
from askbharat.memguard import available_mb

# How many pages one myScheme process handles before it is replaced. See
# `run_chunked` for why it is replaced at all.
#
# Sized from the measured leak, not picked round: the process grows ~4.5 MB per
# page, so 150 pages peaks near 700 MB — comfortable beside an editor and a
# browser on this machine, where 300 would have reached 1.4 GB. A chunk takes
# about twenty minutes at the observed rate and costs one browser launch, so
# the overhead of restarting this often is on the order of a tenth of a
# percent.
MYSCHEME_CHUNK = 150

JOBS = {
    "static": (
        ["-m", "askbharat.scripts.harvest_static", "--all", "--workers", "6"],
        DATA_DIR / "static_harvest.log",
    ),
    "myscheme": (
        ["-m", "askbharat.scripts.harvest_myscheme",
         "--limit", str(MYSCHEME_CHUNK), "--concurrency", "2"],
        DATA_DIR / "myscheme_harvest.log",
    ),
}


def run(name: str) -> int:
    argv, log_path = JOBS[name]
    print(f"\n=== {name} — {available_mb()} MB available — log: {log_path}",
          flush=True)
    t0 = time.time()
    # Append: these logs are the record of every run including the one that
    # died, and overwriting them would erase the evidence of where it stopped.
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n\n=== run started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"({available_mb()} MB available) ===\n")
        log.flush()
        rc = subprocess.call([sys.executable, *argv], stdout=log,
                             stderr=subprocess.STDOUT)
    print(f"=== {name} exited {rc} after {(time.time() - t0) / 60:.1f}m "
          f"— {available_mb()} MB available", flush=True)
    return rc


def myscheme_progress() -> tuple[int, int]:
    """(pages captured, slugs settled).

    Two numbers because they answer different questions. "Captured" is what the
    corpus gained and is what belongs in a report. "Settled" — captured plus
    confirmed-dead — is what decides whether the loop is still making progress,
    and it has to include the dead ones: a chunk that lands on a run of dead
    slugs advances the harvest without capturing a single page, and judging it
    by captures alone would stop the run with thousands of live schemes left.
    """
    path = DATA_DIR / "myscheme_pages.jsonl"
    if not path.exists():
        return 0, 0
    captured, settled = set(), set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok"):
                captured.add(rec["slug"])
                settled.add(rec["slug"])
            elif rec.get("error") == "not_found":
                settled.add(rec["slug"])
    return len(captured), len(settled)


def run_chunked(name: str, chunk: int) -> int:
    """Run a job repeatedly in fresh processes until it stops making progress.

    The myScheme harvest leaks about 4 MB per page and I could not find where.
    Three attempts missed: restarting the browser reclaimed 147 MB of a 2.3 GB
    process; restarting the whole Playwright connection reclaimed no more; and
    dropping the per-page HTML capture, which a synthetic benchmark said would
    cut retention from 4.75 MB to 0.28 MB per page, changed nothing on the real
    site. That benchmark was wrong because it fetched a `data:` URL, which
    issues no network requests and so never touched the `page.route("**/*")`
    interception that every real page hits dozens of times.

    So stop paying for a diagnosis. The harvest is resumable by design, every
    captured page is already on disk, and a fresh process starts at 22 MB. A
    process that exits every few hundred pages returns *everything* — heap,
    driver, browser — with no need to know which of them was holding it.

    The cost is a browser launch per chunk, about two seconds against the ten
    minutes a chunk takes. Progress is measured from the file rather than the
    exit code, so a chunk that dies still counts what it captured, and the loop
    stops when a whole chunk adds nothing.
    """
    while True:
        cap_before, set_before = myscheme_progress()
        rc = run(name)
        cap_after, set_after = myscheme_progress()
        captured, settled = cap_after - cap_before, set_after - set_before
        print(f"    chunk: +{captured} captured, +{settled} settled "
              f"({cap_after} pages held)", flush=True)
        if settled == 0:
            # Nothing advanced at all — everything is done, or something is
            # broken. Either way another chunk would only spin.
            print(f"=== {name}: no slug settled in a full chunk of {chunk}, "
                  f"stopping (rc={rc})", flush=True)
            return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(JOBS),
                    help="run a single job instead of the sequence")
    args = ap.parse_args()

    names = [args.only] if args.only else list(JOBS)
    for name in names:
        # myScheme runs in fresh processes per chunk; static does not need to,
        # its memory is bounded by the parse throttle and it finishes in an hour.
        rc = (run_chunked(name, MYSCHEME_CHUNK) if name == "myscheme"
              else run(name))
        if rc != 0:
            # Keep going. A non-zero exit is usually one harvest hitting a wall
            # the other does not care about, and both resume cleanly on a later
            # run — stopping the sequence here would only waste the machine's
            # night.
            print(f"  (continuing despite {name} rc={rc})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
