"""Cross-encoder reranking.

Retrieval and reranking answer different questions, which is why doing both
beats doing either well:

- The retrievers are **bi-encoders**. Query and document are embedded
  separately and compared by distance, so 4,810 documents can be searched in
  milliseconds — but the model never sees the pair together, and the query's
  specifics cannot influence how a document is read.
- The reranker is a **cross-encoder**. It reads query and document jointly and
  scores the pair directly. Far more accurate, and far too slow to run over a
  whole corpus — but perfectly affordable over the ~30 candidates fusion hands
  it.

So: hybrid retrieval for recall, cross-encoder for precision. The reranker's
job is to fix fusion's characteristic mistake — a scheme that both retrievers
rank moderately well for shallow reasons outranking the one that actually
answers the question.

Degrades to a no-op rather than failing. If the model cannot load, the fused
order is returned untouched: a slightly worse ordering is a far better outcome
than a broken assistant.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence

log = logging.getLogger(__name__)

# Measured on the live catalogue over seven questions lexical search got wrong,
# reranking a pool of 30:
#
#   BAAI/bge-reranker-base            7/7 top-3, 3998 ms median, ~1.1 GB
#   cross-encoder/ms-marco-MiniLM-L-6  7/7 top-3,  661 ms median,  ~90 MB
#
# Same results, six times faster, a twelfth of the memory. The multilingual
# model looked like the principled default — the corpus has Hindi queries and
# an English-only cross-encoder should mangle them — but it did not survive
# measurement, for a reason worth writing down: the *retriever* is multilingual,
# so it finds the right candidates for a Devanagari query on its own, and the
# reranker only ever sees the English (and transliterated-Hindi) catalogue text
# it is good at. "मुझे छात्रवृत्ति चाहिए" still lands on a scholarship.
#
# Override with RERANK_MODEL if the corpus ever stops being Latin-script.
MODEL_NAME = os.environ.get(
    "RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)

_model = None
_lock = threading.Lock()
_failed = False


def model():
    """Load the cross-encoder once, on first use. None if unavailable."""
    global _model, _failed
    if _failed:
        return None
    if _model is None:
        with _lock:
            if _model is None and not _failed:
                try:
                    from sentence_transformers import CrossEncoder
                    log.info("loading reranker %s", MODEL_NAME)
                    _model = CrossEncoder(MODEL_NAME, device="cpu",
                                          max_length=512)
                except Exception as exc:            # noqa: BLE001
                    _failed = True
                    log.warning("reranker unavailable (%s); keeping fused order",
                                str(exc)[:200])
                    return None
    return _model


def rerank(
    query: str,
    documents: Sequence[str],
    top_k: int | None = None,
) -> list[int] | None:
    """Return document indices ordered best-first, or None if unavailable.

    Indices rather than documents so the caller keeps whatever richer object it
    was carrying, and so a None result is unambiguous.
    """
    if not query or not documents:
        return None
    encoder = model()
    if encoder is None:
        return None
    try:
        scores = encoder.predict(
            [(query, d) for d in documents],
            show_progress_bar=False,
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("rerank failed, keeping fused order: %s", str(exc)[:200])
        return None

    order = sorted(range(len(documents)), key=lambda i: -float(scores[i]))
    return order[:top_k] if top_k else order
