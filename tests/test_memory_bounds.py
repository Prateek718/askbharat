"""Tests for the memory bounds added after a harvest exhausted the machine.

The failure these guard against is not a wrong answer, it is a dead process:
two harvests ran together on a 7 GB box, the static fetcher buffered whole
responses before checking their size, and pdfplumber ran unbounded across
twelve threads. Everything here asserts that a limit actually limits — that the
cap is enforced *before* the bytes land, not reported after.
"""
from __future__ import annotations

import gzip
import threading
import time

import httpx

from askbharat.ingest.adapters import static as S
from askbharat.memguard import Throttle, available_mb, wait_for_memory


def _fetcher_with(handler) -> S.StaticFetcher:
    """A StaticFetcher whose client is backed by an in-process transport."""
    f = S.StaticFetcher()
    f._client.close()
    f._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return f


def test_oversize_body_is_never_fully_buffered(monkeypatch):
    """The cap must stop the read, not merely label the result afterwards."""
    monkeypatch.setattr(S, "MAX_PDF_BYTES", 64 * 1024)
    delivered = 0

    def stream():
        nonlocal delivered
        for _ in range(200):                 # 200 x 8 KB = 1.6 MB if unbounded
            delivered += 8192
            yield b"\0" * 8192

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            stream=_IterStream(stream()),
        )

    with _fetcher_with(handler) as f:
        doc = f.fetch("https://example.invalid/big.pdf")

    assert doc.status is S.FetchStatus.TOO_LARGE
    # Read stopped near the cap rather than draining the whole response.
    assert delivered <= 64 * 1024 + 8192 * 2, f"buffered {delivered} bytes"


def test_declared_oversize_is_rejected_without_reading_a_byte(monkeypatch):
    monkeypatch.setattr(S, "MAX_PDF_BYTES", 64 * 1024)
    read = False

    def stream():
        nonlocal read
        read = True
        yield b"\0" * 1024

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf",
                     "content-length": str(10 * 1024 * 1024)},
            stream=_IterStream(stream()),
        )

    with _fetcher_with(handler) as f:
        doc = f.fetch("https://example.invalid/huge.pdf")

    assert doc.status is S.FetchStatus.TOO_LARGE
    assert not read, "content-length should have short-circuited the download"


def test_html_still_decodes_with_declared_charset():
    """Streaming must not cost us httpx's charset handling."""
    body = "Ministry of Coöperation — ₹1,00,000 benefit".encode("iso-8859-1",
                                                                "replace")

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=iso-8859-1"},
            content=b"<html><body><article><p>" + body + b"</p></article></body></html>",
        )

    with _fetcher_with(handler) as f:
        doc = f.fetch("https://example.invalid/page.html")

    assert doc.kind is S.DocKind.HTML
    assert "Min" in doc.text and "�" not in doc.text[:20]


def test_gzipped_html_is_not_decompressed_twice():
    """Regression: streaming yields decoded bytes, so the transfer headers lie.

    Rebuilding a response around already-decompressed bytes while keeping
    `content-encoding: gzip` made httpx gunzip them again and raise, which
    killed a whole harvest run on the first compressed page it met.
    """
    html = (b"<html><body><article><p>"
            + b"Application form for a ration card. " * 20
            + b"</p></article></body></html>")

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8",
                     "content-encoding": "gzip"},
            content=gzip.compress(html),
        )

    with _fetcher_with(handler) as f:
        doc = f.fetch("https://example.invalid/gzipped.html")

    assert doc.status is S.FetchStatus.OK, doc.detail
    assert "ration card" in doc.text


def test_pdfplumber_is_skipped_above_the_size_cap(monkeypatch):
    """A big corrupt PDF keeps pypdf's output and says so, rather than OOMing."""
    monkeypatch.setattr(S, "MAX_PDFPLUMBER_BYTES", 1024)
    monkeypatch.setattr(S, "_pypdf_text", lambda b: ("Averyveryverylongglued" + "word" * 10, 1))

    def boom(body):
        raise AssertionError("pdfplumber must not run on an oversized document")

    monkeypatch.setattr(S, "_pdfplumber_text", boom)

    text, pages, note = S.extract_pdf(b"%PDF-" + b"x" * 4096)
    assert "pdfplumber skipped" in note
    assert "extractor=pypdf" in note


def test_pdfplumber_still_runs_on_a_small_corrupt_document(monkeypatch):
    monkeypatch.setattr(S, "MAX_PDFPLUMBER_BYTES", 1 << 20)
    monkeypatch.setattr(S, "_pypdf_text",
                        lambda b: ("Deputationofprogressivefarmerstomainland", 1))
    monkeypatch.setattr(S, "_pdfplumber_text",
                        lambda b: "Deputation of progressive farmers to mainland")

    text, pages, note = S.extract_pdf(b"%PDF-small")
    assert note.startswith("extractor=pdfplumber")
    assert "progressive farmers" in text


def test_throttle_caps_concurrent_entries():
    """Worker count must stop being the peak-memory multiplier."""
    t = Throttle("test", slots=2)
    inside = 0
    peak = 0
    lock = threading.Lock()

    def work():
        nonlocal inside, peak
        with t:
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert peak <= 2, f"{peak} threads were inside a 2-slot throttle"


def test_available_mb_reports_a_plausible_number():
    mb = available_mb()
    assert mb > 0
    # Sanity, not a threshold: a machine reporting terabytes free means we are
    # reading the wrong field or the wrong units.
    assert mb < 1 << 22


def test_wait_for_memory_times_out_instead_of_hanging(monkeypatch):
    """An unsatisfiable floor must return False, not block the harvest forever."""
    monkeypatch.setattr("askbharat.memguard.available_mb", lambda: 10)
    start = time.monotonic()
    ok = wait_for_memory(floor_mb=1 << 20, timeout_s=0.3, poll_s=0.05)
    assert ok is False
    assert time.monotonic() - start < 3


def test_wait_for_memory_returns_immediately_when_satisfied():
    assert wait_for_memory(floor_mb=1, timeout_s=1) is True


class _IterStream(httpx.SyncByteStream):
    """Wraps a generator so MockTransport can serve it lazily."""

    def __init__(self, gen):
        self._gen = gen

    def __iter__(self):
        return iter(self._gen)
