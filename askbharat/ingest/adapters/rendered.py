"""Browser-rendered fetching, for sites whose content only exists after JS runs.

Why this exists: myScheme holds the richest citizen-facing content in the whole
corpus — Eligibility, Documents Required, Application Process, Benefits and FAQs
for ~4,860 schemes, which is exactly the structure india.gov.in lacks. But its
pages are fully client-rendered (`__NEXT_DATA__.pageProps` is empty) and its
backing API returns 401 without a credential.

We do not extract that credential. We render the public page the same way a
citizen's browser does, which needs no privileged access and is unambiguously
permitted. It costs a few seconds per page instead of milliseconds; for a
one-time harvest of a few thousand pages that is an acceptable trade.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

from playwright.async_api import (
    Browser,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from askbharat.config import settings

log = logging.getLogger(__name__)


@dataclass
class RenderedPage:
    url: str
    status: int | None
    html: str
    text: str
    content_hash: str
    error: str | None = None      # 'not_found' means a dead URL, do not retry
    # Section headings the page exposed, used to verify we waited long enough.
    sections: list[str] = field(default_factory=list)
    # Outbound links (Sources & References etc.) — the hrefs are content too,
    # and a text dump throws them away.
    links: list[dict] = field(default_factory=list)
    # Length of the visible-only text, kept so the hidden-content gain is
    # measurable per page rather than assumed. On myScheme this is roughly
    # 6.4k visible vs 15.1k walked — the difference is every FAQ answer.
    text_len_visible_only: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text) and len(self.text) > 200


class RenderedFetcher:
    """A pooled headless browser for rendering pages.

    Use as an async context manager; it keeps one browser and reuses contexts,
    because launching Chromium per page would dominate the runtime.
    """

    def __init__(
        self,
        concurrency: int = 3,
        timeout_ms: int = 45_000,
        wait_for: str | None = None,
        block_assets: bool = True,
        reveal_hidden: bool = True,
        capture_html: bool = False,
    ):
        self.concurrency = concurrency
        self.timeout_ms = timeout_ms
        self.wait_for = wait_for
        self.block_assets = block_assets
        self.reveal_hidden = reveal_hidden
        # Off by default because it was the single largest allocation per page
        # and nothing consumed it. A myScheme page is ~1 MB of HTML against
        # ~15 KB of extracted text, so a RenderedPage that carries its source
        # is sixty times the size of one that does not — and the harvest writes
        # only the text. Measured at ~4.75 MB retained per page with this on.
        # Turn it on deliberately for debugging a page that extracted wrongly.
        self.capture_html = capture_html
        self._pw = None
        self._browser: Browser | None = None
        self._sem = asyncio.Semaphore(concurrency)

    async def _launch(self) -> Browser:
        return await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                # Chromium sizes its heap for a desktop with the whole machine
                # to itself. This harvest runs for hours beside a fetcher and an
                # editor on 7 GB, so give the renderer a budget it must live
                # within rather than letting it discover the limit by taking the
                # machine down.
                "--js-flags=--max-old-space-size=512",
                "--disable-extensions",
                "--disable-background-networking",
                "--renderer-process-limit=2",
            ],
        )

    async def __aenter__(self) -> RenderedFetcher:
        self._pw = await async_playwright().start()
        self._browser = await self._launch()
        return self

    async def recycle(self, full: bool = True) -> None:
        """Restart the browser, and by default the Playwright connection too.

        Restarting only the browser turned out to treat the smaller half of the
        problem. Chromium's own growth is real and a relaunch does return it —
        measured at ~240 MB on a 400-page run. But the Python process was
        sitting on 2.1 GB of anonymous heap at the same point, and none of that
        belongs to Chromium: every intercepted request registers an object in
        the Playwright client's connection registry, and `page.route("**/*")`
        means every request on every page goes through it. Those objects are
        owned by the *connection*, which a browser relaunch leaves untouched.

        Stopping and restarting Playwright discards the registry and the driver
        process with it. It costs a second or two more than a browser restart.
        Pass full=False for the cheap version. Never call with fetches in
        flight.
        """
        if self._browser:
            await self._browser.close()
            self._browser = None
        if full and self._pw:
            await self._pw.stop()
            self._pw = await async_playwright().start()
        self._browser = await self._launch()

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    @staticmethod
    async def _extract_text(page, reveal_hidden: bool = True) -> str:
        """Read the page's text, including content the site has collapsed.

        Three things had to be got right here, each found by measuring:

        1. **Don't mutate the live DOM.** An earlier version removed
           script/style nodes in place; since this runs twice per page, the
           first call left a damaged DOM for the second and silently lost text.
           Everything happens on a clone.

        2. **Don't click to expand.** myScheme's FAQ toggles are bare
           `div.cursor-pointer` with no ARIA, and so are its sidebar nav items.
           Clicking them switched the SPA into a tab view and cut the page from
           6,448 characters to 710 — worse than doing nothing.

        3. **Just reveal it instead.** The collapsed answers are already in the
           DOM, merely CSS-hidden: `textContent` is 15,039 chars where
           `innerText` is 6,448. Forcing visibility on the clone recovers all
           of it with structure intact, no interaction and no navigation risk.
        """
        return await page.evaluate(
            """(reveal) => {
                const src = document.querySelector('main') || document.body;
                if (!src) return '';

                if (!reveal) {
                    // Baseline: exactly what a sighted visitor sees.
                    return src.innerText || '';
                }

                // Walk the tree ourselves. innerText is layout-dependent and
                // therefore skips collapsed panels no matter how hard we
                // override CSS on a clone; textContent sees them but discards
                // every boundary, running an answer into the next question.
                // Walking gives us textContent's reach with innerText's shape.
                const BLOCK = new Set([
                    'ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','BR','DD','DIV','DL','DT',
                    'FIELDSET','FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3',
                    'H4','H5','H6','HEADER','HR','LI','MAIN','NAV','OL','P','PRE',
                    'SECTION','TABLE','TD','TFOOT','TH','THEAD','TR','UL'
                ]);
                const SKIP = new Set([
                    'SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME','CANVAS','TEMPLATE'
                ]);

                const out = [];
                (function walk(node) {
                    if (node.nodeType === Node.TEXT_NODE) {
                        const t = node.nodeValue.replace(/\\s+/g, ' ');
                        if (t.trim()) out.push(t);
                        return;
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE) return;
                    if (SKIP.has(node.tagName)) return;
                    if (node.getAttribute &&
                        node.getAttribute('aria-hidden') === 'true') return;

                    const block = BLOCK.has(node.tagName);
                    if (block) out.push('\\n');
                    for (const child of node.childNodes) walk(child);
                    if (block) out.push('\\n');
                })(src);

                return out.join('')
                    .replace(/[ \\t]+/g, ' ')
                    .replace(/ *\\n */g, '\\n')
                    .replace(/\\n{3,}/g, '\\n\\n')
                    .trim();
            }""",
            reveal_hidden,
        )

    async def _wait_for_content(
        self, page, min_chars: int = 900, settle_ms: int = 500
    ) -> None:
        """Poll until the page's text stops growing, or the timeout expires.

        Content-based rather than selector-based, so it works on any template.
        Returns as soon as two consecutive polls agree, which on a fast page is
        under a second — the timeout is a ceiling, not a wait.
        """
        deadline = self.timeout_ms
        elapsed, last = 0, -1
        while elapsed < deadline:
            try:
                n = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').length"
                )
            except PlaywrightError:
                # The page is mid-navigation and the execution context was
                # swapped out. Keep waiting: returning here meant extraction ran
                # against a blank document, which is how every page came back
                # empty. Never treat a transient evaluate failure as "settled".
                n = last
            if n >= min_chars and n == last:
                return                      # grown enough and settled
            last = n
            await page.wait_for_timeout(settle_ms)
            elapsed += settle_ms

    @staticmethod
    async def _looks_missing(text: str) -> bool:
        """A soft 404 — the server says 200 but the SPA rendered 'not found'."""
        head = text[:300].lower()
        return any(
            s in head for s in
            ("page not found", "404", "couldn't find the page",
             "could not find the page", "page you're looking for")
        )

    async def _block(self, route) -> None:
        """Drop images/fonts/media. Roughly halves render time; no content lost."""
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
        else:
            await route.continue_()

    async def fetch(self, url: str) -> RenderedPage:
        async with self._sem:
            ctx = await self._browser.new_context(
                user_agent=settings.user_agent,
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=True,   # government TLS is often misconfigured
            )
            page = await ctx.new_page()
            if self.block_assets:
                await page.route("**/*", self._block)
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                status = resp.status if resp else None

                # Content arrives after an XHR, so domcontentloaded is too early.
                #
                # Waiting on a named section ("text=Eligibility") looked right
                # and cost 28% of the corpus: not every scheme page has every
                # section, so on those the wait burned the full timeout and the
                # page was captured empty. Wait on the *amount* of content
                # instead — that works whatever sections a page happens to have.
                await self._wait_for_content(page)
                if self.wait_for:
                    try:
                        await page.wait_for_selector(self.wait_for, timeout=3_000)
                    except PlaywrightTimeout:
                        pass          # nice-to-have, never load-bearing
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(self.timeout_ms, 12_000)
                    )
                except PlaywrightTimeout:
                    pass

                # Visible-only text, kept as a baseline so the hidden-content
                # gain is measurable per page rather than assumed.
                before = await self._extract_text(page, reveal_hidden=False)

                html = await page.content() if self.capture_html else ""
                text = await self._extract_text(page, reveal_hidden=self.reveal_hidden)
                links = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('main a[href], a[href]'))
                        .map(a => ({text: (a.innerText||'').trim().slice(0,80),
                                    href: a.href}))
                        .filter(l => l.href && !l.href.startsWith('javascript:'))
                        .slice(0, 120)"""
                )
                sections = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('h1,h2,h3'))
                        .map(h => h.innerText.trim()).filter(Boolean).slice(0, 40)"""
                )
                text = (text or "").strip()
                if await self._looks_missing(text):
                    # Distinguish a dead slug from a transient failure: one
                    # should never be retried, the other always should.
                    return RenderedPage(
                        url=url, status=status, html=html, text="",
                        content_hash="", error="not_found",
                    )
                return RenderedPage(
                    url=url,
                    status=status,
                    html=html,
                    text=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    sections=sections,
                    links=links,
                    text_len_visible_only=len(before or ""),
                )
            except (PlaywrightTimeout, PlaywrightError) as e:
                return RenderedPage(
                    url=url, status=None, html="", text="", content_hash="",
                    error=f"{type(e).__name__}: {str(e)[:160]}",
                )
            finally:
                await page.close()
                await ctx.close()

    async def fetch_with_retry(
        self, url: str, attempts: int = 3, backoff_s: float = 4.0
    ) -> RenderedPage:
        """Fetch, retrying empty renders with growing backoff.

        myScheme throttles by returning HTTP 200 with an unpopulated SPA rather
        than a 429 — measured directly: pages that come back empty under load
        return 8-17k characters when the site is idle. An empty render is
        therefore a *rate-limit signal*, not a verdict about the page, and the
        only correct response is to wait and ask again.

        A 'not_found' result is never retried; that one really is a dead URL.
        """
        last = None
        for i in range(attempts):
            page = await self.fetch(url)
            if page.ok or page.error == "not_found":
                return page
            last = page
            if i < attempts - 1:
                await asyncio.sleep(backoff_s * (2**i))
        return last

    async def fetch_many(
        self, urls: list[str], retry: bool = True
    ) -> list[RenderedPage]:
        fn = self.fetch_with_retry if retry else self.fetch
        return await asyncio.gather(*(fn(u) for u in urls))
