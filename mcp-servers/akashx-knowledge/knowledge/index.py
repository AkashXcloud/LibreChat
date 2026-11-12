"""KnowledgeIndex — the façade the rest of Cognate uses for RAG.

Builds (or loads) the vector index from a Markdown directory and answers
``search``. The Knowledge Agent's ``search_knowledge`` tool calls straight into
this. Lazy: building is deferred until first use or an explicit ``--reindex``.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass

from knowledge.documents import load_markdown_dir, split_markdown
from knowledge.embeddings import Embedder
from knowledge.retriever import Retriever
from knowledge.vector_store import NumpyVectorStore


@dataclass
class RetrievedChunk:
    text: str
    citation: str
    doc_path: str
    score: float


class KnowledgeIndex:
    """Owns the vector store + retriever for one knowledge corpus."""

    def __init__(self, embedder: Embedder, knowledge_dir: str, index_path: str) -> None:
        self._embedder = embedder
        self._dir = knowledge_dir
        # Namespace the persisted index by embedder identity + dimension, so
        # switching embedders (local <-> openai) rebuilds instead of colliding
        # on incompatible vector dimensions.
        tag = f"{type(embedder).__name__}-{getattr(embedder, 'dim', 'na')}"
        self._index_path = f"{index_path}.{tag}.json"
        self._store: NumpyVectorStore | None = None
        self._retriever: Retriever | None = None
        self._chunk_count = 0
        # The index is shared by every worker thread; two first searches used to
        # race here and both build the store.
        self._ready_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def ensure_ready(self, rebuild: bool = False) -> None:
        if self._retriever is not None and not rebuild:
            return
        with self._ready_lock:
            if self._retriever is not None and not rebuild:
                return  # another thread finished while we waited
            store = None if rebuild else NumpyVectorStore.load(self._index_path)
            if store is None:
                store = self._build()
            self._store = store
            self._chunk_count = len(store)
            self._retriever = Retriever(self._embedder, store)

    def _build(self) -> NumpyVectorStore:
        store = NumpyVectorStore()
        docs = load_markdown_dir(self._dir)
        chunks = [c for d in docs for c in split_markdown(d)]
        if chunks:
            vectors = self._embedder.embed([c.text for c in chunks])
            payloads = [{"text": c.text, "citation": c.citation(), "doc_path": c.doc_path}
                        for c in chunks]
            store.add(vectors, payloads)
            try:
                store.save(self._index_path)
            except OSError:
                pass
        return store

    def reindex(self) -> int:
        """Force a rebuild; return the number of chunks indexed."""
        self.ensure_ready(rebuild=True)
        return self._chunk_count

    # ------------------------------------------------------------------ #
    def search(self, query: str, k: int = 5) -> list[RetrievedChunk]:
        self.ensure_ready()
        assert self._retriever is not None
        return [
            RetrievedChunk(text=h.text, citation=h.citation, doc_path=h.doc_path, score=h.score)
            for h in self._retriever.search(query, k=k)
        ]

    @property
    def chunk_count(self) -> int:
        self.ensure_ready()
        return self._chunk_count

    @property
    def available(self) -> bool:
        return bool(self._dir)
