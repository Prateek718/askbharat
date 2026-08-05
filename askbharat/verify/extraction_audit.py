#!/usr/bin/env python3
"""Verify that extracted text faithfully represents the source document.

Status counts prove a fetch succeeded. They prove nothing about whether the
text is *right*. This checks fidelity from four independent angles, none of
which trusts the extractor's own report:

1. **Coverage** — re-extract each page individually and confirm every page's
   content is present in the stored text. Catches silent page loss, which a
   character count cannot.
2. **Spacing integrity** — the failure that motivated the pdfplumber fallback.
   `A ssurance ... gro wers` reads fine to a human and is destroyed for search.
3. **Lexical sanity** — real prose has a plausible proportion of very common
   words. Garbled or encoding-mangled output does not.
4. **Token recall** — compare our stored tokens against a fresh re-extraction
   by an independent library. Invariant to line-breaking and hyphenation, so
   it measures content loss rather than extractor disagreement.

Usage:
    python -m askbharat.verify.extraction_audit --limit 15
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import warnings
from collections import Counter

warnings.filterwarnings("ignore")

import httpx
from pypdf import PdfReader

from askbharat.config import DATA_DIR, settings
from askbharat.ingest.adapters.static import (
    _pdfplumber_text,
    extract_html,
    is_corrupt,
    spacing_corruption,
)

DOCS = DATA_DIR / "static_docs.jsonl"

# Very common English words. Prose containing almost none of these is usually
# mangled, encoding-broken, or not prose at all.
COMMON = {
    "the", "of", "and", "to", "in", "for", "is", "be", "shall", "will", "or",
    "by", "with", "as", "that", "on", "at", "from", "this", "any", "may",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def lexical_sanity(text: str) -> dict:
    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return {"words": 0, "common_pct": 0.0}
    low = [w.lower() for w in words]
    return {
        "words": len(words),
        "common_pct": sum(1 for w in low if w in COMMON) / len(low) * 100,
    }


def tokens(text: str) -> Counter:
    """Bag of content tokens, with line-break hyphenation rejoined.

    Token recall replaced an earlier phrase-matching check that reported only
    65% fidelity. Investigating those "misses" showed the content was present
    and correct — `Sub- sequently`, `Agri- Business`, `MBA Fi-` — pypdf and
    pdfplumber simply disagree about hyphenated line-breaks and column reading
    order. That test was measuring extractor disagreement, not data loss.
    Comparing token bags is invariant to both and measures what we care about:
    did we capture the source's words?
    """
    text = re.sub(r"-\s+", "", text)          # rejoin words split across lines
    return Counter(re.findall(r"[a-z0-9]{3,}", text.lower()))


def token_recall(stored: str, source: str) -> tuple[float, float]:
    """(recall of source tokens, share of stored tokens not in source)."""
    st, src = tokens(stored), tokens(source)
    src_total = sum(src.values()) or 1
    st_total = sum(st.values()) or 1
    return (
        sum((st & src).values()) / src_total * 100,
        sum((st - src).values()) / st_total * 100,
    )


def audit_pdf(body: bytes, stored: str) -> dict:
    reader = PdfReader(io.BytesIO(body))
    n_pages = len(reader.pages)
    stored_n = norm(stored)

    covered = missing = 0
    for page in reader.pages[:30]:
        try:
            t = (page.extract_text() or "").strip()
        except Exception:
            continue
        words = re.findall(r"\S+", t)
        if len(words) < 8:
            continue                  # blank/near-blank page, nothing to check
        probe = norm(" ".join(words[:6]))
        if probe and probe in stored_n:
            covered += 1
        else:
            missing += 1

    recall, extra = token_recall(stored, _pdfplumber_text(body))
    return {"pages": n_pages, "pages_covered": covered,
            "pages_missing": missing, "recall": recall, "extra": extra}


def audit_html(html: str, stored: str) -> dict:
    fresh, _ = extract_html(html)
    recall, extra = token_recall(stored, fresh)
    return {"recall": recall, "extra": extra}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    if not DOCS.exists():
        print(f"missing {DOCS}", file=sys.stderr)
        return 1

    rows = [json.loads(x) for x in DOCS.open(encoding="utf-8")]
    ok = [r for r in rows if r["status"] == "ok" and len(r.get("text") or "") > 300]
    random.seed(args.seed)
    random.shuffle(ok)
    sample = ok[:args.limit]
    print(f"auditing {len(sample)} of {len(ok)} successfully-extracted documents\n")

    hdr = (f"{'document':<36}{'kind':>6}{'pages':>7}{'covrg':>8}"
           f"{'RECALL':>9}{'extra':>7}{'1-ltr%':>8}{'common%':>9}")
    print(hdr)
    print("-" * len(hdr))

    agg = {"cov_ok": 0, "cov_miss": 0}
    recalls: list[float] = []
    flagged: set[str] = set()

    with httpx.Client(timeout=60, verify=False, follow_redirects=True,
                      headers={"User-Agent": settings.user_agent}) as c:
        for r in sample:
            try:
                resp = c.get(r["url"])
                if resp.status_code != 200:
                    print(f"{r['title'][:34]:<36}  source now HTTP {resp.status_code}")
                    continue
                body = resp.content
            except Exception as e:
                print(f"{r['title'][:34]:<36}  refetch failed ({type(e).__name__})")
                continue

            stored = r["text"]
            pct, spaced, glued = spacing_corruption(stored)
            lex = lexical_sanity(stored)

            if r["kind"] == "pdf" and body[:4] == b"%PDF":
                a = audit_pdf(body, stored)
                agg["cov_ok"] += a["pages_covered"]
                agg["cov_miss"] += a["pages_missing"]
                denom = a["pages_covered"] + a["pages_missing"]
                cov = f"{a['pages_covered']}/{denom}" if denom else "-"
                pages = str(a["pages"])
            else:
                a = audit_html(resp.text, stored)
                cov, pages = "-", "-"

            recalls.append(a["recall"])

            flag = ""
            if a["recall"] < 90:
                flag += "  <-- LOW RECALL"
                flagged.add(r["title"][:52])
            if is_corrupt(stored):
                flag += "  <-- BOUNDARIES"
                flagged.add(r["title"][:52])
            if lex["common_pct"] < 4 and lex["words"] > 200:
                flag += "  <-- NOT PROSE"
                flagged.add(r["title"][:52])

            print(f"{r['title'][:34]:<36}{r['kind']:>6}{pages:>7}{cov:>8}"
                  f"{a['recall']:>8.1f}%{a['extra']:>6.1f}%"
                  f"{pct:>7.1f}%{lex['common_pct']:>8.1f}%{flag}")

    print("-" * len(hdr))
    cov_tot = agg["cov_ok"] + agg["cov_miss"]
    if cov_tot:
        print(f"  PDF page coverage : {agg['cov_ok']}/{cov_tot} "
              f"({agg['cov_ok'] / cov_tot * 100:.1f}%) of non-blank pages present")
    if recalls:
        recalls.sort()
        print(f"  token recall      : mean {sum(recalls) / len(recalls):.1f}%  "
              f"median {recalls[len(recalls) // 2]:.1f}%  worst {recalls[0]:.1f}%")
    print(f"  documents flagged : {len(flagged)}")
    for b in sorted(flagged):
        print(f"      - {b}")
    print("\n  Token recall is the fidelity measure: what share of the source's")
    print("  words we actually captured. Page coverage catches silent page loss.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
