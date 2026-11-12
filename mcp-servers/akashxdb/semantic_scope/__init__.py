"""Semantic-model scope for the AkashX MCP server (the same model and rules Cognate uses).

A semantic model is the list of tables, Ontology Views and RagBuckets an agent may use. It is
stored by Superset/Cognate in Postgres (public.cognate_semantic_models) and selected per request
with the X-Semantic-Model-Id header (set on the scoped MCP server entry in librechat.yaml).

Vendored unchanged from cognate_backend_service (apart from import paths):
  semantic_model.py  <- cognate/assistant/context/semantic_model.py   (parse the stored DDL)
  guard.py           <- cognate/agent/guard.py                        (allow only model entities)
  datacontract.py    <- cognate/agent/datacontract.py                 (measured status notes)
render_data_layer / _entity_block / the AkashX connection rules are copied from
cognate/agent/context.py, and _decode_schema_part / _merge_schema_text from cognate/api/helpers.py.

  python -m semantic_scope render <id>   prints the prompt block for a model (used by setup_librechat.py)
"""
import dataclasses
import json
import logging
import os
import threading
import time
from typing import Any, Optional

from semantic_scope.datacontract import _profile
from semantic_scope.guard import SemanticGuard
from semantic_scope.semantic_model import SemanticEntity, SemanticModel

logger = logging.getLogger(__name__)

#: Above this many characters the data layer lists names only (cognate/agent/context.py).
FULL_SCHEMA_BUDGET = 24000
NOTE_LIMIT = 300
TTL_SECONDS = 600   # same cache lifetime as Cognate's view profiles

# cognate/agent/context.py _ENGINES["starrocks"] + build_system_prompt's connection layer
CONNECTION = (
    "# Connection\nYou are connected to a AkashX database. SQL rules:\n"
    "- Reference tables as `database.table`, or unqualified when the semantic model lists them that way. "
    "Never use a three-part `catalog.database.table` name for internal tables.\n"
    "- MySQL-compatible syntax: backtick identifiers, `LIMIT n`, functions such as `date_trunc`, "
    "`date_format`, `ifnull`, `array_agg`.\n"
    "- No FULL OUTER JOIN emulation tricks are needed; standard joins, CTEs and window functions work.\n"
    "Queries are read-only and results are limited; aggregate in SQL rather than fetching raw rows.")

SCOPE_RULE = ("The objects available to you are exactly those in the semantic model below; you run only "
              "`SELECT`, `WITH` and `ASK`. Queries that reference anything else are refused.")


# ---- cognate/api/helpers.py ---------------------------------------------------------------
def _decode_schema_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return "\n\n".join(str(v) for v in value.values()).strip()
    if isinstance(value, list):
        return "\n\n".join(str(v) for v in value).strip()
    text = str(value).strip()
    if not text or text[0] not in "{[":
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    if isinstance(data, dict):
        return "\n\n".join(str(v) for v in data.values()).strip()
    if isinstance(data, list):
        return "\n\n".join(str(v) for v in data).strip()
    return text


def _merge_schema_text(definition: Optional[str], schema_text: Optional[str]) -> Optional[str]:
    parts = []
    for value in (definition, schema_text):
        if value is None:
            continue
        decoded = _decode_schema_part(value)
        if decoded:
            parts.append(decoded)
    if not parts:
        return None
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    return "\n\n".join(parts)


# ---- cognate/agent/context.py -------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _entity_block(e: SemanticEntity, notes: Optional[dict] = None) -> str:
    if e.kind == "ragbucket":
        line = f"- {e.name} [RagBucket] — ask with: ASK '<question>' ON {e.short_name}"
        return line + (f"\n  {_clip(e.note, NOTE_LIMIT)}" if e.note else "")
    kind = "Ontology View" if e.kind == "ontology_view" else "Table"
    head = f"- {e.name} [{kind}]"
    if e.note:
        head += f" — {_clip(e.note, NOTE_LIMIT)}"
    if e.columns:
        cols = ", ".join(f"{c} {t}" for c, t in e.columns)
        head += f"\n  columns: {cols}"
    measured = (notes or {}).get(e.name)
    if measured:
        head += f"\n  data quality: {measured}"
    return head


def render_data_layer(model: SemanticModel, notes: Optional[dict] = None) -> str:
    if not model.entities:
        return ("# Data\nNo tables are defined in the semantic model for this conversation. "
                "You cannot query data; say so if the user asks for numbers.")
    blocks = [_entity_block(e, notes) for e in model.entities]
    full = "\n".join(blocks)
    if len(full) <= FULL_SCHEMA_BUDGET:
        return ("# Data\nThe semantic model below is the complete list of what you may query. "
                "Treat it as authoritative.\n" + full)
    names = "\n".join(
        f"- {e.name} [{'RagBucket' if e.kind == 'ragbucket' else 'Ontology View' if e.kind == 'ontology_view' else 'Table'}"
        f"{', ' + str(len(e.columns)) + ' columns' if e.columns else ''}]"
        + (f" — {_clip(e.note, 120)}" if e.note else "")
        for e in model.entities
    )
    return ("# Data\nThe semantic model is large, so only names are listed. Call `table_overview` "
            "for the tables you need before writing SQL. You may query only these:\n" + names)


# ---- loading --------------------------------------------------------------------------------
class Scope:
    """One semantic model: parsed entities, its guard and the measured view notes."""

    def __init__(self, model_id: str, name: str, model: SemanticModel, notes: dict) -> None:
        self.model_id, self.name, self.model, self.notes = model_id, name, model, notes
        self.guard = SemanticGuard(model, "starrocks")
        self.databases = {p.split(".")[-2].lower() for p in (e.name for e in model.entities) if p.count(".") >= 1}

    def render(self) -> str:
        """The connection rules, the scope rule and the data layer, as in Cognate's system prompt."""
        return CONNECTION + "\n\n" + SCOPE_RULE + "\n\n" + render_data_layer(self.model, self.notes)

    def find(self, table: str) -> Optional[SemanticEntity]:
        return self.model.find((table or "").split(".")[-1])


def _fetch_definition(model_id: str) -> tuple[str, str]:
    """(name, merged DDL) of a live semantic model — Cognate's fetch_semantic_model_details query."""
    import psycopg

    dsn = os.environ.get("SEMANTIC_MODEL_DSN")
    if not dsn:
        raise RuntimeError("SEMANTIC_MODEL_DSN is not set")
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute('SELECT name, definition, "schema" FROM public.cognate_semantic_models '
                    "WHERE id = %s AND deleted_at IS NULL AND enabled", (int(model_id),))
        row = cur.fetchone()
    if not row:
        raise LookupError(f"semantic model {model_id} not found or not enabled")
    return row[0], _merge_schema_text(row[1], row[2]) or ""


class _Rows:
    """The ``backend.fetch_rows`` surface datacontract._profile expects, over the MCP db client."""

    def __init__(self, db_client: Any, db: Optional[str]) -> None:
        self._db, self._client = db, db_client

    def fetch_rows(self, sql: str) -> list[dict]:
        res = self._client.execute(sql, db=self._db)
        if not res.success:
            raise RuntimeError(res.error_message)
        return [dict(zip(res.column_names or [], r)) for r in res.rows or []]


_cache: dict[str, tuple[float, Scope]] = {}
_lock = threading.Lock()


def load_scope(model_id: str, db_client: Any) -> Scope:
    """The semantic model with its guard and view notes, cached for TTL_SECONDS. Raises on failure."""
    model_id = str(model_id).strip()
    with _lock:
        hit = _cache.get(model_id)
        if hit and time.time() - hit[0] < TTL_SECONDS:
            return hit[1]
        name, text = _fetch_definition(model_id)
        model = SemanticModel.parse(text)
        notes: dict = {}
        for e in model.entities:
            if e.kind == "ontology_view" and e.columns:
                db = e.name.split(".")[-2] if e.name.count(".") >= 1 else None
                try:
                    # the MCP connection rejects three-part catalog.db.table names; query the view by name
                    contract = _profile(_Rows(db_client, db), dataclasses.replace(e, name=e.short_name), "starrocks")
                except Exception as exc:   # same as Cognate: keep the default when profiling fails
                    logger.info("[semantic_scope] could not profile %s (%s)", e.name, exc)
                    continue
                if contract.note:
                    notes[e.name] = contract.note
        scope = Scope(model_id, name, model, notes)
        _cache[model_id] = (time.time(), scope)
        logger.info("[semantic_scope] model %s '%s': %d entities, %d view notes",
                    model_id, name, len(model.entities), len(notes))
        return scope
