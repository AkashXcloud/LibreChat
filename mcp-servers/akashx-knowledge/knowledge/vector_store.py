"""Vector store — ARCHITECTURE.md §7.

A small, persistable store of (vector, chunk-metadata). Pure-Python cosine
similarity (numpy-accelerated if available) so it runs anywhere; swap the
:class:`VectorStore` interface for Chroma/pgvector/Qdrant in production. Persists
as a single JSON file (fine at this scale; move to a real ANN index for millions
of chunks).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

try:
    import numpy as _np  # optional acceleration
except Exception:  # pragma: no cover
    _np = None


@dataclass
class ScoredItem:
    score: float
    payload: dict[str, Any]


@runtime_checkable
class VectorStore(Protocol):
    def add(self, vectors: list[list[float]], payloads: list[dict[str, Any]]) -> None: ...
    def query(self, vector: list[float], k: int = 5) -> list[ScoredItem]: ...
    def __len__(self) -> int: ...


class NumpyVectorStore:
    """Cosine-similarity store over dense vectors (vectors are pre-normalized)."""

    def __init__(self) -> None:
        self._vectors: list[list[float]] = []
        self._payloads: list[dict[str, Any]] = []

    def add(self, vectors: list[list[float]], payloads: list[dict[str, Any]]) -> None:
        self._vectors.extend(vectors)
        self._payloads.extend(payloads)

    def query(self, vector: list[float], k: int = 5) -> list[ScoredItem]:
        if not self._vectors:
            return []
        if _np is not None:
            mat = _np.asarray(self._vectors, dtype=float)
            q = _np.asarray(vector, dtype=float)
            scores = mat @ q  # vectors are L2-normalized → dot == cosine
            order = scores.argsort()[::-1][:k]
            return [ScoredItem(float(scores[i]), self._payloads[i]) for i in order]
        # pure-python fallback
        scored = [(_dot(v, vector), i) for i, v in enumerate(self._vectors)]
        scored.sort(reverse=True)
        return [ScoredItem(s, self._payloads[i]) for s, i in scored[:k]]

    def __len__(self) -> int:
        return len(self._vectors)

    # --- persistence --------------------------------------------------- #
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"vectors": self._vectors, "payloads": self._payloads}, fh)

    @classmethod
    def load(cls, path: str) -> "NumpyVectorStore | None":
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        store = cls()
        store._vectors = data.get("vectors", [])
        store._payloads = data.get("payloads", [])
        return store


def _dot(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))
