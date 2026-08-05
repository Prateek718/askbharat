"""Static fetching for plain HTML and binary documents.

Tool choices here were benchmarked, not assumed (see bench/html_extraction_bench.py):

**HTML → trafilatura.** Head-to-head against our own selectolax extractor and
RAGFlow's DeepDoc HTML parser on 8 content-qualified government pages, feeding
identical bytes into an identical extraction stage:

    trafilatura      13 fields   0.0% markup leakage
    ours/selectolax  11 fields   0.0%
    RAGFlow deepdoc   2 fields  70.1%  <- dumped raw <td class=...> into "text"

trafilatura also renders tables as markdown, which matters: a lot of government
content is "notification | date | download link" rows, and losing the row
structure loses the association between them.

**PDF → pypdf.** A 40-PDF sample of the corpus found 68% unfetchable (PDFs rot
faster than the 39.6% corpus average) and, of those that fetch, **92% carry a
text layer**. Only ~8% are scanned. The heavyweight ML pipelines (MinerU,
marker, surya, docling) exist to solve OCR and layout — a problem we have about
30 documents' worth of, at a cost of ~2.5 GB of torch. Scanned PDFs are flagged
here rather than silently dropped, so that gap stays visible and countable.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
import trafilatura
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from askbharat.config import redact, settings
from askbharat.memguard import Throttle

log = logging.getLogger(__name__)

# Scanned-PDF detection has to be per-page, not absolute. A 16-page policy
# document that yields 200 characters is plainly a scan, while a 1-page form
# with 200 characters may be perfectly real. An absolute floor mislabels both.
MIN_PDF_TEXT = 200           # a document this short overall is never useful
MIN_CHARS_PER_PAGE = 80      # below this the "text layer" is page furniture
MAX_PDF_BYTES = 40 * 1024 * 1024
MAX_PDF_PAGES = 60          # beyond this it is a manual, not a service page

# pdfplumber builds a Python object per character, per line and per rect on
# every page it touches. On a small form that is nothing; on a 20 MB scanned
# annexure it is over a gigabyte, and it was the largest single contributor to
# the run that exhausted this machine's RAM. pypdf's output on a file that big
# is kept as-is: corrupt word boundaries in a 20 MB manual cost some retrieval
# quality, an OOM costs the whole harvest.
MAX_PDFPLUMBER_BYTES = 6 * 1024 * 1024

# However many workers the caller starts, at most this many documents are being
# parsed at once. Parsing is where the memory goes; fetching is where the time
# goes. Separating the two lets worker count stay a throughput dial without
# also being the peak-memory multiplier.
_PDF_PARSE = Throttle("pdf-parse", slots=3)


class DocKind(StrEnum):
    HTML = "html"
    PDF = "pdf"
    OFFICE = "office"        # .doc/.xls/.ppt — not parsed yet, recorded as a gap
    UNKNOWN = "unknown"


class FetchStatus(StrEnum):
    OK = "ok"
    SCANNED_PDF = "scanned_pdf"      # reachable, but needs OCR we don't run
    UNPARSEABLE = "unparseable"
    HTTP_ERROR = "http_error"
    UNREACHABLE = "unreachable"
    TOO_LARGE = "too_large"


@dataclass
class StaticDoc:
    url: str
    status: FetchStatus
    kind: DocKind = DocKind.UNKNOWN
    http_status: int | None = None
    text: str = ""
    content_hash: str = ""
    body_bytes: int = 0
    pages: int | None = None
    detail: str = ""
    # Markdown tables trafilatura recovered — kept separately because they carry
    # row structure that flattens into noise once concatenated into prose.
    tables: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK and len(self.text) > 100


def _sha(b: bytes | str) -> str:
    if isinstance(b, str):
        b = b.encode("utf-8", "replace")
    return hashlib.sha256(b).hexdigest()


def _kind_from(url: str, content_type: str, body: bytes) -> DocKind:
    """Trust the bytes over the extension — plenty of .aspx URLs serve PDFs."""
    if body[:4] == b"%PDF":
        return DocKind.PDF
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return DocKind.PDF
    if any(x in ct for x in ("html", "xml", "text/plain")):
        return DocKind.HTML
    if re.search(r"\.(docx?|xlsx?|pptx?)(\?|$)", url, re.I):
        return DocKind.OFFICE
    if any(x in ct for x in ("msword", "officedocument", "excel", "powerpoint")):
        return DocKind.OFFICE
    return DocKind.HTML if body[:512].lstrip()[:1] == b"<" else DocKind.UNKNOWN


def _decode(body: bytes, headers) -> str:
    """Bytes to text, using httpx's own charset logic.

    Government pages declare every encoding there is and plenty declare none,
    so a flat utf-8 decode mangles the Latin-1 ones. Streaming the download
    means no response object is left holding the body, so one is rebuilt around
    the bytes purely to borrow that logic.

    The transfer headers must be dropped first. `iter_bytes()` has already
    decompressed the body, and handing httpx back a `content-encoding: gzip`
    alongside plain bytes makes it try to gunzip them a second time — which
    fails, loudly, on every compressed page in the corpus.
    """
    clean = {
        k: v for k, v in headers.items()
        if k.lower() not in ("content-encoding", "content-length",
                             "transfer-encoding")
    }
    try:
        return httpx.Response(200, headers=clean, content=body).text
    except Exception:
        return body.decode("utf-8", "replace")


def extract_html(html: str) -> tuple[str, list[str]]:
    """Main text plus any markdown tables trafilatura recovered."""
    text = trafilatura.extract(
        html,
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=True,      # government pages bury content in odd wrappers
        no_fallback=False,      # let it fall back rather than return nothing
    ) or ""
    tables = [
        blk for blk in re.split(r"\n{2,}", text)
        if blk.count("|") >= 4 and blk.count("\n") >= 1
    ]
    return text.strip(), tables


_SPACED_RUN = re.compile(r"(?:\b[A-Za-z]\s){4,}")
# The longest words in real Indian government prose ("responsibilities",
# "Panchayati") sit well under 25 characters. Anything longer is almost always
# several words glued together.
_GLUED = re.compile(r"\b[A-Za-z]{25,}\b")


def spacing_corruption(text: str) -> tuple[float, int, int]:
    """Detect PDFs whose word boundaries came out wrong — in either direction.

    Two opposite failures, both of which destroy retrieval while still looking
    readable to a human skimming the text:

    **Extra spaces.** Some PDFs position every glyph individually, and pypdf
    emits 'A ssurance of a remunerative ... for gro wers/farmers' or even
    'r e m u n e r a t i v e  p r i c e s'.

    **Missing spaces.** Others carry no explicit word breaks, producing
    'AnimalHusbandrySector' and
    'Deputationofprogressivefarmerstomainlandforexposurevisit'. This one was
    found only after adding the first check — a detector that looks for extra
    spaces scores glued text as perfectly clean.

    Either way BM25 will never match the real word and the embedding is noise.

    Returns (single-letter word %, spaced-letter runs, glued mega-words).
    """
    words = re.findall(r"\S+", text)
    if not words:
        return 0.0, 0, 0
    singles = sum(1 for w in words if len(w) == 1 and w.isalpha())
    return (
        singles / len(words) * 100,
        len(_SPACED_RUN.findall(text)),
        len(_GLUED.findall(text)),
    )


def is_corrupt(text: str) -> bool:
    """Whether word boundaries are broken badly enough to re-extract.

    A *single* 25-character alphabetic run is already conclusive — no English
    word is that long — so glued text triggers on one occurrence, while spaced
    text needs a few runs to rule out coincidence. False positives are cheap:
    the fallback only replaces the text if the second extractor scores better.
    """
    pct, spaced, glued = spacing_corruption(text)
    return pct > 8 or spaced > 2 or glued >= 1


def _pypdf_text(body: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(body))
    n = len(reader.pages)
    parts = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:            # one bad page must not lose the document
            continue
    return "\n".join(parts), n


def _pdfplumber_text(body: bytes) -> str:
    import pdfplumber  # imported lazily; it is the slow path

    parts = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
    return "\n".join(parts)


def extract_pdf(body: bytes) -> tuple[str, int, str]:
    """Return (text, page_count, note). Empty text means the PDF needs OCR.

    pypdf runs first because it is several times faster. When its output shows
    spacing corruption we re-extract with pdfplumber, whose layout-aware
    algorithm reconstructs word boundaries correctly. Measured on a corpus PDF
    that pypdf mangled: 14.8% single-letter words and 73 spaced runs became
    0.5% and 0. Roughly 1 in 10 documents needs this, so paying pdfplumber's
    cost on every file would be wasteful — and trusting pypdf alone would
    silently poison that tenth.

    The fallback is also size-gated: above `MAX_PDFPLUMBER_BYTES` it is skipped
    and the note records the skip, so the quality loss stays countable rather
    than becoming an invisible difference between big and small documents.
    """
    with _PDF_PARSE:
        text, n = _pypdf_text(body)
        used = "pypdf"
        skipped_big = False

        if is_corrupt(text):
            if len(body) > MAX_PDFPLUMBER_BYTES:
                skipped_big = True
            else:
                try:
                    alt = _pdfplumber_text(body)
                    # Only switch if it is genuinely better; on some files
                    # pdfplumber returns less text and we would trade
                    # corruption for loss.
                    better = spacing_corruption(alt) < spacing_corruption(text)
                    if better and len(alt) > len(text) * 0.6:
                        text, used = alt, "pdfplumber"
                except Exception as e:
                    log.debug("pdfplumber fallback failed: %s", str(e)[:100])

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    notes = [f"extractor={used}"]
    if skipped_big:
        notes.append(f"pdfplumber skipped: {len(body) // 1024 // 1024} MB "
                     f"exceeds {MAX_PDFPLUMBER_BYTES // 1024 // 1024} MB memory cap")
    if n > MAX_PDF_PAGES:
        notes.append(f"truncated at {MAX_PDF_PAGES} of {n} pages")
    if is_corrupt(text):
        pct, spaced, glued = spacing_corruption(text)
        notes.append(f"WORD BOUNDARIES STILL BROKEN "
                     f"({pct:.1f}% 1-letter, {spaced} spaced, {glued} glued)")
    return text, n, "; ".join(notes)


class StaticFetcher:
    def __init__(
        self,
        timeout: float = 30.0,
        verify_tls: bool = False,
        connect_timeout: float = 8.0,
        max_connections: int = 40,
    ):
        # Split timeouts. Nearly 40% of this corpus is dead links, and a dead
        # host costs the *connect* timeout on every attempt — that is where the
        # wall-clock goes, not on reading real documents. A short connect
        # timeout fails fast on rot while a generous read timeout still allows
        # a slow 3 MB PDF to finish.
        #
        # verify_tls=False by default: the link audit found 20 of 302 reachable
        # government hosts serve broken certificates. Refusing them would drop
        # real content. TLS failures are still recorded on the document.
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=True,
            verify=verify_tls,
            limits=httpx.Limits(max_connections=max_connections),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/pdf,*/*",
            },
        )
        # One in-flight request per host, however many workers are running.
        # The corpus spans 5,414 distinct hosts, so wide concurrency is fast
        # *and* polite — but two simultaneous hits on one small department
        # server would not be.
        self._host_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _host_lock(self, url: str) -> threading.Lock:
        host = httpx.URL(url).host or ""
        with self._locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def __enter__(self) -> StaticFetcher:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def fetch(self, url: str) -> StaticDoc:
        with self._host_lock(url):
            return self._fetch(url)

    def _read_capped(self, r: httpx.Response) -> bytes | None:
        """Read the body, giving up the moment it exceeds the cap.

        `client.get()` buffers the whole response before anyone can look at its
        size, so a single mislinked 500 MB dataset dump — and this corpus points
        at data portals — was enough to blow the process no matter what the cap
        said afterwards. Streaming makes the cap actually bound the memory
        rather than merely label the outcome. None means "too large".
        """
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_PDF_BYTES:
            return None                     # not a byte read

        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_bytes():
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                return None                 # connection closes on context exit
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch(self, url: str) -> StaticDoc:
        try:
            with self._client.stream("GET", url) as r:
                if r.status_code >= 400:
                    return StaticDoc(url=url, status=FetchStatus.HTTP_ERROR,
                                     http_status=r.status_code,
                                     detail=f"HTTP {r.status_code}")
                body = self._read_capped(r)
                headers, http_status = r.headers, r.status_code
        except Exception as e:
            return StaticDoc(url=url, status=FetchStatus.UNREACHABLE,
                             detail=redact(str(e))[:160])

        if body is None:
            return StaticDoc(url=url, status=FetchStatus.TOO_LARGE,
                             http_status=http_status,
                             detail=f"over {MAX_PDF_BYTES // 1024 // 1024} MB cap")

        kind = _kind_from(url, headers.get("content-type", ""), body)
        base = {
            "url": url, "kind": kind, "http_status": http_status,
            "body_bytes": len(body), "content_hash": _sha(body),
        }

        if kind is DocKind.PDF:
            try:
                text, pages, note = extract_pdf(body)
            except (PdfReadError, Exception) as e:
                return StaticDoc(**base, status=FetchStatus.UNPARSEABLE,
                                 detail=f"{type(e).__name__}: {str(e)[:110]}")
            sampled = min(pages, MAX_PDF_PAGES) or 1
            per_page = len(text) / sampled
            if len(text) < MIN_PDF_TEXT or per_page < MIN_CHARS_PER_PAGE:
                # Reachable and valid, just image-only. Recorded as a distinct
                # status so the OCR gap is countable rather than invisible.
                return StaticDoc(**base, status=FetchStatus.SCANNED_PDF,
                                 pages=pages,
                                 detail=(f"no usable text layer "
                                         f"({len(text)} chars / {sampled} pages "
                                         f"= {per_page:.0f} per page)"))
            return StaticDoc(**base, status=FetchStatus.OK, text=text,
                             pages=pages, detail=note)

        if kind is DocKind.OFFICE:
            return StaticDoc(**base, status=FetchStatus.UNPARSEABLE,
                             detail="office document; no parser wired")

        text, tables = extract_html(_decode(body, headers))
        if not text:
            return StaticDoc(**base, status=FetchStatus.UNPARSEABLE,
                             detail="no extractable text")
        return StaticDoc(**base, status=FetchStatus.OK, text=text, tables=tables)
