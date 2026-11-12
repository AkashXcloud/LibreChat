# Architecture - RagBucket, Ontology, Cognitive SQL

## Production Architecture

```
                          +-------------------------+
                          |      FE (Leader)        |
                          |                         |
                          |  Parses SQL, creates    |
                          |  execution plan,        |
                          |  distributes fragments  |
                          |  to BE nodes            |
                          +-------------------------+
                            /          |          \
                           /           |           \
               (fragments) /           |            \ (fragments)
                         v             v              v
  +---------------------+  +---------------------+  +---------------------+
  |      BE Node 1      |  |      BE Node 2      |  |      BE Node 3      |
  |                     |  |                     |  |                     |
  |  +---------------+  |  |  +---------------+  |  |  +---------------+  |
  |  | BE Process    |  |  |  | BE Process    |  |  |  | BE Process    |  |
  |  | (C++ engine)  |  |  |  | (C++ engine)  |  |  |  | (C++ engine)  |  |
  |  +---------------+  |  |  +---------------+  |  |  +---------------+  |
  |                     |  |                     |  |                     |
  |  +---------------+  |  |  +---------------+  |  |  +---------------+  |
  |  | App (FastAPI) |  |  |  | App (FastAPI) |  |  |  | App (FastAPI) |  |
  |  | localhost:8002|  |  |  | localhost:8002|  |  |  | localhost:8002|  |
  |  | 30 workers    |  |  |  | 30 workers    |  |  |  | 30 workers    |  |
  |  +---------------+  |  |  +---------------+  |  |  +---------------+  |
  +---------------------+  +---------------------+  +---------------------+
           |                         |                         |
           |   Ontology / RagBucket  |                         |
           |   (async: submit to     |                         |
           |    Redis via UDF)       |                         |
           v                         v                         v
  +-------------------------------------------------------------------+
  |              Redis + Worker + Nginx Node (Single)                  |
  |                                                                   |
  |  +------------------+  +------------------+  +------------------+ |
  |  |      Nginx       |  |     Redis 7      |  |     Worker       | |
  |  |   Port 80/443    |  |    Port 6379     |  |  Async forwarder | |
  |  |                  |  |                  |  |                  | |
  |  |  Load balancer   |  |  Streams:        |  |  Polls streams:  | |
  |  |  (least_conn)    |  |  ONTOLOGY:STREAM |  |  - ONTOLOGY      | |
  |  |  Routes to BE    |  |  ragbucket_stream|  |  - ragbucket     | |
  |  |  app instances   |  |                  |  |                  | |
  |  |                  |  |  Hashes:          |  |  Forwards to     | |
  |  |  upstream pool:  |  |  ONTOLOGY:*      |  |  nginx → BE app  | |
  |  |   BE1:8002       |  |  RAGBUCKET:*     |  |  via HTTP        | |
  |  |   BE2:8002       |  |                  |  |                  | |
  |  |   BE3:8002       |  |  Telemetry:      |  |  500 concurrent  | |
  |  |                  |  |  COGNITIVE_LLM_*  |  |  requests max    | |
  |  +------------------+  +------------------+  +------------------+ |
  +-------------------------------------------------------------------+
```

## Request Flow by Feature

### Cognitive SQL Functions (Synchronous)

FE sends query fragments to BE nodes. Each BE node executes the Python UDF locally, which calls its **local app.py** (localhost:8002) directly. No Redis involvement.

```
FE (Leader)
  |
  |  distributes fragments
  v
BE Node
  |
  |  Python UDF executes on fragment
  v
Local App (localhost:8002)
  |
  |  POST /classify_batch, /rate_batch, etc.
  v
LLM Provider API (OpenAI, Grok, Anthropic, etc.)
  |
  v
Results returned inline to BE → FE → Client
```

### Ontology & RagBucket (Asynchronous)

FE distributes fragments to BE nodes. Each BE executes the Python UDF which submits tasks to Redis. The Worker picks up tasks from Redis streams and forwards them to Nginx, which distributes them round-robin across app.py instances running on all BE nodes. The app.py processes the task (calls LLM, parses documents, etc.) and updates the Redis hash with the result.

```
FE (Leader)
  |
  |  distributes fragments
  v
BE Node (Python UDF executes on fragment)
  |
  |  ontology_redis_submit_udf / submit_task_to_redis
  |  writes hash + enqueues to stream
  v
Redis (on Redis+Worker+Nginx node)
  |
  |  XADD to ONTOLOGY:STREAM / ragbucket_stream
  v
Worker (polls Redis streams)
  |
  |  reads stream entry, loads hash data
  v
Nginx (load balancer, round-robin / least_conn)
  |
  |  distributes across all BE node app.py instances
  v
app.py on BE Node (any node, :8002)
  |
  |  /ontology_submit_task or /extract_row_level_chunks_vectors
  |  calls LLM, processes media/documents
  |
  |  updates Redis hash with result, status, tokens
  v
Redis (hash updated with result)
  |
  v
BE Node (status UDF polls Redis for completion)
  |
  |  extracts result, writes to database table
  v
Results stored in ontology/ragbucket table
```

## UDF_STRUCTURED Service Architecture

The FastAPI app (`app.py`) runs on **every BE node** as part of the BE deployment. The Redis + Worker + Nginx infrastructure is deployed as a **single separate node**.

### Docker Compose Deployments

There are three docker compose files for different deployment scenarios:

---

### Development: `docker-compose.yml`

Single-machine deployment with all services together. Used when running everything on one machine.

```
+--------------------------------------------------+
|               Single Machine (BE)                 |
|                                                   |
|  +--------+    +--------+    +--------+           |
|  |  App   |    | Worker |    | Redis  |           |
|  | (Fast  |    | (Polls |    | (Queue |           |
|  |  API)  |    | Redis  |    |  +     |           |
|  |  4     |    | stream)|    | State) |           |
|  | workers|    |        |    |        |           |
|  | :8002  |    |        |    | :6379  |           |
|  +--------+    +--------+    +--------+           |
|       ^             |             ^               |
|       |             +-------------+               |
|       |        (reads stream,                     |
|       |         forwards to app)                  |
|       |                                           |
|  BE UDFs call localhost:8002 directly             |
+--------------------------------------------------+
```

- **App**: Gunicorn + 4 Uvicorn workers (reload enabled for development)
- **Worker**: Polls Redis streams, forwards tasks to app via HTTP
- **Redis**: Local Alpine image with persistent volume
- **Usage**: `docker compose up`

---

### Production BE Node: `docker-compose.app.yml`

Deployed on **each BE machine**. The FastAPI app runs alongside the BE process.

```
  +---------------------------+    +---------------------------+
  |      BE Node 1            |    |      BE Node 2            |
  |                           |    |                           |
  |  +---------------------+  |    |  +---------------------+  |
  |  | BE Process (C++)    |  |    |  | BE Process (C++)    |  |
  |  +---------------------+  |    |  +---------------------+  |
  |                            |    |                            |
  |  +---------------------+  |    |  +---------------------+  |
  |  |  Gunicorn + FastAPI  |  |    |  |  Gunicorn + FastAPI  |  |
  |  |  30 Uvicorn workers  |  |    |  |  30 Uvicorn workers  |  |
  |  |  Port 8002           |  |    |  |  Port 8002           |  |
  |  |                      |  |    |  |                      |  |
  |  |  REDIS_HOST = nginx  |  |    |  |  REDIS_HOST = nginx  |  |
  |  |  node IP             |  |    |  |  node IP             |  |
  |  +---------------------+  |    |  +---------------------+  |
  +---------------------------+    +---------------------------+
```

- **30 workers** per BE node (production-ready)
- BE process calls local app for cognitive SQL (localhost:8002)
- App connects to Redis on the nginx node for ontology/ragbucket tasks (via `REDIS_HOST` env var)
- Uses external `udf_net` Docker network
- **Usage**: `docker compose -f docker-compose.app.yml up -d --build`

---

### Production Redis + Worker + Nginx Node: `docker-compose.nginx.yml`

Single central infrastructure node. Handles async task queuing and load-balanced dispatch.

```
  +-------------------------------------------------------------------+
  |            Redis + Worker + Nginx Node (Single)                    |
  |                                                                   |
  |  +------------------+                                             |
  |  |      Nginx       |  Load balancer (least_conn)                 |
  |  |    Port 80/443   |  Routes to BE node apps                      |
  |  |                  |  600s timeout (for LLM calls)                |
  |  |  upstream pool:  |  keepalive 32 connections                    |
  |  |   BE1:8002       |  100MB max body                             |
  |  |   BE2:8002       |                                             |
  |  |   BE3:8002       |                                             |
  |  +------------------+                                             |
  |          |                                                        |
  |          v                                                        |
  |  +------------------+    +------------------+                     |
  |  |     Redis 7      |    |     Worker       |                     |
  |  |    Port 6379     |    |  Async forwarder |                     |
  |  |                  |    |                  |                     |
  |  |  Streams:        |    |  Polls streams:  |                     |
  |  |  ONTOLOGY:STREAM |<---|  - ONTOLOGY      |                     |
  |  |  ragbucket_stream|    |  - ragbucket     |                     |
  |  |                  |    |                  |                     |
  |  |  Hashes:         |    |  Forwards to     |                     |
  |  |  ONTOLOGY:*      |    |  nginx → BE app  |                     |
  |  |  RAGBUCKET:*     |    |  via HTTP        |                     |
  |  |                  |    |                  |                     |
  |  |  Telemetry:      |    |  500 concurrent  |                     |
  |  |  COGNITIVE_LLM_* |    |  requests max    |                     |
  |  +------------------+    +------------------+                     |
  +-------------------------------------------------------------------+
```

- **Nginx 1.27**: `least_conn` load balancing across BE node apps, 600s read timeout for long LLM calls, keepalive connection pooling
- **Redis 7**: Persistent volume, stores task hashes and streams
- **Worker**: Async forwarder that polls Redis streams and dispatches to BE node apps via nginx
- **Usage**: `docker compose -f docker-compose.nginx.yml up -d --build`

---

### Production Setup Steps

```bash
# 1. Create shared Docker network (on all machines)
docker network create udf_net

# 2. On the Redis+Worker+Nginx node:
#    Update nginx.conf upstream block with BE node IPs:
#      upstream gunicorn_pool {
#          least_conn;
#          server <be-node-1-ip>:8002 max_fails=3 fail_timeout=30s;
#          server <be-node-2-ip>:8002 max_fails=3 fail_timeout=30s;
#          server <be-node-3-ip>:8002 max_fails=3 fail_timeout=30s;
#          keepalive 32;
#      }
docker compose -f docker-compose.nginx.yml up -d --build

# 3. On each BE node:
#    Set REDIS_HOST=<nginx-node-ip> in .env
docker compose -f docker-compose.app.yml up -d --build
```

### Scaling

To add more BE nodes:
1. Deploy `docker-compose.app.yml` on the new BE machine
2. Add the BE machine's IP to `nginx.conf` upstream block
3. Reload nginx: `docker exec nginx nginx -s reload`

## LLM Provider Support

All features support multiple LLM providers:

- **OpenAI** - GPT-4o-mini, etc.
- **Grok** - X.AI models (grok-4-fast, etc.)
- **Anthropic** - Claude models
- **Google Gemini** - Gemini models
- **Cumulus** - IonRouter
- **Local LLM** - Ollama-compatible local models

## Telemetry

Every UDF call returns AI telemetry metadata:
- `input_tokens`, `output_tokens` - Token usage
- `model`, `provider` - Which LLM was used
- `latency`, `cost` - Performance metrics

Telemetry is streamed to Redis (`COGNITIVE_LLM_REQUESTS` stream) and can be synced to database tables via `/sync/per_call_logs` and `/sync/aggregate_logs` endpoints.

## Configuration

### BE Configuration (`be.conf`)

```properties
COGNITIVE_EMBED_URL=http://localhost:8002/dsrag/embed
```

### Key Environment Variables (`.env` in UDF_STRUCTURED)

```properties
OPENAI_API_KEY=...
GROK_API_KEY=...
ANTHROPIC_API_KEY=...
COHERE_API_KEY=...
REDIS_HOST=localhost
REDIS_PORT=6379
```

## Key Files

| Component | Path |
|-----------|------|
| Grammar | `fe/fe-core/src/main/java/com/starrocks/sql/parser/StarRocks.g4` |
| Cognitive Embed (C++) | `be/src/exprs/cognitive_functions.cpp` |
| FastAPI App | `be/UDF_STRUCTURED/app.py` |
| Worker | `be/UDF_STRUCTURED/worker.py` |
| Nginx Config | `be/UDF_STRUCTURED/nginx.conf` |
| Docker (Dev) | `be/UDF_STRUCTURED/docker-compose.yml` |
| Docker (Prod BE Node) | `be/UDF_STRUCTURED/docker-compose.app.yml` |
| Docker (Prod Redis+Worker+Nginx) | `be/UDF_STRUCTURED/docker-compose.nginx.yml` |
| Ontology Route | `be/UDF_STRUCTURED/routes/ontology.py` |
| RagBucket Route | `be/UDF_STRUCTURED/routes/ragbucket.py` |
| UDF SQL Scripts | `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/` |
| Config Files | `be/UDF_STRUCTURED/config/` |
