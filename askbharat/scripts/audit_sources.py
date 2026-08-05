#!/usr/bin/env python3
"""Phase 0.2 — licence and robots.txt audit.

Run this before crawling anything at scale. `Source.crawlable` stays False until
a source has been audited, so the ingest layer physically cannot run ahead of
this check.

For each source it records:
  - whether robots.txt permits our user-agent on the base path
  - the declared Crawl-delay, if any
  - the reuse licence, detected from the site's own terms/copyright page

On licence detection: most Government of India content is reusable under
GODL-India with attribution, but that is a default to *verify*, not assume.
Anything we can't classify is left as None and flagged for a human — an
unaudited source is simply not crawled.

Usage:
    python -m askbharat.scripts.audit_sources            # audit all
    python -m askbharat.scripts.audit_sources --tier 1   # tier 1 only
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from askbharat.config import redact, settings
from askbharat.db.models import Source
from askbharat.db.session import init_db, session_scope
from askbharat.ingest.registry import load_specs, sync_to_db

TIMEOUT = 25

# Ordered most-specific first: GODL is the meaningful finding, a bare
# "copyright" notice is not a licence.
LICENCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("GODL-India", re.compile(r"government open data licen[cs]e|\bGODL\b", re.I)),
    ("CC-BY-4.0", re.compile(r"creative commons attribution 4\.0|CC[\s-]?BY[\s-]?4\.0", re.I)),
    ("CC-BY", re.compile(r"creative commons attribution", re.I)),
    ("public-domain", re.compile(r"public domain", re.I)),
    ("free-to-reuse", re.compile(r"may be reproduced free of charge|freely (?:be )?(?:used|reproduced)", re.I)),
    ("all-rights-reserved", re.compile(r"all rights reserved", re.I)),
]

# Where Indian government sites usually put terms. Tried in order.
TERMS_PATHS = [
    "/website-policy", "/website-policies", "/copyright-policy", "/copyright",
    "/terms-and-conditions", "/terms", "/disclaimer", "/hyperlinking-policy",
]


def fetch(client: httpx.Client, url: str) -> tuple[int | None, str]:
    try:
        r = client.get(url)
        ctype = r.headers.get("content-type", "")
        body = r.text if "text" in ctype or "html" in ctype or not ctype else ""
        return r.status_code, body
    except Exception as e:
        return None, f"__error__ {redact(str(e))[:120]}"


def audit_robots(client: httpx.Client, base_url: str, ua: str) -> dict:
    robots_url = urljoin(base_url, "/robots.txt")
    status, body = fetch(client, robots_url)
    out = {
        "robots_url": robots_url,
        "robots_status": status,
        "robots_allows": None,
        "robots_crawl_delay": None,
        "robots_error": None,
    }
    if status is None:
        out["robots_error"] = body.replace("__error__ ", "")
        return out
    if status == 404:
        # No robots.txt means no restrictions expressed. Permitted, but noted.
        out["robots_allows"] = True
        out["robots_error"] = "no robots.txt (404) — treated as allow"
        return out
    if status != 200 or body.startswith("__error__"):
        out["robots_error"] = f"unexpected status {status}"
        return out

    rp = RobotFileParser()
    rp.parse(body.splitlines())
    out["robots_allows"] = rp.can_fetch(ua, base_url)
    try:
        delay = rp.crawl_delay(ua)
        if delay is None:  # stdlib returns None for wildcard-only in some cases
            m = re.search(r"(?im)^\s*crawl-delay:\s*([\d.]+)", body)
            delay = float(m.group(1)) if m else None
        out["robots_crawl_delay"] = float(delay) if delay is not None else None
    except Exception:
        pass
    return out


def detect_licence(client: httpx.Client, base_url: str) -> dict:
    """Look for a reuse licence on the site's own terms pages."""
    for path in TERMS_PATHS:
        url = urljoin(base_url, path)
        status, body = fetch(client, url)
        if status != 200 or not body or body.startswith("__error__"):
            continue
        text = re.sub(r"<[^>]+>", " ", body)
        for name, pat in LICENCE_PATTERNS:
            if pat.search(text):
                return {"licence": name, "licence_url": url}
    return {"licence": None, "licence_url": None}


def audit_one(client: httpx.Client, src: Source, ua: str, manual: dict | None) -> dict:
    robots = audit_robots(client, src.base_url, ua)

    # Some government sites sit behind a WAF that rejects any non-browser
    # User-Agent, so an honest crawler cannot read robots.txt at all. That is a
    # bot-protection artifact, not a crawl policy. Where a human has read the
    # policy directly and recorded it in the registry, we trust that over a
    # WAF error — but we never *infer* permission from a 403.
    if manual and robots["robots_allows"] is None:
        robots["robots_allows"] = bool(manual.get("allows"))
        robots["robots_crawl_delay"] = manual.get("crawl_delay")
        robots["robots_error"] = (
            f"WAF blocked automated read ({robots.get('robots_status')}); "
            f"policy verified by hand {manual.get('verified_on')}"
        )
        robots["manual"] = True

    licence = detect_licence(client, src.base_url) if robots["robots_allows"] else {
        "licence": None, "licence_url": None
    }
    return {**robots, **licence}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, default=None, help="audit only this tier")
    ap.add_argument("--slug", type=str, default=None, help="audit a single source")
    args = ap.parse_args()

    init_db()
    created, updated = sync_to_db()
    print(f"registry synced: {created} new, {updated} existing\n")

    ua = settings.user_agent
    headers = {"User-Agent": ua, "Accept": "text/html,text/plain,*/*"}

    with session_scope() as s:
        q = s.query(Source)
        if args.tier:
            q = q.filter(Source.tier == args.tier)
        if args.slug:
            q = q.filter(Source.slug == args.slug)
        sources = q.order_by(Source.tier, Source.slug).all()

        print(f"auditing {len(sources)} sources as {ua}\n")
        print(f"{'source':<18}{'robots':>8}{'delay':>7}  {'licence':<22}notes")
        print("-" * 88)

        manual_by_slug = {
            spec.slug: spec.extra.get("robots_verified_manually")
            for spec in load_specs()
        }

        allowed = blocked = unlicensed = waf = 0
        # verify=False: government TLS is frequently misconfigured (the link
        # audit found 20/302 reachable hosts need it). We are reading public
        # policy pages, not transmitting anything, so this is acceptable here —
        # it is NOT acceptable in the ingest path without recording the fact.
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers=headers, verify=False
        ) as client:
            for src in sources:
                r = audit_one(client, src, ua, manual_by_slug.get(src.slug))
                if r.get("manual"):
                    waf += 1
                src.robots_allows = r["robots_allows"]
                src.robots_crawl_delay = r["robots_crawl_delay"]
                src.licence = r["licence"]
                src.licence_url = r["licence_url"]
                src.audited_at = datetime.now(UTC)

                note = r.get("robots_error") or ""
                if r["robots_allows"]:
                    allowed += 1
                elif r["robots_allows"] is False:
                    blocked += 1
                    note = note or "DISALLOWED by robots.txt"
                if not r["licence"]:
                    unlicensed += 1

                delay = r["robots_crawl_delay"]
                print(
                    f"{src.slug:<18}"
                    f"{('yes' if r['robots_allows'] else 'NO' if r['robots_allows'] is False else '?'):>8}"
                    f"{(str(delay) if delay else '-'):>7}  "
                    f"{(r['licence'] or '— unknown —'):<22}{note[:30]}"
                )

    print("-" * 88)
    print(f"  robots-allowed  : {allowed}")
    print(f"  robots-blocked  : {blocked}   <- will not be crawled")
    print(f"  WAF-blocked     : {waf}   <- policy verified by hand in the registry")
    print(f"  licence unknown : {unlicensed}   <- needs a human before publication")

    unreadable = [s for s in sources if s.robots_allows is None]
    if unreadable:
        print(
            f"\n{len(unreadable)} source(s) returned a WAF error and have no manual"
            " verification:"
        )
        for s in unreadable:
            print(f"    - {s.slug} ({s.base_url})")
        print(
            "  These stay uncrawlable. To clear one, read its robots.txt yourself\n"
            "  and add a `robots_verified_manually:` block to sources/registry.yaml.\n"
            "  Do not work around a WAF by disguising the crawler's User-Agent."
        )
    if unlicensed:
        print("\nAn unknown licence does not block reading a page, but it does block")
        print("redistributing its content in a published dataset (Phase 9).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
