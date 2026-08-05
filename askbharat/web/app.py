"""The citizen-facing site.

Server-rendered HTML, no build step, no client framework. That is a UX decision
before it is a technical one: the audience is largely on mid-range phones and
intermittent connections, where a 40 KB page that renders on arrival beats a
JavaScript bundle that has to boot before showing a word. Search and filtering
are plain links and a form, so they work with JS disabled, are shareable as
URLs, and restore correctly on the back button.

The chat endpoint is the one place that streams, because there the wait is the
interaction.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from askbharat.config import settings
from askbharat.db import session as db_session
from askbharat.web import chat as chat_engine
from askbharat.web import queries as q
from askbharat.web.icons import CATEGORY_ICON, CATEGORY_TONE

log = logging.getLogger("askbharat.web")

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

# Misconfiguration is reported at import, before the port is bound, so a bad
# deploy fails visibly at boot instead of serving wrong data quietly. It logs
# rather than raises: refusing to start would take the site down over a
# placeholder contact URL, and a dark site helps nobody looking for a pension.
for _problem in settings.check_production_ready():
    log.error("production config: %s", _problem)

app = FastAPI(
    title="askbharat",
    # The interactive docs enumerate every route and schema. Useful locally,
    # needless attack surface in production.
    docs_url=None if settings.is_production else "/api/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

templates.env.globals["icon_for"] = lambda c: CATEGORY_ICON.get(c, "default")
templates.env.globals["tone_for"] = lambda c: CATEGORY_TONE.get(c, "slate")


def render(request: Request, name: str, status_code: int = 200, **kw):
    """Render with the shared context.

    Starlette's current signature is (request, name, context); the older
    (name, context) form is silently misparsed here rather than warned about,
    so every call goes through this one helper.
    """
    ctx = {"progress": q.extraction_progress()}
    ctx.update(kw)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "index.html",
                  stats=q.stats(), categories=q.categories(),
                  states=q.states())


@app.get("/schemes", response_class=HTMLResponse)
def browse(
    request: Request,
    query: str = Query("", alias="q"),
    category: str = "",
    ministry: str = "",
    state: str = "",
    page: int = 1,
):
    page = max(1, page)
    query = query.strip()
    results = q.search(q=query, category=category, ministry=ministry,
                       state=state, page=page)
    return render(request, "browse.html",
                  results=results, query=query, category=category,
                  ministry=ministry, state=state,
                  categories=q.categories(query, ministry, state),
                  ministries=q.ministries(query, category, state, limit=25),
                  states=q.states(query, category, ministry),
                  total_matching=q.total_matching(query, ministry, state))


@app.get("/scheme/{slug}", response_class=HTMLResponse)
def scheme_detail(request: Request, slug: str):
    row = q.scheme(slug)
    if row is None:
        return render(request, "not_found.html", status_code=404, slug=slug)
    return render(request, "scheme.html",
                  s=row, payload=row.get("payload") or {},
                  related=q.related(slug, row.get("categories") or []))


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return render(request, "chat.html")


@app.get("/api/schemes")
def api_schemes(
    query: str = Query("", alias="q"),
    category: str = "",
    state: str = "",
    page: int = 1,
):
    res = q.search(q=query.strip(), category=category, state=state,
                   page=max(1, page))
    return JSONResponse({
        "total": res.total, "page": res.page, "pages": res.pages,
        "items": res.items,
    })


@app.post("/api/chat")
def api_chat(payload: dict = Body(...)):
    """Stream a grounded answer as newline-delimited JSON events."""
    question = (payload.get("q") or "").strip()[:500]
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    # The conversation lives in the browser, not on the server: no sessions, no
    # per-user state to expire, and a refresh starts clean. `chat_engine`
    # treats both fields as untrusted and bounds them.
    return StreamingResponse(
        chat_engine.answer_stream(
            question,
            history=payload.get("history"),
            context_slugs=payload.get("context_slugs"),
        ),
        media_type="application/x-ndjson",
        # Proxies that buffer would defeat the point of streaming.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
def health():
    """Liveness: is this process able to answer at all?

    Deliberately touches nothing external. A liveness probe that queries the
    database conflates "the app is wedged" with "the database is down", and an
    orchestrator responds to the first by restarting the container — which
    cannot fix the second and produces a restart loop for the whole duration
    of a database outage. Readiness is where dependencies belong.
    """
    return {"ok": True}


@app.get("/api/ready")
def ready():
    """Readiness: can this process actually serve a request right now?

    503 rather than 500 when the database is unreachable, so a load balancer
    takes the instance out of rotation instead of serving errors, and puts it
    back without a deploy once the database returns.
    """
    if not db_session.ping():
        return JSONResponse(
            {"ready": False, "reason": "database unreachable"}, status_code=503
        )
    return {"ready": True}


@app.get("/api/status")
def status():
    """Build metrics: corpus size and how far extraction has got.

    This is the payload /api/health used to return. It runs eleven aggregate
    subqueries, which is fine on demand and wasteful every few seconds against
    a probe, so it now sits behind its own route.
    """
    return {"ok": True, **q.stats(), "extraction": q.extraction_progress()}
