"""Searchable index of an uploaded file: parse once, cache, search by meaning and words.

How the big assistants handle uploads, applied to Cognate:

* **Parse once.** A file is parsed the first time it is used and cached on the
  shared upload volume, keyed by its content hash, so later questions (and other
  conversations attaching the same file) start instantly.
* **Tables stay tables.** PDFs are read in layout mode, so a statement row stays
  on one line with its year columns ("Purchases of PP&E | (1,577) | (1,373)")
  instead of one number per line.
* **Pages are kept.** Every chunk knows its page, so the agent can open a page
  and cite it.
* **Hybrid search.** Each chunk is scored by keywords (BM25: exact terms such as
  "PP&E" or "FY2019" matter in financial text) and, when an embedding model is
  configured, by meaning; the two rankings are fused (reciprocal rank fusion).
  Without an embedding model, search is keyword-only and still works.
* **Spreadsheets become tables.** CSV / Excel sheets are loaded as SQL tables
  (see ``tables``) so the agent computes instead of reading numbers as text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

import logging

logger = logging.getLogger(__name__)

PARSER_VERSION = "2"
CACHE_DIR = os.getenv("COGNATE_FILE_INDEX_DIR",
                      os.path.join(os.getenv("COGNATE_ATTACHMENT_DIR", "/app/uploads/attachments"),
                                   ".cognate_index"))
CHUNK_CHARS = int(os.getenv("COGNATE_FILE_CHUNK_CHARS", "1800"))
CHUNK_OVERLAP = int(os.getenv("COGNATE_FILE_CHUNK_OVERLAP", "250"))
MAX_FILE_CHARS = int(os.getenv("COGNATE_FILE_INDEX_MAX_CHARS", str(5_000_000)))
MAX_TABLE_ROWS = int(os.getenv("COGNATE_FILE_MAX_ROWS", "200000"))
PAGE_CHARS = 6000          # "pages" for formats without real pages

_WORD = re.compile(r"[a-z0-9]+(?:[.&'][a-z0-9]+)*")
_SPACES = re.compile(r"[ \t]{3,}")
_STOP = set("the a an of and or to in on for by with at from as is are was were be been this "
            "that these those it its which what how did does do has have had than then into "
            "per vs".split())
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _stem(word: str) -> str:
    """Light plural folding so 'sheet' finds 'Sheets' and 'liability' finds 'liabilities'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _WORD.findall(text.lower()) if t not in _STOP]


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _compact(line: str) -> str:
    """A layout line with column gaps as `` | `` separators."""
    return _SPACES.sub(" | ", line.strip())


def _parse_pdf_range(path: str, start: int, stop: int) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages: list[str] = []
    for i in range(start, min(stop, len(reader.pages))):
        page = reader.pages[i]
        try:
            raw = page.extract_text(extraction_mode="layout") or ""
        except Exception:  # some pages trip the layout engine; plain text still helps
            raw = page.extract_text() or ""
        lines = [_compact(line) for line in raw.splitlines()]
        pages.append(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip())
    return pages


PARSE_WORKERS = int(os.getenv("COGNATE_FILE_PARSE_WORKERS", str(min(8, os.cpu_count() or 2))))
_pool = None
_pool_guard = threading.Lock()


def _parse_pool():
    """Worker processes for parsing (pure-Python PDF parsing is CPU-bound)."""
    global _pool
    with _pool_guard:
        if _pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            _pool = ProcessPoolExecutor(max_workers=PARSE_WORKERS,
                                        mp_context=multiprocessing.get_context("spawn"))
        return _pool


def parse_pdf(path: str) -> list[str]:
    """Every page in layout mode; large files are split across worker processes."""
    from pypdf import PdfReader

    total = len(PdfReader(path).pages)
    if total <= 20 or PARSE_WORKERS <= 1:
        return _parse_pdf_range(path, 0, total)
    step = max(10, -(-total // PARSE_WORKERS))
    ranges = [(s, s + step) for s in range(0, total, step)]
    try:
        pool = _parse_pool()
        parts = list(pool.map(_parse_pdf_range, [path] * len(ranges),
                              [r[0] for r in ranges], [r[1] for r in ranges]))
    except Exception as exc:  # a broken pool must not lose the file
        logger.warning("[files] parallel parse failed (%s); parsing in one process", exc)
        return _parse_pdf_range(path, 0, total)
    return [page for part in parts for page in part]


def _paginate(text: str) -> list[str]:
    """Split page-less text into ~PAGE_CHARS pages on paragraph boundaries."""
    pages, cur = [], []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        if cur and size + len(para) > PAGE_CHARS:
            pages.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para) + 2
    if cur:
        pages.append("\n\n".join(cur))
    return [p for p in pages if p.strip()] or [text]


def read_tables(path: str, kind: str) -> list[dict]:
    """CSV / Excel sheets as ``{name, columns, rows}`` (all values as strings)."""
    import pandas as pd

    frames: dict[str, Any] = {}
    if kind == "csv":
        frames[os.path.splitext(os.path.basename(path))[0]] = pd.read_csv(
            path, dtype=str, keep_default_na=False, nrows=MAX_TABLE_ROWS, sep=None, engine="python")
    elif kind in ("xlsx", "xls", "excel"):
        frames = pd.read_excel(path, sheet_name=None, dtype=str, nrows=MAX_TABLE_ROWS)
    out = []
    for name, df in frames.items():
        df = df.fillna("")
        out.append({"name": str(name), "columns": [str(c) for c in df.columns],
                    "rows": df.astype(str).values.tolist()})
    return out


# --------------------------------------------------------------------------- #
# chunks + search
# --------------------------------------------------------------------------- #
CHUNKER_VERSION = "3"
TABLE_CHUNK_CHARS = int(os.getenv("COGNATE_FILE_TABLE_CHUNK_CHARS", "4500"))
_HEADING_WORD = re.compile(r"[A-Za-z][A-Za-z'&.-]*")


@dataclass
class Chunk:
    page: int          # 1-based
    text: str
    section: str = ""  # the heading the chunk sits under
    table: bool = False


# --------------------------------------------------------------------------- #
# document structure: headings, outline, tables
# --------------------------------------------------------------------------- #
def _is_heading(line: str) -> bool:
    """A short title-like line: no table columns, few digits, mostly capitalised words."""
    text = line.strip().strip("|").strip()
    if not 4 <= len(text) <= 90 or " | " in text or text.endswith((".", ",", ";", ":")):
        return False
    words = _HEADING_WORD.findall(text)
    if len(words) < 2 or sum(c.isdigit() for c in text) > len(text) * 0.2:
        return False
    if text.isupper():
        return True
    small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the",
             "to", "with", "per", "vs", "and/or"}
    caps = [w for w in words if w.lower() not in small]
    return bool(caps) and sum(w[0].isupper() for w in caps) / len(caps) >= 0.8


def page_headings(pages: list[str], per_page: int = 3) -> list[list[str]]:
    """Headings per page, without running headers/footers (lines on many pages)."""
    found: list[list[str]] = []
    counts: Counter = Counter()
    for page in pages:
        heads, seen = [], set()
        for line in page.splitlines()[:60]:
            text = " ".join(line.strip().strip("|").split())
            if _is_heading(text) and text.lower() not in seen:
                seen.add(text.lower())
                heads.append(text)
        found.append(heads)
        counts.update(seen)
    limit = max(3, int(len(pages) * 0.2))
    return [[h for h in heads if counts[h.lower()] <= limit][:per_page] for heads in found]


def _blocks(text: str) -> list[tuple[str, bool]]:
    """Paragraph and table blocks of a page; a table keeps its rows together."""
    out: list[tuple[str, bool]] = []
    cur: list[str] = []
    cur_table = False

    def flush() -> None:
        if cur:
            out.append(("\n".join(cur).strip(), cur_table))

    for line in text.splitlines():
        is_row = line.count(" | ") >= 2
        if not line.strip():
            if cur and not cur_table:
                flush()
                cur, cur_table = [], False
            continue
        if cur and is_row != cur_table and (is_row or not _is_heading(line)):
            flush()
            cur = []
        if not cur:
            cur_table = is_row
        cur.append(line)
    flush()
    return [(b, t) for b, t in out if b]


def _split_table(block: str) -> list[str]:
    """A long table in pieces, each repeating the table's header rows."""
    lines = block.splitlines()
    head = lines[:2]
    pieces, cur, size = [], list(head), sum(len(x) for x in head)
    for line in lines[2:]:
        if size + len(line) > TABLE_CHUNK_CHARS and len(cur) > len(head):
            pieces.append("\n".join(cur))
            cur, size = list(head), sum(len(x) for x in head)
        cur.append(line)
        size += len(line) + 1
    if len(cur) > len(head) or not pieces:
        pieces.append("\n".join(cur))
    return pieces


def chunk_pages(pages: list[str], headings: Optional[list[list[str]]] = None) -> list[Chunk]:
    """Chunks that never cross a page. Paragraphs are packed to ~CHUNK_CHARS with a
    small overlap; a table stays in one chunk (up to TABLE_CHUNK_CHARS), and each
    chunk knows the section heading it sits under."""
    headings = headings if headings is not None else page_headings(pages)
    out: list[Chunk] = []
    section = ""
    for number, page in enumerate(pages, 1):
        heads = headings[number - 1] if number - 1 < len(headings) else []
        cur: list[str] = []
        size = 0
        for block, is_table in _blocks(page):
            first = block.splitlines()[0].strip()
            if first in heads:
                section = first
            if is_table:
                if cur:
                    out.append(Chunk(number, "\n\n".join(cur), section))
                    cur, size = [], 0
                for piece in _split_table(block):
                    out.append(Chunk(number, piece, section, table=True))
                continue
            if cur and size + len(block) > CHUNK_CHARS:
                out.append(Chunk(number, "\n\n".join(cur), section))
                tail = cur[-1] if len(cur[-1]) <= CHUNK_OVERLAP * 2 else ""
                cur, size = ([tail] if tail else []), len(tail)
            while len(block) > CHUNK_CHARS:
                cut = block.rfind("\n", CHUNK_CHARS // 2, CHUNK_CHARS)
                cut = cut if cut > 0 else CHUNK_CHARS
                out.append(Chunk(number, "\n\n".join(cur + [block[:cut]]), section))
                cur, size = [], 0
                block = block[max(cut - CHUNK_OVERLAP, 1):]
            cur.append(block)
            size += len(block) + 2
        if cur:
            out.append(Chunk(number, "\n\n".join(cur), section))
    return [c for c in out if c.text.strip()]


@dataclass
class Hit:
    page: int
    text: str
    score: float
    section: str = ""
    table: bool = False


@dataclass
class FileIndex:
    name: str
    kind: str
    pages: list[str]
    chunks: list[Chunk]
    tables: list[dict] = field(default_factory=list)
    vectors: Optional[np.ndarray] = None
    embed_query: Optional[Callable[[str], list[float]]] = None
    headings: list[list[str]] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _tf: list[Counter] = field(default_factory=list)
    _avg_len: float = 1.0

    def __post_init__(self) -> None:
        for chunk in self.chunks:
            # Section titles weigh more than body text (field boosting).
            tf = Counter(_tokens(chunk.text) + _tokens(chunk.section) * 3)
            self._tf.append(tf)
            self._df.update(tf.keys())
        self._avg_len = (sum(sum(tf.values()) for tf in self._tf) / max(len(self._tf), 1)) or 1.0

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(f"[page {i}]\n{p}" for i, p in enumerate(self.pages, 1) if p)

    def page(self, number: int) -> str:
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1]
        return ""

    def matching_sections(self, query: str, k: int = 5) -> list[tuple[int, str]]:
        """Headings that cover most of the query's words, best first."""
        terms = set(_tokens(query))
        if not terms:
            return []
        scored = []
        for page, heading in self.outline:
            words = set(_tokens(heading))
            if not words:
                continue
            overlap = len(terms & words)
            if overlap and overlap / len(words) >= 0.5:
                scored.append((overlap / len(terms) + overlap / len(words), page, heading))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out, seen = [], set()
        for _, page, heading in scored:
            if (page, heading.lower()) not in seen:
                seen.add((page, heading.lower()))
                out.append((page, heading))
        return out[:k]

    @property
    def outline(self) -> list[tuple[int, str]]:
        """``(page, heading)`` for every heading, in document order."""
        return [(i, h) for i, heads in enumerate(self.headings, 1) for h in heads]

    def _bm25(self, query: str) -> np.ndarray:
        n = len(self.chunks)
        scores = np.zeros(n, dtype=np.float32)
        terms = set(_tokens(query))
        if not terms or not n:
            return scores
        k1, b = 1.5, 0.75
        for term in terms:
            df = self._df.get(term)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self._tf):
                f = tf.get(term)
                if f:
                    length = sum(tf.values())
                    scores[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self._avg_len))
        return scores

    def search(self, query: str, k: int = 6) -> list[Hit]:
        if not self.chunks:
            return []
        keyword = self._bm25(query)
        rankings = [np.argsort(-keyword)[: max(k * 8, 40)]] if keyword.any() else []
        if self.vectors is not None and self.embed_query is not None:
            try:
                q = np.asarray(self.embed_query(query), dtype=np.float32)
                q /= (np.linalg.norm(q) or 1.0)
                rankings.append(np.argsort(-(self.vectors @ q))[: max(k * 8, 40)])
            except Exception as exc:
                logger.warning("[files] query embedding failed (%s); keyword search only", exc)
        if not rankings:
            return []
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, idx in enumerate(ranking):
                fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (60 + rank)
        best = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        return [Hit(self.chunks[i].page, self.chunks[i].text, round(s, 5),
                    self.chunks[i].section, self.chunks[i].table) for i, s in best]


# --------------------------------------------------------------------------- #
# build / cache
# --------------------------------------------------------------------------- #
def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def build_index(path: str, name: str, kind: str, embedder: Any = None) -> FileIndex:
    """Parse (or load the cached parse of) ``path`` and attach embeddings.

    Blocking: call it in a worker thread. Safe to call concurrently for the
    same file (one parse; the others wait and read the cache).
    """
    digest = file_hash(path)
    folder = os.path.join(CACHE_DIR, f"{digest}-v{PARSER_VERSION}")
    with _lock_for(folder):
        os.makedirs(folder, exist_ok=True)
        pages_path = os.path.join(folder, "pages.json")
        tables_path = os.path.join(folder, "tables.json")
        if os.path.exists(pages_path):
            with open(pages_path, encoding="utf-8") as fh:
                pages = json.load(fh)
            tables = []
            if os.path.exists(tables_path):
                with open(tables_path, encoding="utf-8") as fh:
                    tables = json.load(fh)
        else:
            tables = []
            if kind == "pdf":
                pages = parse_pdf(path)
            else:
                from cognate.files.extraction import extract_text

                if kind in ("csv", "xlsx", "xls", "excel"):
                    try:
                        tables = read_tables(path, kind)
                    except Exception as exc:
                        logger.warning("[files] could not load %s as a table: %s", name, exc)
                pages = _paginate(extract_text(path) or "")
            total, kept = 0, []
            for page in pages:
                if total >= MAX_FILE_CHARS:
                    break
                kept.append(page[: MAX_FILE_CHARS - total])
                total += len(kept[-1])
            pages = kept
            _write_json(pages_path, pages)
            if tables:
                _write_json(tables_path, tables)

        headings = page_headings(pages)
        index = FileIndex(name=name, kind=kind, pages=pages, chunks=chunk_pages(pages, headings),
                          tables=tables, headings=headings)
        attach_vectors(index, folder, embedder)
        return index


def attach_vectors(index: FileIndex, folder: str, embedder: Any) -> None:
    """Embed the index's chunks (cached in ``folder``) so search can use meaning."""
    if embedder is None or not index.chunks:
        return
    embed_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", getattr(embedder, "cache_id", "") or
                      f"{type(embedder).__name__}-{getattr(embedder, 'dim', 0)}")
    vec_path = os.path.join(folder, f"vectors-c{CHUNKER_VERSION}-{embed_id}.npy")
    vectors = None
    if os.path.exists(vec_path):
        vectors = np.load(vec_path)
        if vectors.shape[0] != len(index.chunks):
            vectors = None
    if vectors is None:
        try:
            vectors = _embed_chunks(embedder, index)
            np.save(vec_path, vectors)
        except Exception as exc:
            logger.warning("[files] embedding %s failed (%s); keyword search only", index.name, exc)
    if vectors is not None:
        index.vectors = vectors
        index.embed_query = lambda q, _e=embedder: _e.embed([q])[0]


def embed_with_retry(embed: Callable[[list[str]], list[list[float]]], texts: list[str],
                     attempts: int = 5, base_delay: float = 2.0) -> list[list[float]]:
    """Call ``embed`` with exponential backoff on transient failures (cognate.assistant_bridge)."""
    import time

    for attempt in range(attempts):
        try:
            return embed(texts)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * 2 ** attempt)
    return []


def _embed_chunks(embedder: Any, index: FileIndex) -> np.ndarray:
    texts = [f"{index.name} p.{c.page} {c.section}\n{c.text}" for c in index.chunks]
    out: list[list[float]] = []
    batch, size = [], 0
    for text in texts:
        if batch and (len(batch) >= 128 or size + len(text) > 300_000):
            out.extend(embed_with_retry(embedder.embed, batch))
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        out.extend(embed_with_retry(embedder.embed, batch))
    mat = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)
