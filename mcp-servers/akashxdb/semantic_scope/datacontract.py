"""Measured data contracts for Ontology Views.

The AkashX convention is "rows whose extraction finished have status = 'done'",
and both the prompt and the executor used to enforce it blindly. Real data can
break a convention: in one warehouse 82 of 84 rows carried status 'deleted'
with every extracted field filled and no deletion date, so every Ontology View
query silently saw 2 documents and the agent fell back to slower, weaker
sources (FinanceBench, 2026-10-12).

So each view is profiled (cached): how many rows have extracted values, and
whether ``status = 'done'`` actually selects them. When it does, the convention
stands. When it does not, the usable rows are defined by the data itself
(extracted values present, not deleted), the executor applies that filter
instead, and the agent is told in plain words.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

_SYSTEM_COLUMNS = {
    "id", "raw_text", "file_url", "https_url", "request_id", "deleted_at", "message", "error",
    "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "status", "retry",
    "result", "absent_fields", "miscellaneous", "summary", "created_at", "updated_at",
}
_TTL_SECONDS = 600
_cache: dict[str, tuple[float, "ViewContract"]] = {}


@dataclass
class ViewContract:
    name: str
    total: int = 0
    with_values: int = 0
    done_with_values: int = 0
    statuses: dict = None
    #: Template for the executor ("{a}" = alias prefix); None = default status filter.
    row_filter: Optional[str] = None
    note: str = ""


def _quote(col: str, db_type: str) -> str:
    q = '"' if db_type == "postgresql" else "`"
    return f"{q}{col}{q}"


def _profile(backend: Any, entity: Any, db_type: str) -> ViewContract:
    cols = [c for c, _ in entity.columns]
    lower = {c.lower() for c in cols}
    contract = ViewContract(name=entity.name, statuses={})
    if "status" not in lower:
        contract.row_filter = ""
        return contract
    extracted = [c for c in cols if c.lower() not in _SYSTEM_COLUMNS][:4]
    if not extracted:
        return contract
    filled = " OR ".join(f"{_quote(c, db_type)} IS NOT NULL" for c in extracted)
    table = entity.name
    rows = backend.fetch_rows(
        f"SELECT status, COUNT(*) AS n, COUNT(CASE WHEN {filled} THEN 1 END) AS filled "
        f"FROM {table} GROUP BY status")
    for r in rows:
        status = str(r.get("status"))
        n, f = int(r.get("n") or 0), int(r.get("filled") or 0)
        contract.statuses[status] = n
        contract.total += n
        contract.with_values += f
        if status == "done":
            contract.done_with_values += f
    if contract.with_values == 0:
        contract.note = "no extracted values yet"
        return contract
    if contract.done_with_values >= 0.9 * contract.with_values:
        return contract          # the convention holds
    prefix_cols = " OR ".join(f"{{a}}{_quote(c, db_type)} IS NOT NULL" for c in extracted)
    deleted = f" AND {{a}}{_quote('deleted_at', db_type)} IS NULL" if "deleted_at" in lower else ""
    contract.row_filter = f"({prefix_cols}){deleted}"
    breakdown = ", ".join(f"{k} {v}" for k, v in sorted(contract.statuses.items(), key=lambda kv: -kv[1]))
    contract.note = (f"`status` does not mark usable rows here: {contract.with_values} of {contract.total} rows "
                     f"have extracted values but only {contract.done_with_values} have status 'done' "
                     f"(statuses: {breakdown}). Use every row with values (rows with deleted_at set are "
                     f"excluded automatically); do not filter on status.")
    return contract


async def profile_views(model: Any, backend: Any, db_type: str, cache_key: str) -> dict[str, ViewContract]:
    """Contracts for every Ontology View in the model (cached for 10 minutes)."""
    views = [e for e in getattr(model, "entities", []) if e.kind == "ontology_view" and e.columns]
    out: dict[str, ViewContract] = {}

    async def one(entity: Any) -> None:
        key = f"{cache_key}|{entity.name}"
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL_SECONDS:
            out[entity.name] = hit[1]
            return
        try:
            contract = await asyncio.to_thread(_profile, backend, entity, db_type)
        except Exception as exc:
            logger.info("[datacontract] could not profile %s (%s); keeping the default", entity.name, exc)
            return
        _cache[key] = (time.time(), contract)
        out[entity.name] = contract

    if views and hasattr(backend, "fetch_rows"):
        await asyncio.gather(*(one(v) for v in views))
    return out


def apply_to_sources(cognitive_sources: dict, contracts: dict[str, ViewContract]) -> None:
    """Hand each view's measured filter to the executor's ontology filter."""
    kinds = cognitive_sources.setdefault("table_kinds", {})
    for name, contract in contracts.items():
        if contract.row_filter is not None and name in kinds:
            kinds[name]["row_filter"] = contract.row_filter
