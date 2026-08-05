#!/usr/bin/env python3
"""
Measure link rot in the harvested india.gov.in catalogue.

Every record in services.jsonl is fundamentally a pointer to somewhere else, so
the catalogue's usefulness is bounded by how many of those pointers still work.
This takes a random sample and classifies each destination.

Usage:  python check_links.py [sample_size]
Output: data/link_check.json  + a summary to stdout
"""
import json
import random
import ssl
import sys
import socket
import urllib.request
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path(__file__).parent / "data"
SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 400
TIMEOUT = 20
WORKERS = 16
UA = "Mozilla/5.0 (compatible; portfolio-research/1.0)"

# Government sites are riddled with expired/misconfigured certs; we still want to
# know whether the page is *there*, so verify separately from reachability.
LAX = ssl.create_default_context()
LAX.check_hostname = False
LAX.verify_mode = ssl.CERT_NONE


def classify(url):
    """Return (bucket, detail) for one URL."""
    req = urllib.request.Request(url, headers={"User-Agent": UA},
                                 method="GET")
    # first attempt: proper cert verification
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return ("ok", r.status)
    except urllib.error.HTTPError as e:
        return ("http_error", e.code)
    except ssl.SSLCertVerificationError as e:
        pass  # retry without verification below
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        pass
    except Exception:
        pass

    # second attempt: ignore TLS problems, to separate "cert broken" from "gone"
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=LAX) as r:
            return ("tls_broken", r.status)
    except urllib.error.HTTPError as e:
        return ("http_error", e.code)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        low = reason.lower()
        if "name or service not known" in low or "nodename nor servname" in low \
                or "getaddrinfo" in low or "temporary failure in name" in low:
            return ("dns_fail", reason[:60])
        if "timed out" in low or "timeout" in low:
            return ("timeout", reason[:60])
        if "connection refused" in low:
            return ("refused", reason[:60])
        return ("unreachable", reason[:60])
    except (socket.timeout, TimeoutError):
        return ("timeout", "read timeout")
    except Exception as e:
        return ("unreachable", str(e)[:60])


def main():
    rows = [json.loads(l) for l in open(OUT / "services.jsonl")]
    urls = [(r["id"], r["title"], r["url"]) for r in rows if r.get("url")]
    random.seed(42)
    sample = random.sample(urls, min(SAMPLE, len(urls)))
    print(f"Checking {len(sample)} of {len(urls)} destination links "
          f"({WORKERS} workers, {TIMEOUT}s timeout)...\n")

    results = []

    def run(item):
        rid, title, url = item
        bucket, detail = classify(url)
        return {"id": rid, "title": title, "url": url,
                "bucket": bucket, "detail": detail}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, res in enumerate(ex.map(run, sample), 1):
            results.append(res)
            if i % 50 == 0:
                print(f"  ...{i}/{len(sample)}")

    (OUT / "link_check.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    c = Counter(r["bucket"] for r in results)
    n = len(results)
    print(f"\n{'bucket':<16}{'count':>7}{'share':>9}")
    for b, k in c.most_common():
        print(f"{b:<16}{k:>7}{k/n*100:>8.1f}%")

    reachable = c["ok"] + c["tls_broken"]
    print(f"\n  reachable        : {reachable}/{n}  ({reachable/n*100:.1f}%)")
    print(f"  broken/unreachable: {n-reachable}/{n}  ({(n-reachable)/n*100:.1f}%)")

    codes = Counter(r["detail"] for r in results if r["bucket"] == "http_error")
    if codes:
        print("\n  HTTP error codes:", dict(codes.most_common(8)))

    print("\n  examples of dead links:")
    shown = 0
    for r in results:
        if r["bucket"] in ("dns_fail", "http_error", "refused") and shown < 8:
            print(f"    [{r['bucket']}/{r['detail']}] {r['title'][:56]}")
            print(f"        {r['url'][:96]}")
            shown += 1

    print(f"\n  -> {OUT}/link_check.json")


if __name__ == "__main__":
    main()
