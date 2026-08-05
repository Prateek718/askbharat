#!/usr/bin/env python3
"""Harvest static HTML pages and binary documents from the service catalogue.

This closes the largest remaining gap. The catalogue's 10,645 service records
point almost entirely off-site, and until now the pipeline skipped every one of
them that was a PDF — 1,209 records, 11.4% of the corpus, and from their titles
("Application form for...", "Guidelines and application forms for...") among the
most useful documents we have.

What to expect, from a measured sample rather than hope:

- Roughly a third of URLs return content. The rest are the link rot the audit
  quantified at 39.6% overall; PDFs rot faster still.
- Of the PDFs that do fetch, ~90% carry a text layer and need nothing more than
  pypdf. The remainder are scans, recorded as `scanned_pdf` so the OCR gap stays
  countable instead of vanishing.

Resumable: results append to JSONL and a re-run skips URLs already attempted,
except HTTP errors and unreachable hosts, which are worth retrying later since
government hosts flap.

Usage:
    python -m askbharat.scripts.harvest_static --pdfs --limit 200
    python -m askbharat.scripts.harvest_static --all
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from askbharat.config import DATA_DIR
from askbharat.ingest.adapters.static import FetchStatus, StaticFetcher
from askbharat.memguard import available_mb, wait_for_memory

SERVICES = DATA_DIR / "services.jsonl"
OUT = DATA_DIR / "static_docs.jsonl"

# Statuses worth another attempt on a later run — government hosts flap, and a
# 503 today is often a 200 tomorrow. A scanned PDF or an office document will
# not change, so those are not retried.
RETRYABLE = {FetchStatus.HTTP_ERROR.value, FetchStatus.UNREACHABLE.value}


def load_targets(only_pdfs: bool, only_html: bool) -> list[dict]:
    seen, out = set(), []
    with SERVICES.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = (r.get("url") or "").strip()
            if not u.startswith("http") or u in seen:
                continue
            is_pdf = u.lower().endswith(".pdf")
            if only_pdfs and not is_pdf:
                continue
            if only_html and is_pdf:
                continue
            seen.add(u)
            out.append({"id": r.get("id"), "title": r.get("title"), "url": u})
    return out


def already_attempted() -> set[str]:
    if not OUT.exists():
        return set()
    done = set()
    with OUT.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") not in RETRYABLE:
                done.add(rec["url"])
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pdfs", action="store_true", help="binary documents only")
    ap.add_argument("--html", action="store_true", help="web pages only")
    # Six, not twelve. Twelve threads was fine for fetching and fatal for
    # parsing: each could be holding a 40 MB document while pdfplumber built
    # per-character objects over it, and a run at that width took the whole
    # machine down. Parsing is now capped separately inside the adapter, so the
    # only thing worker count buys is fetch parallelism on dead links — and six
    # already saturates that. Raise it only with the memory floor in view.
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel fetches; the fetcher serialises per host, "
                         "and the corpus spans 5,414 distinct hosts")
    ap.add_argument("--floor-mb", type=int, default=900,
                    help="pause new fetches when available memory drops below this")
    args = ap.parse_args()

    targets = load_targets(args.pdfs, args.html)
    done = already_attempted()
    todo = [t for t in targets if t["url"] not in done]
    if not args.all:
        todo = todo[:args.limit]

    print(f"{len(targets)} candidate URLs | {len(done)} already attempted "
          f"| {len(todo)} to fetch\n")
    if not todo:
        print("nothing to do")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    chars = 0
    tables = 0
    t0 = time.time()
    done_n = 0
    stalls = 0
    write_lock = threading.Lock()
    print(f"workers={args.workers}  memory floor={args.floor_mb} MB  "
          f"(available now: {available_mb()} MB)\n")

    # Concurrency is safe and polite here in a way it was not for myScheme:
    # these URLs span 5,414 distinct hosts, and the fetcher serialises per host,
    # so no single department server ever sees two simultaneous requests. Most
    # of the wall-clock is dead links timing out — ~40% of this corpus is rot —
    # and those waits parallelise perfectly.
    with StaticFetcher() as fetcher, OUT.open("a", encoding="utf-8") as out:

        def work(t: dict) -> None:
            nonlocal chars, tables, done_n, stalls
            # Backstop for everything this process does not control — the
            # browser harvest, an editor, a second job started by hand. Better
            # to idle a worker for a few seconds than to have the OOM killer
            # choose which process dies.
            if not wait_for_memory(args.floor_mb, timeout_s=90,
                                   on_wait=lambda mb: print(
                                       f"  [mem] {mb} MB free, waiting for "
                                       f"{args.floor_mb} MB", flush=True)):
                with write_lock:
                    stalls += 1
            d = fetcher.fetch(t["url"])
            record = {
                "id": t["id"], "title": t["title"], "url": d.url,
                "status": d.status.value, "kind": d.kind.value,
                "http_status": d.http_status, "content_hash": d.content_hash,
                "body_bytes": d.body_bytes, "pages": d.pages,
                "detail": d.detail, "text": d.text, "tables": d.tables,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
            with write_lock:
                counts[d.status.value] += 1
                kinds[d.kind.value] += 1
                if d.ok:
                    chars += len(d.text)
                    tables += len(d.tables)
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                done_n += 1
                if done_n % 100 == 0:
                    out.flush()
                    rate = done_n / max(time.time() - t0, 1e-9)
                    eta = (len(todo) - done_n) / max(rate, 1e-9) / 60
                    print(f"  {done_n:>6}/{len(todo)}  ok={counts['ok']} "
                          f"http_err={counts['http_error']} "
                          f"dead={counts['unreachable']} "
                          f"scan={counts['scanned_pdf']}  "
                          f"{rate:.1f}/s  eta {eta:.0f}m  "
                          f"mem {available_mb()}MB", flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, todo))

    dur = time.time() - t0
    n = sum(counts.values())
    print(f"\ndone in {dur / 60:.1f}m — {n} URLs")
    if stalls:
        print(f"  {stalls} fetches proceeded without clearing the memory floor "
              f"— the machine was under pressure from something else")
    print(f"\n  {'status':<18}{'n':>6}{'share':>8}")
    for k, v in counts.most_common():
        print(f"  {k:<18}{v:>6}{v / n * 100:>7.1f}%")
    print(f"\n  document kinds: {dict(kinds)}")
    if counts["ok"]:
        print(f"  text captured : {chars:,} chars "
              f"(~{chars // counts['ok']:,}/doc)")
        print(f"  markdown tables recovered: {tables}")
    reachable = counts["ok"] + counts["scanned_pdf"]
    if reachable:
        print(f"\n  of {reachable} reachable docs, "
              f"{counts['ok'] / reachable * 100:.0f}% had usable text; "
              f"{counts['scanned_pdf']} need OCR we do not run")
    print(f"  -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
