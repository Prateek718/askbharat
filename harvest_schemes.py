#!/usr/bin/env python3
"""
Harvest all government schemes from india.gov.in.

Endpoint (public, unauthenticated, honours a large pageSize):

    POST /my-government/schemes/search/dataservices/getschemes
    {"categories":[],"mustFilter":[],"pageNumber":1,"pageSize":500}

Also pulls the facet list (categories / states / ministries) from getSchemeFacets.

Output (in ./data):
    schemes.jsonl
    schemes.csv
    scheme_facets.json
"""
import json
import csv
import time
import urllib.request
from pathlib import Path

BASE = "https://www.india.gov.in/my-government/schemes/search/dataservices"
UA = "Mozilla/5.0 (compatible; portfolio-research/1.0)"
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

PAGE_SIZE = 500
RATE = 2.0
MAX_SWEEPS = 40           # hard ceiling on repeated full sweeps
STOP_AFTER_BARREN = 4     # stop once N consecutive sweeps yield nothing new


def post(path, payload, retries=4):
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{BASE}/{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ! failed {path}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def find_results(obj):
    """Locate the list of scheme dicts wherever it sits in the response."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "results" and isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            found = find_results(v)
            if found is not None:
                return found
    return None


def find_total(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("total", "totalCount") and isinstance(v, int):
                return v
            found = find_total(v)
            if found is not None:
                return found
    return None


def main():
    t0 = time.time()

    print("[1/2] Fetching scheme facets...")
    facets = post("getSchemeFacets", {"categories": [], "mustFilter": []})
    if facets:
        (OUT / "scheme_facets.json").write_text(
            json.dumps(facets, indent=2, ensure_ascii=False))
        print("  ✓ scheme_facets.json")

    # The endpoint's pagination is NOT deterministic: every sweep returns exactly
    # `total` rows, but with a randomly varying set of duplicates and omissions.
    # A single sweep therefore yields only ~75-80% of the catalogue. We repeat
    # full sweeps and union the results until they stop yielding new records.
    print(f"[2/2] Sweeping schemes at pageSize={PAGE_SIZE} until saturation...")
    seen, uniq, total = set(), [], None
    sweep = 0
    barren = 0
    while sweep < MAX_SWEEPS and barren < STOP_AFTER_BARREN:
        sweep += 1
        rows_this = 0
        new_this = 0
        page = 1
        while True:
            d = post("getschemes", {"categories": [], "mustFilter": [],
                                    "pageNumber": page, "pageSize": PAGE_SIZE})
            if not d:
                break
            if total is None:
                total = find_total(d)
                print(f"  reported total: {total}")
            rows = find_results(d) or []
            if not rows:
                break
            rows_this += len(rows)
            for r in rows:
                key = (r.get("slug"), r.get("title"))
                if key not in seen:
                    seen.add(key)
                    uniq.append(r)
                    new_this += 1
            if len(rows) < PAGE_SIZE:
                break
            page += 1
            time.sleep(1.0 / RATE)
        pct = (len(uniq) / total * 100) if total else 0
        print(f"  sweep {sweep:>2}: {rows_this} rows, +{new_this:<4} new "
              f"-> {len(uniq)} unique ({pct:.1f}% of reported {total})")
        barren = barren + 1 if new_this == 0 else 0

    with open(OUT / "schemes.jsonl", "w", encoding="utf-8") as f:
        for r in uniq:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cols = ["title", "slug", "description", "ministry", "npiMinistry",
            "schemeCategory", "beneficiaryState", "tags"]
    with open(OUT / "schemes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in uniq:
            row = dict(r)
            for k in cols:
                if isinstance(row.get(k), list):
                    row[k] = " | ".join(str(x) for x in row[k])
            w.writerow(row)

    print(f"\nDone in {time.time()-t0:.0f}s after {sweep} sweeps")
    print(f"  reported total : {total}")
    print(f"  unique schemes : {len(uniq)}")
    if total:
        print(f"  shortfall      : {total - len(uniq)} "
              f"({(total-len(uniq))/total*100:.1f}% of the advertised count "
              f"never appeared)")
    print(f"  -> {OUT}/schemes.jsonl, schemes.csv, scheme_facets.json")


if __name__ == "__main__":
    main()
