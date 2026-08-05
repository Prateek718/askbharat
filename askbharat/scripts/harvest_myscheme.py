#!/usr/bin/env python3
"""Harvest myScheme scheme detail pages.

This is the richest citizen-facing content in the corpus: Details, Benefits,
Eligibility, Application Process, Documents Required, FAQs and Sources for each
scheme — precisely the "answer page" india.gov.in never built.

Two measured facts shape this script:

- The pages are fully client-rendered and the backing API needs a credential we
  will not extract, so every page goes through a real browser.
- More than half the text is CSS-collapsed. A naive render captures the 25 FAQ
  *questions* and none of the answers; the tree-walk extractor in the rendered
  adapter recovers ~8,700 extra characters per page. Anything that harvests
  myScheme with plain `innerText` is silently dropping most of the value.

Resumable: every page is written to JSONL as it lands, and a re-run skips slugs
already captured. A 4,860-page browser harvest takes hours, so it must survive
being interrupted.

Usage:
    python -m askbharat.scripts.harvest_myscheme --limit 20     # sample
    python -m askbharat.scripts.harvest_myscheme --all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime

from askbharat.config import DATA_DIR
from askbharat.ingest.adapters.rendered import RenderedFetcher
from askbharat.memguard import available_mb, trim_heap

SCHEMES = DATA_DIR / "schemes.jsonl"
OUT = DATA_DIR / "myscheme_pages.jsonl"
BASE = "https://www.myscheme.gov.in/schemes/"

# The section headings a complete scheme page carries. Used to score coverage —
# a page missing "Documents Required" either genuinely lacks it or was captured
# too early, and we want to know which.
EXPECTED_SECTIONS = [
    "Details", "Benefits", "Eligibility", "Application Process",
    "Documents Required", "Frequently Asked Questions",
]


def load_slugs() -> list[dict]:
    if not SCHEMES.exists():
        print(f"missing {SCHEMES} — run harvest_schemes first", file=sys.stderr)
        return []
    seen, out = set(), []
    with SCHEMES.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = (r.get("slug") or "").strip()
            if slug and slug not in seen:
                seen.add(slug)
                out.append({"slug": slug, "title": r.get("title")})
    return out


def already_done() -> set[str]:
    """Slugs that need no further attempt: captured, or confirmed dead.

    `not_found` counts as settled. It always should have — the run summary has
    always called these "dead slugs, will not retry" — but while the harvest ran
    as one pass over the whole corpus the distinction was invisible: a dead slug
    simply failed again at the end of a run nobody repeated.

    Chunking made it load-bearing. Every chunk recomputes `todo` and takes the
    first N, so a slug that never becomes "done" sits at the front of the queue
    forever and is re-fetched by every chunk. At the observed 32% dead rate the
    dead set grows until a whole chunk is nothing but corpses, the chunk
    captures nothing, and the runner stops — having quietly skipped a couple of
    thousand live schemes while reporting success.

    Genuine failures stay retryable: those are myScheme throttling, which is
    transient, and they run at a few per hundred rather than a third.
    """
    if not OUT.exists():
        return set()
    done = set()
    with OUT.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok") or rec.get("error") == "not_found":
                done.add(rec["slug"])
    return done


def coverage(text: str) -> list[str]:
    return [s for s in EXPECTED_SECTIONS if s.lower() in text.lower()]


async def run(
    limit: int | None,
    concurrency: int,
    retry_failed: bool,
    floor_mb: int = 900,
    recycle_every: int = 50,
) -> int:
    slugs = load_slugs()
    if not slugs:
        return 1
    done = set() if retry_failed else already_done()
    todo = [s for s in slugs if s["slug"] not in done]
    if limit:
        todo = todo[:limit]

    print(f"{len(slugs)} schemes known | {len(done)} already captured "
          f"| {len(todo)} to fetch\n")
    if not todo:
        print("nothing to do")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    stats = {"ok": 0, "failed": 0, "thin": 0, "not_found": 0}
    section_hits = dict.fromkeys(EXPECTED_SECTIONS, 0)
    gained = 0

    # No wait_for: keying on a named section cost 28% of the corpus, because
    # not every scheme page has every section. The fetcher waits on content
    # volume instead. Concurrency stays low deliberately — myScheme throttles
    # by serving an empty SPA rather than a 429, so pushing harder silently
    # loses pages instead of erroring.
    with OUT.open("a", encoding="utf-8") as out:
      async with RenderedFetcher(
        concurrency=concurrency, timeout_ms=45_000
      ) as fetcher:
        batch = 8
        for i in range(0, len(todo), batch):
            # Between batches is the only safe moment to restart the browser —
            # nothing is in flight. A run this long otherwise ends with Chromium
            # holding more memory than the rest of the machine has left.
            batch_n = i // batch
            low = available_mb() < floor_mb
            if low or (batch_n and batch_n % recycle_every == 0):
                why = "under floor" if low else "scheduled"
                before = available_mb()
                print(f"  [mem] {before} MB free — recycling browser ({why})",
                      flush=True)
                await fetcher.recycle()
                # Report the delta, not just the new number. A recycle that
                # reclaims nothing means the memory is being held somewhere
                # this does not reach, and that is worth seeing in the log
                # rather than inferring afterwards.
                print(f"  [mem] {available_mb()} MB free after recycle "
                      f"(reclaimed {available_mb() - before} MB)", flush=True)

            chunk = todo[i:i + batch]
            pages = await fetcher.fetch_many([BASE + s["slug"] for s in chunk])
            for meta, page in zip(chunk, pages, strict=True):
                secs = coverage(page.text) if page.ok else []
                for s in secs:
                    section_hits[s] += 1
                if page.ok:
                    stats["ok"] += 1
                    gained += len(page.text) - page.text_len_visible_only
                    if len(secs) < 3:
                        stats["thin"] += 1
                elif page.error == "not_found":
                    stats["not_found"] += 1
                else:
                    stats["failed"] += 1

                out.write(json.dumps({
                    "slug": meta["slug"],
                    "title": meta["title"],
                    "url": page.url,
                    "ok": page.ok,
                    "status": page.status,
                    "error": page.error,
                    "content_hash": page.content_hash,
                    "text": page.text,
                    "text_len_visible_only": page.text_len_visible_only,
                    "sections_found": secs,
                    "links": page.links[:40],
                    "fetched_at": datetime.now(UTC).isoformat(),
                }, ensure_ascii=False) + "\n")
            out.flush()
            # The batch's pages are written and no longer needed. Releasing
            # them inside Python is not enough — glibc holds the freed blocks
            # unless asked to give them back, and this loop cycles a megabyte
            # of text per page for thousands of pages.
            del pages
            trim_heap()

            n = stats["ok"] + stats["failed"] + stats["not_found"]
            rate = n / max(time.time() - t0, 1e-9)
            eta = (len(todo) - n) / rate / 60 if rate else 0
            print(f"  {n:>5}/{len(todo)}  ok={stats['ok']} fail={stats['failed']} "
                  f"404={stats['not_found']}  {rate:.2f}/s  eta {eta:.0f}m  "
                  f"mem {available_mb()}MB", flush=True)

    dur = time.time() - t0
    print(f"\ndone in {dur / 60:.1f}m")
    print(f"  captured   : {stats['ok']}")
    print(f"  failed     : {stats['failed']}   <- retry these; likely throttled")
    print(f"  not found  : {stats['not_found']}   <- dead slugs, will not retry")
    print(f"  thin pages : {stats['thin']}  (<3 expected sections)")
    if stats["ok"]:
        print(f"  hidden text recovered: {gained:,} chars "
              f"(~{gained // stats['ok']:,}/page)")
        print("\n  section coverage:")
        for s, n in section_hits.items():
            print(f"    {s:<28}{n:>5}/{stats['ok']}  {n / stats['ok'] * 100:>5.1f}%")
    print(f"  -> {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-fetch slugs that previously failed")
    ap.add_argument("--floor-mb", type=int, default=900,
                    help="recycle the browser when available memory drops below this")
    ap.add_argument("--recycle-every", type=int, default=50,
                    help="also recycle the browser every N batches of 8 pages")
    args = ap.parse_args()
    return asyncio.run(
        run(None if args.all else args.limit, args.concurrency,
            args.retry_failed, args.floor_mb, args.recycle_every)
    )


if __name__ == "__main__":
    sys.exit(main())
