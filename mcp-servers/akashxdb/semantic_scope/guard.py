"""The semantic model as a guard: agent SQL may touch only its entities.

Checked in code before the warehouse sees the statement, on top of the
read-only checks ``ExecutorBackend.run`` already applies. Rules, fail-closed:

- The statement is parsed with sqlglot; unparseable SQL is refused.
- Every table reference must match an entity of the semantic model. A
  reference matches on its table name; when both the reference and the entity
  carry a database qualifier, those must match too. CTE names are exempt.
- System catalogs (``information_schema``, ``pg_catalog``, ``sys``, ``mysql``,
  ``performance_schema``) are refused: the model already lists the schema.
- Table-valued functions as a FROM source are refused (they can read outside
  any table).
- ``ASK '...' ON <bucket>`` must target a RagBucket of the semantic model.
- With no entities in the model, nothing may be queried.
"""

from __future__ import annotations

import re
from typing import Optional

from semantic_scope.semantic_model import SemanticModel
import logging

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pragma: no cover - only when the dependency is missing
    sqlglot = None
    exp = None

_SQLGLOT_DIALECT = {"starrocks": "starrocks", "mysql": "mysql", "postgresql": "postgres"}
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "sys", "mysql", "performance_schema",
                   "_statistics_"}
_ASK_RE = re.compile(r"^\s*(ASK|PROMPT)\b", re.IGNORECASE)
_ASK_ON_RE = re.compile(r"\bON\s+([`\"]?\w+[`\"]?(?:\.[`\"]?\w+[`\"]?){0,2})", re.IGNORECASE)
_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'")


def _clean(name: object) -> str:
    return str(name or "").strip().strip("`\"").lower()


def _parts(name: str) -> list[str]:
    return [_clean(p) for p in (name or "").split(".") if _clean(p)]


class SemanticGuard:
    def __init__(self, model: SemanticModel, db_type: str) -> None:
        self._dialect = _SQLGLOT_DIALECT.get((db_type or "").lower(), "starrocks")
        self._tables: list[list[str]] = []
        self._buckets: list[list[str]] = []
        for e in model.entities:
            (self._buckets if e.kind == "ragbucket" else self._tables).append(_parts(e.name))

    @staticmethod
    def is_ask(sql: str) -> bool:
        return bool(_ASK_RE.match(sql or ""))

    @staticmethod
    def _matches(ref: list[str], allowed: list[list[str]]) -> bool:
        """``ref`` and each allowed name are [catalog?, db?, table] parts."""
        if not ref:
            return False
        for name in allowed:
            if not name or name[-1] != ref[-1]:
                continue
            # Compare qualifiers both sides have, right to left.
            if all(a == b for a, b in zip(reversed(ref[:-1]), reversed(name[:-1]))):
                return True
        return False

    def allowed_names(self) -> str:
        names = [".".join(p) for p in self._tables + self._buckets]
        return ", ".join(names[:40]) + (" …" if len(names) > 40 else "")

    def check(self, sql: str) -> Optional[str]:
        """An error message for the model, or None when the SQL is in scope."""
        if not self._tables and not self._buckets:
            return "The semantic model defines no tables, so nothing can be queried."
        if self.is_ask(sql):
            masked = _LITERAL_RE.sub("''", sql)
            match = _ASK_ON_RE.search(masked)
            if match is None:
                return None if masked.lstrip().upper().startswith("PROMPT") else \
                    "ASK must name its RagBucket: ASK '<question>' ON <bucket>."
            if not self._matches(_parts(match.group(1)), self._buckets):
                return (f"'{match.group(1)}' is not a RagBucket in the semantic model. "
                        f"Available: {self.allowed_names()}")
            return None

        if sqlglot is None:
            logger.error("[guard] sqlglot is not installed; refusing unverifiable SQL")
            return "The query could not be verified against the semantic model."
        try:
            statements = [s for s in sqlglot.parse(sql, read=self._dialect) if s is not None]
        except Exception as exc:
            return f"The query could not be parsed ({str(exc).splitlines()[0][:200]}). Rewrite it as one standard SELECT."
        if len(statements) != 1:
            return "Run exactly one statement per call."

        statement = statements[0]
        ctes = {_clean(c.alias_or_name) for c in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                return "Table functions are not allowed as a FROM source; select from a table."
            ref = [p for p in (_clean(table.catalog), _clean(table.db), _clean(table.name)) if p]
            if len(ref) == 1 and ref[0] in ctes:
                continue
            if any(p in _SYSTEM_SCHEMAS for p in ref[:-1]):
                return ("System catalogs are not available. The semantic model already lists "
                        "every table and column you can use.")
            if not self._matches(ref, self._tables):
                if self._matches(ref, self._buckets):
                    return (f"'{'.'.join(ref)}' is a RagBucket, not a table. "
                            f"Use ASK '<question>' ON {ref[-1]}.")
                label = ".".join(ref)
                logger.info("[guard] blocked reference outside the semantic model: %s", label)
                return (f"'{label}' is not in the semantic model. Query only: "
                        f"{self.allowed_names()}")
        return None
