"""Loading and heading-aware chunking of Markdown knowledge (ARCHITECTURE.md §7).

We split on Markdown headings so each chunk is a self-contained concept and
carries its heading path (e.g. ``RAGBUCKET.md > Usage > SharePoint``) as
metadata — which makes retrieved snippets citable. Oversized sections are
further split on blank lines with a soft character budget.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CHUNK_BUDGET = 1200  # soft max characters per chunk


@dataclass
class Document:
    path: str          # source file path (relative for display)
    title: str
    text: str


@dataclass
class Chunk:
    id: str
    doc_path: str
    heading_path: str  # "File > H1 > H2 …" for citations
    text: str
    meta: dict = field(default_factory=dict)

    def citation(self) -> str:
        return self.heading_path or os.path.basename(self.doc_path)


def load_markdown_dir(directory: str) -> list[Document]:
    """Load all ``*.md`` files under *directory* (recursively)."""
    docs: list[Document] = []
    if not directory or not os.path.isdir(directory):
        return docs
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if not fname.lower().endswith((".md", ".markdown")):
                continue
            full = os.path.join(root, fname)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(full, directory)
            docs.append(Document(path=rel, title=_derive_title(fname, text), text=text))
    return docs


def split_markdown(doc: Document) -> list[Chunk]:
    """Split one document into heading-scoped chunks."""
    lines = doc.text.splitlines()
    base = doc.title
    heading_stack: list[str] = []
    sections: list[tuple[str, list[str]]] = []  # (heading_path, body lines)
    current_body: list[str] = []
    current_path = base

    def flush() -> None:
        if any(line.strip() for line in current_body):
            sections.append((current_path, list(current_body)))

    for line in lines:
        m = _HEADING.match(line)
        if m:
            flush()
            current_body = []
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_stack = heading_stack[: level - 1] + [title]
            current_path = " > ".join([base, *heading_stack])
        else:
            current_body.append(line)
    flush()

    chunks: list[Chunk] = []
    for idx, (path, body) in enumerate(sections):
        for sub_idx, piece in enumerate(_pack("\n".join(body).strip())):
            if not piece.strip():
                continue
            chunks.append(Chunk(
                id=f"{doc.path}::{idx}::{sub_idx}",
                doc_path=doc.path,
                heading_path=path,
                text=piece.strip(),
                meta={"title": doc.title},
            ))
    # A doc with no headings still yields one chunk.
    if not chunks and doc.text.strip():
        chunks.append(Chunk(id=f"{doc.path}::0::0", doc_path=doc.path,
                            heading_path=base, text=doc.text.strip()[:_CHUNK_BUDGET],
                            meta={"title": doc.title}))
    return chunks


def _pack(text: str) -> list[str]:
    """Greedily pack paragraphs into <= budget-sized pieces."""
    if len(text) <= _CHUNK_BUDGET:
        return [text]
    pieces, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) > _CHUNK_BUDGET:
            pieces.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        pieces.append(buf)
    return pieces


def _derive_title(fname: str, text: str) -> str:
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(2).strip()
    return os.path.splitext(fname)[0]
