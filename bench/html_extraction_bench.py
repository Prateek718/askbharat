#!/usr/bin/env python3
"""Head-to-head: our stack vs RAGFlow's DeepDoc HTML parser.

Fairness rules this benchmark holds to:

- Every method gets the **same bytes**. The page is fetched once and the same
  HTML string is handed to all three extractors, so network variance and site
  throttling cannot favour one.
- The downstream stage is **identical**: same extraction schema, same prompt,
  same model chain. The only variable is the preprocessor.
- The metric that decides it is **downstream field recall**, not character
  count. Extracting more characters is not better if the extra characters are
  navigation menus — noise actively hurts the model.

Why only the HTML parser and not the full RAGFlow: RAGFlow requires >=16 GB RAM
(Elasticsearch + MySQL + Redis + MinIO). This machine has 7.3 GB. A benchmark
run on a thrashing box would be meaningless, so we test the component that
actually does the document processing, in isolation, on equal terms.
"""
from __future__ import annotations

import json
import random
import re
import statistics
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import httpx
import trafilatura
from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ragflow_html_parser import RAGFlowHtmlParser  # noqa: E402

from askbharat.config import DATA_DIR, settings  # noqa: E402
from askbharat.llm.provider import LLMProvider, SchemaValidationFailed  # noqa: E402
from askbharat.schema.extraction import ExtractedService  # noqa: E402
from askbharat.scripts.extraction_spike import SYSTEM  # noqa: E402

OUT = DATA_DIR / "bench_html.json"

CONTENT_FIELDS = [
    "what_it_is", "who_is_eligible", "documents_required", "fee_amount",
    "fee_notes", "how_to_apply", "application_modes", "online_url",
    "processing_time", "validity", "helpline", "email", "office_address",
    "grievance_route", "ministry",
]

# Navigation furniture that appears on nearly every Indian government portal.
# Used as a boilerplate proxy: the more of these survive extraction, the more
# chrome the model has to read past to find the answer.
BOILERPLATE = [
    "skip to main content", "screen reader access", "text size", "a+ a-",
    "sitemap", "site map", "terms and conditions", "privacy policy",
    "copyright policy", "hyperlinking policy", "last updated", "visitor",
    "accessibility", "font size", "colour scheme", "help", "feedback",
]


# ---------------------------------------------------------------- extractors
def extract_ours(html: str) -> str:
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "svg", "iframe",
                "header", "footer", "nav"):
        for n in tree.css(tag):
            n.decompose()
    body = tree.body or tree.root
    return (body.text(separator="\n", strip=True) if body else "").strip()


def extract_trafilatura(html: str) -> str:
    return (trafilatura.extract(
        html, include_tables=True, include_links=False,
        favor_recall=True, no_fallback=False,
    ) or "").strip()


def extract_ragflow(html: str) -> str:
    try:
        chunks = RAGFlowHtmlParser.parser_txt(html, 512)
        return "\n\n".join(chunks).strip()
    except Exception as e:
        return f"__error__ {type(e).__name__}: {str(e)[:80]}"


METHODS = {
    "ours (selectolax)": extract_ours,
    "trafilatura": extract_trafilatura,
    "RAGFlow deepdoc": extract_ragflow,
}


# ------------------------------------------------------------------ measures
def boilerplate_ratio(text: str) -> float:
    low = text.lower()
    hits = sum(low.count(b) for b in BOILERPLATE)
    lines = max(len([x for x in text.splitlines() if x.strip()]), 1)
    return hits / lines


# A page can only exercise an extractor if it actually contains the kind of
# content we extract. The first run sampled telephone directories and tourism
# blurbs, where every method scored ~0 fields — that measured the corpus, not
# the extractors. Qualify the sample on content signal first.
SERVICE_SIGNAL = re.compile(
    r"eligib|documents?\s+required|how\s+to\s+apply|application\s+(process|form)|"
    r"who\s+can\s+apply|fee|beneficiar|criteria", re.I)


def qualifies(html: str) -> bool:
    return len(SERVICE_SIGNAL.findall(html)) >= 3


def pick_pages(n: int, seed: int = 5) -> list[dict]:
    """Reachable deep links only — homepages have nothing to extract."""
    checked = json.loads((DATA_DIR / "link_check.json").read_text())
    reach = {c["url"] for c in checked if c.get("bucket") in ("ok", "tls_broken")}
    rows = []
    for line in (DATA_DIR / "services.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        u = r.get("url") or ""
        if not u.startswith("http") or u.lower().endswith(".pdf"):
            continue
        if u not in reach or not httpx.URL(u).path.strip("/"):
            continue
        rows.append({"title": r["title"], "url": u})
    random.seed(seed)
    random.shuffle(rows)
    seen, out = set(), []
    for r in rows:
        h = httpx.URL(r["url"]).host
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
        if len(out) >= n:
            break
    return out


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    pages = pick_pages(n * 5)   # over-sample; most will not qualify
    print(f"benchmarking {len(pages)} pages x {len(METHODS)} extractors\n")

    p = LLMProvider()
    results = []
    headers = {"User-Agent": settings.user_agent}

    with httpx.Client(timeout=35, follow_redirects=True, verify=False,
                      headers=headers) as c:
        for i, page in enumerate(pages, 1):
            try:
                r = c.get(page["url"])
                if r.status_code != 200 or len(r.text) < 500:
                    print(f"  [{i:>2}] skip (HTTP {r.status_code})")
                    continue
                html = r.text
                if not qualifies(html):
                    print(f"  [{i:>2}] skip (no service content) "
                          f"{page['title'][:44]}")
                    continue
            except Exception as e:
                print(f"  [{i:>2}] skip ({type(e).__name__})")
                continue

                
            if len(results) >= n:
                break
            row = {"title": page["title"], "url": page["url"], "methods": {}}
            print(f"  [{i:>2}] {page['title'][:56]}")

            for name, fn in METHODS.items():
                t0 = time.time()
                text = fn(html)
                elapsed = time.time() - t0
                if text.startswith("__error__"):
                    row["methods"][name] = {"error": text}
                    print(f"        {name:<20} ERROR {text[:44]}")
                    continue

                populated, err = 0, None
                try:
                    comp = p.complete(
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content":
                          f"URL: {page['url']}\n\nPAGE TEXT:\n{text[:24000]}"}],
                        task="extract", schema=ExtractedService, max_tokens=8000,
                    )
                    d = comp.parsed.model_dump()
                    populated = sum(
                        1 for f in CONTENT_FIELDS if d.get(f) not in (None, [], "")
                    )
                except SchemaValidationFailed as e:
                    err = str(e)[:90]
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:70]}"

                row["methods"][name] = {
                    "chars": len(text),
                    "boilerplate": round(boilerplate_ratio(text), 4),
                    "extract_s": round(elapsed, 3),
                    "fields": populated,
                    "error": err,
                }
                print(f"        {name:<20}{len(text):>7}ch  "
                      f"boiler={boilerplate_ratio(text):.3f}  "
                      f"{elapsed * 1000:>6.0f}ms  fields={populated}"
                      f"{'  ERR ' + err[:30] if err else ''}")
            results.append(row)

    OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False))

    print("\n" + "=" * 74)
    print(f"{'method':<22}{'pages':>6}{'med chars':>11}{'boiler':>9}"
          f"{'med ms':>9}{'MED FIELDS':>12}{'fails':>7}")
    print("-" * 74)
    for name in METHODS:
        rows = [r["methods"].get(name, {}) for r in results]
        rows = [x for x in rows if x and "chars" in x]
        if not rows:
            print(f"{name:<22} no successful runs")
            continue
        fails = sum(1 for x in rows if x.get("error"))
        print(f"{name:<22}{len(rows):>6}"
              f"{statistics.median(x['chars'] for x in rows):>11.0f}"
              f"{statistics.median(x['boilerplate'] for x in rows):>9.3f}"
              f"{statistics.median(x['extract_s'] for x in rows) * 1000:>9.0f}"
              f"{statistics.median(x['fields'] for x in rows):>12.1f}"
              f"{fails:>7}")
    print(f"\nquota used: {p.quota.used}/{p.quota.cap}")
    print(f"-> {OUT}")
    print("\nField recall is the decider. Character count only matters insofar as")
    print("the characters are content — boilerplate makes the model's job harder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
