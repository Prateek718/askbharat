# AskBharat

> Searchable discovery portal and grounded AI assistant for India's 4,810 welfare schemes with exact source citations.

[![CI](https://github.com/Prateek718/askbharat/actions/workflows/ci.yml/badge.svg)](https://github.com/Prateek718/askbharat/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Live demo:** Deployment in progress.


## Overview

AskBharat is a searchable, plain-language discovery portal and conversational assistant for India's government welfare schemes. It archives, normalizes, and indexes **4,810 central and state schemes** across 36 states/UTs and 52 ministries from [myScheme](https://www.myscheme.gov.in).

Citizens rarely search using official bureaucratic titles (e.g. they may search *"my husband died and I have no income"* rather than *"Indira Gandhi National Widow Pension Scheme"*). AskBharat bridges this discovery gap with a 3-stage hybrid search pipeline and an AI assistant that answers questions in plain English, citing the exact official page and archive timestamp behind every answer.

Out of 4,810 central and state schemes **4,721 detailed scheme pages** are 100% extracted into structured fields: eligibility criteria, exclusions, required documents, step-by-step application procedures, and fee schedules. The website did not provide data about other schemes.

## Architecture

```
[ Data Source ] ────────► [ Ingestion Pipeline ] ────────► [ Storage & Vectors ]
  myScheme API & SPA        Playwright & Fetchers            PostgreSQL 16 + pgvector
  (4,810 schemes)           4,721 Pages Extracted            - scheme_catalogue (Official)
                                                             - service_records (Extracted)
                                                             - 384-dim HNSW Cosine Index
                                                                       │
                                                                       ▼
[ Citizen Request ] ───► [ 3-Stage Hybrid Retrieval ] ───► [ Grounded AI Assistant ]
  - English                1. Lexical (FTS + Trigram)        - OpenRouter LLM Stream
  - Need-based questions   2. Dense Vector (e5-small)        - Exact Source Citations
                           3. RRF Fusion (k=60)              - Plain JS/CSS UI (Zero-npm)
                           4. Cross-Encoder Reranker
```

1. **Recall Stage**:
   - **Lexical**: PostgreSQL full-text search (GIN) + Trigram matching (`pg_trgm`) over scheme titles and tags.
   - **Dense Vectors**: `intfloat/multilingual-e5-small` bi-encoder (384 dims, normalized cosine distance) independently searching titles and extracted eligibility rules.
2. **Rank Fusion**: Reciprocal Rank Fusion (RRF, $k=60$) combines disparate ranking scales into a top candidate pool of 30 items.
3. **Cross-Encoder Precision Reranking**: `cross-encoder/ms-marco-MiniLM-L-6-v2` jointly reads each candidate pair with the citizen's query to score relevance before generating the final answer.
4. **Grounded Generation**: OpenRouter streams plain-language answers with strict grounding prompts and source links.

## Tech stack

Python 3.14 · FastAPI · PostgreSQL 16 + pgvector 0.8 + pg_trgm · SQLAlchemy 2.0 · Alembic · sentence-transformers (`multilingual-e5-small` & `ms-marco-MiniLM-L-6-v2`) · CPU PyTorch · Jinja2 (Server-Rendered HTML) · Vanilla CSS/JS · Docker · GitHub Actions CI.

## Quick start

Requires [Docker](https://docs.docker.com/get-docker/) and a `.env` file:

```bash
cp .env.example .env
# Fill in OPENROUTER_API_KEY if testing the live AI assistant
```

Start the database and run migrations:

```bash
docker compose up -d db
.venv/bin/alembic upgrade head
```

Run the web application:

```bash
./run_web.sh
```

The application will be live at `http://127.0.0.1:8077`.

Run the test suite and linter:

```bash
pytest -q
ruff check askbharat/ tests/
```

## Key engineering decisions

- **CPU-Constrained Optimization:** Designed to run efficiently on modest CPU hardware (< 1.5 GB RAM footprint). Benchmarked `cross-encoder/ms-marco-MiniLM-L-6-v2` (~90 MB RAM, ~660 ms latency) against `bge-reranker-base` (~1.1 GB RAM, ~4,000 ms latency) — MiniLM delivered identical 7/7 top-3 retrieval precision while running **6x faster** and using **1/12th the memory**.
- **Semantic Intent & Rule-Based Dense Retrieval:** Government scheme titles are bureaucratic, while citizens search using lived situations (e.g. "my husband died and I have no income"). By embedding both scheme metadata and extracted eligibility rules, the dense vector retriever bridges vocabulary mismatches that pure keyword search fails on.
- **Zero-NPM Lightweight Frontend:** The target audience includes citizens on mid-range mobile devices and patchy connections. Replaced heavy JavaScript SPA bundles with ~25 KB hand-written CSS, ~180 lines of vanilla JS, and server-rendered Jinja2 templates that load instantly and work with JavaScript disabled.
- **Schema-Constrained LLM Extraction:** Coerced free-tier LLM output shapes in `schema/extraction.py`, reducing extraction retries from **3.7 requests/page down to 1.06 requests/page** and saving significant API quota.
- **Strict Provenance & Honesty:** Architecture splits official government metadata (`scheme_catalogue`) from extracted rules (`service_records`) via a LEFT JOIN. Enforces via unit tests that unextracted pages state they have not been processed yet rather than falsely claiming the government source is silent.

## Known limitations

- **Free-Tier OpenRouter Rate Limits:** Live chat uses free-tier OpenRouter models, which are subject to provider rate limits (20 requests/min).
- **Static Catalog Snapshot:** The dataset reflects a complete archive of myScheme as of the extraction run. Continuous live delta-syncing with government portals would require a background scheduled harvester.
- **Cold Starts:** Free-tier container hosting spins down during periods of inactivity; the initial wake-up request takes ~15 seconds.

## License

[MIT](LICENSE)
