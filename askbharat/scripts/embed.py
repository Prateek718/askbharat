#!/usr/bin/env python3
"""Populate the vector columns.

Two targets, indexing different text for different questions:

  catalogue  scheme_catalogue.embedding — title + description + tags.
             Complete today, for all 4,810 schemes.
  records    service_records.embedding  — eligibility, documents, steps.
             Grows as extraction lands, so this is meant to be re-run.

Both are incremental: only rows whose vector is missing are embedded, so
re-running after another day of extraction costs only the new rows. Safe to
interrupt.

Usage:
    python -m askbharat.scripts.embed                 # both, missing only
    python -m askbharat.scripts.embed --target records
    python -m askbharat.scripts.embed --target catalogue --all
"""
from __future__ import annotations

import argparse
import sys
import time

from sqlalchemy import text

from askbharat.db.session import session_scope
from askbharat.llm.embeddings import (
    DIMENSIONS,
    MODEL_NAME,
    catalogue_text,
    embed_documents,
    record_text,
)

BATCH = 64


def _fetch(sql: str) -> list[dict]:
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(sql)).mappings().all()]


def _write(table: str, key: str, pairs: list[tuple[str, list[float]]]) -> None:
    with session_scope() as s:
        for ident, vector in pairs:
            s.execute(
                text(f"UPDATE {table} SET embedding = :v WHERE {key} = :id"),
                {"v": str(vector), "id": ident},
            )


def embed_catalogue(all_rows: bool, batch: int) -> int:
    where = "" if all_rows else "WHERE embedding IS NULL"
    rows = _fetch("SELECT slug, title, description, tags "
                  f"FROM scheme_catalogue {where} ORDER BY slug")
    return _run("catalogue", rows, batch, "scheme_catalogue", "slug",
                lambda r: (r["slug"],
                           catalogue_text(r["title"], r["description"], r["tags"])))


def embed_records(all_rows: bool, batch: int) -> int:
    where = "" if all_rows else "WHERE embedding IS NULL"
    rows = _fetch("SELECT id, canonical_name, payload "
                  f"FROM service_records {where} ORDER BY id")
    return _run("records", rows, batch, "service_records", "id",
                lambda r: (r["id"], record_text(r["canonical_name"], r["payload"])))


def _run(label, rows, batch, table, key, to_text) -> int:
    if not rows:
        print(f"{label}: nothing to embed")
        return 0

    started = time.time()
    done = skipped = 0

    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        prepared = [to_text(r) for r in chunk]
        # A record with nothing extracted yet has no rules to index. Embedding
        # its bare name would put a near-duplicate of the catalogue vector into
        # the rules index and pollute exactly the signal it exists to provide.
        usable = [(ident, body) for ident, body in prepared if body]
        skipped += len(prepared) - len(usable)
        if not usable:
            continue

        vectors = embed_documents([b for _, b in usable], batch_size=batch)
        _write(table, key, list(zip([i_ for i_, _ in usable], vectors, strict=True)))
        done += len(usable)
        rate = done / max(time.time() - started, 1e-9)
        print(f"  {label}: {done}/{len(rows)}  {rate:5.1f} rows/s", flush=True)

    print(f"{label}: embedded {done}, skipped {skipped} (nothing to index) "
          f"in {(time.time() - started) / 60:.1f}m")
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["catalogue", "records", "all"],
                    default="all")
    ap.add_argument("--all", action="store_true",
                    help="re-embed rows that already have a vector")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    print(f"model: {MODEL_NAME} ({DIMENSIONS}d, CPU)\n")
    if args.target in ("catalogue", "all"):
        embed_catalogue(args.all, args.batch)
    if args.target in ("records", "all"):
        embed_records(args.all, args.batch)

    with session_scope() as s:
        cat = s.execute(text("SELECT count(*), count(embedding) "
                             "FROM scheme_catalogue")).one()
        rec = s.execute(text("SELECT count(*), count(embedding) "
                             "FROM service_records")).one()
    print(f"\n  catalogue : {cat[1]}/{cat[0]} embedded")
    print(f"  records   : {rec[1]}/{rec[0]} embedded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
