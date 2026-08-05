# Runtime image for the site. Not for the harvest stack — that needs Playwright
# and a ~400 MB Chromium a web server never opens, which is why requirements/
# is split by role.

# ---------------------------------------------------------------- build stage
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies are installed into a venv rather than the system Python purely
# so the runtime stage can take the whole tree in one COPY.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Requirements are copied alone, before the source, so editing a template does
# not invalidate the layer that spends minutes resolving torch.
COPY requirements/base.txt requirements/web.txt /tmp/requirements/
RUN pip install -r /tmp/requirements/web.txt

# Bake the retrieval models into the image (~560 MB: the e5 bi-encoder and the
# cross-encoder). They would otherwise be fetched from Hugging Face on first
# use, which makes the first citizen to ask a question wait for a download, and
# makes booting depend on a third party being up. An image that needs the
# network to answer its first request is not really deployable.
ENV HF_HOME=/opt/models
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('intfloat/multilingual-e5-small', device='cpu'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2', device='cpu')"

# -------------------------------------------------------------- runtime stage
FROM python:3.14-slim AS runtime

# libpq for psycopg, curl for the container healthcheck. No build toolchain:
# nothing compiles here, and a compiler in a runtime image is just reachable
# capability.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged. Nothing in the serving path writes to disk — page bodies are
# read from the mounted data volume — so the whole image can stay read-only to
# the app.
RUN useradd --create-home --uid 10001 askbharat

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models \
    # Offline: the weights are already present, and this turns a silent
    # network fetch into a loud error if that ever stops being true.
    HF_HUB_OFFLINE=1 \
    APP_ENV=production \
    # Bind all interfaces — inside a container the publish rule, not the bind
    # address, is what decides real exposure.
    HOST=0.0.0.0 \
    PORT=8077

WORKDIR /app
COPY --chown=askbharat:askbharat alembic.ini ./
COPY --chown=askbharat:askbharat migrations/ ./migrations/
COPY --chown=askbharat:askbharat askbharat/ ./askbharat/

USER askbharat
EXPOSE 8077

# Liveness only: process-level, no database. /api/ready is the dependency
# check, and it belongs to whatever routes traffic, not to the runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# Exec form, so uvicorn is PID 1 and receives SIGTERM directly. Wrapped in a
# shell only to expand the env vars, which the exec form does not do by itself.
CMD ["sh", "-c", "exec uvicorn askbharat.web.app:app --host \"$HOST\" --port \"$PORT\" --workers \"${WEB_CONCURRENCY:-1}\""]
