"""Hybrid retrieval — ARCHITECTURE.md §7.

Fuses dense (embedding cosine) and sparse (keyword/BM25-lite) scores. Product
jargon like "RagBucket" or "Ask Function" is exact-match sensitive, where pure
vectors under-retrieve; the keyword signal covers that. Final score:

    score = alpha * dense + (1 - alpha) * keyword
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from knowledge.embeddings import Embedder
from knowledge.vector_store import VectorStore

_TOKEN = re.compile(r"[a-z0-9_]+")


@dataclass
class Hit:
    score: float
    text: str
    citation: str
    doc_path: str


class Retriever:
    """Hybrid retriever over a populated vector store + a keyword index."""

    def __init__(self, embedder: Embedder, store: VectorStore, alpha: float = 0.6) -> None:
        self._embedder = embedder
        self._store = store
        self._alpha = alpha
        # keyword index built lazily from the store payloads
        self._df: Counter[str] = Counter()
        self._built = False

    def _build_keyword_index(self) -> None:
        payloads = getattr(self._store, "_payloads", [])
        for p in payloads:
            for term in set(_TOKEN.findall(p.get("text", "").lower())):
                self._df[term] += 1
        self._built = True

    def search(self, query: str, k: int = 5) -> list[Hit]:
        if len(self._store) == 0:
            return []
        if not self._built:
            self._build_keyword_index()

        # Dense: over-fetch, then re-rank with the keyword signal.
        qvec = self._embedder.embed([query])[0]
        dense = self._store.query(qvec, k=max(k * 4, 12))

        q_terms = _TOKEN.findall(query.lower())
        n_docs = max(len(getattr(self._store, "_payloads", [])), 1)

        hits: list[Hit] = []
        for item in dense:
            payload: dict[str, Any] = item.payload
            kw = self._keyword_score(q_terms, payload.get("text", ""), n_docs)
            score = self._alpha * item.score + (1 - self._alpha) * kw
            hits.append(Hit(
                score=score,
                text=payload.get("text", ""),
                citation=payload.get("citation", ""),
                doc_path=payload.get("doc_path", ""),
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _keyword_score(self, q_terms: list[str], text: str, n_docs: int) -> float:
        if not q_terms:
            return 0.0
        tokens = _TOKEN.findall(text.lower())
        if not tokens:
            return 0.0
        tf = Counter(tokens)
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + n_docs / (1 + self._df.get(term, 0)))
            score += (tf[term] / len(tokens)) * idf
        # squash into ~[0,1] so it is comparable to cosine
        return score / (1 + score)
