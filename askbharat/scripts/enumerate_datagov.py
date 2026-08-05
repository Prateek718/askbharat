#!/usr/bin/env python3
"""Enumerate the data.gov.in catalogue and shortlist directory datasets.

Why enumerate at all: the API has **no free-text search**. The `q=` parameter is
silently ignored and returns the unfiltered catalogue, so there is no way to ask
"which datasets are hospital directories". The only route is to page the whole
Central catalogue once, store the titles and field schemas, and filter locally.

Why filter hard: `/lists` reports 285,829 resources, but that includes
user-uploaded junk ("Sample Data11111"). `filters[org_type]=Central` narrows it
to ~278,039, and the overwhelming majority of *that* is statistical — census
tables, production figures, enrolment counts. None of it answers a citizen's
question. We want the small directory slice: pincodes, hospitals, schools,
offices, helplines — the "where / which" lookups.

This script only *proposes*. A human approves the shortlist before anything is
ingested, which is what keeps the lookup layer small and high-signal.

Usage:
    python -m askbharat.scripts.enumerate_datagov --pages 20     # sample
    python -m askbharat.scripts.enumerate_datagov --all          # full catalogue
    python -m askbharat.scripts.enumerate_datagov --shortlist    # re-filter cache
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import httpx

from askbharat.config import DATA_DIR, redact, settings

LISTS_URL = "https://api.data.gov.in/lists"
PAGE_SIZE = 100
CACHE = DATA_DIR / "datagov_catalogue.jsonl"
SHORTLIST = DATA_DIR / "datagov_shortlist.json"

# Titles that indicate a lookup/directory dataset.
#
# Tuned against the full 278,037-entry catalogue. The first version of this
# pattern matched 14,500 entries (5.2%) because it included bare organisational
# nouns — "centre", "office", "institute", "contact". Those appear inside the
# *names of organisations* ("National Remote Sensing Centre") far more often
# than they describe a directory, so a daily rainfall feed matched. The signal
# has to be that the dataset IS a list, not that it mentions a place.
DIRECTORY_TITLE = re.compile(
    r"(?:"
    r"\bdirector(?:y|ies)\b"                       # "Blood Bank Directory"
    r"|^\s*list of\b"                              # "List of Wellness Centres"
    r"|\bpin\s?code\b"                             # pincode datasets
    r"|\b(?:list|register|roster|index)\s+of\s+"   # "... register of hospitals"
    r"(?:all\s+)?(?:the\s+)?"
    r"(?:hospital|school|college|centre|center|office|bank|clinic|"
    r"institut|univers|village|panchayat|kendra|helpline)"
    r")",
    re.I,
)

# Titles that are statistical even when they match above ("list of villages by
# population", "State-wise fund released..."). Checked second — these veto a
# match. Tuned against a 2,500-entry sample: the "X-wise <measure>" and
# "status/progress of" shapes are the common false positives, because a
# statistical table about hospitals still has state and district columns.
STATISTICAL_TITLE = re.compile(
    r"\b(number of|no\.? of|count of|total|statistics|census|"
    r"year[\s-]?wise|month[\s-]?wise|quarter[\s-]?wise|scheme[\s-]?wise|"
    r"(?:state|district|city|region)[\s-]?wise\s+"
    r"(?:number|total|count|status|progress|fund|amount|allocation|release)|"
    r"status of|progress of|performance of|achievement|"
    r"fund(?:s)? (?:released|allocated|sanctioned|utilis)|"
    r"production|consumption|expenditure|revenue|budget|allocation|"
    r"growth|rate of|percentage|per capita|index|survey|"
    r"during \d{4}|for the year|as per census|"
    # Time-series feeds. These are the loudest false positives once
    # organisational nouns are allowed anywhere in the title.
    r"daily|weekly|monthly|annual|hourly|real[\s-]?time|"
    r"rainfall|temperature|weather|water level|discharge|"
    r"data from|readings?|observations?|forecast)\b",
    re.I,
)

# A directory row is useful only if it locates something.
LOCATOR_FIELDS = {
    "pincode", "pin_code", "district", "districtname", "state", "statename",
    "address", "latitude", "longitude", "lat", "lon", "long", "location",
    "city", "town", "village", "taluk", "block", "phone", "contact",
    "telephone", "email",
}


def fetch_page(client: httpx.Client, offset: int, api_key: str) -> dict:
    params = {
        "api-key": api_key,
        "format": "json",
        "limit": PAGE_SIZE,
        "offset": offset,
        "filters[org_type]": "Central",
    }
    r = client.get(LISTS_URL, params=params)
    r.raise_for_status()
    return r.json()


def enumerate_catalogue(max_pages: int | None, resume: bool = True) -> int:
    api_key = settings.require("data_gov_api_key")
    CACHE.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    if resume and CACHE.exists():
        with CACHE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["index_name"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"resuming — {len(seen)} entries already cached")

    offset = (len(seen) // PAGE_SIZE) * PAGE_SIZE
    total = None
    written = 0
    pages = 0

    headers = {"User-Agent": settings.user_agent}
    with (
        httpx.Client(timeout=60, headers=headers, follow_redirects=True) as client,
        CACHE.open("a", encoding="utf-8") as out,
    ):
        while True:
            if max_pages is not None and pages >= max_pages:
                break
            try:
                d = fetch_page(client, offset, api_key)
            except Exception as e:
                # redact(): the key rides in the query string on this API
                print(f"  ! offset {offset}: {redact(str(e))[:110]}", file=sys.stderr)
                time.sleep(3)
                offset += PAGE_SIZE
                pages += 1
                continue

            if total is None:
                total = d.get("total")
                print(f"catalogue reports {total} Central resources\n")

            records = d.get("records") or []
            if not records:
                break

            for rec in records:
                idx = rec.get("index_name")
                if not idx or idx in seen:
                    continue
                seen.add(idx)
                out.write(json.dumps({
                    "index_name": idx,
                    "title": rec.get("title"),
                    "desc": rec.get("desc"),
                    "org_type": rec.get("org_type"),
                    "source": rec.get("source"),
                    "fields": [f.get("id") for f in (rec.get("field") or [])],
                    "updated": rec.get("updated"),
                }, ensure_ascii=False) + "\n")
                written += 1

            out.flush()
            pages += 1
            offset += PAGE_SIZE
            if pages % 10 == 0:
                print(f"  {len(seen):>7} cached  (offset {offset})")
            if total and offset >= total:
                break
            time.sleep(0.15)      # be polite; this is one long sweep

    print(f"\nenumerated {written} new entries; {len(seen)} total in cache")
    return len(seen)


# Themes a citizen actually asks about. Everything else is a niche
# administrative list (FM transmitter allocations, BPO units approved under a
# policy) — real directories, but nobody queries a chatbot for them.
CITIZEN_THEMES: list[tuple[str, str]] = [
    ("health", r"hospital|dispensar|blood bank|clinic|medical|health|ayush|wellness"),
    ("postal-geo", r"pin\s?code|postal|village|panchayat|ward"),
    ("education", r"school|college|univers|institut|\biti\b|polytechnic|navodaya"),
    ("helpline", r"helpline|toll[\s-]?free|grievance"),
    ("finance", r"\bbank\b|\batm\b|insurance|post office"),
    ("transport", r"transport|railway|airport|\brto\b|bus stand"),
    ("legal", r"advocate|court|legal aid|lok adalat"),
]


def theme_of(title: str) -> str | None:
    for name, pat in CITIZEN_THEMES:
        if re.search(pat, title or "", re.I):
            return name
    return None


def is_directory(entry: dict) -> tuple[bool, str]:
    title = entry.get("title") or ""
    fields = {str(f).lower() for f in (entry.get("fields") or [])}

    if not DIRECTORY_TITLE.search(title):
        return False, "title not directory-shaped"
    if STATISTICAL_TITLE.search(title):
        return False, "title reads statistical"
    overlap = fields & LOCATOR_FIELDS
    if not overlap:
        return False, "no locator field"
    return True, f"locators: {', '.join(sorted(overlap)[:4])}"


def shortlist() -> list[dict]:
    if not CACHE.exists():
        print("no catalogue cache — run enumeration first", file=sys.stderr)
        return []

    entries, hits = 0, []
    with CACHE.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries += 1
            ok, why = is_directory(e)
            if ok:
                theme = theme_of(e.get("title") or "")
                hits.append({**e, "why": why, "theme": theme,
                             "recommended": theme is not None,
                             "approved": False})

    hits.sort(key=lambda e: (e.get("title") or "").lower())
    SHORTLIST.write_text(json.dumps(hits, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\nscanned {entries} catalogue entries")
    print(f"shortlisted {len(hits)} directory-shaped datasets "
          f"({len(hits) / entries * 100:.2f}%)\n" if entries else "")
    rec = [e for e in hits if e["recommended"]]
    import collections as _c
    by_theme = _c.Counter(e["theme"] for e in rec)
    print(f"of those, {len(rec)} fall in a theme citizens actually ask about:")
    for t, n in by_theme.most_common():
        print(f"    {t:<12}{n:>4}")
    print()
    for e in rec[:30]:
        print(f"  [{e['theme']:<10}] {str(e['title'])[:56]:<56} {e['why'][:26]}")
    if len(rec) > 30:
        print(f"  ... and {len(rec) - 30} more recommended")
    print(f"\n  ({len(hits) - len(rec)} others are real directories but niche —")
    print("   FM transmitter allocations, units approved under a policy, etc.)")

    print(f"\n-> {SHORTLIST}")
    print('Review it and set "approved": true on the ones worth ingesting.')
    print("Nothing is ingested until a human approves — that is deliberate.")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20, help="pages to fetch (100 each)")
    ap.add_argument("--all", action="store_true", help="fetch the whole catalogue")
    ap.add_argument("--shortlist", action="store_true", help="re-filter the cache only")
    args = ap.parse_args()

    if not args.shortlist:
        enumerate_catalogue(max_pages=None if args.all else args.pages)
    shortlist()
    return 0


if __name__ == "__main__":
    sys.exit(main())
