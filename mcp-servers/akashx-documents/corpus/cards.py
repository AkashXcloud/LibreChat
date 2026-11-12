"""Document cards: what each document is, so the right one is found first.

A card says which company a document is about, what kind of document it is,
and which period it covers. Cards come from two places:

* ``heuristic_card``: the file name and the bucket's document title (always
  available, instant);
* ``build_cards`` (Cognate only, not vendored): one model call per document
  reading its title and first pages, cached as cards-v1.json in the bucket's
  folder; this server reads those cached cards.

``rank`` scores cards against what a question names (company, year, form,
period), so a question is scoped to the right filing before anything is read.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

CARD_VERSION = "1"
_FORMS = [
    (re.compile(r"\b10[\s_-]?k\b|annual report on form 10-k", re.I), "10-K"),
    (re.compile(r"\b10[\s_-]?q\b|quarterly report", re.I), "10-Q"),
    (re.compile(r"\b8[\s_-]?k\b|current report", re.I), "8-K"),
    (re.compile(r"\bdef[\s_-]?14a\b|proxy statement", re.I), "DEF 14A"),
    (re.compile(r"earnings|press release|results", re.I), "earnings release"),
    (re.compile(r"annual[\s_-]?report", re.I), "annual report"),
]
_QUARTER = re.compile(r"(?:^|[^a-z0-9])(?:(20\d\d)\s*q([1-4])|q([1-4])\s*[-_ ]?\s*(20\d\d)|q([1-4]))(?:[^a-z0-9]|$)", re.I)
_YEAR = re.compile(r"(?<!\d)(19|20)\d\d(?!\d)")
_CORP_WORDS = set("inc incorporated corp corporation company co ltd limited plc holdings holding group "
                  "the llc lp sa ag nv".split())


def _norm(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9&]+", (text or "").lower()) if w not in _CORP_WORDS]


def heuristic_card(name: str, title: str = "") -> dict:
    """A card from the file name and document title alone."""
    stem = os.path.splitext(name)[0]
    text = f"{stem.replace('_', ' ')} {title}"
    form = next((label for rx, label in _FORMS if rx.search(text)), "")
    years = [m.group(0) for m in _YEAR.finditer(stem)] or [m.group(0) for m in _YEAR.finditer(title)]
    quarter = ""
    q = _QUARTER.search(stem.replace("_", " "))
    if q:
        quarter = f"Q{q.group(2) or q.group(3) or q.group(5)}"
    # Company: the leading words of the file name before the first year/form token.
    words = re.split(r"[_\s-]+", stem)
    company_words = []
    for w in words:
        if _YEAR.fullmatch(w) or re.fullmatch(r"(19|20)\d\dq[1-4]", w, re.I) or \
                any(rx.fullmatch(w) for rx, _ in _FORMS[:4]):
            break
        company_words.append(w)
    company = " ".join(company_words) or stem
    return {"company": company, "ticker": "", "form": form,
            "fiscal_year": years[0] if years else "", "fiscal_period": quarter or ("FY" if form in ("10-K", "annual report") else ""),
            "period_end": "", "fiscal_year_end": "", "currency": "", "units": "",
            "other_names": [], "title": title, "source": "heuristic"}


def _cards_path(corpus: Any) -> str:
    return os.path.join(corpus.folder, f"cards-v{CARD_VERSION}.json")


def load_cards(corpus: Any, docs: list[Any]) -> dict[str, dict]:
    """Cached model-built cards where present and current, heuristic cards otherwise."""
    saved: dict = {}
    try:
        with open(_cards_path(corpus), encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    cards = {}
    for d in docs:
        card = saved.get(d.name)
        if not card or card.get("_fp") != d.fingerprint:
            card = heuristic_card(d.name, d.title)
        card = dict(card)
        card.update({"document": d.name, "pages": d.pages, "file_url": d.file_url,
                     "title": card.get("title") or d.title})
        cards[d.name] = card
    return cards


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def _period_tokens(period: str) -> tuple[str, str]:
    p = (period or "").strip().upper().replace(" ", "")
    q = re.search(r"Q([1-4])", p)
    return (f"Q{q.group(1)}" if q else ("FY" if "FY" in p or "ANNUAL" in p else ""),
            (_YEAR.search(p).group(0) if _YEAR.search(p) else ""))


def rank(cards: dict[str, dict], *, company: str = "", year: str = "", form: str = "",
         period: str = "", query: str = "", k: int = 8) -> list[tuple[float, dict]]:
    """Cards scored against what a question names, best first."""
    want_company = set(_norm(company))
    want_query = set(_norm(query))
    want_year = str(year or "").strip()[:4]
    want_q, year_in_period = _period_tokens(period)
    want_year = want_year or year_in_period
    want_form = (form or "").strip().lower()
    out = []
    for card in cards.values():
        names = set(_norm(card.get("company", ""))) | set(_norm(card.get("ticker", "")))
        for other in card.get("other_names") or []:
            names |= set(_norm(str(other)))
        names |= set(_norm(os.path.splitext(card.get("document", ""))[0].split("_")[0]))
        score = 0.0
        if want_company:
            overlap = len(want_company & names)
            if not overlap:
                continue
            score += 4.0 * overlap / max(len(want_company), 1)
        elif want_query:
            overlap = len(want_query & names)
            score += 2.0 * overlap
        doc_year = str(card.get("fiscal_year") or "")[:4]
        if want_year:
            if doc_year == want_year:
                score += 3.0
            elif str(card.get("period_end") or "")[:4] == want_year:
                score += 2.0
            elif doc_year and abs(int(doc_year) - int(want_year)) == 1:
                score += 0.5
            else:
                score -= 1.0
        card_form = str(card.get("form") or "").lower()
        if want_form:
            if want_form in card_form or card_form in want_form:
                score += 2.0
            elif card_form:
                score -= 0.5
        if want_q:
            card_q = str(card.get("fiscal_period") or "").upper()
            if card_q == want_q:
                score += 2.0
            elif card_q:
                score -= 0.5
        if want_query:
            title_words = set(_norm(card.get("title", "")))
            score += 0.2 * len(want_query & title_words)
        out.append((score, card))
    out.sort(key=lambda x: (-x[0], x[1].get("document", "")))
    return out[:k]


def card_line(card: dict) -> str:
    parts = [card.get("company") or "?"]
    if card.get("ticker"):
        parts.append(f"({card['ticker']})")
    desc = " ".join(p for p in parts if p)
    period = " ".join(x for x in [card.get("form"), card.get("fiscal_period"),
                                  card.get("fiscal_year")] if x)
    extra = []
    if card.get("period_end"):
        extra.append(f"period end {card['period_end']}")
    if card.get("fiscal_year_end"):
        extra.append(f"fiscal year ends {card['fiscal_year_end']}")
    if card.get("units"):
        extra.append(f"{card.get('currency') or ''} {card['units']}".strip())
    line = f"{card.get('document')}: {desc} — {period or 'document'}"
    if extra:
        line += f" ({'; '.join(extra)})"
    line += f", {card.get('pages') or '?'} pages"
    return line
