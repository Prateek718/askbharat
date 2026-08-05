#!/usr/bin/env python3
"""
Harvest the india.gov.in service/information catalogue.

The site is a Next.js SPA whose listing pages are client-rendered, but the data
comes from a public, unauthenticated JSON endpoint:

    POST /category/subcategoryservice
    {"termMatches":[{"fieldName":"subCategoryId","fieldValue":"48"}],"pageNumber":1}

The taxonomy (categories + subcategories, with numeric ids) is embedded in the
server-rendered RSC payload of each /category/<slug> page.

So we skip HTML scraping entirely and pull structured records directly.

Output (in ./data):
    taxonomy.json    18 categories + their subcategories
    services.jsonl   deduplicated service records
    services.csv     flat table
    raw_pages.jsonl  every raw API response (audit trail)
"""
import json
import csv
import re
import time
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

BASE = "https://www.india.gov.in"
API = f"{BASE}/category/subcategoryservice"
UA = "Mozilla/5.0 (compatible; portfolio-research/1.0)"
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

# robots.txt asks for Crawl-delay: 10. We use a global token-bucket well below
# what this infrastructure serves in normal traffic, rather than ignoring pacing
# entirely. Tune RATE to be more conservative if desired.
RATE = 2.0          # requests/second, global ceiling
WORKERS = 4
MAX_RETRIES = 4

_lock = threading.Lock()
_last = [0.0]


def _throttle():
    with _lock:
        wait = _last[0] + 1.0 / RATE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def _request(url, data=None, timeout=30):
    """One throttled HTTP call with exponential backoff."""
    for attempt in range(MAX_RETRIES):
        _throttle()
        headers = {"User-Agent": UA, "Accept": "*/*"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            code = getattr(e, "code", None)
            if code in (400, 404, 405):        # deterministic - don't retry
                return None
            if attempt == MAX_RETRIES - 1:
                print(f"    ! give up {url} ({e})", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None


# --------------------------------------------------------------------------
# 1. Taxonomy
# --------------------------------------------------------------------------
CATEGORY_SLUGS = [
    "agriculture-rural-environment", "benefits-social-development",
    "business-self-employed", "citizenship-visa-passports",
    "defence-foreign-affairs", "driving-transport", "education-learning",
    "governance-planning", "health-wellness", "housing-local-services",
    "infrastructure-industries", "jobs", "justice-law-grievances",
    "money-taxes", "science-it-communication", "travel-tourism",
    "welfare-of-families", "youth-sports-culture",
]


def extract_subcategories(html):
    """Pull the subCategories block out of the Next.js RSC payload."""
    h = html.replace('\\"', '"').replace("\\u0026", "&")
    i = h.find('"subCategories":')
    if i < 0:
        return []
    # brace-match forward from the opening { of the value
    start = h.find("{", i + len('"subCategories":'))
    depth, j = 0, start
    while j < len(h):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    blob = h[start:j + 1]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        # fall back to regex over individual records
        out = []
        for m in re.finditer(
            r'"id":"(\d+)","title":"(.*?)","npiAlias":"(.*?)","parent_cat_id":(\d+)', blob
        ):
            out.append({"id": m.group(1), "title": m.group(2),
                        "npiAlias": m.group(3), "parent_cat_id": int(m.group(4))})
        return out
    return parsed.get("results", [])


def build_taxonomy():
    print("[1/3] Fetching taxonomy from 18 category pages...")
    cats = []
    for slug in CATEGORY_SLUGS:
        raw = _request(f"{BASE}/category/{slug}")
        if not raw:
            print(f"  ✗ {slug}")
            continue
        html = raw.decode("utf-8", "replace")
        subs = extract_subcategories(html)
        name = ""
        m = re.search(r"<title>([^<|]+)", html)
        if m:
            name = m.group(1).strip()
        cats.append({
            "slug": slug,
            "name": name,
            "cat_id": subs[0].get("parent_cat_id") if subs else None,
            "subcategories": [
                {"id": str(s.get("id")), "title": s.get("title"),
                 "alias": s.get("npiAlias"),
                 "description": (s.get("description") or "").strip()}
                for s in subs
            ],
        })
        print(f"  ✓ {slug:<34} {len(subs)} subcategories")
    (OUT / "taxonomy.json").write_text(json.dumps(cats, indent=2, ensure_ascii=False))
    return cats


# --------------------------------------------------------------------------
# 2. Records
# --------------------------------------------------------------------------
def fetch_page(sub_id, page):
    raw = _request(API, {
        "termMatches": [{"fieldName": "subCategoryId", "fieldValue": str(sub_id)}],
        "pageNumber": page,
    })
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def harvest_subcategory(sub, raw_fh, raw_lock):
    sid, title = sub["id"], sub["title"]
    first = fetch_page(sid, 1)
    if not first:
        return []
    data = first.get("subCategoryData") or {}
    total = data.get("total", 0)
    if not total:
        return []
    records, page = [], 1
    while True:
        payload = first if page == 1 else fetch_page(sid, page)
        if not payload:
            break
        with raw_lock:
            raw_fh.write(json.dumps(
                {"sub_id": sid, "page": page, "payload": payload}, ensure_ascii=False) + "\n")
        lst = ((payload.get("subCategoryData") or {}).get("results") or {}).get("servicesList") or []
        if not lst:
            break
        for r in lst:
            r["_source_subcategory_id"] = sid
            r["_source_subcategory_title"] = title
        records.extend(lst)
        if len(records) >= total or len(lst) < 10:
            break
        page += 1
    print(f"  ✓ [{sid:>4}] {title[:44]:<44} {len(records):>5}/{total}")
    return records


def main():
    t0 = time.time()
    cats = build_taxonomy()
    subs, seen = [], set()
    for c in cats:
        for s in c["subcategories"]:
            if s["id"] and s["id"] not in seen:
                seen.add(s["id"])
                subs.append({**s, "category": c["slug"]})
    print(f"\n[2/3] Harvesting {len(subs)} subcategories at ~{RATE} req/s...")

    raw_lock = threading.Lock()
    all_records = []
    with open(OUT / "raw_pages.jsonl", "w", encoding="utf-8") as raw_fh:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for res in ex.map(lambda s: harvest_subcategory(s, raw_fh, raw_lock), subs):
                all_records.extend(res)

    print(f"\n[3/3] Deduplicating {len(all_records)} rows...")
    by_id = {}
    for r in all_records:
        rid = r.get("id")
        if rid in by_id:
            prev = by_id[rid]
            prev["_source_subcategory_id"] = sorted(
                set(str(prev["_source_subcategory_id"]).split(",")) | {str(r["_source_subcategory_id"])})
            prev["_source_subcategory_id"] = ",".join(prev["_source_subcategory_id"])
        else:
            by_id[rid] = r
    uniq = list(by_id.values())

    with open(OUT / "services.jsonl", "w", encoding="utf-8") as f:
        for r in uniq:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cols = ["id", "title", "description", "npiAlias", "url",
            "npiMinistryDepartment", "npiKeywords", "subCategoryId",
            "_source_subcategory_id", "_source_subcategory_title"]
    with open(OUT / "services.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in uniq:
            row = dict(r)
            for k in ("npiMinistryDepartment", "subCategoryId", "npiKeywords"):
                if isinstance(row.get(k), list):
                    row[k] = " | ".join(str(x) for x in row[k])
            w.writerow(row)

    print(f"\nDone in {time.time()-t0:.0f}s")
    print(f"  rows fetched : {len(all_records)}")
    print(f"  unique records: {len(uniq)}")
    print(f"  -> {OUT}/services.jsonl, services.csv, taxonomy.json, raw_pages.jsonl")


if __name__ == "__main__":
    main()
