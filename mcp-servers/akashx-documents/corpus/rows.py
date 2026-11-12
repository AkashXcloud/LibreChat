"""Rebuild table rows from plain PDF text.

Plain PDF text extraction writes a table cell per line:

    Purchases of property, plant and equipment (PP&E)
     $
    (1,577) $
    (1,373) $

which hides which number belongs to which line item. ``rebuild_rows`` joins a
label with the value lines that follow it, so the same text reads

    Purchases of property, plant and equipment (PP&E) | $ (1,577) | $ (1,373)

the layout uploaded files already get from layout-mode parsing. Prose is left
alone: only short runs of value-only lines are attached to the line above.
"""

from __future__ import annotations

import re

# A value cell: numbers, currency, signs, parentheses, percent, dashes, footnote
# markers. Must contain a digit or be a lone dash (an empty cell).
_VALUE = re.compile(r"^[\s$€£¥(),.%*\-–—+]*\d[\d\s$€£¥(),.%*\-–—+]*[x×]?$|^\s*[-–—]\s*$")
_CURRENCY_ONLY = re.compile(r"^\s*[$€£¥]\s*$")
_MAX_LABEL = 160


def _is_value(line: str) -> bool:
    text = line.strip()
    return bool(text) and len(text) <= 24 and bool(_VALUE.match(text))


def rebuild_rows(text: str) -> str:
    """Join label lines with the value lines that follow them (`` | `` between cells)."""
    out: list[str] = []
    cells: list[str] = []          # cells of the row being built
    pending_currency = ""

    def flush() -> None:
        nonlocal cells
        if cells:
            out.append(" | ".join(cells))
            cells = []

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _CURRENCY_ONLY.match(line):
            pending_currency = line
            continue
        if _is_value(line):
            value = f"{pending_currency} {line}".strip() if pending_currency else line
            pending_currency = ""
            # Split "(1,577) $" style tails: a trailing currency belongs to the next cell.
            if value.endswith(("$", "€", "£", "¥")) and len(value) > 1:
                pending_currency = value[-1]
                value = value[:-1].strip()
            if cells or out:
                if not cells:          # values right after a finished row: extend it
                    cells = out.pop().split(" | ")
                cells.append(value)
                continue
            out.append(value)
            continue
        flush()
        pending_currency = ""
        cells = [line] if len(line) <= _MAX_LABEL else []
        if not cells:
            out.append(line)
    flush()
    return "\n".join(out)
