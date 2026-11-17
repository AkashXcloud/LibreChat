"""Unit tests for audit_launcher.py (the AkashX MCP wrapper) and the optional semantic-model scope.

Needs mcp-server-akashxdb installed (pip install <path to mcp-server-akashxdb>); no AkashX server,
Postgres or network is used.

  cd mcp-servers/akashxdb
  python -m pytest tests -q
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STARROCKS_HOST"] = "akashx.invalid"   # configured, never contacted
os.environ["STARROCKS_DB"] = ""                    # empty means no default database
os.environ["SEMANTIC_MODEL_ID"] = ""               # the default: no semantic model
os.environ.pop("MCP_AUDIT_LOG", None)

import audit_launcher as launcher  # noqa: E402
import semantic_scope  # noqa: E402
from semantic_scope.semantic_model import SemanticModel  # noqa: E402

MODEL = """
-- Ontology view: one row per support ticket with extracted fields
CREATE TABLE salesdb.ov_tickets (id INT, product VARCHAR(64), severity INT);
-- Orders placed by customers
CREATE TABLE salesdb.orders (id INT, customer VARCHAR(64), total DOUBLE);
-- RAGBUCKET 'salesdb.contracts_rb' customer contracts
"""


def make_scope() -> semantic_scope.Scope:
    return semantic_scope.Scope("7", "test model", SemanticModel.parse(MODEL), {})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def headers(monkeypatch):
    """Request headers seen by the wrapper (empty by default)."""
    current = {}
    monkeypatch.setattr(launcher, "get_http_headers", lambda: current)
    return current


# ---- defaults --------------------------------------------------------------------------
def test_empty_default_database_is_removed_from_the_environment():
    assert "STARROCKS_DB" not in os.environ


def test_no_semantic_model_by_default():
    assert launcher.DEFAULT_MODEL == ""
    assert "semantic model" not in launcher.INSTRUCTIONS.split("\n")[0].lower()


def test_instructions_are_general_and_sent_by_the_server():
    text = launcher.server.mcp._mcp_server.instructions
    assert text == launcher.INSTRUCTIONS
    assert "SHOW DATABASES" in text and "Do not change data" in text


# ---- the tool wrapper ----------------------------------------------------------------------
def test_without_semantic_model_tools_run_unscoped(headers):
    def read_query(query: str) -> str:
        return f"ran {query}"

    wrapped = launcher._wrap("read_query", read_query)
    assert run(wrapped(query="SHOW DATABASES")) == "ran SHOW DATABASES"


def test_not_configured_connection_is_refused(headers, monkeypatch):
    monkeypatch.setattr(launcher, "CONFIGURED", False)

    def read_query(query: str) -> str:
        raise AssertionError("must not run")

    out = run(launcher._wrap("read_query", read_query)(query="SELECT 1"))
    assert out.startswith("Refused:") and "not configured" in out


def test_configured_model_that_cannot_load_refuses(headers, monkeypatch):
    monkeypatch.setattr(launcher, "DEFAULT_MODEL", "7")

    def boom(model_id, client):
        raise RuntimeError("SEMANTIC_MODEL_DSN is not set")

    monkeypatch.setattr(launcher.semantic_scope, "load_scope", boom)

    def read_query(query: str) -> str:
        raise AssertionError("must not run")

    out = run(launcher._wrap("read_query", read_query)(query="SELECT 1"))
    assert out.startswith("Refused:") and "could not be loaded" in out


def test_header_selects_a_model_per_request(headers, monkeypatch):
    seen = []
    monkeypatch.setattr(launcher.semantic_scope, "load_scope", lambda mid, client: seen.append(mid) or make_scope())
    headers["x-semantic-model-id"] = "7"

    def read_query(query: str) -> str:
        return "rows"

    assert run(launcher._wrap("read_query", read_query)(query="SELECT id FROM salesdb.orders")) == "rows"
    assert seen == ["7"]


# ---- scope rules (only when a semantic model is set) ----------------------------------------
def test_scope_allows_model_tables_and_ask():
    s = make_scope()
    assert launcher._scope_error(s, "read_query", {"query": "SELECT customer, SUM(total) FROM salesdb.orders GROUP BY 1"}) is None
    assert launcher._scope_error(s, "read_query", {"query": "ASK 'which contracts renew?' ON contracts_rb"}) is None


def test_scope_refuses_tables_outside_the_model():
    out = launcher._scope_error(make_scope(), "read_query", {"query": "SELECT * FROM hrdb.salaries"})
    assert out is not None and out.content[0].text.startswith("Refused:")


def test_scope_refuses_writes_and_other_tools():
    s = make_scope()
    assert launcher._scope_error(s, "read_query", {"query": "DELETE FROM salesdb.orders"}) is not None
    assert launcher._scope_error(s, "write_query", {"query": "DROP TABLE salesdb.orders"}) is not None


def test_db_summary_returns_the_model_and_refuses_other_databases():
    s = make_scope()
    summary = launcher._scope_error(s, "db_summary", {})
    assert "salesdb.orders" in summary.content[0].text
    other = launcher._scope_error(s, "db_summary", {"db": "hrdb"})
    assert other.content[0].text.startswith("Refused:") and "hrdb" in other.content[0].text


def test_table_overview_follows_the_model():
    s = make_scope()
    assert launcher._scope_error(s, "table_overview", {"table": "salesdb.ov_tickets"}) is None
    assert launcher._scope_error(s, "table_overview", {"table": "hrdb.salaries"}) is not None
