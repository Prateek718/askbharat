"""The assistant: retrieve, then answer only from what was retrieved.

The product promise is that a citizen can act on the answer, which makes
grounding the entire design rather than a feature of it:

- **Retrieval is hybrid, then reranked.** Three retrievers run independently —
  lexical full-text, semantic over scheme *names*, and semantic over extracted
  *rules* — are fused by reciprocal rank, and the pool is reordered by a
  cross-encoder. Lexical alone could not connect "my husband died and I have no
  income" to a widow pension, and returned nothing at all for Devanagari; both
  measured against the live catalogue, not assumed.
- **The model never answers from its own knowledge.** It is given the retrieved
  schemes and told that anything outside them does not exist. A confident
  invented eligibility rule is the one failure that actually harms someone.
- **Citations are computed, not generated.** The model is not asked to write
  reference markers, because a model that invents a fee can invent a citation.
  We cite the records we retrieved, so a citation can never point at a page we
  did not read.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

from sqlalchemy import text

from askbharat.db.session import session_scope
from askbharat.llm.embeddings import catalogue_text
from askbharat.llm.limiter import DailyQuotaExceeded
from askbharat.llm.provider import LLMProvider, StreamInterrupted
from askbharat.llm.rerank import rerank

# Deltas are coalesced to roughly this many characters before being sent, so
# the reader sees text appear continuously without a JSON line per character.
log = logging.getLogger(__name__)

FLUSH_CHARS = 12

TOP_K = 6
# Candidates handed to the cross-encoder. Wide enough that the right scheme is
# almost always somewhere in the pool, narrow enough to score in well under a
# second on CPU — the reranker is quadratic in nothing but it does one forward
# pass per candidate.
RERANK_POOL = 30
# Enough of each record to answer from, small enough that six of them fit
# comfortably in the context alongside the instructions.
SNIPPET_CHARS = 1400

# Conversation memory. Six messages is three exchanges — enough for "what
# documents do I need for that one?" to resolve, bounded so a long session
# cannot crowd out the scheme records, which are what the answer must come
# from. Older turns fall off rather than being summarised: a summary of a
# summary is exactly where invented facts creep in.
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 700
# Schemes carried over from the previous turn, so a follow-up that names no
# scheme still has the right ones in context.
MAX_CARRIED = 4

ANSWER_SYSTEM = """\
You are an assistant for Indian citizens looking for government schemes. You \
answer ONLY from the scheme records provided in the user message.

Rules, in priority order:

1. NEVER state a fact that is not in the provided records. Not a fee, not an
   eligibility rule, not a deadline, not a helpline. If you know something from
   general knowledge, it is still not permitted here — the citizen will act on
   what you say, and the records are the only thing we have verified.

2. If the records do not answer the question, say so plainly and say what IS
   there. "The pages I have don't state the income limit for this scheme" is a
   good answer. Inventing a limit is not.

3. If a scheme's details have not been extracted yet, you will see
   "(details not extracted yet)". Say the scheme exists and point the citizen
   at its page rather than pretending to know its rules.

4. Refer to schemes by their exact name so the citizen can find them. Do not
   write footnote markers or reference numbers — the interface adds the links.

4a. If a record has a "NOT eligible" line, say so alongside the eligibility.
   Telling someone they qualify when the page bars them is the most damaging
   thing you can do here — it costs them an application and a wasted trip.

5. Be brief and concrete. Lead with the schemes that match. Use short
   paragraphs or a short list. No preamble, no "great question".

6. Write in the language the citizen used. If they wrote in Hindi, answer in
   Hindi.

7. This is a conversation. Earlier turns are shown above the records. When the
   citizen says "that one", "the second scheme", or "it", resolve the reference
   from the earlier turns and answer about that scheme. If the reference is
   genuinely ambiguous, ask which one they mean instead of guessing.

8. Rules 1 and 2 still bind on follow-ups. Do not carry a fact from your own
   knowledge into a later turn because the conversation has built up momentum —
   if the records do not state it now, it is still not stated.
"""


# Words carried by a spoken question that say nothing about which scheme is
# wanted. Postgres strips English stopwords itself, but these reach to_tsquery
# as real lexemes and would otherwise dilute the ranking.
_NOISE = {
    "am", "are", "can", "get", "any", "for", "the", "and", "you", "there",
    "what", "which", "who", "how", "does", "apply", "eligible", "eligibility",
    "scheme", "schemes", "government", "india", "want", "need", "help",
    "please", "tell", "give", "have", "has", "with", "from", "this", "that",
    "would", "could", "should", "about", "into", "your", "mine", "myself",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Must match the expression behind ix_cat_fts character for character, or
# Postgres cannot use the index and silently recomputes to_tsvector for all
# 4,810 rows on every question (~270ms of pure waste per query).
DOC = ("to_tsvector('english', coalesce(c.title,'') || ' ' || "
       "coalesce(c.description,'') || ' ' || coalesce(c.tags::text,''))")


def _any_term_query(q: str, limit: int = 12) -> str:
    """Build an OR tsquery from the meaningful words in a question.

    `plainto_tsquery` ANDs every term, so a natural sentence — "I am a farmer
    in Bihar with two acres" becomes 'farmer & bihar & two & acr & appli' —
    matches nothing, because no single scheme contains all of it. Citizens
    describe their situation in sentences, so ANDing is the wrong default for
    the assistant even though it is right for the search box.

    Tokens are letters only, so the joined string cannot carry tsquery
    operators and needs no further escaping.
    """
    words = [w.lower() for w in _WORD.findall(q or "")]
    terms = [w for w in words if len(w) > 2 and w not in _NOISE]
    # dedupe, keep first-seen order
    seen, out = set(), []
    for w in terms:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return " | ".join(out)


def lexical_candidates(query: str, limit: int = 30) -> list[str]:
    """Slugs by literal word overlap, best first.

    Three tiers, most precise first:

      0. every term present  — a deliberate search ("post matric scholarship")
      1. any term, ranked    — a described situation ("I am a widow in Punjab")
      2. trigram similarity  — a partial or misspelled word ("schol", "widw")

    Strong where the citizen uses the corpus's own vocabulary, and blind where
    they do not — which is what `vector_candidates` is for.
    """
    q = (query or "").strip()
    if not q:
        return []
    any_terms = _any_term_query(q)
    with session_scope() as s:
        rows = s.execute(text(f"""
            WITH fts AS (
                SELECT c.slug, 0 AS tier,
                       ts_rank({DOC}, plainto_tsquery('english', :q)) AS score
                  FROM scheme_catalogue c
                 WHERE {DOC} @@ plainto_tsquery('english', :q)
                 LIMIT 40
            ),
            loose AS (
                SELECT c.slug, 1 AS tier,
                       ts_rank({DOC}, to_tsquery('english', :any_terms)) AS score
                  FROM scheme_catalogue c
                 WHERE :any_terms <> ''
                   AND {DOC} @@ to_tsquery('english', :any_terms)
                 ORDER BY score DESC
                 LIMIT 40
            ),
            -- similarity(a,b) rather than the `a % b` operator: a literal
            -- percent sign in a text() statement gets doubled on its way
            -- through SQLAlchemy into psycopg's own placeholder escaping and
            -- arrives as an unknown operator. The function form is immune,
            -- and at 4,810 rows the sequential scan costs nothing.
            fuzzy AS (
                SELECT c.slug, 2 AS tier, similarity(c.title, :q) AS score
                  FROM scheme_catalogue c
                 WHERE similarity(c.title, :q) > 0.25
                 LIMIT 20
            ),
            merged AS (
                SELECT DISTINCT ON (slug) slug, tier, score
                  FROM (SELECT * FROM fts
                        UNION ALL SELECT * FROM loose
                        UNION ALL SELECT * FROM fuzzy) u
                 ORDER BY slug, tier, score DESC
            )
            SELECT m.slug
              FROM merged m
             ORDER BY m.tier, m.score DESC
             LIMIT :k
        """), {"q": q, "any_terms": any_terms, "k": limit}).mappings().all()
        return [r["slug"] for r in rows]


def vector_candidates(query: str, limit: int = 30) -> list[str]:
    """Slugs by meaning, best first.

    This is the half that answers "my husband died and I have no income" with a
    widow pension, and that answers Hindi at all. Degrades to an empty list
    rather than raising: if the model cannot load, or the catalogue has not
    been embedded yet, the assistant should quietly fall back to lexical search
    rather than fail.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        from askbharat.llm.embeddings import embed_query
        vector = embed_query(q)
    except Exception as exc:                       # noqa: BLE001
        log.warning("semantic retrieval unavailable, using lexical only: %s",
                    str(exc)[:200])
        return []

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT slug
              FROM scheme_catalogue
             WHERE embedding IS NOT NULL
             ORDER BY embedding <=> CAST(:v AS vector)
             LIMIT :k
        """), {"v": str(vector), "k": limit}).mappings().all()
    return [r["slug"] for r in rows]


def rules_candidates(query: str, limit: int = 30) -> list[str]:
    """Slugs by similarity to the extracted *rules*, best first.

    Searches `service_records.embedding`, which indexes eligibility, documents
    and application steps rather than the scheme's name. This is the retriever
    that can answer "I have a BPL card and two acres" — a question about
    conditions, which no amount of title similarity will match.

    Coverage grows with extraction, so this returns few rows early on and more
    each day. That is fine: fusion weights by rank, so a short list simply
    contributes less rather than distorting the result.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        from askbharat.llm.embeddings import embed_query
        vector = embed_query(q)
    except Exception as exc:                       # noqa: BLE001
        log.warning("rules retrieval unavailable: %s", str(exc)[:200])
        return []

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT replace(id, 'myscheme:', '') AS slug
              FROM service_records
             WHERE embedding IS NOT NULL AND id LIKE 'myscheme:%'
             ORDER BY embedding <=> CAST(:v AS vector)
             LIMIT :k
        """), {"v": str(vector), "k": limit}).mappings().all()
    return [r["slug"] for r in rows]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Combine ranked lists by rank position, not by score.

    Cosine distance and `ts_rank` are not on a common scale and no fixed
    weighting between them survives contact with real queries. RRF sidesteps
    that entirely: only an item's *position* in each list counts, so a scheme
    ranked highly by both retrievers beats one that a single retriever loves.

    k=60 is the standard damping constant — large enough that the difference
    between rank 1 and rank 2 does not dominate agreement between the two.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, slug in enumerate(ranking):
            scores[slug] = scores.get(slug, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores, key=lambda s: -scores[s])


def retrieve(query: str, k: int = TOP_K, rerank_pool: int = RERANK_POOL) -> list[dict]:
    """Hybrid retrieval with cross-encoder reranking.

    Three stages, each fixing the stage before:

      1. **Recall** — three independent retrievers. Lexical cannot connect
         "I cannot see" to a disability scheme and returns nothing at all for
         Devanagari. Semantic-over-names blurs exact titles, offering five
         adjacent scholarships when one was asked for. Semantic-over-rules
         matches on eligibility instead of name, which is the only one that
         can answer "I have a BPL card and two acres" — and it covers only
         what extraction has reached so far.
      2. **Fusion** — reciprocal rank fusion over both lists, widened to
         `rerank_pool` rather than `k`. Recall matters more than order here,
         because the next stage does the ordering.
      3. **Rerank** — a cross-encoder reads each candidate *with* the query and
         scores the pair. This is what fixes fusion's characteristic failure: a
         scheme both retrievers like for shallow reasons outranking the one
         that actually answers.

    If the reranker is unavailable the fused order is used as-is, so the
    assistant degrades in quality rather than breaking.
    """
    q = (query or "").strip()
    if not q:
        return []

    fused = reciprocal_rank_fusion([
        lexical_candidates(q),      # the words the citizen used
        vector_candidates(q),       # what the scheme is called, by meaning
        rules_candidates(q),        # the conditions it imposes, by meaning
    ])[:rerank_pool]
    if not fused:
        return []

    with session_scope() as s:
        rows = [dict(r) for r in s.execute(text("""
            SELECT c.slug, c.title, c.description, c.categories, c.url,
                   c.ministry, c.tags, sr.payload, sr.confidence
              FROM scheme_catalogue c
              LEFT JOIN service_records sr ON sr.id = 'myscheme:' || c.slug
             WHERE c.slug = ANY(:slugs)
        """), {"slugs": fused}).mappings().all()]

    fused_rank = {slug: i for i, slug in enumerate(fused)}
    rows.sort(key=lambda r: fused_rank[r["slug"]])

    order = rerank(
        q,
        [catalogue_text(r["title"], r["description"], r["tags"]) for r in rows],
        top_k=k,
    )
    if order is None:
        return rows[:k]
    return [rows[i] for i in order]


def fetch_by_slug(slugs: list[str]) -> list[dict]:
    """Re-load schemes the citizen was shown last turn, in the order given."""
    if not slugs:
        return []
    with session_scope() as s:
        rows = [dict(r) for r in s.execute(text("""
            SELECT c.slug, c.title, c.description, c.categories, c.url,
                   c.ministry, sr.payload, sr.confidence
              FROM scheme_catalogue c
              LEFT JOIN service_records sr ON sr.id = 'myscheme:' || c.slug
             WHERE c.slug = ANY(:slugs)
        """), {"slugs": slugs}).mappings().all()]
    order = {s_: i for i, s_ in enumerate(slugs)}
    return sorted(rows, key=lambda r: order.get(r["slug"], 999))


def merge_context(fresh: list[dict], carried: list[dict], k: int = TOP_K) -> list[dict]:
    """Fresh hits first, then schemes carried from the previous turn.

    A follow-up like "what documents do I need for that?" retrieves almost
    nothing on its own — it names no scheme and shares no vocabulary with the
    corpus. Carrying the previous turn's schemes forward is what makes the
    reference resolvable. Fresh results still rank first, so a genuine change
    of subject is not held hostage by what came before.
    """
    out, seen = [], set()
    for row in [*fresh, *carried]:
        if row["slug"] in seen:
            continue
        seen.add(row["slug"])
        out.append(row)
        if len(out) >= k:
            break
    return out


def clean_history(history: list) -> list[dict]:
    """Trim client-supplied history to the last few turns, bounded in size.

    History arrives from the browser, so it is untrusted input, not state we
    own: roles are whitelisted and content is truncated. The worst a crafted
    history can do is waste its own context window.
    """
    if not isinstance(history, list):
        return []
    cleaned = []
    for msg in history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()[:MAX_HISTORY_CHARS]
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _snippet(row: dict) -> str:
    """Render one retrieved scheme as context the model can quote from."""
    p = row.get("payload") or {}
    parts = [f"SCHEME: {row['title']}"]
    if row.get("ministry"):
        parts.append(f"Ministry: {row['ministry']}")
    if row.get("categories"):
        parts.append(f"Category: {', '.join(row['categories'])}")

    if not p:
        parts.append("(details not extracted yet — only the official summary "
                     "below is available)")
        if row.get("description"):
            parts.append(f"Summary: {row['description']}")
        return "\n".join(parts)[:SNIPPET_CHARS]

    def add(label: str, value):
        if not value:
            return
        if isinstance(value, list):
            value = "; ".join(str(x) for x in value)
        parts.append(f"{label}: {value}")

    add("What it is", p.get("what_it_is") or row.get("description"))
    add("Who is eligible", p.get("who_is_eligible"))
    add("NOT eligible", p.get("exclusions"))
    add("Documents required", p.get("documents_required"))
    add("How to apply", p.get("how_to_apply"))
    add("Apply by", p.get("application_modes"))
    add("Fee", p.get("fee_amount") if p.get("fee_amount") is not None
        else p.get("fee_notes"))
    add("Processing time", p.get("processing_time"))
    add("Helpline", p.get("helpline"))
    add("Email", p.get("email"))
    return "\n".join(parts)[:SNIPPET_CHARS]


def build_context(rows: list[dict]) -> str:
    return "\n\n---\n\n".join(_snippet(r) for r in rows)


def answer_stream(
    question: str,
    history: list | None = None,
    context_slugs: list[str] | None = None,
) -> Iterator[str]:
    """Yield newline-delimited JSON events for the browser.

    Events: {"type":"token","text":...}, {"type":"citations","items":[...]},
    {"type":"error","message":...}. NDJSON rather than SSE because the client
    is a plain fetch() reader and NDJSON needs no event-framing on either side.
    """
    def event(**kw) -> str:
        return json.dumps(kw, ensure_ascii=False) + "\n"

    turns = clean_history(history)
    carried = fetch_by_slug((context_slugs or [])[:MAX_CARRIED]) if turns else []
    rows = merge_context(retrieve(question), carried)

    if not rows:
        yield event(type="token", text=(
            "I couldn't find any scheme matching that. Try naming the benefit "
            "you're looking for — 'scholarship', 'pension', 'housing loan' — "
            "or browse the categories."))
        return

    provider = LLMProvider()
    user = (
        f"CITIZEN'S QUESTION:\n{question}\n\n"
        f"SCHEME RECORDS ({len(rows)} found):\n\n{build_context(rows)}\n\n"
        "Answer using only these records."
    )
    # History sits between the instructions and the records so the records are
    # the last thing the model reads before answering — the grounding material
    # gets recency, the conversation only supplies the referent.
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM},
        *turns,
        {"role": "user", "content": user},
    ]

    citations = [{"slug": r["slug"], "title": r["title"]} for r in rows]
    buf = ""
    emitted = False

    # Retrieval finishes in well under a tenth of a second; the model then
    # spends around ten of them on reasoning tokens before its first word of
    # answer. Streaming alone does not fix that wait — it only starts once the
    # model does. Saying what was found, immediately, turns a silent ten
    # seconds into visible progress, and it is true rather than a placebo
    # spinner: these are the schemes the answer will be built from.
    yield event(type="status",
                text=f"Found {len(rows)} matching scheme"
                     f"{'' if len(rows) == 1 else 's'} — reading them…")

    try:
        for piece in provider.stream(
            messages, task="answer", max_tokens=1200, temperature=0.1,
        ):
            buf += piece
            # Deltas can be a single character. One JSON line each would be
            # mostly framing overhead, so they are coalesced — small enough
            # that the text still visibly types out, large enough to be worth
            # a line on a slow connection.
            if len(buf) >= FLUSH_CHARS:
                emitted = True
                yield event(type="token", text=buf)
                buf = ""
        if buf:
            emitted = True
            yield event(type="token", text=buf)

    except DailyQuotaExceeded:
        yield event(type="error", message=(
            "The assistant has used up today's request allowance. The schemes "
            "below still match your question, and browsing still works."))
        yield event(type="citations", items=citations)
        return
    except StreamInterrupted:
        # Part of an answer is already on screen. Say the answer is incomplete
        # rather than letting it read as a finished thought that simply stops.
        if buf:
            yield event(type="token", text=buf)
        yield event(type="error", message=(
            "The answer was cut off before it finished. Please ask again, or "
            "open the schemes below directly."))
        yield event(type="citations", items=citations)
        return
    except Exception:                                   # noqa: BLE001
        yield event(type="error", message=(
            "I couldn't reach the assistant just now. The schemes below match "
            "your question."))
        yield event(type="citations", items=citations)
        return

    if not emitted:
        yield event(type="error", message=(
            "The assistant didn't return an answer. The schemes below match "
            "your question."))

    # Cited from what we retrieved, never from what the model wrote.
    yield event(type="citations", items=citations)
