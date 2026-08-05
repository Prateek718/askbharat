# askbharat

A searchable, plain-language front end to India's government welfare schemes,
plus an assistant that answers questions about them and cites the official page
every answer came from.

4,810 central and state schemes from [myScheme](https://www.myscheme.gov.in),
archived and structured: what each scheme is, who qualifies, who is explicitly
barred, which documents are needed, and how to apply.

The governing principle is that **a citizen will act on what this says**. Every
design decision below follows from that — the site would rather show a visible
gap than a confident guess, and it distinguishes "the official page does not
state this" from "we have not read this page yet", because those are opposite
claims to a reader and identical in the database.

---

## Stack

### Web

| | |
|---|---|
| FastAPI 0.140 | routing, NDJSON streaming chat endpoint |
| Uvicorn 0.51 | ASGI server |
| Jinja2 3.1 | server-rendered templates |
| Vanilla CSS + JS | ~25 KB hand-written CSS, ~180 lines of plain JS |

**No React, no Tailwind, no build step, no npm.** The audience is largely on
mid-range Android phones over patchy networks, where a page that renders on
arrival beats a bundle that has to boot first. Search and filtering are plain
links and a form: they work with JavaScript disabled, are shareable as URLs and
restore correctly on the back button. The only JS that matters is the chat
stream reader and a small markdown renderer that builds DOM nodes rather than
setting `innerHTML`.

### Data

| | |
|---|---|
| PostgreSQL 16 | via Docker, `pgvector/pgvector:pg16`, host port **5433** |
| pgvector 0.8 | 384-dim embeddings, HNSW cosine indexes |
| pg_trgm 1.6 | trigram matching for partial words (`schol` → scholarship) |
| Native FTS | GIN index over title + description + tags |
| SQLAlchemy 2.0 + psycopg 3.3 | ORM and driver |
| Alembic 1.18 | migrations (5 revisions) |

Port 5433 rather than 5432, to avoid colliding with a local Postgres.

### Retrieval

| | |
|---|---|
| `intfloat/multilingual-e5-small` | 384d bi-encoder, ~470 MB, CPU |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | reranker, ~90 MB, ~660 ms/query |
| sentence-transformers 5.6, torch 2.13+cpu | CPU-only build, no CUDA |

Three retrievers run independently and are fused by reciprocal rank, then the
pool is reordered by the cross-encoder. See [Retrieval](#retrieval-1) below for
why all three are needed.

### LLM

OpenRouter through the `openai` SDK (OpenRouter speaks the OpenAI API).
Free-tier models, chained with fallbacks; Pydantic 2.13 for schema-constrained
extraction.

### Harvest

Playwright 1.61 + headless Chromium for myScheme's client-rendered SPA,
selectolax for static HTML, httpx for plain fetches.

### Tooling

Python 3.14, pytest (110 tests), ruff, GitHub Actions.

---

## Running it

### Prerequisites

- Python 3.12+ and Docker
- An [OpenRouter](https://openrouter.ai/keys) API key — only needed for
  extraction and the assistant; browsing works without one

### Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # the full dev set

cp .env.example .env      # then fill in OPENROUTER_API_KEY

docker compose up -d db
.venv/bin/alembic upgrade head
```

`alembic upgrade head` creates the pgvector extension itself, so no manual
`psql` step is needed.

Dependencies are split by role under `requirements/`, and pinned exactly so
CI, the image and your machine resolve to one tree:

| | |
|---|---|
| `base.txt` | config, database, LLM client |
| `web.txt` | base + FastAPI, Jinja, sentence-transformers, CPU torch |
| `harvest.txt` | base + Playwright, selectolax, pypdf |
| `dev.txt` | everything, plus pytest and ruff |

The split is what keeps the runtime image from shipping a ~400 MB Chromium
that a web server never opens. `web.txt` carries the PyTorch CPU index, so
torch installs in one step rather than the two-step dance this section used to
describe.

Only regenerating the corpus needs the browser binary:

```bash
.venv/bin/playwright install chromium
```

### Load the corpus

Assumes the harvest JSONL files are already in `data/` (see
[Harvesting](#harvesting-from-scratch) to regenerate them).

```bash
.venv/bin/python -m askbharat.scripts.load_catalogue          # 4,810 scheme records
.venv/bin/python -m askbharat.scripts.load_corpus --source myscheme
.venv/bin/python -m askbharat.scripts.load_corpus --source static --no-enqueue
.venv/bin/python -m askbharat.scripts.embed                   # ~6 min on CPU
```

Every one of these is idempotent — re-running inserts only what is new.

`--no-enqueue` loads the off-site corpus for retrieval without spending LLM
quota on it: that catalogue is dominated by department homepages with nothing
to extract.

### Run the site

```bash
./run_web.sh                      # http://127.0.0.1:8077
```

### Run extraction

```bash
setsid nohup ./run_extraction.sh > /dev/null 2>&1 &
tail -f data/extraction.log
```

Multi-day and self-resuming. Launch it with `setsid` so it survives the shell.

> A `pkill -f run_extraction` typed inline matches the invoking shell's own
> command line and kills the caller. Use a bracketed pattern:
> `pkill -f 'run_[e]xtraction'`.

### Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check askbharat/ tests/
```

The tests that assert over real scheme data skip when the corpus is not
loaded, and say so in those words. They do not report the empty database as a
finished one — that would be the exact confusion this project exists to avoid.

---

## Running it in a container

```bash
docker compose up -d --build      # db, migrations, then the site on :8077
```

Three services. `db` is the pgvector image. `migrate` is a one-shot that runs
`alembic upgrade head` and exits; the site waits on it completing successfully.
`web` is the app.

Migrations are their own service rather than part of the app's entrypoint
because two replicas starting together would otherwise race for the same
Alembic lock, and a schema change would run once per container.

### The image

Multi-stage, CPU-only torch, unprivileged user. **3.0 GB on disk, ~825 MB
compressed** — the dependency tree is 1.4 GB of that and the models 559 MB.
Most of the remaining weight is torch, and the CPU wheel is already the small
one; the default CUDA build would add roughly another 2 GB for hardware that
is not there.

The retrieval models are **baked in at build time** rather than fetched on
first use: otherwise the first citizen to ask a question waits for a 559 MB
download, and booting depends on Hugging Face being reachable.
`HF_HUB_OFFLINE=1` in the runtime stage turns any future accidental fetch into
a loud error instead of a silent one.

The harvest stack is deliberately absent. Rebuilding the corpus is a job you
run on a host, not a capability the web server needs.

### Probes

| | |
|---|---|
| `GET /api/health` | liveness — process only, no database |
| `GET /api/ready` | readiness — 503 when the database is unreachable |
| `GET /api/status` | corpus size and extraction progress |

The split matters. A liveness probe that queries the database cannot tell "the
app is wedged" from "the database is down", and an orchestrator answers the
first by restarting the container — which does nothing for the second except
produce a restart loop for the length of the outage. Readiness is where
dependencies belong, and 503 is what makes a load balancer drain an instance
and return it without a deploy.

### Configuration

Everything comes from the environment; `.env` is a local convenience and never
enters an image layer. See `.env.example` for the full set.

`APP_ENV=production` disables `/api/docs` and the OpenAPI schema, and runs a
startup check for a `localhost` database URL, the development password, and the
placeholder crawler contact. It **only ever narrows what is exposed and widens
what is checked** — it never changes what the site says about a scheme. A
citizen must not get a different answer per environment.

That check logs rather than raises. A dark site helps nobody looking for a
pension, and the assistant's API key is deliberately not required: without it,
browse, search and every scheme page still work, and only the assistant goes
quiet. That is the catalogue/extraction split doing its job.

`WEB_CONCURRENCY` defaults to 1, and that is a memory ceiling rather than a
number left untuned — each worker loads its own bi-encoder and cross-encoder,
about 1.1 GB resident apiece.

### What is not done here

This is deployable, not deployed. Still outstanding for a real host: a TLS
terminator and reverse proxy, a registry to push the image to, backups for the
Postgres volume, and log shipping. The corpus is a bind mount, which is right
for one box and wrong for more than one.

---

## How it fits together

```
myScheme SPA ──Playwright──► data/myscheme_pages.jsonl
                                      │
                              load_corpus.py
                                      ▼
                        raw_documents  (bodies on disk,
                                        content-addressed)
                                      │
                              extraction_queue
                                      │  extract.py — LLM, days
                                      ▼
                             service_records
                                      │
myScheme API ──► scheme_catalogue ────┤
                                      ▼
                          FastAPI + Jinja  ──►  browse / detail
                                      │
                          hybrid retrieval  ──►  assistant
```

### The provenance split

Two tables, joined on `service_records.id = 'myscheme:' || scheme_catalogue.slug`:

- **`scheme_catalogue`** — what myScheme's own API published. Title,
  description, categories and tags are populated on **100%** of 4,810 rows.
- **`service_records`** — what the LLM extracted. Arrives over days.

The join is a LEFT JOIN in that direction on purpose. A scheme with no
extraction is still a first-class page showing the government's own
description; extraction *adds* eligibility, documents and steps but is never a
precondition. That is what let the site ship on day one, and what keeps it
honest afterwards.

Two things the API does **not** give you: `beneficiaryState` is empty on all
4,810 rows and `ministry` on 84%. State filtering can only come from
extraction.

### Retrieval

Three stages, each fixing the one before:

1. **Recall** — lexical (FTS + trigram), semantic over scheme *names*, and
   semantic over extracted *rules*, run independently.
2. **Fusion** — reciprocal rank fusion, k=60, into a pool of 30. RRF because
   `ts_rank` and cosine distance share no scale, so only rank position is
   comparable.
3. **Rerank** — a cross-encoder reads each candidate *with* the query.

Measured on seven questions lexical search got wrong:

| | top-3 relevant | median |
|---|---|---|
| Lexical only | 6/7 | 47 ms |
| Hybrid | 7/7 | 89 ms |
| Hybrid + rerank | 7/7 | 661 ms |

Reranking does not move top-3 but sharply improves top-1: *"help for people who
cannot see"* goes from a house-construction subsidy to **"Pension to the
Persons who lost 100% eye sight"**.

Lexical alone could not connect *"my husband died and I have no income"* to a
widow pension, and returned **zero** rows for Devanagari. The corpus is
Latin-script (0 of 4,810 titles contain Devanagari), so Hindi is served
entirely by the vector stage.

### Extraction economics

The daily cap is an **OpenRouter account-level limit on all `:free` models
combined** — 20 req/min, 50/day under $10 lifetime credit, 1,000/day above it.
Switching between free models does not raise it.

**Requests per page, not pages per hour, sets the schedule.** Two measurements
shaped the pipeline:

- **Model choice is about latency, not size.** `gpt-oss-20b:free` returns a
  valid record in ~100 s. `nemotron-120b:free` never returned one inside 15
  minutes — it spends the completion budget on reasoning.
- **Free models get the content right and the container wrong.** Eligibility
  comes back as a list where the schema wants prose; steps as one numbered
  string where it wants a list; helpline as `{phone, email}`. Each shape
  mismatch used to cost a corrective request. Coercing in
  `schema/extraction.py` took it from **3.7 to 1.06 requests/page**.

Sequential extraction is latency-bound *below* the quota, so the runner uses 6
worker threads against a lock-guarded token bucket, making quota the binding
constraint as intended. It runs at `--quota-cap 900`, reserving 100 requests/day
for the live assistant; they share one counter file.

---

## Layout

```
askbharat/
  db/models.py          schema — read the docstrings, they carry the reasoning
  ingest/adapters/      rendered (Playwright) and static (httpx) fetchers
  llm/
    provider.py         the single LLM interface; model chains, fallback, streaming
    prompts.py          extraction prompt, tuned against hand-scored output
    limiter.py          token bucket + persistent daily quota
    embeddings.py       e5 encoder, query/passage prefixes
    rerank.py           cross-encoder, degrades to a no-op if unavailable
  schema/extraction.py  ExtractedService + the coercion layer
  scripts/              harvest_*, load_*, embed, extract
  web/
    app.py              routes
    queries.py          read-side SQL
    chat.py             retrieval + grounded answering
    templates/, static/
migrations/             alembic
requirements/           dependencies split by role, pinned
tests/                  110 tests
Dockerfile              runtime image — web only, models baked in
docker-compose.yml      db + one-shot migrate + web
.github/workflows/      lint, tests, and a build-and-boot check
```

`data/` holds the harvest JSONL and content-addressed page bodies (~600 MB in
total; `datagov_catalogue.jsonl` alone is 451 MB and must always be streamed
line by line, never loaded whole).

---

## Harvesting from scratch

Only needed to rebuild `data/` — the loaders above assume it exists.

```bash
.venv/bin/python -m askbharat.scripts.audit_sources     # licence + robots gate
.venv/bin/python -m askbharat.scripts.harvest_all --only myscheme
```

myScheme is client-rendered and throttles by serving an empty SPA rather than a
429, so concurrency stays at 2 and an empty render is retried rather than
recorded. The full run is ~6 hours for 4,721 pages. `harvest_all` chunks it into
separate processes because the renderer leaks ~4.5 MB/page; chunking reclaims it
on process exit.

## Notes for whoever works on this next

- **The machine matters.** This was built on a 7.2 GB box that suspends
  nightly. Two bugs came from that: a long `sleep` that counted a monotonic
  clock frozen during suspend, and dead TCP sockets on resume that hung the
  worker at 0% CPU. Both are fixed (polling, and an explicit client timeout),
  but assume suspend will happen.
- **Alembic cannot compare expression indexes.** It once silently dropped the
  full-text indexes while adding an unrelated column. They are declared in
  `models.py` and excluded via `include_object` in `migrations/env.py` — do not
  remove that exclusion.
- **Every schema change costs a re-extraction pass** over already-done records.
  At free-tier throughput that is days, not minutes. Treat the schema as frozen
  unless something genuinely misleads a citizen.

## Data and licence

Scheme data is from myScheme, Government of India. The crawler identifies
itself honestly with a contact URL and is gated on a per-source licence and
robots audit (`sources/`, `audit_sources.py`) — `Source.crawlable` refuses any
source that has not been cleared.

Schemes change. Every detail page links to its official source and says when it
was archived.
