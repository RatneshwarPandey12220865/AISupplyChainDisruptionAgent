"""Retrieval over supplier contracts / SLAs.

Embeddings come from a pluggable provider (EMBEDDING_PROVIDER):
  * ``openai``: any OpenAI-compatible ``/embeddings`` endpoint (OpenAI, Voyage via proxy, vLLM, Ollama, ...).
    Set EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL and make EMBED_DIM match the model output.
  * ``hash`` (default): a dependency-free feature-hashing embedder, so everything works offline.
If the remote provider fails, ``embed`` degrades to the hash embedder (same dimension) and logs a warning.
After switching provider or dimension, re-embed stored contracts with ``python -m app.rag reindex``
(a dimension change on Postgres also needs a migration of the pgvector column).
On Postgres vectors live in a pgvector column and are ranked in SQL by cosine distance; on SQLite in numpy.
"""
import hashlib
import logging
import re

import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EMBED_DIM, SupplierContract

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z0-9]+")


def _bucket(token: str) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
    return h % EMBED_DIM, 1.0 if (h >> 63) & 1 else -1.0


def hash_embed(text: str) -> list[float]:
    tokens = _TOKEN.findall(text.lower())
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    for tok in tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]:
        idx, sign = _bucket(tok)
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    return (vec / norm if norm else vec).tolist()


def remote_embed(text: str) -> list[float]:
    resp = httpx.post(
        f"{settings.embedding_base_url.rstrip('/')}/embeddings", timeout=20,
        headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        json={"model": settings.embedding_model, "input": text[:8000], "dimensions": EMBED_DIM})
    resp.raise_for_status()
    vec = resp.json()["data"][0]["embedding"]
    if len(vec) != EMBED_DIM:
        raise ValueError(f"Embedding model returned {len(vec)} dims but EMBED_DIM={EMBED_DIM}")
    arr = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    return (arr / norm if norm else arr).tolist()


def embed(text: str) -> list[float]:
    if settings.embedding_provider == "openai" and settings.embedding_api_key:
        try:
            return remote_embed(text)
        except Exception as exc:
            log.warning("Embedding provider failed (%s); using hash embedder", exc)
    return hash_embed(text)


def reindex(session: Session) -> int:
    """Re-embed every contract with the currently configured provider."""
    docs = session.scalars(select(SupplierContract)).all()
    for d in docs:
        d.embedding = embed(f"{d.title}\n{d.content}")
    return len(docs)


def add_contract(session: Session, supplier_id: int, title: str, content: str) -> SupplierContract:
    doc = SupplierContract(supplier_id=supplier_id, title=title, content=content,
                           embedding=embed(f"{title}\n{content}"))
    session.add(doc)
    return doc


def search_contracts(session: Session, query: str, supplier_ids: list[int] | None = None,
                     k: int = 4) -> list[dict]:
    q = embed(query)
    stmt = select(SupplierContract)
    if supplier_ids:
        stmt = stmt.where(SupplierContract.supplier_id.in_(supplier_ids))

    if settings.is_postgres:
        dist = SupplierContract.embedding.cosine_distance(q)
        rows = session.execute(stmt.add_columns(dist.label("d")).order_by(dist).limit(k)).all()
        scored = [(doc, 1 - float(d)) for doc, d in rows]
    else:
        docs = session.scalars(stmt).all()
        qv = np.asarray(q)
        scored = sorted(((d, float(np.dot(qv, np.asarray(d.embedding)))) for d in docs),
                        key=lambda x: x[1], reverse=True)[:k]

    return [{"supplier_id": d.supplier_id, "supplier": d.supplier.name, "title": d.title,
             "content": d.content, "score": round(s, 3)} for d, s in scored]


if __name__ == "__main__":
    import sys

    from app.db import init_db, session_scope

    if sys.argv[1:] == ["reindex"]:
        init_db()
        with session_scope() as sess:
            print(f"Re-embedded {reindex(sess)} contract(s) with provider={settings.embedding_provider}")
    else:
        print("usage: python -m app.rag reindex")
