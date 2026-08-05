"""Read-side queries for the website.

Everything the site renders comes from two tables joined on the slug:

- `scheme_catalogue` — what myScheme published. Complete today.
- `service_records`  — what we extracted. Arrives over days.

The join is deliberately a LEFT JOIN in that direction. A scheme with no
extraction yet is still a first-class row: it lists, it searches, it has a
detail page with its official description. Extraction *adds* eligibility,
documents and steps; it is never a precondition for the scheme existing on the
site. That is what lets the site ship before the five-day extraction finishes,
and it is also what keeps it honest afterwards — a page with nothing extracted
looks visibly incomplete rather than quietly absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from askbharat.db.session import session_scope

PER_PAGE = 24


@dataclass
class Page:
    items: list[dict]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def _rows(sql: str, params: dict) -> list[dict]:
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]


def _one(sql: str, params: dict) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def stats() -> dict:
    """Headline counts. Cheap enough to run per request at this size.

    `detailed` deliberately is not `count(*) FROM service_records`. A row
    exists the moment extraction touches a page, including when the model
    found very little, so the raw count overstates what a citizen can actually
    use. This counts records that answer the question the site promises to
    answer: who qualifies, plus either the documents or the steps.

    `pages_read` comes from the queue rather than the records table, because
    re-extracting a page upserts its row — the record count sits still while
    real work happens, which is exactly what made the homepage look frozen.
    It is kept for /api/health, which is where a build metric belongs; the
    homepage stopped showing it once the run finished, because "4,721 pages
    read so far" answers a question only the person building this asks.

    `states` and `last_read` replaced it. Both answer something a citizen
    actually needs before trusting the page: is my state in here, and how
    stale is this. `last_read` is the harvest date rather than the extraction
    date — it is when we last saw what the government published, which is what
    "how current is this" means to a reader.
    """
    return _one("""
        SELECT (SELECT count(*) FROM scheme_catalogue)                    AS schemes,
               (SELECT count(*) FROM service_records
                 WHERE payload->>'who_is_eligible' IS NOT NULL
                   AND (payload->>'documents_required' <> '[]'
                        OR payload->>'how_to_apply' <> '[]'))             AS detailed,
               (SELECT count(*) FROM extraction_queue WHERE status='done') AS pages_read,
               (SELECT count(*) FROM raw_documents)                       AS documents,
               (SELECT count(DISTINCT ministry) FROM scheme_catalogue
                 WHERE ministry IS NOT NULL)                              AS ministries,
               (SELECT count(DISTINCT state) FROM scheme_catalogue
                 WHERE state IS NOT NULL AND state <> 'Central')          AS states,
               (SELECT max(fetched_at) FROM raw_documents)                AS last_read
    """, {}) or {}


def _filters(
    q: str = "",
    category: str = "",
    ministry: str = "",
    state: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """WHERE fragments shared by the listing and every facet count.

    Each facet builds its scope by asking for the filters *other* than its own,
    so the four call sites were four near-copies of the same three clauses.
    They had already drifted once — `state` would have made it five.
    """
    where = ["TRUE"]
    params: dict[str, Any] = {}
    if q:
        where.append("""(
            c.title ILIKE :like OR c.description ILIKE :like
            OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(c.tags) t
                        WHERE t ILIKE :like)
        )""")
        params["like"] = f"%{q}%"
    if category:
        where.append("c.categories ? :category")
        params["category"] = category
    if ministry:
        where.append("c.ministry = :ministry")
        params["ministry"] = ministry
    if state:
        where.append("c.state = :state")
        params["state"] = state
    return where, params


def categories(q: str = "", ministry: str = "", state: str = "") -> list[dict]:
    """Every category with its scheme count, largest first.

    Counts are scoped to the *other* active filters, which is the only way a
    facet count can be honest: with a search for "scholarship" the sidebar
    advertised "Education & Learning 1124" and clicking it returned 441,
    because the count came from the whole catalogue while the result did not.

    A facet's own value is deliberately excluded from its scope — the count
    answers "how many would I get if I picked this", so scoping a category
    count by the currently-selected category would just show the selection.
    """
    where, params = _filters(q=q, ministry=ministry, state=state)

    return _rows(f"""
        SELECT cat AS name, count(*) AS n
          FROM scheme_catalogue c, jsonb_array_elements_text(c.categories) AS cat
         WHERE {" AND ".join(where)}
         GROUP BY cat
         ORDER BY n DESC
    """, params)


def total_matching(q: str = "", ministry: str = "", state: str = "") -> int:
    """How many schemes match everything except the category filter.

    This is what "All categories" should show — clicking it clears only the
    category, so the number has to be the count you would actually land on.
    It was hardcoded to 4,810, which was both wrong under a search and a magic
    number that would go stale the moment the catalogue changed.
    """
    where, params = _filters(q=q, ministry=ministry, state=state)
    return _one(
        f"SELECT count(*) AS n FROM scheme_catalogue c "
        f"WHERE {' AND '.join(where)}", params
    )["n"]


def ministries(
    q: str = "", category: str = "", state: str = "", limit: int = 40
) -> list[dict]:
    """Ministries with counts, scoped to the other active filters."""
    where, params = _filters(q=q, category=category, state=state)
    where += ["c.ministry IS NOT NULL", "c.ministry <> ''"]
    params["limit"] = limit
    return _rows(f"""
        SELECT c.ministry AS name, count(*) AS n
          FROM scheme_catalogue c
         WHERE {" AND ".join(where)}
         GROUP BY c.ministry ORDER BY n DESC LIMIT :limit
    """, params)


def states(q: str = "", category: str = "", ministry: str = "") -> list[dict]:
    """Jurisdictions with counts, scoped to the other active filters.

    'Central' sorts first because it is the one option that applies to every
    citizen regardless of where they live; the states follow alphabetically
    rather than by count, because someone picking their own state is looking
    up a known name, not browsing the biggest.

    Rows where `scheme_catalogue.state` is NULL are pages we have not harvested
    yet, so no jurisdiction is known. They are left out of the facet instead of
    being shown as a bucket — 'Unknown 101' invites a click that answers a
    question nobody asked. They are still reachable with the filter cleared.
    """
    where, params = _filters(q=q, category=category, ministry=ministry)
    where.append("c.state IS NOT NULL")
    return _rows(f"""
        SELECT c.state AS name, count(*) AS n
          FROM scheme_catalogue c
         WHERE {" AND ".join(where)}
         GROUP BY c.state
         ORDER BY (c.state <> 'Central'), c.state
    """, params)


def search(
    q: str = "",
    category: str = "",
    ministry: str = "",
    state: str = "",
    page: int = 1,
    per_page: int = PER_PAGE,
) -> Page:
    """Filtered, paginated scheme listing.

    Ranking puts title matches above description matches above tag matches.
    ILIKE rather than full-text search: at 4,810 rows it is fast enough, it
    handles the partial-word typing a citizen actually does ('schol' ->
    'scholarship'), and it avoids committing to a stemmer before we know which
    languages the queries arrive in.
    """
    where, params = _filters(q=q, category=category, ministry=ministry, state=state)

    clause = " AND ".join(where)
    total = _one(
        f"SELECT count(*) AS n FROM scheme_catalogue c WHERE {clause}", params
    )["n"]

    # Clamp rather than trust the URL. `?page=999` on a 201-page result read
    # "page 999 of 201" above an empty list whose message blamed the search
    # terms — three wrong signals from one unvalidated integer. A page past the
    # end is a stale link or a typo, and the last page is what was meant.
    last_page = max(1, -(-total // per_page))
    page = min(max(1, page), last_page)

    params |= {"limit": per_page, "offset": (page - 1) * per_page}
    # A bare integer in ORDER BY is an ordinal column reference, not a
    # constant — 'ORDER BY 0' is a syntax error rather than a no-op — so the
    # unranked case omits the expression entirely instead of ordering by zero.
    if q:
        order = """
            CASE WHEN c.title ILIKE :like THEN 0
                 WHEN c.description ILIKE :like THEN 1
                 ELSE 2 END, c.title
        """
    else:
        order = "c.title"

    items = _rows(f"""
        SELECT c.slug, c.title, c.description, c.categories, c.tags,
               c.ministry, c.url, c.state,
               sr.confidence,
               (sr.id IS NOT NULL) AS has_detail,
               sr.payload->>'who_is_eligible' AS eligibility_preview
          FROM scheme_catalogue c
          LEFT JOIN service_records sr ON sr.id = 'myscheme:' || c.slug
         WHERE {clause}
         ORDER BY {order}
         LIMIT :limit OFFSET :offset
    """, params)
    return Page(items=items, total=total, page=page, per_page=per_page)


def scheme(slug: str) -> dict | None:
    """One scheme: catalogue row, extracted payload, and citation target."""
    return _one("""
        SELECT c.slug, c.title, c.description, c.categories, c.tags,
               c.ministry, c.url, c.state,
               sr.payload, sr.confidence, sr.kind,
               sr.jurisdiction_level, sr.state AS extracted_state,
               sr.updated_at AS extracted_at,
               rd.id AS raw_document_id, rd.fetched_at, rd.url AS source_url,
               rd.meta->'links' AS source_links
          FROM scheme_catalogue c
          LEFT JOIN service_records sr ON sr.id = 'myscheme:' || c.slug
          LEFT JOIN raw_documents rd
                 ON rd.meta->>'slug' = c.slug
                AND rd.source_id = (SELECT id FROM sources WHERE slug='myscheme')
         WHERE c.slug = :slug
    """, {"slug": slug})


def related(slug: str, cats: list[str], limit: int = 6) -> list[dict]:
    """Other schemes sharing a category. Keeps a dead end from being dead."""
    if not cats:
        return []
    return _rows("""
        SELECT c.slug, c.title, c.categories
          FROM scheme_catalogue c
         WHERE c.slug <> :slug AND c.categories ?| :cats
         ORDER BY random()
         LIMIT :limit
    """, {"slug": slug, "cats": list(cats), "limit": limit})


def extraction_progress() -> dict:
    """How far the multi-day extraction has got, and whether it is alive.

    `completed_last_hour` is the liveness signal. Without it the only visible
    number was a record count that stands still whenever the run is
    re-extracting pages it has already seen, which is indistinguishable from a
    crashed job — and was in fact mistaken for one.
    """
    row = _one("""
        SELECT
          (SELECT count(*) FROM extraction_queue WHERE status='done')    AS done,
          (SELECT count(*) FROM extraction_queue WHERE status='pending') AS pending,
          (SELECT count(*) FROM extraction_queue WHERE status='parked')  AS parked,
          (SELECT count(*) FROM extraction_queue)                        AS total,
          (SELECT count(*) FROM extraction_queue
            WHERE completed_at > now() - interval '1 hour')              AS completed_last_hour,
          (SELECT max(completed_at) FROM extraction_queue)               AS last_completed_at
    """, {}) or {}
    total = row.get("total") or 1
    row["pct"] = round((row.get("done") or 0) / total * 100, 1)
    row["running"] = bool(row.get("completed_last_hour"))
    return row
