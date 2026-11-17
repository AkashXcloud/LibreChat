"""Unit tests for the akashx-documents MCP server (no AkashX server, Postgres or OpenAI needed).

  cd mcp-servers/akashx-documents
  python -m pytest tests -q
"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["AKASHX_HOST"] = ""            # not configured unless a test sets it
os.environ["SEMANTIC_MODEL_ID"] = ""      # the default: no semantic model
os.environ["OPENAI_API_KEY"] = ""         # keyword search only
os.environ["AKASHX_DOCS_CACHE"] = str(HERE / ".cache")

import server  # noqa: E402
from corpus import cards, store  # noqa: E402

CHUNKS = {
    "salesdb.contracts_rb_chunks": [
        {"file_name": "master_services_agreement_2024.pdf", "document_title": "Master Services Agreement 2024",
         "chunk_index": 0, "chunk_page_start": 1, "chunk_page_end": 1, "section_title": "Scope",
         "chunk_text": "This agreement covers consulting services and support levels.", "file_url": "s3://x/msa.pdf"},
        {"file_name": "master_services_agreement_2024.pdf", "document_title": "Master Services Agreement 2024",
         "chunk_index": 1, "chunk_page_start": 2, "chunk_page_end": 2, "section_title": "Termination",
         "chunk_text": "Either party may terminate with ninety days written notice.", "file_url": "s3://x/msa.pdf"},
        {"file_name": "shared_policy.pdf", "document_title": "Data Retention Policy",
         "chunk_index": 0, "chunk_page_start": 1, "chunk_page_end": 1, "section_title": "Retention",
         "chunk_text": "Records are retained for seven years.", "file_url": "s3://x/p.pdf"},
    ],
    "hrdb.policies_rb_chunks": [
        {"file_name": "shared_policy.pdf", "document_title": "Travel Policy",
         "chunk_index": 0, "chunk_page_start": 1, "chunk_page_end": 1, "section_title": "Travel",
         "chunk_text": "Economy class is required for flights under six hours.", "file_url": "s3://y/p.pdf"},
        {"file_name": "employee_handbook.pdf", "document_title": "Employee Handbook",
         "chunk_index": 0, "chunk_page_start": 1, "chunk_page_end": 1, "section_title": "Leave",
         "chunk_text": "Employees receive twenty days of paid leave per year.", "file_url": "s3://y/h.pdf"},
    ],
}
RAGBUCKETS = {
    "salesdb": [{"ragbucket": "contracts_rb", "database_name": "salesdb", "chunks_table": "salesdb.contracts_rb_chunks"}],
    "hrdb": [{"ragbucket": "policies_rb", "database_name": "hrdb", "chunks_table": "hrdb.policies_rb_chunks"}],
    "emptydb": [],
}


def fake_fetch_rows(sql: str, params=None) -> list[dict]:
    """A tiny stand-in for the AkashX server: databases, RagBuckets and chunks tables."""
    s = " ".join(sql.split())
    if s == "SHOW DATABASES":
        return [{"Database": d} for d in ("information_schema", "sys", "salesdb", "hrdb", "emptydb")]
    if s.startswith("SHOW RAGBUCKET FROM"):
        return RAGBUCKETS[s.split("`")[1]]
    for table, rows in CHUNKS.items():
        if f"FROM {table} " in s + " ":
            if "GROUP BY file_name" in s:
                out = {}
                for r in rows:
                    d = out.setdefault(r["file_name"], {"file_name": r["file_name"], "file_url": r["file_url"],
                                                        "title": r["document_title"], "pages": 0, "chunks": 0})
                    d["pages"] = max(d["pages"], r["chunk_page_end"])
                    d["chunks"] += 1
                return list(out.values())
            return [r for r in rows if r["file_name"] == (params or {}).get("name")]
    raise AssertionError(f"unexpected SQL: {s}")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, tmp_path):
    """Each test: the fake server, empty caches, no semantic model."""
    monkeypatch.setattr(server, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(store, "CORPUS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_catalog_cache", (0.0, []))
    server._corpora.clear()
    server._model_cache.clear()
    store._index_cache.clear()
    monkeypatch.setattr(server, "DEFAULT_MODEL", "")
    monkeypatch.delenv("AKASHX_DOCS_DATABASES", raising=False)


def scope(model_id: str = "", bucket: str = "") -> server.Scope:
    s, err = server._scope(model_id, bucket)
    assert s is not None, err
    return s


# ---- bucket discovery without a semantic model -------------------------------------------
def test_without_semantic_model_every_ragbucket_is_available():
    names = sorted(b.name for b in scope().buckets)
    assert names == ["contracts_rb", "policies_rb"]


def test_system_databases_are_skipped():
    assert all(b.database not in ("information_schema", "sys") for b in server._catalog())


def test_docs_databases_setting_limits_discovery(monkeypatch):
    monkeypatch.setenv("AKASHX_DOCS_DATABASES", "hrdb")
    assert [b.name for b in server._catalog()] == ["policies_rb"]


def test_find_documents_without_query_lists_each_collection():
    out = server._find(scope(), "", 10)
    assert "contracts_rb (database salesdb): 2 documents" in out
    assert "policies_rb (database hrdb): 2 documents" in out


def test_find_documents_ranks_by_name_and_title():
    out = server._find(scope(), "services agreement", 5)
    assert out.splitlines()[1].startswith("master_services_agreement_2024.pdf [contracts_rb]")


def test_find_documents_reports_no_match():
    assert "No document names or titles match" in server._find(scope(), "zzzz", 5)


def test_unknown_bucket_is_an_error_listing_the_available_ones():
    s, err = server._scope("", "nope")
    assert s is None and "no collection named 'nope'" in err and "contracts_rb" in err


def test_bucket_argument_narrows_the_scope():
    assert [b.name for b in scope(bucket="policies_rb").buckets] == ["policies_rb"]
    assert [b.name for b in scope(bucket="hrdb.policies_rb").buckets] == ["policies_rb"]


# ---- reading -------------------------------------------------------------------------------
def test_read_and_search_a_document():
    s = scope()
    page = server._read(s, "master_services_agreement_2024.pdf", 2, 1)
    assert "ninety days written notice" in page and "page 2" in page
    hits = server._search(s, "terminate notice", ["master_services_agreement_2024.pdf"], 3)
    assert "Termination" in hits or "ninety days" in hits


def test_page_out_of_range_is_reported():
    assert "has pages 1-2" in server._read(scope(), "master_services_agreement_2024.pdf", 9, 1)


def test_same_document_name_in_two_collections_needs_bucket():
    hit, err = scope().document("shared_policy.pdf")
    assert hit is None and "several collections" in err
    hit, err = scope(bucket="policies_rb").document("shared_policy.pdf")
    assert hit is not None and hit[0].bucket == "policies_rb"


def test_unknown_document_is_reported():
    assert "no document named" in server._read(scope(), "missing.pdf", 1, 1)


# ---- optional semantic model -----------------------------------------------------------------
def test_semantic_model_limits_to_its_ragbuckets(monkeypatch):
    monkeypatch.setattr(server, "_model_bucket_names", lambda mid: [("salesdb.contracts_rb", "contracts_rb")])
    assert [b.name for b in scope("7").buckets] == ["contracts_rb"]


def test_semantic_model_matches_three_part_names(monkeypatch):
    monkeypatch.setattr(server, "_model_bucket_names", lambda mid: [("default_catalog.hrdb.policies_rb", "policies_rb")])
    assert [b.name for b in scope("7").buckets] == ["policies_rb"]


def test_semantic_model_that_cannot_load_refuses(monkeypatch):
    def broken(mid):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(server, "_model_bucket_names", broken)
    s, err = server._scope("7", "")
    assert s is None and err.startswith("Refused: semantic model 7 could not be loaded")


def test_semantic_model_without_dsn_refuses(monkeypatch):
    monkeypatch.delenv("SEMANTIC_MODEL_DSN", raising=False)
    s, err = server._scope("7", "")
    assert s is None and "SEMANTIC_MODEL_DSN is not set" in err


# ---- connection --------------------------------------------------------------------------
def test_missing_host_is_reported_not_raised(monkeypatch):
    monkeypatch.undo()   # use the real fetch_rows
    monkeypatch.setattr(server, "_pool", None)
    monkeypatch.setattr(server, "_catalog_cache", (0.0, []))
    monkeypatch.setenv("AKASHX_HOST", "")
    s, err = server._scope("", "")
    assert s is None and "not configured" in err


def test_fetch_rows_waits_for_a_free_connection(monkeypatch):
    """More concurrent queries than connections must queue, not fail with "pool exhausted"."""
    size = server.POOL_SIZE
    state = {"open": 0, "max": 0}
    lock = threading.Lock()

    class Conn:
        def cursor(self, dictionary=True):
            return self

        def execute(self, sql, params):
            time.sleep(0.02)

        def fetchall(self):
            return [{"ok": 1}]

        def close(self):
            with lock:
                state["open"] -= 1

    class Pool:
        def get_connection(self):
            with lock:
                if state["open"] >= size:
                    raise RuntimeError("Failed getting connection; pool exhausted")
                state["open"] += 1
                state["max"] = max(state["max"], state["open"])
            return Conn()

    monkeypatch.undo()
    monkeypatch.setattr(server, "_get_pool", lambda: Pool())
    errors = []

    def run():
        try:
            assert server.fetch_rows("SELECT 1") == [{"ok": 1}]
        except Exception as exc:   # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(size * 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and state["max"] <= size


# ---- cards ---------------------------------------------------------------------------------
def test_card_rank_and_line():
    docs = {"a.pdf": {"document": "annual_plan_2025.pdf", "title": "Annual Plan", "description": "", "pages": 3},
            "b.pdf": {"document": "notes.pdf", "title": "Meeting notes", "description": "", "pages": 1}}
    ranked = cards.rank(docs, query="plan 2025")
    assert ranked[0][1]["document"] == "annual_plan_2025.pdf"
    assert cards.rank(docs, query="") == []
    assert cards.card_line(docs["a.pdf"], "plans") == "annual_plan_2025.pdf [plans]: Annual Plan, 3 pages"


def test_saved_card_text_becomes_the_description(tmp_path):
    class C:
        folder = str(tmp_path)

    (tmp_path / "cards-v1.json").write_text(
        '{"x.pdf": {"_fp": "2-1", "kind": "contract", "party": "Acme", "title": "Supply contract"}}', encoding="utf-8")
    doc = store.Document(name="x.pdf", title="", pages=1, chunks=2)
    card = cards.load_cards(C(), [doc])["x.pdf"]
    assert card["title"] == "Supply contract" and "Acme" in card["description"]


def test_overlapping_chunk_text_is_removed():
    prev = "The quick brown fox jumps over the lazy dog near the river bank."
    assert store._drop_overlap(prev, "over the lazy dog near the river bank. Then it rested.") == " Then it rested."
