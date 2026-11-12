"""Starts mcp-server-akashxdb with two additions for multi-user use and token benchmarks.

1. Concurrency: upstream tools are plain functions, which FastMCP 2.12 calls directly on the
   event loop, so one slow query (e.g. an ASK) blocks every other client. Here each sync tool
   runs in a worker thread instead; the DB client already uses a connection pool
   (STARROCKS_POOL_SIZE).
2. Audit log (only when MCP_AUDIT_LOG is set): each `tools/call` is appended as one JSON line
   with the LibreChat run's message id (X-LibreChat-Message-Id header, set in librechat.yaml),
   tool name, arguments, time to result and the raw result.
3. Semantic-model scope (only for requests carrying X-Semantic-Model-Id, i.e. the
   mcp-akashxdb-scoped entry in librechat.yaml): the model is loaded from Cognate's
   public.cognate_semantic_models (SEMANTIC_MODEL_DSN) and every query must stay inside it —
   Cognate's SemanticGuard, plus SELECT/WITH/ASK only. db_summary returns the semantic model
   instead of the whole database; tools that are not read-only queries are refused. A model that
   cannot be loaded refuses everything (fail-closed, as in Cognate).

  python audit_launcher.py --mode streamable-http --host 0.0.0.0 --port 8000
"""
import asyncio
import functools
import inspect
import json
import os
import time

import anyio
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.middleware import Middleware

from mcp_server_starrocks import server
import semantic_scope

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # the guard itself refuses SQL it cannot verify
    sqlglot = exp = None

LOG = os.getenv("MCP_AUDIT_LOG")
SCOPE_HEADER = "x-semantic-model-id"


def _refusal(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=f"Refused: {text}")])


def _scope_error(scope: "semantic_scope.Scope", tool: str, args: dict):
    """None when the call is inside the semantic model, else the refusal (or db_summary's answer)."""
    allowed = scope.guard.allowed_names()
    if tool == "db_summary":
        return ToolResult(content=[TextContent(type="text", text=scope.render())])
    if tool == "table_overview":
        msg = scope.guard.check(f"SELECT * FROM {args.get('table') or ''}")
        return _refusal(msg) if msg else None
    if tool != "read_query":
        return _refusal(f"{tool} is not available inside the semantic model. Use read_query.")
    query, db = str(args.get("query") or ""), args.get("db")
    if db and str(db).strip("`").lower() not in scope.databases:
        return _refusal(f"database '{db}' is not in the semantic model. Query only: {allowed}")
    if not scope.guard.is_ask(query):
        try:
            statements = [s for s in sqlglot.parse(query, read="starrocks") if s is not None] if sqlglot else []
        except Exception:
            statements = []   # the guard reports the parse error
        if len(statements) == 1 and not isinstance(statements[0], exp.Query):
            return _refusal("only SELECT, WITH and ASK queries are allowed.")
    msg = scope.guard.check(query)
    return _refusal(msg) if msg else None


def _wrap(name, fn):
    """Runs sync tools in a worker thread and applies the semantic-model scope when requested."""
    is_async = inspect.iscoroutinefunction(fn)
    # tools declared `-> str` have an output schema, so they must answer with text, not a ToolResult
    returns_str = inspect.signature(fn).return_annotation in (str, "str")

    def reply(result: ToolResult):
        return result.content[0].text if returns_str else result

    @functools.wraps(fn)
    async def run(*args, **kwargs):
        model_id = (get_http_headers() or {}).get(SCOPE_HEADER)
        if model_id:
            try:
                scope = await anyio.to_thread.run_sync(semantic_scope.load_scope, model_id, server.db_client)
            except Exception as exc:
                return reply(_refusal(f"the semantic model could not be loaded ({exc}), so nothing can be queried."))
            blocked = _scope_error(scope, name, kwargs)
            if blocked is not None:
                return reply(blocked)
        if is_async:
            return await fn(*args, **kwargs)
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return run


for _name, _tool in server.mcp._tool_manager._tools.items():
    _tool.fn = _wrap(_name, _tool.fn)


class AuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        parts, more = [], True
        while more:
            msg = await receive()
            parts.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(parts)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        try:
            req = json.loads(body)
        except ValueError:
            req = None
        if not (isinstance(req, dict) and req.get("method") == "tools/call"):
            return await self.app(scope, replay, send)

        out, done = [], []

        async def capture(msg):
            if msg["type"] == "http.response.body":
                chunk = msg.get("body", b"")
                out.append(chunk)
                if not done and b'"jsonrpc"' in chunk:   # the result frame (the SSE stream may stay open)
                    done.append(time.time())
            await send(msg)

        started = time.time()
        try:
            await self.app(scope, replay, capture)
        finally:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            params = req.get("params") or {}
            rec = {"ts": started, "secs": round((done[0] if done else time.time()) - started, 3),
                   "message_id": headers.get("x-librechat-message-id"),
                   "semantic_model_id": headers.get(SCOPE_HEADER),
                   "tool": params.get("name"), "arguments": params.get("arguments"),
                   "result": b"".join(out).decode("utf-8", "replace")[:50000]}
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


if LOG:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _run_async = server.mcp.run_async

    async def run_async(*args, middleware=None, **kwargs):
        return await _run_async(*args, middleware=[*(middleware or []), Middleware(AuditMiddleware)], **kwargs)

    server.mcp.run_async = run_async

if __name__ == "__main__":
    asyncio.run(server.main())
