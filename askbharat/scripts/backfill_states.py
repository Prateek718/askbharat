#!/usr/bin/env python3
"""Populate `scheme_catalogue.state` so the site can filter by jurisdiction.

The column has existed since the first migration and has been NULL on all
4,810 rows, because the only thing that ever wrote it was `load_catalogue`,
reading `beneficiaryState` out of myScheme's search API — a field the API
returns but never fills in. The catalogue therefore knows a scheme's category,
tags and ministry, but not which state it belongs to.

The state *is* on the harvested detail page, in the chip immediately beside the
title:

    Check Eligibility
    Madhya Pradesh                     <- the chip
    Financial Assistance to Shaurya Medal Recipients ...
    Award                              <- tags follow
    Financial Assistance

Central schemes have no such chip, so the tags (or the ministry) butt straight
up against the title. That absence is the signal: a page whose title-adjacent
line is not one of the 36 states/UTs is a central scheme.

The chip sits on either side of the title depending on the page, so this reads
both neighbours — and *only* those two. An earlier version scanned a wider
window and picked up tags several lines down, which is how three schemes were
filed under states they have nothing to do with. Measured over the corpus, the
chip is at offset -1 on 963 pages and +1 on 3,043, and never anywhere else.

Checked against the independent LLM extraction, which read the whole page body
rather than the chip, the two agree on jurisdiction for 3,231 of 3,270 schemes
(98.8%) and on the state name for 2,557 of 2,586 (98.9%). Nearly every
remaining difference is the extraction's, not this script's: `Tamil Nadu`
with a narrow no-break space, `Pondicherry` for Puducherry. Where they genuinely
disagree the chip wins, because it is what the source published rather than
what a model inferred.

Written values are the 36 standard state/UT names, or 'Central'. A row stays
NULL when no page was harvested for it — unknown and central are different
facts, and collapsing them would file ~143 unread schemes under Central.

Usage:
    python -m askbharat.scripts.backfill_states --dry-run
    python -m askbharat.scripts.backfill_states
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter

from sqlalchemy import text

from askbharat.config import DATA_DIR
from askbharat.db.session import session_scope

PAGES = DATA_DIR / "myscheme_pages.jsonl"

CENTRAL = "Central"

# The 28 states and 8 union territories, spelled as myScheme's own
# `beneficiaryState` facet spells them. This is the filter vocabulary, so it is
# pinned here rather than read from the harvested facet dump at runtime: the
# dump also carries 'All' and one row of 'The Dadra And Nagar Haveli And Daman
# And Diu', and a facet list that drifts should not silently redefine what the
# site offers.
STATES: tuple[str, ...] = (
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh",
    "Assam", "Bihar", "Chandigarh", "Chhattisgarh",
    "Dadra & Nagar Haveli and Daman & Diu", "Delhi", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand",
    "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
)

# Variants seen in the extracted records, which are used as a fallback for
# schemes whose page was never harvested. Keys are already folded by `_fold`.
ALIASES: dict[str, str] = {
    "pondicherry": "Puducherry",
    "orissa": "Odisha",
    "uttaranchal": "Uttarakhand",
    "nct of delhi": "Delhi",
    "delhi ncr": "Delhi",
    "national capital territory of delhi": "Delhi",
    "jammu and kashmir (ut)": "Jammu and Kashmir",
    "daman and diu and dadra and nagar haveli": "Dadra & Nagar Haveli and Daman & Diu",
    "dadra and nagar haveli": "Dadra & Nagar Haveli and Daman & Diu",
    "daman and diu": "Dadra & Nagar Haveli and Daman & Diu",
    "the dadra and nagar haveli and daman and diu": "Dadra & Nagar Haveli and Daman & Diu",
    "andaman and nicobar": "Andaman and Nicobar Islands",
    "pondichery": "Puducherry",
}


def _fold(value: str) -> str:
    """Collapse a state label to a comparison key.

    NFKC is what rescues `Tamil Nadu`: the narrow no-break space the
    extraction picked up off the rendered page normalises to a plain space,
    where a bare `.strip()` leaves it looking like a different state.
    """
    out = unicodedata.normalize("NFKC", value)
    out = out.replace("&", " and ")
    out = re.sub(r"[^\w\s]", " ", out)
    out = re.sub(r"\s+", " ", out).strip().lower()
    return out


_LOOKUP: dict[str, str] = {_fold(s): s for s in STATES} | ALIASES


def canonicalise(value: str | None) -> str | None:
    """Map any spelling of a state to its standard name, or None."""
    if not value:
        return None
    return _LOOKUP.get(_fold(value))


def state_from_page(page: dict) -> str | None:
    """The state chip beside the title, or CENTRAL when there is none.

    Returns None only when the title cannot be located in the page text, which
    means the layout is not the one this reads and guessing would be worse than
    leaving the row unknown.
    """
    title = (page.get("title") or "").strip()
    if not title:
        return None
    lines = [ln.strip() for ln in page["text"].split("\n") if ln.strip()]
    try:
        i = lines.index(title)
    except ValueError:
        return None
    for j in (i - 1, i + 1):
        if 0 <= j < len(lines):
            hit = canonicalise(lines[j])
            if hit:
                return hit
    return CENTRAL


def derive_from_pages() -> dict[str, str]:
    """slug -> state, read off the harvested detail pages."""
    if not PAGES.exists():
        print(f"missing {PAGES}", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    with PAGES.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                page = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not page.get("ok") or not page.get("text"):
                continue
            slug = page.get("slug")
            if not slug or slug in out:
                continue
            value = state_from_page(page)
            if value:
                out[slug] = value
    return out


def derive_from_extraction() -> dict[str, str]:
    """slug -> state, from the LLM extraction. Fallback for unharvested pages.

    Only used where there is no page to read, so it never overrides the chip.
    Rows whose extracted state does not canonicalise (multi-state strings like
    'Kerala, Tamil Nadu', or prose) are skipped rather than guessed at.
    """
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT replace(id, 'myscheme:', '') AS slug,
                   jurisdiction_level, state
              FROM service_records
             WHERE id LIKE 'myscheme:%'
        """)).mappings().all()
    out: dict[str, str] = {}
    for r in rows:
        if r["jurisdiction_level"] == "central":
            out[r["slug"]] = CENTRAL
            continue
        hit = canonicalise(r["state"])
        if hit:
            out[r["slug"]] = hit
    return out


def backfill(dry_run: bool = False) -> int:
    with session_scope() as s:
        slugs = [r[0] for r in s.execute(text("SELECT slug FROM scheme_catalogue"))]

    pages = derive_from_pages()
    fallback = derive_from_extraction()

    resolved: dict[str, str] = {}
    from_page = from_extraction = 0
    for slug in slugs:
        if slug in pages:
            resolved[slug] = pages[slug]
            from_page += 1
        elif slug in fallback:
            resolved[slug] = fallback[slug]
            from_extraction += 1

    unknown = len(slugs) - len(resolved)
    counts = Counter(resolved.values())
    print(f"catalogue rows    : {len(slugs)}")
    print(f"  from page chip  : {from_page}")
    print(f"  from extraction : {from_extraction}   (no page harvested)")
    print(f"  left unknown    : {unknown}")
    print(f"\n{CENTRAL:38} {counts.get(CENTRAL, 0):>5}")
    for name, n in sorted(
        ((k, v) for k, v in counts.items() if k != CENTRAL), key=lambda kv: -kv[1]
    ):
        print(f"{name:38} {n:>5}")
    missing = [s for s in STATES if s not in counts]
    if missing:
        print(f"\nstates with no schemes: {missing}")

    if dry_run:
        print("\ndry run — nothing written")
        return 0

    # Rewritten from scratch every run: the chip is the authority, so a rerun
    # after more pages are harvested should be able to correct an earlier
    # fallback guess, not just fill blanks.
    with session_scope() as s:
        s.execute(text("UPDATE scheme_catalogue SET state = NULL"))
        payload = [{"slug": k, "state": v} for k, v in resolved.items()]
        for i in range(0, len(payload), 500):
            s.execute(
                text("UPDATE scheme_catalogue SET state = :state WHERE slug = :slug"),
                payload[i:i + 500],
            )

    with session_scope() as s:
        written = s.execute(
            text("SELECT count(*) FROM scheme_catalogue WHERE state IS NOT NULL")
        ).scalar_one()
    print(f"\nwrote state on {written} rows")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="show the distribution without writing")
    args = ap.parse_args()
    return backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
