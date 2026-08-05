#!/usr/bin/env python3
"""Load myScheme's catalogue metadata into `scheme_catalogue`.

`schemes.jsonl` is what myScheme's own search API returned: title, description,
category, tags. It is complete where our LLM extraction has not yet run, so it
is what the website's browse and filter layer is built on.

Two clean-ups happen here rather than in the templates, because doing them in
the view means doing them on every request and getting them subtly different in
each one:

1. **HTML entities.** The API returns `Agriculture, Rural &amp; Environment`.
   Unescaped, that renders as literal `&amp;` on the page.
2. **Category naming.** The catalogue uses 15 category strings; the separate
   `taxonomy.json` uses 18 with different spacing (`Agriculture,Rural` versus
   `Agriculture, Rural`). Left alone they split into duplicate facets in the
   UI. Normalising whitespace collapses them.

Usage:
    python -m askbharat.scripts.load_catalogue
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from askbharat.config import DATA_DIR
from askbharat.db.models import SchemeCatalogue
from askbharat.db.session import session_scope

SCHEMES = DATA_DIR / "schemes.jsonl"
BASE_URL = "https://www.myscheme.gov.in/schemes/"
BATCH = 500


def clean(value: str | None) -> str | None:
    """Unescape entities and normalise whitespace. Idempotent."""
    if not value:
        return None
    out = html.unescape(str(value))
    out = re.sub(r"\s+", " ", out).strip()
    return out or None


def clean_category(value: str) -> str | None:
    """Normalise a category label so variants collapse to one facet."""
    out = clean(value)
    if not out:
        return None
    # 'Agriculture,Rural & Environment' -> 'Agriculture, Rural & Environment'
    out = re.sub(r",(?=\S)", ", ", out)
    return out


def load(dry_run: bool = False) -> int:
    if not SCHEMES.exists():
        print(f"missing {SCHEMES}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    now = datetime.now(UTC)

    with SCHEMES.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = r.get("slug")
            if not slug or slug in seen:
                skipped += 1
                continue
            seen.add(slug)

            cats = [c for c in (clean_category(c) for c in (r.get("schemeCategory") or [])) if c]
            tags = [t for t in (clean(t) for t in (r.get("tags") or [])) if t]
            rows.append({
                "slug": slug,
                "title": clean(r.get("title")) or slug,
                "description": clean(r.get("description")),
                "categories": cats,
                "tags": tags,
                "ministry": clean(r.get("ministry")),
                "state": clean(r.get("beneficiaryState")),
                "url": f"{BASE_URL}{slug}",
                "updated_at": now,
            })

    print(f"read {len(rows)} unique schemes ({skipped} duplicate/blank skipped)")
    if dry_run:
        cats: dict[str, int] = {}
        for r in rows:
            for c in r["categories"]:
                cats[c] = cats.get(c, 0) + 1
        print(f"\n{len(cats)} distinct categories after normalisation:")
        for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {c}")
        return 0

    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        with session_scope() as s:
            stmt = pg_insert(SchemeCatalogue).values(chunk)
            s.execute(stmt.on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "title": stmt.excluded.title,
                    "description": stmt.excluded.description,
                    "categories": stmt.excluded.categories,
                    "tags": stmt.excluded.tags,
                    "ministry": stmt.excluded.ministry,
                    "state": stmt.excluded.state,
                    "url": stmt.excluded.url,
                    "updated_at": stmt.excluded.updated_at,
                },
            ))

    with session_scope() as s:
        total = s.execute(text("SELECT count(*) FROM scheme_catalogue")).scalar_one()
        joined = s.execute(text("""
            SELECT count(*) FROM scheme_catalogue c
              JOIN raw_documents rd
                ON rd.meta->>'slug' = c.slug AND rd.source_id =
                   (SELECT id FROM sources WHERE slug='myscheme')
        """)).scalar_one()
    print(f"catalogue rows       : {total}")
    print(f"with a harvested page: {joined}   <- these get full detail pages")
    print(f"catalogue-only       : {total - joined}   <- listing only, no page captured")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show normalised category counts without writing")
    args = ap.parse_args()
    return load(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
