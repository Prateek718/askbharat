"""Sentence embeddings for semantic retrieval.

Lexical search fails on the query this product exists to serve. A citizen does
not type "widow pension scheme"; she types "my husband died and I have no
income" — and the word *widow* appears nowhere in it. Measured against the live
catalogue, that query returned an education scholarship and an old-age pension,
and "help for people who cannot see" returned a housing subsidy. Both are
lexically reasonable and useless to the person asking.

Model choice — `intfloat/multilingual-e5-small`:

- **384 dimensions, ~470 MB resident.** A 1024-dim large model would be the wrong trade on a CPU host with limited RAM.
- **E5 needs asymmetric prefixes.** Queries are prefixed `query: ` and
  documents `passage: `. Omitting them silently degrades retrieval rather than
  erroring, which is why it is done here rather than left to callers.

`service_records.embedding` is declared `Vector(1024)` from an earlier design;
the catalogue column added alongside it is 384 to match this model. They are
not interchangeable — see the comment on `SchemeCatalogue.embedding`.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

log = logging.getLogger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-small"
DIMENSIONS = 384

_model = None
_lock = threading.Lock()


def model():
    """Load the encoder once, on first use.

    Import and load are deferred so that importing this module — which the web
    app does at startup — does not pull ~470 MB into memory in a process that
    may never embed anything.
    """
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                log.info("loading %s", MODEL_NAME)
                _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def embed_documents(texts: Sequence[str], batch_size: int = 32) -> list[list[float]]:
    """Embed corpus text. Normalised, so cosine distance is a dot product."""
    prefixed = [f"passage: {t}" for t in texts]
    vectors = model().encode(
        prefixed,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


def embed_query(text: str) -> list[float]:
    """Embed one citizen question."""
    vector = model().encode(
        f"query: {text}",
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vector.tolist()


def record_text(name: str, payload: dict | None) -> str:
    """The text that represents one *extracted* scheme to the retriever.

    Deliberately different from `catalogue_text`. That one indexes what a
    scheme is called; this one indexes the rules a citizen has to satisfy —
    eligibility, documents, how to apply. Embedding them separately is what
    lets "I have a BPL card and two acres" match on the conditions rather than
    on a scheme's name, which is a question the catalogue text cannot answer at
    any embedding quality.

    Returns "" when there is nothing extracted to index, so callers can skip.
    """
    p = payload or {}
    parts = [name or ""]
    for value in (
        p.get("who_is_eligible"),
        p.get("what_it_is"),
        p.get("documents_required"),
        p.get("how_to_apply"),
        p.get("application_modes"),
    ):
        if not value:
            continue
        if isinstance(value, list):
            value = "; ".join(str(x) for x in value)
        parts.append(str(value))
    if len(parts) <= 1:
        return ""
    return " — ".join(p_ for p_ in parts if p_)[:1500]


def catalogue_text(title: str, description: str | None,
                   tags: Sequence[str] | None) -> str:
    """The text that represents one scheme to the retriever.

    Tags are included because they carry the colloquial vocabulary — 'Widow',
    'Divyangjan', 'Farmer' — that formal scheme titles omit, and those are the
    words a citizen's description actually rhymes with.
    """
    parts = [title or ""]
    if description:
        parts.append(description)
    if tags:
        parts.append(", ".join(str(t) for t in tags))
    return " — ".join(p for p in parts if p)[:1000]
