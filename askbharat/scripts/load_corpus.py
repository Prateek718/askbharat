#!/usr/bin/env python3
"""Load a harvested JSONL corpus into `raw_documents` and enqueue extraction.

The harvest writes JSONL; this puts it behind the citation substrate. Two
properties matter and both come from `models.py`:

- **Bodies live on disk, not in Postgres.** `raw_documents.body_path` points at
  a content-addressed file. The myScheme corpus alone is 45.7 M chars; keeping
  that in the DB would bloat every backup and every query plan for no gain.
- **`(url, content_hash)` is unique.** Re-running this script over the same
  JSONL is a no-op, and re-running it after a fresh harvest inserts only what
  actually changed. That is what makes the crawl cheap to repeat.

Deduplication is the subtle part for myScheme. The harvester appends rather
than rewrites, so a slug that failed and was later re-fetched has several rows:
4,810 unique slugs across 5,089 records. We keep one row per slug — the
successful capture, newest first — because loading all of them would enqueue
duplicate extraction work against a metered quota.

Usage:
    python -m askbharat.scripts.load_corpus --source myscheme
    python -m askbharat.scripts.load_corpus --source static --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from askbharat.config import DATA_DIR, RAW_DIR
from askbharat.db.models import RawDocument, Source
from askbharat.db.session import session_scope

# A page shorter than this has nothing to extract. The spike used the same
# threshold; keeping it identical means spike numbers stay comparable.
MIN_TEXT_CHARS = 200

BATCH = 500


class CorpusSpec:
    """How to read one harvest's JSONL into a common shape."""

    def __init__(self, source_slug: str, path: Path, content_type: str):
        self.source_slug = source_slug
        self.path = path
        self.content_type = content_type

    def rows(self) -> Iterator[dict]:
        raise NotImplementedError


class MySchemeCorpus(CorpusSpec):
    """myScheme scheme pages — one row per slug, successful capture preferred."""

    def __init__(self):
        super().__init__("myscheme", DATA_DIR / "myscheme_pages.jsonl", "text/plain")

    def rows(self) -> Iterator[dict]:
        # Two passes so we never hold 5,089 full page texts in memory at once:
        # pass 1 decides which byte offset wins for each slug, pass 2 reads
        # only those. The file is 56 MB; the texts inside it are the bulk.
        best: dict[str, tuple[int, str]] = {}   # slug -> (offset, fetched_at)
        with self.path.open(encoding="utf-8") as f:
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not r.get("ok"):
                    continue          # failures and dead slugs carry no text
                slug = r.get("slug")
                if not slug:
                    continue
                stamp = r.get("fetched_at") or ""
                prev = best.get(slug)
                if prev is None or stamp > prev[1]:
                    best[slug] = (offset, stamp)

        with self.path.open(encoding="utf-8") as f:
            for slug, (offset, _) in best.items():
                f.seek(offset)
                r = json.loads(f.readline())
                body = r.get("text") or ""
                yield {
                    "url": r["url"],
                    "text": body,
                    "content_hash": r.get("content_hash") or _sha(body),
                    "http_status": r.get("status"),
                    "fetched_at": r.get("fetched_at"),
                    "meta": {
                        "slug": slug,
                        "title": r.get("title"),
                        "sections_found": r.get("sections_found") or [],
                        "links": r.get("links") or [],
                        "text_len_visible_only": r.get("text_len_visible_only"),
                    },
                }


class StaticCorpus(CorpusSpec):
    """Off-site service pages and documents pulled by harvest_static."""

    def __init__(self):
        super().__init__("india-gov-in", DATA_DIR / "static_docs.jsonl", "text/plain")

    def rows(self) -> Iterator[dict]:
        seen: set[str] = set()
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                body = r.get("text") or ""
                if len(body) < MIN_TEXT_CHARS:
                    continue          # unreachable hosts, scanned PDFs, empties
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                yield {
                    "url": url,
                    "text": body,
                    "content_hash": r.get("content_hash") or _sha(body),
                    "http_status": r.get("http_status"),
                    "fetched_at": r.get("fetched_at"),
                    "meta": {
                        "title": r.get("title"),
                        "kind": r.get("kind"),
                        "pages": r.get("pages"),
                        "tables": r.get("tables") or [],
                    },
                }


CORPORA = {"myscheme": MySchemeCorpus, "static": StaticCorpus}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _parse_ts(value) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def body_file(source_slug: str, content_hash: str) -> Path:
    """Content-addressed path, fanned out so no directory holds 4,700 files."""
    return RAW_DIR / source_slug / content_hash[:2] / f"{content_hash}.txt"


def load(
    source: str,
    dry_run: bool = False,
    limit: int | None = None,
    enqueue: bool = True,
) -> int:
    spec = CORPORA[source]()
    if not spec.path.exists():
        print(f"missing {spec.path} — run the harvest first", file=sys.stderr)
        return 1

    with session_scope() as s:
        src = s.execute(
            select(Source).where(Source.slug == spec.source_slug)
        ).scalar_one_or_none()
        if src is None:
            print(f"source {spec.source_slug!r} is not registered — run "
                  f"audit_sources first", file=sys.stderr)
            return 1
        if not src.crawlable:
            print(f"source {spec.source_slug!r} is not cleared for use "
                  f"(audited={bool(src.audited_at)}, robots={src.robots_allows})",
                  file=sys.stderr)
            return 1
        source_id, tier = src.id, src.tier

    print(f"source {spec.source_slug} (id={source_id}, tier={tier})")
    print(f"reading {spec.path.name} ...")

    def count_docs() -> int:
        with session_scope() as s:
            return s.execute(
                select(func.count()).select_from(RawDocument)
                .where(RawDocument.source_id == source_id)
            ).scalar_one()

    batch: list[dict] = []
    seen = skipped_thin = 0
    before = 0 if dry_run else count_docs()

    def flush(rows: list[dict]) -> None:
        # rowcount is not usable here: psycopg3 reports -1 for a multi-values
        # INSERT ... ON CONFLICT DO NOTHING, so inserted rows are counted by
        # differencing the table instead.
        if not rows or dry_run:
            return
        with session_scope() as s:
            stmt = pg_insert(RawDocument).values(rows)
            s.execute(stmt.on_conflict_do_nothing(constraint="uq_raw_url_hash"))

    for row in spec.rows():
        seen += 1
        if len(row["text"]) < MIN_TEXT_CHARS:
            skipped_thin += 1
            continue

        path = body_file(spec.source_slug, row["content_hash"])
        if not dry_run and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(row["text"], encoding="utf-8")

        batch.append({
            "source_id": source_id,
            "url": row["url"],
            "content_hash": row["content_hash"],
            "content_type": spec.content_type,
            "http_status": row["http_status"],
            "body_path": str(path.relative_to(DATA_DIR.parent)),
            "body_bytes": len(row["text"].encode("utf-8")),
            "meta": row["meta"],
            "fetched_at": _parse_ts(row["fetched_at"]),
        })

        if len(batch) >= BATCH:
            flush(batch)
            batch.clear()
            print(f"  {seen:>5} read", flush=True)
        if limit and seen >= limit:
            break

    flush(batch)
    written = 0 if dry_run else count_docs() - before

    print(f"\n  records read     : {seen}")
    print(f"  skipped (thin)   : {skipped_thin}   <- under {MIN_TEXT_CHARS} chars")
    print(f"  newly inserted   : {written}")
    if dry_run:
        print("\n  (dry run — nothing written)")
        return 0

    if not enqueue:
        # Loaded for retrieval only. The off-site catalogue is dominated by
        # department homepages and index pages — the spike flagged 10 of 14
        # sampled as not-a-service — so paying LLM quota to extract structured
        # fields from them buys almost nothing. They are still worth having in
        # raw_documents: the answer engine can retrieve and cite them.
        print("  enqueued         : 0  (--no-enqueue: retrieval corpus only)")
        return 0

    # Enqueue set-based rather than row-by-row: one statement, idempotent, and
    # it picks up anything a previous interrupted run left un-enqueued.
    with session_scope() as s:
        # Queue order decides what the site can answer *first*, and at ~750
        # pages/day the tail is a week away. Insertion order put Atal Pension
        # Yojana — a national scheme with a Ministry Of Finance badge — 1,840th,
        # behind hundreds of single-district ones. A populated `ministry` marks
        # the central-government schemes, which are the ones citizens arrive
        # already looking for, so those go first.
        queued = s.execute(text("""
            INSERT INTO extraction_queue (raw_document_id, tier, status, attempts, created_at)
            SELECT rd.id,
                   CASE WHEN c.ministry IS NOT NULL THEN 0 ELSE :tier END,
                   'pending', 0, now()
              FROM raw_documents rd
              LEFT JOIN extraction_queue q ON q.raw_document_id = rd.id
              LEFT JOIN scheme_catalogue c ON c.slug = rd.meta->>'slug'
             WHERE rd.source_id = :sid AND q.id IS NULL
        """), {"tier": tier, "sid": source_id}).rowcount or 0

        totals = s.execute(text("""
            SELECT (SELECT count(*) FROM raw_documents WHERE source_id = :sid),
                   (SELECT count(*) FROM extraction_queue q
                      JOIN raw_documents rd ON rd.id = q.raw_document_id
                     WHERE rd.source_id = :sid AND q.status = 'pending')
        """), {"sid": source_id}).one()

    print(f"  newly enqueued   : {queued}")
    print(f"\n  raw_documents for {spec.source_slug}: {totals[0]}")
    print(f"  pending extraction tasks        : {totals[1]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=sorted(CORPORA), required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-enqueue", action="store_true",
                    help="load for retrieval only; do not spend extraction quota")
    args = ap.parse_args()
    return load(args.source, dry_run=args.dry_run, limit=args.limit,
                enqueue=not args.no_enqueue)


if __name__ == "__main__":
    sys.exit(main())
