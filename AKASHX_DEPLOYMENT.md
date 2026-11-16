# Deploying LibreChat as the AkashX database assistant

This deploys LibreChat with the AkashX tools against an AkashX database server that already runs
its own AkashX MCP server. Nothing is installed or changed on the AkashX server.

## What runs

| Container | Purpose | Talks to |
|---|---|---|
| `LibreChat` | Chat app and agents (port 3080) | MongoDB, the MCP servers below, OpenAI |
| `akashx-documents` | Document tools: find, search and read documents in the RagBuckets | AkashX database (MySQL protocol), OpenAI (embeddings) |
| `akashx-knowledge` | AkashX product documentation tools | OpenAI (embeddings) |
| `fileagent`, `chat-mongodb`, `chat-meilisearch`, `vectordb`, `rag_api`, `admin-panel` | Standard LibreChat services | |
| Remote AkashX MCP server (not in this repo) | Database tools: `read_query`, `db_summary`, `table_overview`, … | AkashX database |

The local `akashxdb-mcp` container in `docker-compose.override.yml` is only for development; it
is neither built nor started unless `COMPOSE_PROFILES=local-db-mcp` is set.

## 1. Network access needed from the LibreChat host

| To | Port | Used by |
|---|---|---|
| The AkashX MCP URL (e.g. `https://<mcp-host>/mcp`) | 443 or its port | LibreChat (database tools) |
| The AkashX database | its MySQL port (9030 or 3333) | `akashx-documents` |
| `api.openai.com` | 443 | LibreChat, `akashx-documents`, `akashx-knowledge` |
| `registry.npmjs.org` | 443 | the Firecrawl tool (`npx firecrawl-mcp` at startup) |

Check from the LibreChat host before starting:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<mcp-host>/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
# expect 200
nc -zv <akashx-db-host> <akashx-db-port>      # expect "succeeded"
```

## 2. Configuration

### `.env` (create from `.env.example`; never commit it)

```bash
cp .env.example .env
```

LibreChat security keys. Generate new values. Without them LibreChat runs on temporary generated
keys and logs a warning; if those are lost, every user is logged out and stored keys can no
longer be read:

```bash
JWT_SECRET=<openssl rand -hex 32>
JWT_REFRESH_SECRET=<openssl rand -hex 32>
CREDS_KEY=<openssl rand -hex 32>          # 64 hex characters
CREDS_IV=<openssl rand -hex 16>           # 32 hex characters
MEILI_MASTER_KEY=<openssl rand -hex 32>
DOMAIN_CLIENT=https://<public librechat url>
DOMAIN_SERVER=https://<public librechat url>
```

OpenAI key for the chat model. Set it here so users are not asked for their own key (or leave
`user_provided` to make each user enter one):

```bash
OPENAI_API_KEY=<openai key>
```

AkashX section:

```bash
# remote AkashX MCP server: no local MCP container (its URL goes in librechat.yaml, below)
COMPOSE_PROFILES=

# the AkashX database, for the document tools (a read-only user is recommended)
AKASHX_MCP_HOST=<akashx-db-host>
AKASHX_MCP_PORT=<akashx-db-port>
AKASHX_MCP_USER=<user>
AKASHX_MCP_PASSWORD=<password>
AKASHX_MCP_DB=
# databases to search for RagBuckets (comma-separated); recommended when there are many or very
# large buckets, empty = all databases
AKASHX_DOCS_DATABASES=

# leave empty: the assistant uses the whole database
AKASHX_SEMANTIC_MODEL_ID=
AKASHX_SEMANTIC_MODEL_DSN=
```

Other tools, if used: `TAVILY_API_KEY` (web search), `FIRECRAWL_API_KEY` (Firecrawl), `OCR_API_KEY`.

### `librechat.yaml`

Two edits. Set the URL of `mcp-akashxdb` to the remote MCP server, and add its host (host name
or IP only, no `https://` or port) to `mcpSettings.allowedDomains`; LibreChat refuses MCP
connections to hosts not listed there:

```yaml
mcpSettings:
  allowedDomains:
  - <mcp-host>
  - fileagent
  - akashxdb-mcp
  - akashx-knowledge
  - akashx-documents

mcpServers:
  mcp-akashxdb:
    type: streamable-http
    url: https://<mcp-host>/mcp
```

Write the URL itself, not a `${VARIABLE}`: LibreChat checks the allowed domains before it
resolves variables, so a variable there is rejected (`Domain "unknown" is not allowed`).

### OpenAI key for the AkashX tool servers (not in git)

```bash
printf 'OPENAI_API_KEY=<openai key>\n' > mcp-servers/akashx-documents/.env
printf 'OPENAI_API_KEY=<openai key>\n' > mcp-servers/akashx-knowledge/.env
```

Without it, document and product-doc search fall back to keyword search.

## 3. Build and start

```bash
docker compose up -d --build
docker ps --format "table {{.Names}}\t{{.Status}}"
```

`akashx-documents`, `akashx-knowledge` and `fileagent` should become `healthy`; `akashxdb-mcp`
must not be listed.

If the build stops with "additional privileges requested: pass --allow=fs.read=Dockerfile", build
the images directly and start without building:

```bash
docker build -t librechat-dev:cap06 -f Dockerfile .
docker build -t librechat-fileagent mcp-servers/fileagent
docker build -t librechat-akashx-documents mcp-servers/akashx-documents
docker build -t librechat-akashx-knowledge mcp-servers/akashx-knowledge
docker compose up -d --no-build
```

## 4. Check

Tested setup: LibreChat with the Tesla staging AkashX MCP
(`https://mcp-server.tesla-stg.azure.akashx.ai/mcp`) and its database for the document tools,
with no local MCP container. All three tool groups answered correctly.

```bash
docker logs LibreChat 2>&1 | grep -E "Server listening|mcp-akashx"
# expect "Server listening ... 3080" and a block per AkashX MCP server with
# "Server Instructions: configured" for mcp-akashxdb and mcp-akashx-documents

docker logs akashx-documents 2>&1 | grep -E "document lists loaded|warm-up skipped"
# expect "document lists loaded" (can take a minute with large buckets);
# "warm-up skipped: ..." names a database connection problem
```

The MCP connections open when a user first uses the tools, so "Tools: undefined" at startup is
normal.

## 5. Create the assistant agent (once)

Agents are stored in MongoDB, not in git.

1. Log in as an admin user and open **Agents → New agent**.
2. Name `AkashX Database Assistant`, provider **OpenAI**, the model to use.
3. Instructions: the text after the `---` line in `mcp-servers/AKASHX_ASSISTANT_PROMPT.md`.
4. Tools:
   - `mcp-akashxdb`: `read_query`, `db_summary`, `table_overview` (not `write_query`)
   - `mcp-akashx-documents`: `find_documents`, `search_documents`, `read_document`, `document_outline`
   - `mcp-akashx-knowledge`: `search_akashx_docs`, `describe_akashx_function`, `list_akashx_topics`
5. Share the agent with all users. This needs agent sharing enabled in `librechat.yaml`
   (`interface.agents.share: true`, and `public: true` to share with everyone); it is off in the
   repo.
6. Test in a new chat: "What data do we have in AkashX?" and "Which document collections are
   there?".

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `[MCP][mcp-akashxdb] Failed to initialize: Domain "…" is not allowed` | The MCP host is not in `mcpSettings.allowedDomains`, or the URL uses a `${VARIABLE}`; fix `librechat.yaml` and `docker restart LibreChat` |
| `[MCP][mcp-akashxdb] Failed to connect` | The MCP URL is not reachable from the LibreChat host (step 1) |
| Document tools answer "connection is not configured" | `AKASHX_MCP_HOST` is empty in `.env` |
| Document tools answer with a connection error | The AkashX database port is not reachable, or the user or password is wrong |
| The agent sees fewer databases than expected | The remote MCP server's database user only has access to those databases |
| Users are asked for an OpenAI key | `OPENAI_API_KEY=user_provided` in `.env` |
| A rebuild fails with "Directory '.' is not installable" | `COMPOSE_PROFILES=local-db-mcp` is set; clear it for a remote MCP server |

After any `.env` change: `docker compose up -d --force-recreate`.
