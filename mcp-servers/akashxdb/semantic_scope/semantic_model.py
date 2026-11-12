"""The Semantic Model — the authoritative, user-provided schema (spec Goal 1).

When a database holds many catalogs / databases / thousands of tables, live
introspection is impractical and noisy. Instead the user supplies a **semantic
model**: a text blob (DDL-like) describing only the *selected* entities the
assistant should reason over. Cognate treats it as the PRIMARY source of truth
for the schema.

The model may declare three kinds of entity:
  * **table**          — a normal table; query with SQL.
  * **ontology_view**  — an AI-extracted structured table; query like a table.
  * **ragbucket**      — a document corpus; NOT a table and has no columns.
                         Query it via ``ASK ON <name> '<question>'`` (managed
                         retrieval / AkashX Cognitive SQL), never with SELECT.

This module parses that text into structured entities so the schema tools can
list/describe them and the SQL agent knows how to query each kind. The raw text
is always preserved and returned verbatim by ``get_database_schema``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+([`\"\w.]+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_RAGBUCKET = re.compile(r"--\s*RAGBUCKET\s+'([^']+)'\s*(.*)", re.IGNORECASE)
_COLUMN = re.compile(r"^\s*([`\"\w]+)\s+([A-Za-z][\w(),]*(?:\s+[\w(),]+)*)")


@dataclass
class SemanticEntity:
    name: str                              # as written (may be catalog.db.table)
    kind: str                              # table | ontology_view | ragbucket
    note: str = ""                         # the descriptive comment / usage hint
    columns: list[tuple[str, str]] = field(default_factory=list)
    ddl: str = ""                          # raw block (for tables/views)

    @property
    def short_name(self) -> str:
        return self.name.strip("`\"").split(".")[-1]

    def index_line(self) -> str:
        if self.kind == "ragbucket":
            return f"  - {self.name} [ragbucket] → query with: ASK '<question>' ON {self.short_name}"
        cols = f"{len(self.columns)} cols" if self.columns else "table"
        return f"  - {self.name} [{self.kind}, {cols}]"

    def describe(self) -> str:
        if self.kind == "ragbucket":
            return (f"{self.name} is a RagBucket (document corpus), NOT a table.\n"
                    f"{self.note}\n"
                    f"Query it with: ASK '<your natural-language question>' ON {self.short_name}")
        head = f"-- {self.note}\n" if self.note else ""
        return head + (self.ddl or self.name)


class SemanticModel:
    """Parsed view over a semantic-model text blob (raw text preserved)."""

    def __init__(self, raw: str, entities: list[SemanticEntity]) -> None:
        self.raw = raw
        self.entities = entities

    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, text: str) -> "SemanticModel":
        entities: list[SemanticEntity] = []
        lines = text.splitlines()

        # Tables / ontology views (with the comment line just above, if any).
        for m in _CREATE_TABLE.finditer(text):
            name, body = m.group(1), m.group(2)
            note = _preceding_comment(text, m.start())
            kind = "ontology_view" if "ontology" in note.lower() else "table"
            entities.append(SemanticEntity(
                name=name.strip("`\""), kind=kind, note=note,
                columns=_parse_columns(body), ddl=m.group(0).strip(),
            ))

        # RagBuckets (declared by a `-- RAGBUCKET '<name>' …` comment).
        for line in lines:
            rm = _RAGBUCKET.search(line)
            if rm:
                entities.append(SemanticEntity(
                    name=rm.group(1).strip(), kind="ragbucket",
                    note=rm.group(2).strip() or line.strip("- ").strip(),
                ))

        return cls(raw=text, entities=entities)

    # ------------------------------------------------------------------ #
    def find(self, name: str) -> SemanticEntity | None:
        key = name.strip("`\"").lower()
        for e in self.entities:
            if e.name.lower() == key or e.short_name.lower() == key:
                return e
        return None

    def entity_names(self) -> list[str]:
        return [e.name for e in self.entities]

    def render_index(self) -> str:
        """A compact listing of entities (names + kinds) for the prompt."""
        if not self.entities:
            return "(semantic model present but no entities parsed)"
        tables = [e for e in self.entities if e.kind != "ragbucket"]
        buckets = [e for e in self.entities if e.kind == "ragbucket"]
        parts: list[str] = []
        if tables:
            parts.append("Tables / ontology views (query with SQL):")
            parts += [e.index_line() for e in tables]
        if buckets:
            parts.append("RagBuckets (managed retrieval — query with ASK '<question>' ON <name>):")
            parts += [e.index_line() for e in buckets]
        return "\n".join(parts)

    def __bool__(self) -> bool:
        return bool(self.raw.strip())


# --------------------------------------------------------------------------- #
def _preceding_comment(text: str, start: int) -> str:
    """Return the `--` comment line immediately above position ``start``."""
    prefix = text[:start].rstrip()
    last_nl = prefix.rfind("\n")
    line = prefix[last_nl + 1:].strip()
    if line.startswith("--"):
        return line.lstrip("-").strip()
    return ""


def _split_top_level(body: str) -> list[str]:
    """Split on commas outside parentheses, so ``DECIMAL(12,2)`` stays whole."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return parts


def _parse_columns(body: str) -> list[tuple[str, str]]:
    cols: list[tuple[str, str]] = []
    for raw in _split_top_level(body):
        line = raw.strip()
        if not line or line.upper().startswith(("PRIMARY KEY", "KEY", "INDEX", "CONSTRAINT")):
            continue
        m = _COLUMN.match(line)
        if m:
            cols.append((m.group(1).strip("`\""), m.group(2).strip()))
    return cols
