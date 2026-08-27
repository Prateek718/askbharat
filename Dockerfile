# Hugging Face Spaces & Production All-in-One Image
# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements/base.txt requirements/web.txt /tmp/requirements/
RUN pip install -r /tmp/requirements/web.txt

# Bake retrieval models into image (~560 MB: e5 bi-encoder and cross-encoder)
ENV HF_HOME=/opt/models
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('intfloat/multilingual-e5-small', device='cpu'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2', device='cpu')"

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

# Install PostgreSQL 16 + pgvector and runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release postgresql-common \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get update && apt-get install -y --no-install-recommends \
    postgresql-16 \
    postgresql-16-pgvector \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with UID 1000 (standard for Hugging Face Spaces)
RUN useradd --create-home --uid 1000 askbharat && \
    mkdir -p /var/lib/postgresql/data /var/log/postgresql /run/postgresql && \
    chown -R askbharat:askbharat /var/lib/postgresql /var/log/postgresql /run/postgresql /home/askbharat

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

ENV PATH="/opt/venv/bin:/usr/lib/postgresql/16/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    APP_ENV=production \
    PGDATA=/var/lib/postgresql/data \
    DATABASE_URL="postgresql+psycopg://askbharat:askbharat@127.0.0.1:5432/askbharat" \
    HOST=0.0.0.0 \
    PORT=7860

# Pre-populate database at image build time
USER askbharat
COPY --chown=askbharat:askbharat deploy/data/askbharat.dump /tmp/askbharat.dump
RUN initdb -D "$PGDATA" && \
    pg_ctl -D "$PGDATA" -l /tmp/pg.log start && \
    psql -d postgres -c "ALTER USER askbharat WITH PASSWORD 'askbharat';" && \
    createdb -O askbharat askbharat && \
    pg_restore -U askbharat -d askbharat -v /tmp/askbharat.dump && \
    pg_ctl -D "$PGDATA" stop && \
    rm -f /tmp/askbharat.dump /tmp/pg.log

WORKDIR /app
COPY --chown=askbharat:askbharat alembic.ini ./
COPY --chown=askbharat:askbharat migrations/ ./migrations/
COPY --chown=askbharat:askbharat askbharat/ ./askbharat/
COPY --chown=askbharat:askbharat deploy/entrypoint.sh ./entrypoint.sh

USER askbharat
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["./entrypoint.sh"]
