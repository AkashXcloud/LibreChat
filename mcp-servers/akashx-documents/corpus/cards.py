"""Document cards: a short description of each document, so the right one is found first.

A card holds the document's file name, its title from the RagBucket and its page count. If the
bucket's cache folder has a ``cards-v1.json`` (descriptions written earlier, e.g. by Cognate),
the matching card's text fields are kept as the document's description; nothing about the
documents' subject is assumed.

``rank`` scores cards by how many words of a query appear in a document's name, title and
description.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

CARD_VERSION = "1"
_STOP = set("a an and the of for in on to with by at from is are was were pdf doc docx txt file document".split())
_SKIP = {"document", "title", "source", "file_url", "pages", "other_names"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9&]+", (text or "").lower()) if w not in _STOP]


def _description(saved: dict) -> str:
    """The saved card's text fields as one short line."""
    parts = [str(v).strip() for k, v in saved.items()
             if not k.startswith("_") and k not in _SKIP and isinstance(v, (str, int)) and str(v).strip()]
    parts += [str(v) for v in saved.get("other_names") or [] if str(v).strip()]
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)[:240]


def _cards_path(corpus: Any) -> str:
    return os.path.join(corpus.folder, f"cards-v{CARD_VERSION}.json")


def load_cards(corpus: Any, docs: list[Any]) -> dict[str, dict]:
    """One card per document: name, title, pages, and a cached description when one is current."""
    saved: dict = {}
    try:
        with open(_cards_path(corpus), encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    cards = {}
    for d in docs:
        old = saved.get(d.name) or {}
        current = old.get("_fp") == d.fingerprint
        cards[d.name] = {"document": d.name, "title": (old.get("title") if current else "") or d.title,
                         "description": _description(old) if current else "",
                         "pages": d.pages, "file_url": d.file_url}
    return cards


def rank(cards: dict[str, dict], *, query: str = "", k: int = 10) -> list[tuple[float, dict]]:
    """Cards whose name, title or description share words with the query, best first."""
    want = set(_words(query))
    if not want:
        return []
    out = []
    for card in cards.values():
        name = set(_words(os.path.splitext(card.get("document", ""))[0].replace("_", " ")))
        text = set(_words(card.get("title", ""))) | set(_words(card.get("description", "")))
        score = 2.0 * len(want & name) + 1.0 * len(want & (text - name))
        if score > 0:
            out.append((score / len(want), card))
    out.sort(key=lambda x: (-x[0], x[1].get("document", "")))
    return out[:k]


def card_line(card: dict, bucket: str = "") -> str:
    line = card.get("document") or "?"
    if bucket:
        line += f" [{bucket}]"
    about = " — ".join(x for x in (card.get("title"), card.get("description")) if x)
    if about:
        line += f": {about}"
    return line + f", {card.get('pages') or '?'} pages"
