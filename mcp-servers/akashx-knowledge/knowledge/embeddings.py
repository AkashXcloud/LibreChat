"""Embedders — ARCHITECTURE.md §7.

An ``Embedder`` maps text to a fixed-length vector. The default
:class:`LocalHashEmbedder` is **dependency-free and deterministic** (feature
hashing over token n-grams, L2-normalized): good enough to demonstrate real
ranking offline. Production swaps in :class:`OpenAIEmbedder` or a
``sentence-transformers`` model by implementing this one method.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable


_TOKEN = re.compile(r"[a-z0-9_]+")


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalHashEmbedder:
    """Deterministic, offline embedder using the hashing trick over 1- & 2-grams."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _TOKEN.findall(text.lower())
        grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for gram in grams:
            h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


# Native output dimensions per OpenAI embedding model, so the index is
# namespaced correctly when the operator swaps models via env.
_OPENAI_MODEL_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedder:
    """OpenAI embeddings (optional). Requires the ``openai`` package + key.

    ``model`` and optional ``dimensions`` are operator-configurable (env-driven in
    the bridge). ``dim`` reflects the ACTUAL output size — either the explicit
    ``dimensions`` (v3 models support reducing it) or the model's native size — so
    the persisted index is namespaced by the real vector size and switching models
    rebuilds cleanly instead of colliding on a stale dimension.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        dimensions: int | None = None,
    ) -> None:
        from openai import OpenAI  # lazy

        self._client = OpenAI(api_key=api_key or None)
        self._model = model
        self._dimensions = dimensions
        self.dim = dimensions or _OPENAI_MODEL_DIMS.get(model, 1536)

    def embed(self, texts: list[str]) -> list[list[float]]:
        kwargs: dict = {"model": self._model, "input": texts}
        if self._dimensions:
            kwargs["dimensions"] = self._dimensions
        resp = self._client.embeddings.create(**kwargs)
        return [d.embedding for d in resp.data]


_AZURE_DATED_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


