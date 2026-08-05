#!/usr/bin/env python3
"""Work the extraction queue: raw_documents -> service_records.

This is the long pole. At 1,000 requests/day the full myScheme corpus takes
roughly five days, so the design constraint is not speed — it is that five days
of partial progress must never be lost or repeated.

How that is achieved:

- **Claiming is atomic.** `FOR UPDATE SKIP LOCKED` means two runners can work
  the same queue without collision, and a crash mid-flight leaves a row in
  `in_flight` that `--reclaim` returns to `pending`.
- **Quota exhaustion is not an error.** Hitting the daily cap releases the
  claim and exits 0. Tomorrow's run picks up exactly where this one stopped.
- **A schema failure parks rather than loops.** Two failed attempts move the row
  to `parked` for a human, instead of burning quota on the same bad page daily.

Usage:
    python -m askbharat.scripts.extract --limit 30       # validation batch
    python -m askbharat.scripts.extract                  # until quota runs out
    python -m askbharat.scripts.extract --reclaim        # recover after a crash
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from askbharat.config import DATA_DIR, redact
from askbharat.db.session import session_scope
from askbharat.llm.limiter import DailyQuotaExceeded
from askbharat.llm.prompts import EXTRACTION_SYSTEM, MYSCHEME_HINT
from askbharat.llm.provider import LLMProvider, SchemaValidationFailed
from askbharat.schema.extraction import ExtractedService

# Longest myScheme body is ~43k chars, so this truncates nothing in practice.
# It exists to bound a pathological page rather than to save tokens.
MAX_PAGE_CHARS = 48_000

MAX_ATTEMPTS = 2

# Fields that carry citizen-actionable content. Used for the confidence score
# and for the run summary's null-rate table.
CONTENT_FIELDS = [
    "what_it_is", "who_is_eligible", "exclusions", "documents_required", "fee_amount",
    "fee_notes", "how_to_apply", "application_modes", "online_url",
    "processing_time", "validity", "helpline", "email", "office_address",
    "grievance_route", "ministry",
]

SOURCE_HINTS = {"myscheme": MYSCHEME_HINT}


def claim(n: int) -> list[dict]:
    """Atomically take up to n pending tasks."""
    with session_scope() as s:
        rows = s.execute(text("""
            UPDATE extraction_queue q
               SET status = 'in_flight', claimed_at = now(), attempts = q.attempts + 1
             WHERE q.id IN (
                   SELECT id FROM extraction_queue
                    WHERE status = 'pending'
                    ORDER BY tier, id
                      FOR UPDATE SKIP LOCKED
                    LIMIT :n)
         RETURNING q.id, q.raw_document_id, q.attempts
        """), {"n": n}).mappings().all()
        if not rows:
            return []
        ids = [r["raw_document_id"] for r in rows]
        docs = s.execute(text("""
            SELECT rd.id, rd.url, rd.body_path, rd.meta, s.slug AS source_slug
              FROM raw_documents rd JOIN sources s ON s.id = rd.source_id
             WHERE rd.id = ANY(:ids)
        """), {"ids": ids}).mappings().all()
        by_id = {d["id"]: dict(d) for d in docs}
        return [{**dict(r), "doc": by_id[r["raw_document_id"]]} for r in rows]


def release(task_id: int, status: str, error: str | None = None,
            model: str | None = None) -> None:
    # :st is bound twice — once into a varchar column and once into a text
    # comparison — and Postgres refuses to deduce one type for both. The cast
    # pins it rather than splitting it into two parameters that could drift.
    with session_scope() as s:
        s.execute(text("""
            UPDATE extraction_queue
               SET status = CAST(:st AS varchar(16)),
                   last_error = :err,
                   model_used = :model,
                   completed_at = CASE WHEN CAST(:st AS varchar(16)) = 'done'
                                       THEN now() ELSE completed_at END
             WHERE id = :id
        """), {"id": task_id, "st": status, "err": error, "model": model})


def confidence_for(rec: ExtractedService) -> float:
    """A blunt, honest score: how much of the page did we actually capture?

    Not a probability. It ranks records for review and lets the UI show the
    thin ones last. A page that genuinely states little scores low and that is
    correct — the score measures the record, not the model.
    """
    if not rec.page_is_about_a_service:
        return 0.0
    filled = sum(
        1 for f in CONTENT_FIELDS if getattr(rec, f) not in (None, [], "")
    )
    return round(filled / len(CONTENT_FIELDS), 3)


def record_id(source_slug: str, doc: dict) -> str:
    slug = (doc.get("meta") or {}).get("slug")
    return f"{source_slug}:{slug}" if slug else f"{source_slug}:doc{doc['id']}"


def persist(rec: ExtractedService, doc: dict, model: str) -> None:
    payload = rec.model_dump(mode="json")
    # The harvest captured the official title; the model is not the authority
    # on it and does not always return it. Prefer what the source published.
    title = (doc.get("meta") or {}).get("title")
    name = rec.canonical_name or title or doc["url"].rsplit("/", 1)[-1]
    payload["canonical_name"] = name
    payload["_provenance"] = {
        "raw_document_id": doc["id"],
        "url": doc["url"],
        "model": model,
        "extracted_at": datetime.now(UTC).isoformat(),
        "name_from": "model" if rec.canonical_name else "catalogue",
    }
    jurisdiction = rec.jurisdiction_level or "central"
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO service_records
                (id, canonical_name, kind, jurisdiction_level, state, ministry,
                 life_events, payload, confidence, link_status, updated_at)
            VALUES
                (:id, :name, :kind, :jl, :state, :ministry,
                 '[]'::jsonb, :payload, :conf, 'unchecked', now())
            ON CONFLICT (id) DO UPDATE SET
                canonical_name = EXCLUDED.canonical_name,
                kind = EXCLUDED.kind,
                jurisdiction_level = EXCLUDED.jurisdiction_level,
                state = EXCLUDED.state,
                ministry = EXCLUDED.ministry,
                payload = EXCLUDED.payload,
                confidence = EXCLUDED.confidence,
                updated_at = now()
        """), {
            "id": record_id(doc["source_slug"], doc),
            "name": name[:2000],
            "kind": (rec.kind or "scheme")[:16],
            "jl": jurisdiction[:16],
            "state": (rec.state or None) and rec.state[:64],
            "ministry": rec.ministry,
            "payload": json.dumps(payload, ensure_ascii=False),
            "conf": confidence_for(rec),
        })


def body_text(body_path: str) -> str:
    path = Path(body_path)
    if not path.is_absolute():
        path = DATA_DIR.parent / body_path
    return path.read_text(encoding="utf-8")[:MAX_PAGE_CHARS]


class QuotaStop(Exception):
    """Signals the worker pool to wind down — the day's quota is spent."""


def process_one(t: dict, p: LLMProvider) -> dict:
    """Extract one page. Returns a result dict; never raises except QuotaStop.

    Runs on a worker thread. Everything it touches is either thread-local or
    already lock-guarded (`TokenBucket`, `DailyQuota`), and each DB write opens
    its own short-lived session.
    """
    doc = t["doc"]
    try:
        page = body_text(doc["body_path"])
    except OSError as e:
        release(t["id"], "parked", f"body unreadable: {e}")
        return {"outcome": "parked"}

    system = EXTRACTION_SYSTEM + SOURCE_HINTS.get(doc["source_slug"], "")
    try:
        c = p.complete(
            [
                {"role": "system", "content": system},
                {"role": "user",
                 "content": f"URL: {doc['url']}\n\nPAGE TEXT:\n{page}"},
            ],
            task="extract", schema=ExtractedService, max_tokens=8000,
        )
    except DailyQuotaExceeded:
        # Not a failure. Hand the task straight back so tomorrow resumes here.
        release(t["id"], "pending")
        raise QuotaStop from None
    except SchemaValidationFailed as e:
        err = redact(str(e))[:500]
        if t["attempts"] >= MAX_ATTEMPTS:
            release(t["id"], "parked", err)
            return {"outcome": "parked", "url": doc["url"]}
        release(t["id"], "pending", err)
        return {"outcome": "requeued"}

    rec: ExtractedService = c.parsed
    persist(rec, doc, c.usage.model)
    release(t["id"], "done", None, c.usage.model)
    return {
        "outcome": "done",
        "rec": rec,
        "name": rec.canonical_name or (doc.get("meta") or {}).get("title") or "",
        "fell_back": c.usage.fell_back,
        "budget_raised": c.usage.budget_raised,
        "not_a_service": not rec.page_is_about_a_service,
    }


def run(limit: int | None, batch: int = 10, workers: int = 6,
        quota_cap: int = 1000) -> int:
    # Extraction and the chat assistant draw on the same daily allowance. Left
    # alone, a batch run spends the whole 1,000 in about three hours and the
    # site's assistant is dead for the rest of the day. Capping extraction
    # below the true limit reserves the difference for live traffic, which is
    # the half a citizen actually notices.
    p = LLMProvider(daily_cap=quota_cap)
    print(f"model chain : {p.models_for('extract')}")
    print(f"quota       : {p.quota.used}/{p.quota.cap} used today")
    print(f"workers     : {workers}\n")

    stats = {"done": 0, "parked": 0, "requeued": 0, "not_a_service": 0,
             "fell_back": 0, "budget_raised": 0}
    nulls = dict.fromkeys(CONTENT_FIELDS, 0)
    processed = 0
    started = time.time()
    stopping = False

    # A page takes ~100s wall clock, almost all of it waiting on the provider.
    # Sequential, that caps the day at ~860 pages — below the quota we paid
    # for, so latency rather than quota would decide the schedule. Workers run
    # concurrently against the same lock-guarded token bucket, which still
    # enforces the 20/min burst limit.
    while not stopping and (limit is None or processed < limit):
        want = batch if limit is None else min(batch, limit - processed)
        tasks = claim(want)
        if not tasks:
            print("\nqueue empty — nothing left to extract.")
            break

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_one, t, p): t for t in tasks}
            for fut in as_completed(futures):
                processed += 1
                try:
                    res = fut.result()
                except QuotaStop:
                    stopping = True
                    continue
                except Exception as e:                    # noqa: BLE001
                    t = futures[fut]
                    release(t["id"], "pending", redact(str(e))[:500])
                    stats["requeued"] += 1
                    continue

                out = res["outcome"]
                if out != "done":
                    stats[out] += 1
                    if out == "parked" and res.get("url"):
                        print(f"  PARKED  {res['url'][-52:]}", flush=True)
                    continue

                rec = res["rec"]
                stats["done"] += 1
                stats["not_a_service"] += res["not_a_service"]
                stats["fell_back"] += res["fell_back"]
                stats["budget_raised"] += res["budget_raised"]
                for f in CONTENT_FIELDS:
                    if getattr(rec, f) in (None, [], ""):
                        nulls[f] += 1

                filled = len(CONTENT_FIELDS) - sum(
                    1 for f in CONTENT_FIELDS
                    if getattr(rec, f) in (None, [], "")
                )
                if stats["done"] % 5 == 0 or stats["done"] <= 3:
                    rate = processed / max(time.time() - started, 1e-9) * 60
                    print(f"  [{processed:>4}] {filled:>2}/{len(CONTENT_FIELDS)} "
                          f"fields  q={p.quota.used}  {rate:.1f}/min  "
                          f"{res['name'][:42]}", flush=True)

        if stopping:
            print(f"\ndaily quota reached — stopping cleanly at {stats['done']} "
                  f"written. Re-run tomorrow to continue.")

    mins = (time.time() - started) / 60
    print("\n" + "=" * 64)
    print(f"  processed        : {processed} in {mins:.1f}m")
    print(f"  written          : {stats['done']}")
    print(f"  parked           : {stats['parked']}")
    print(f"  requeued (retry) : {stats['requeued']}")
    print(f"  not-a-service    : {stats['not_a_service']}")
    print(f"  model fallbacks  : {stats['fell_back']}"
          f"   budget bumps: {stats['budget_raised']}")
    print(f"  quota used       : {p.quota.used}/{p.quota.cap}")

    if stats["done"]:
        print("\n  null rate per field (high on a thin page is CORRECT):")
        for f, n in sorted(nulls.items(), key=lambda kv: -kv[1]):
            print(f"    {f:<22}{n:>4}/{stats['done']}  {n / stats['done'] * 100:>5.1f}%")

    with session_scope() as s:
        left = s.execute(text(
            "SELECT status, count(*) FROM extraction_queue GROUP BY status "
            "ORDER BY status")).all()
    print("\n  queue:", ", ".join(f"{k}={v}" for k, v in left))
    return 0


def reclaim() -> int:
    """Return tasks stranded in in_flight by a crash."""
    with session_scope() as s:
        n = s.execute(text(
            "UPDATE extraction_queue SET status='pending' "
            "WHERE status='in_flight'")).rowcount
    print(f"reclaimed {n} stranded task(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N pages (default: until quota or queue end)")
    ap.add_argument("--batch", type=int, default=12, help="tasks claimed per round")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent requests (the 20/min bucket still applies)")
    ap.add_argument("--quota-cap", type=int, default=900,
                    help="stop at this many requests/day, reserving the rest "
                         "of the 1000 for the live chat assistant")
    ap.add_argument("--reclaim", action="store_true",
                    help="return in_flight tasks to pending after a crash")
    args = ap.parse_args()
    if args.reclaim:
        return reclaim()
    return run(args.limit, batch=args.batch, workers=args.workers,
               quota_cap=args.quota_cap)


if __name__ == "__main__":
    sys.exit(main())
