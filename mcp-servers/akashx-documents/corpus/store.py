"""A RagBucket's documents as readable, searchable pages (cognate/corpus/store.py, made synchronous).

The pages come from the bucket's chunks table, read with plain SELECTs over a
read-only AkashX connection (no ASK). Everything expensive is cached in
``CORPUS_DIR/<bucket>`` and in memory, keyed by each document's chunk count, so
a document is rebuilt only when the bucket changes it:

* the document list (one GROUP BY over the chunks table),
* each document's pages (its chunks in order, overlaps removed, table rows
  rebuilt by ``rows.rebuild_rows``),
* its ``FileIndex`` (BM25 + embeddings when an embedder is configured),
* the document cards (``cards``; model-built cards are read from cards-v1.json).

The cache layout is Cognate's, so a folder copied from Cognate's
``.cognate_corpus/<bucket>-<hash>`` is used as is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from corpus.rows import rebuild_rows

logger = logging.getLogger(__name__)

CORPUS_DIR = os.getenv("AKASHX_DOCS_CACHE", "/cache")
PAGES_VERSION = "1"
_INDEX_CACHE_SIZE = int(os.getenv("AKASHX_DOCS_INDEX_CACHE", "48"))
_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")
_IDENT = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$")

_index_cache: "OrderedDict[str, Any]" = OrderedDict()
_index_lock = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}


@dataclass
class Document:
    name: str                 # file name, e.g. 3M_2018_10K.pdf
    title: str = ""           # the bucket's document title
    pages: int = 0
    chunks: int = 0
    file_url: str = ""

    @property
    def fingerprint(self) -> str:
        return f"{self.chunks}-{self.pages}"


@dataclass
class Corpus:
    bucket: str                                   # RagBucket name
    chunks_table: str                             # database.table holding the chunks
    fetch_rows: Callable[..., list[dict]]         # (sql, params=None) -> rows
    embedder: Any = None
    docs: list[Document] = field(default_factory=list)
    cards: dict[str, dict] = field(default_factory=dict)

    @property
    def folder(self) -> str:
        return os.path.join(CORPUS_DIR, _SAFE.sub("_", self.bucket))

    # ------------------------------------------------------------------ #
    def documents(self) -> list[Document]:
        if self.docs:
            return self.docs
        sql = (f"SELECT file_name, MAX(file_url) AS file_url, MAX(document_title) AS title, "
               f"MAX(chunk_page_end) AS pages, COUNT(*) AS chunks FROM {self.chunks_table} "
               f"GROUP BY file_name")
        rows = self.fetch_rows(sql)
        self.docs = sorted((Document(name=str(r.get("file_name") or ""), title=str(r.get("title") or ""),
                                     pages=int(r.get("pages") or 0), chunks=int(r.get("chunks") or 0),
                                     file_url=str(r.get("file_url") or ""))
                            for r in rows if r.get("file_name")), key=lambda d: d.name.lower())
        return self.docs

    def document(self, name: str) -> Optional[Document]:
        """Exact name, then case-insensitive, then a unique substring match."""
        docs = self.documents()
        wanted = (name or "").strip()
        if not wanted:
            return None
        for d in docs:
            if d.name == wanted:
                return d
        low = wanted.lower()
        for d in docs:
            if d.name.lower() == low or d.name.lower().rsplit(".", 1)[0] == low:
                return d
        matches = [d for d in docs if low in d.name.lower()]
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------ #
    def index(self, doc: Document) -> Any:
        """The ``FileIndex`` of one document (built once, cached)."""
        cache_key = f"{self.bucket}/{doc.name}/{doc.fingerprint}"
        with _index_lock:
            if cache_key in _index_cache:
                _index_cache.move_to_end(cache_key)
                return _index_cache[cache_key]
        index = self._build_index(doc, self.embedder)
        with _index_lock:
            _index_cache[cache_key] = index
            while len(_index_cache) > _INDEX_CACHE_SIZE:
                _index_cache.popitem(last=False)
        return index

    def _doc_folder(self, doc: Document) -> str:
        digest = hashlib.sha1(doc.name.encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.folder, f"{_SAFE.sub('_', doc.name)[:80]}-{digest}")

    def _build_index(self, doc: Document, embedder: Any) -> Any:
        from corpus.index import FileIndex, attach_vectors, chunk_pages, page_headings

        folder = self._doc_folder(doc)
        with _build_locks.setdefault(folder, threading.Lock()):
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"pages-v{PAGES_VERSION}-{doc.fingerprint}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                pages, sections = saved["pages"], saved["sections"]
            else:
                pages, sections = self._load_pages(doc)
                tmp = f"{path}.tmp{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"pages": pages, "sections": sections}, fh, ensure_ascii=False)
                os.replace(tmp, path)
            detected = page_headings(pages)
            headings = []
            for i in range(len(pages)):
                merged, seen = [], set()
                for h in (sections[i] if i < len(sections) else []) + (detected[i] if i < len(detected) else []):
                    if h and h.lower() not in seen:
                        seen.add(h.lower())
                        merged.append(h)
                headings.append(merged[:4])
            index = FileIndex(name=doc.name, kind="document", pages=pages,
                              chunks=chunk_pages(pages, headings), headings=headings)
            attach_vectors(index, folder, embedder)
            return index

    def _load_pages(self, doc: Document) -> tuple[list[str], list[list[str]]]:
        """The document's chunks in order, rebuilt into pages."""
        sql = (f"SELECT chunk_index, chunk_page_start, section_title, chunk_text FROM {self.chunks_table} "
               f"WHERE file_name = %(name)s ORDER BY chunk_index")
        rows = self.fetch_rows(sql, {"name": doc.name})
        by_page: dict[int, list[str]] = {}
        sections: dict[int, list[str]] = {}
        previous = ""
        for r in rows:
            page = max(1, int(r.get("chunk_page_start") or 1))
            text = str(r.get("chunk_text") or "")
            text = _drop_overlap(previous, text)
            previous = str(r.get("chunk_text") or "")
            by_page.setdefault(page, []).append(text)
            title = " ".join(str(r.get("section_title") or "").split())
            if title and title.lower() not in {"consolidated section"}:
                bucket = sections.setdefault(page, [])
                if title not in bucket:
                    bucket.append(title)
        last = max(by_page) if by_page else 0
        pages = [rebuild_rows("\n".join(by_page.get(p, []))) for p in range(1, last + 1)]
        return pages, [sections.get(p, [])[:3] for p in range(1, last + 1)]


def _drop_overlap(previous: str, text: str, limit: int = 400) -> str:
    """Remove the start of ``text`` that repeats the end of ``previous``."""
    if not previous or not text:
        return text
    for size in range(min(limit, len(previous), len(text)), 19, -1):
        if previous.endswith(text[:size]):
            return text[size:]
    return text


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def chunks_table_for(fetch_rows: Callable[..., list[dict]], bucket: str) -> Optional[str]:
    """The bucket's chunks table, from ``SHOW RAGBUCKET`` (fallback: ``<bucket>_chunks``)."""
    try:
        for row in fetch_rows("SHOW RAGBUCKET"):
            lower = {str(k).lower(): v for k, v in row.items()}
            if str(lower.get("ragbucket") or lower.get("name") or "").lower() == bucket.lower():
                table = str(lower.get("chunks_table") or "").strip()
                if table and _IDENT.match(table):
                    return table
                db = str(lower.get("database_name") or "").strip()
                guess = f"{db}.{bucket}_chunks" if db else f"{bucket}_chunks"
                return guess if _IDENT.match(guess) else None
    except Exception as exc:
        logger.info("[corpus] SHOW RAGBUCKET failed (%s); using the default chunks table name", exc)
    guess = f"{bucket}_chunks"
    return guess if _IDENT.match(guess) else None


def open_corpus(bucket: str, fetch_rows: Callable[..., list[dict]], embedder: Any = None) -> Optional[Corpus]:
    """The bucket's corpus with its document list and cards loaded, or None."""
    from corpus.cards import load_cards

    table = chunks_table_for(fetch_rows, bucket)
    if not table:
        return None
    corpus = Corpus(bucket=bucket, chunks_table=table, fetch_rows=fetch_rows, embedder=embedder)
    if not corpus.documents():
        return None
    corpus.cards = load_cards(corpus, corpus.docs)
    return corpus
