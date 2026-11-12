"""AkashX product knowledge base as an MCP server for LibreChat agents.

Serves the same knowledge Cognate's agent uses (cognate_backend_service/cognate/assistant):
the bundled product docs, their prebuilt hybrid (embedding + keyword) index, and the AkashX
function manifests. Tools:

  search_akashx_docs(query)       top 5 passages with citations (Cognate's search_akashx_docs)
  describe_akashx_function(name)  usage, inputs and examples of an AkashX function (Cognate's describe_function)
  list_akashx_topics()            the docs and their sections, to know what can be looked up

Query embeddings use OpenAI text-embedding-3-small (OPENAI_API_KEY), matching the prebuilt
index. Without a working key it falls back to the offline hash embedder and its own index.
Each tool call is appended to MCP_AUDIT_LOG (when set) with the LibreChat run's message id.

  python server.py --host 0.0.0.0 --port 8000
"""
import argparse
import asyncio
import json
import os
import time
from collections import OrderedDict
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from knowledge.embeddings import LocalHashEmbedder, OpenAIEmbedder
from knowledge.index import KnowledgeIndex

DATA = Path(os.getenv("AKASHX_KB_DATA", Path(__file__).resolve().parent / "data"))
DOCS = DATA / "knowledge_docs"
INDEX_BASE = str(DATA / "index" / "knowledge_index")
FUNCTIONS = DATA / "functions"
LOG = os.getenv("MCP_AUDIT_LOG")
MAX_PASSAGE_CHARS = 1500   # Cognate's _clip limit per passage

mcp = FastMCP("akashx-knowledge")


def _build_index() -> KnowledgeIndex:
    key = os.getenv("OPENAI_API_KEY", "")
    if key and key != "user_provided":
        try:
            embedder = OpenAIEmbedder(os.getenv("AKASHX_KB_EMBED_MODEL", "text-embedding-3-small"), key)
            embedder.embed(["probe"])
            index = KnowledgeIndex(embedder, str(DOCS), INDEX_BASE)
            index.ensure_ready()
            print(f"[akashx-knowledge] OpenAI index ready, {index.chunk_count} chunks", flush=True)
            return index
        except Exception as exc:
            print(f"[akashx-knowledge] OpenAI embedder unavailable ({exc}); using the offline index", flush=True)
    index = KnowledgeIndex(LocalHashEmbedder(), str(DOCS), INDEX_BASE)
    index.ensure_ready()
    print(f"[akashx-knowledge] offline index ready, {index.chunk_count} chunks", flush=True)
    return index


INDEX = _build_index()
FUNCTION_SPECS = {json.loads(p.read_text(encoding="utf-8"))["name"].lower(): json.loads(p.read_text(encoding="utf-8"))
                  for p in sorted(FUNCTIONS.glob("*.json"))}


def _audit(tool: str, arguments: dict, started: float, result: str) -> None:
    if not LOG:
        return
    headers = get_http_headers()
    rec = {"ts": started, "secs": round(time.time() - started, 3),
           "message_id": headers.get("x-librechat-message-id"), "tool": tool,
           "arguments": arguments, "result": result[:50000]}
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _render_function(spec: dict) -> str:
    """Same layout as Cognate's CustomFunction.to_tool_description."""
    lines = [spec.get("description") or spec["name"]]
    if spec.get("use_cases"):
        lines += ["", "When to use:"] + [f"  - {u}" for u in spec["use_cases"]]
    if spec.get("parameters"):
        lines += ["", "Inputs:"] + [
            f"  - {p['name']} ({p.get('type', 'string')}{'' if p.get('required', True) else ', optional'}): "
            f"{p.get('description', '')}" for p in spec["parameters"]]
    if spec.get("returns"):
        lines.append(f"Returns: {spec['returns']}")
    if spec.get("examples"):
        lines.append("Examples: " + "; ".join(spec["examples"]))
    if spec.get("cost"):
        lines.append(f"Cost: {spec['cost']}")
    if (spec.get("grounding") or {}).get("query_hint"):
        lines.append(f"Hint: {spec['grounding']['query_hint']}")
    if spec.get("requires_approval"):
        lines.append("NOTE: side-effecting — requires user approval before running.")
    return "\n".join(lines)


@mcp.tool(description="Search the AkashX product documentation (RagBuckets, Ontology Views, Cognitive SQL, "
                      "ASK, platform features). Use for any question about how AkashX works or its syntax, "
                      "and before writing AkashX-specific SQL you are not sure of.")
async def search_akashx_docs(query: str) -> str:
    started = time.time()
    query = (query or "").strip()
    if not query:
        out = "ERROR: give a search query."
    else:
        hits = await asyncio.to_thread(INDEX.search, query, 5)
        out = ("\n\n".join(f"[{i}] {h.citation}\n{h.text[:MAX_PASSAGE_CHARS]}" for i, h in enumerate(hits, 1))
               if hits else "No matching documentation found.")
    _audit("search_akashx_docs", {"query": query}, started, out)
    return out


@mcp.tool(description="Exact usage, inputs and examples of an AkashX function or product object "
                      "(for example ask_function, RagBucket). Call with an empty name to list them.")
async def describe_akashx_function(name: str = "") -> str:
    started = time.time()
    spec = FUNCTION_SPECS.get((name or "").strip().lower())
    if spec:
        out = _render_function(spec)
    else:
        listing = "\n".join(f"- {s['name']}: {s.get('description', '').splitlines()[0][:160]}"
                            for s in FUNCTION_SPECS.values())
        out = (f"No function named '{name}'. " if name else "") + "Available:\n" + listing
    _audit("describe_akashx_function", {"name": name}, started, out)
    return out


@mcp.tool(description="List the AkashX documentation topics (documents and their sections), to see what "
                      "search_akashx_docs can answer.")
async def list_akashx_topics() -> str:
    started = time.time()
    topics: "OrderedDict[str, list[str]]" = OrderedDict()
    for path in sorted(DOCS.glob("*.md")):
        heads, in_code = [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code   # '#' lines inside code samples are comments, not headings
            elif not in_code and line.startswith(("# ", "## ")):
                heads.append(line.lstrip("#").strip())
        topics[path.stem] = heads
    out = "\n".join(f"{doc}: " + "; ".join(h[:60] for h in heads[:12]) for doc, heads in topics.items())
    _audit("list_akashx_topics", {}, started, out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if LOG:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
    mcp.run(transport="http", host=a.host, port=a.port, path="/mcp")
