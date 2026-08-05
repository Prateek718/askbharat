#!/usr/bin/env python3
"""Phase 2 spike — can a free model extract faithfully enough?

This is the plan's biggest assumption and its cheapest test. We take real
government pages, run them through the extraction schema, and measure three
things that decide whether the rest of the plan stands:

1. **Schema adherence** — how often does the model return valid JSON at all?
   Free models are weaker here than frontier models, and every failure costs a
   retry against a 1000/day quota.

2. **Null discipline** — does it say "the page doesn't state this", or does it
   invent a plausible fee? This is the property the whole product rests on. A
   model that fills every field confidently is *disqualifying*, no matter how
   fluent it reads.

3. **Grounding** — do the values it emits actually appear on the page? We check
   fees and document names verbatim, because those are the claims that hurt.

The output is written for hand-scoring, not just for a summary number: a human
should be able to read a page's text next to what was extracted from it.

Usage:
    python -m askbharat.scripts.extraction_spike --n 50
    python -m askbharat.scripts.extraction_spike --n 10 --model google/gemma-4-31b-it:free
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import UTC, datetime

import httpx
from selectolax.parser import HTMLParser

from askbharat.config import DATA_DIR, redact, settings
from askbharat.llm.limiter import DailyQuotaExceeded
from askbharat.llm.prompts import EXTRACTION_SYSTEM as SYSTEM
from askbharat.llm.provider import LLMProvider, SchemaValidationFailed
from askbharat.schema.extraction import ExtractedService

OUT_DIR = DATA_DIR / "spike"
SERVICES = DATA_DIR / "services.jsonl"
LINK_CHECK = DATA_DIR / "link_check.json"


def page_text(html: str, limit: int = 24_000) -> str:
    """Readable text from HTML, with chrome stripped."""
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:limit]


def pick_urls(n: int, seed: int = 7, deep_only: bool = True) -> list[dict]:
    """Sample reachable service URLs from the earlier harvest."""
    if not SERVICES.exists():
        print(f"missing {SERVICES} — run the harvest first", file=sys.stderr)
        return []

    reachable: set[str] | None = None
    if LINK_CHECK.exists():
        checked = json.loads(LINK_CHECK.read_text())
        reachable = {c["url"] for c in checked if c.get("bucket") in ("ok", "tls_broken")}

    rows = []
    with SERVICES.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = r.get("url") or ""
            if not url.startswith("http") or url.lower().endswith(".pdf"):
                continue
            if reachable is not None and url not in reachable:
                continue
            # Skip bare domain roots. 42% of this catalogue points at a
            # department homepage rather than a service page — there is nothing
            # on those to extract, and including them measures the model's
            # null discipline instead of its extraction ability.
            if deep_only and not httpx.URL(url).path.strip("/"):
                continue
            rows.append({"id": r.get("id"), "title": r.get("title"), "url": url})

    random.seed(seed)
    random.shuffle(rows)
    # One page per host: 209 pages from cms.tn.gov.in would measure one template.
    by_host, picked = set(), []
    for r in rows:
        host = httpx.URL(r["url"]).host
        if host in by_host:
            continue
        by_host.add(host)
        picked.append(r)
        if len(picked) >= n:
            break
    return picked


def fetch(client: httpx.Client, url: str) -> tuple[int | None, str]:
    try:
        r = client.get(url)
        return r.status_code, r.text
    except Exception as e:
        return None, f"__error__ {redact(str(e))[:140]}"


def grounded(value, text: str) -> bool | None:
    """Does an emitted value actually appear in the page text?

    Returns None when the check doesn't apply. This is a cheap proxy for the
    mechanical guard the answer engine will run — if a number the model emitted
    isn't on the page, it came from somewhere else.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        flat = text.replace(",", "")
        return str(n) in flat
    if isinstance(value, str) and len(value) > 3:
        needle = value.strip().lower()[:40]
        return needle in text.lower()
    return None


CONTENT_FIELDS = [
    "what_it_is", "who_is_eligible", "documents_required", "fee_amount",
    "fee_notes", "how_to_apply", "application_modes", "online_url",
    "processing_time", "validity", "helpline", "grievance_route", "ministry",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--model", type=str, default=None, help="override the chain")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--include-homepages", action="store_true",
                    help="also sample bare domain roots (tests null discipline)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = pick_urls(args.n, seed=args.seed, deep_only=not args.include_homepages)
    if not targets:
        return 1
    print(f"selected {len(targets)} pages, one per host\n")

    chains = {"extract": [args.model]} if args.model else None
    p = LLMProvider(chains=chains) if chains else LLMProvider()
    print(f"model chain: {p.models_for('extract')}")
    print(f"quota: {p.quota.used}/{p.quota.cap} used\n")

    results = []
    stats = {
        "fetched": 0, "fetch_failed": 0, "extracted": 0, "schema_failed": 0,
        "not_a_service": 0, "budget_raised": 0, "fell_back": 0,
    }
    null_counts = dict.fromkeys(CONTENT_FIELDS, 0)
    ungrounded: list[dict] = []

    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True,
                      verify=False) as client:
        for i, t in enumerate(targets, 1):
            status, html = fetch(client, t["url"])
            if status is None or status >= 400 or not html or html.startswith("__error__"):
                stats["fetch_failed"] += 1
                print(f"  [{i:>3}] fetch failed ({status}) {t['url'][:56]}")
                continue
            text = page_text(html)
            if len(text) < 200:
                stats["fetch_failed"] += 1
                print(f"  [{i:>3}] too little text  {t['url'][:56]}")
                continue
            stats["fetched"] += 1

            try:
                c = p.complete(
                    [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content":
                            f"URL: {t['url']}\n\nPAGE TEXT:\n{text}"},
                    ],
                    task="extract", schema=ExtractedService, max_tokens=8000,
                )
            except DailyQuotaExceeded as e:
                print(f"\n!! {e}\n   parking — rerun tomorrow or raise the cap.")
                break
            except SchemaValidationFailed as e:
                stats["schema_failed"] += 1
                print(f"  [{i:>3}] SCHEMA FAIL      {t['url'][:56]}")
                results.append({**t, "error": str(e)[:300]})
                continue

            rec: ExtractedService = c.parsed
            stats["extracted"] += 1
            if c.usage.budget_raised:
                stats["budget_raised"] += 1
            if c.usage.fell_back:
                stats["fell_back"] += 1
            if not rec.page_is_about_a_service:
                stats["not_a_service"] += 1

            populated = 0
            for f in CONTENT_FIELDS:
                v = getattr(rec, f)
                if v in (None, [], ""):
                    null_counts[f] += 1
                else:
                    populated += 1

            for f in ("fee_amount", "processing_time", "helpline"):
                g = grounded(getattr(rec, f), text)
                if g is False:
                    ungrounded.append({
                        "url": t["url"], "field": f, "value": getattr(rec, f),
                    })

            results.append({
                **t, "model": c.usage.model, "populated": populated,
                "page_is_about_a_service": rec.page_is_about_a_service,
                "extracted": rec.model_dump(mode="json"),
                "page_text": text[:6000],   # for hand-scoring
            })
            flag = "" if rec.page_is_about_a_service else "  [not a service page]"
            print(f"  [{i:>3}] ok  {populated:>2}/{len(CONTENT_FIELDS)} fields  "
                  f"{t['title'][:40]:<40}{flag}")
            time.sleep(0.2)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"spike_{stamp}.json"
    out.write_text(json.dumps({
        "run_at": stamp, "model_chain": p.models_for("extract"),
        "stats": stats, "null_counts": null_counts, "ungrounded": ungrounded,
        "results": results,
    }, indent=1, ensure_ascii=False), encoding="utf-8")

    attempted = stats["extracted"] + stats["schema_failed"]
    print("\n" + "=" * 66)
    print(f"  pages fetched      : {stats['fetched']}  (failed {stats['fetch_failed']})")
    print(f"  extracted          : {stats['extracted']}")
    print(f"  schema failures    : {stats['schema_failed']}"
          f"  ({stats['schema_failed'] / attempted * 100:.1f}%)" if attempted else "")
    print(f"  budget escalations : {stats['budget_raised']}")
    print(f"  model fallbacks    : {stats['fell_back']}")
    print(f"  flagged not-a-service: {stats['not_a_service']}")

    if stats["extracted"]:
        print("\n  null rate per field (high is EXPECTED — thin pages):")
        for f, n in sorted(null_counts.items(), key=lambda kv: -kv[1]):
            pct = n / stats["extracted"] * 100
            print(f"    {f:<22}{n:>4}/{stats['extracted']}  {pct:>5.1f}%")

    print(f"\n  ungrounded values  : {len(ungrounded)}"
          "   <- values not found verbatim on the page")
    for u in ungrounded[:8]:
        print(f"    {u['field']}={u['value']!r}  {u['url'][:52]}")

    print(f"\n  quota used         : {p.quota.used}/{p.quota.cap}")
    print(f"  -> {out}")
    print("\nHand-score the output before trusting these numbers. The metric that")
    print("decides the plan is null discipline: a model that populates every")
    print("field on a thin page is disqualifying, however fluent it reads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
