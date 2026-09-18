"""Retrieval over supplier contracts / SLAs.

DeepSeek does not offer an embeddings endpoint, so documents are embedded locally with a
dependency-free feature-hashing embedder (unigrams + bigrams, L2-normalised). On Postgres the vectors
live in a pgvector column and are ranked in SQL by cosine distance; on SQLite ranking happens in numpy.
Swap `embed()` for any sentence-embedding model without touching callers (keep EMBED_DIM in sync).
"""
import hashlib
import re

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EMBED_DIM, SupplierContract

_TOKEN = re.compile(r"[a-z0-9]+")


def _bucket(token: str) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
    return h % EMBED_DIM, 1.0 if (h >> 63) & 1 else -1.0


def embed(text: str) -> list[float]:
    tokens = _TOKEN.findall(text.lower())
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    for tok in tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]:
        idx, sign = _bucket(tok)
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    return (vec / norm if norm else vec).tolist()


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
