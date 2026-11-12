# Ontology

## Overview

Ontology is an AI-powered structured data extraction system. It takes unstructured text (or links/files) and extracts structured fields using LLMs, storing the results in a queryable table with typed columns.

## SQL Syntax

```sql
CREATE ONTOLOGY VIEW <view_name>
  (<column_name> <type> AS '<extraction_instruction>', ...)
  ON COLUMN|LINKS|RAGBUCKET <table>.<column>
  USING '<global_prompt>'
  PROPERTIES (
    "rate_limit" = "10",
    "time_interval" = "10",
    "llm_provider" = "grok",
    "llm_model" = "grok-4-fast"
    -- "has_acl" = "true"   -- optional; usually auto-derived (see below)
  );
```

### Source Types

- **COLUMN** - Base column contains raw text. LLM processes text directly.
- **LINKS** - Base column contains URIs/URLs. UDF fetches content from the URL (supports S3 with AWS credentials).
- **RAGBUCKET** - Automatically uses the `_raw_files` table from a ragbucket, extracting from `raw_text` with S3 HTTPS URL conversion.

### `has_acl` (auto-derived)

Controls per-row ACL columns and query-time row-level security. The PROPERTY is optional — the view derives it from the source:

- **ON RAGBUCKET** → reads `bucket.hasAcl` (SharePoint defaults to `true`; S3/Azure to `false`).
- **ON COLUMN / ON LINKS** → `true` only when the base table has all 5 `acl_*` columns + `last_modified_at` + `refreshed_at`.
- Explicit `has_acl = "true"` is rejected if the source can't provide ACL data.
- Explicit `has_acl = "false"` is respected (single-tenant opt-out).

#### Bucket × view `has_acl` combinations

The four ways `bucket.has_acl` and `view.has_acl` can pair:

| Bucket | View | What happens | Useful? |
|---|---|---|---|
| `true` | `true` | View carries 5 ACL cols + 2 timestamp cols. ACL refresh runs on changes. RLS filters rows at query time. | ✓ Standard SharePoint flow. |
| `true` | `false` | View has **no** ACL cols. Preseed skips `acl_*`. No RLS — anyone with `SELECT` on the view sees every row. | Allowed; legit use cases include admin-only views and aggregated/public summaries. Risky if set by mistake — the LLM extractions of restricted files become queryable by everyone. |
| `false` | `true` | Bucket's `_raw_files` has no ACL columns to read. Preseed would reference `r.acl_users` and fail. | ✗ **Rejected at CREATE time** with a clear error. |
| `false` | `false` | No ACL on either side. Standard non-SharePoint flow. | ✓ Standard S3/Azure flow. |

**Foot-gun warning for case 2** (`bucket=true`, `view=false`): the downgrade is silent — only an INFO log line records the mismatch (`Ontology view has_acl resolved: false (derived=true, property=false)`). If you genuinely want a non-RLS view over ACL'd data, set the PROPERTY explicitly so it's intentional, not a typo.

**Soft-delete cascade** runs independently of `has_acl`. It's gated on the bucket being SharePoint-sourced, so cases 1 and 2 both get chunks cleanup + OV `deleted_at` propagation. Case 4 gets neither (no soft-delete signal exists on S3/Azure).

## End-to-End Flow

```
User: CREATE ONTOLOGY VIEW ...
        |
        v
  +-----------------+
  |   SQL Parser    |  AstBuilder.visitCreateOntologyViewStatement()
  |  (StarRocks.g4) |  Parses into CreateOntologyViewStmt
  +-----------------+
        |
        v
  +-----------------+
  |  StmtExecutor   |  Routes to OntologyViewExecutor
  +-----------------+
        |
        v
  +-------------------------------+
  |   OntologyViewExecutor        |
  |   (Main Orchestrator)         |
  |                               |
  |  1. Validate UDFs exist       |
  |  2. Resolve DB/table/column   |
  |  3. Check AWS (if LINKS)      |
  |  4. Build schema JSON         |
  |  5. CREATE TABLE              |
  |  6. INSERT initial rows       |
  |  7. Create 3 scheduled tasks  |
  |  8. Register metadata         |
  +-------------------------------+
        |
        v
  +-------------------------------+
  |   3 Scheduled Tasks (loop)    |
  |                               |
  |  Submit  (every 10s)          |
  |  Status  (every 15s)          |
  |  Cleanup (every 60s)          |
  +-------------------------------+
        |
        v
  +-------------------------------+
  |   Python UDFs → Redis         |
  |   → Worker → FastAPI          |
  |   → LLM → Result back        |
  +-------------------------------+
```

## Execution Steps (OntologyViewExecutor)

### Step 1: UDF Validation

Validates that three required Python UDFs are registered:
- `ontology_redis_submit_udf` - Submits extraction tasks to Redis
- `ontology_redis_status_udf` - Queries task status from Redis
- `ontology_redis_dk_udf` - Deletes completed task records from Redis

### Step 2: Database & Table Resolution

- Resolves target DB/table name (falls back to current DB)
- Validates the ontology view table doesn't already exist
- Validates base table/column exists and is of string type
- Reserved column names: `id`, `status`, `result`, `request_id`, `message`, `error`, `input_tokens`, `output_tokens`, `retry`

### Step 3: AWS Connectivity Check (LINKS mode only)

For LINKS source type, verifies AWS credentials using STS GetCallerIdentity before proceeding.

### Step 4: Schema Building

Maps ontology columns to UDF type schema and builds a JSON schema string:
```json
{"col1": "text", "col2": "int", "col3": "bool"}
```

Supported types: `text`, `int`, `float`, `bool`, `date`, `datetime`, `json`

### Step 5: CREATE TABLE

**Always present:**

| Column | Type | Purpose |
|--------|------|---------|
| `id` | BIGINT (AUTO_INCREMENT) | Primary key |
| `<base_column>` | VARCHAR(1MB) | Source text |
| `<user_columns>` | Various | User-defined extraction columns |
| `request_id` | VARCHAR | UUID for Redis tracking |
| `_base_pk` | base table PK type | FK to base table; used by Tier 3 refresh |
| `status`, `retry`, `result`, `message`, `error`, `input_tokens`, `output_tokens` | mixed | Task lifecycle + LLM output |

**Only for RAGBUCKET mode:** `file_url`, `https_url`.

**Only when `has_acl=true`:**

| Column | Type | Purpose |
|--------|------|---------|
| `acl_users`, `acl_groups`, `acl_sp_groups` | ARRAY | Allowed user / group OIDs |
| `acl_tenant_everyone`, `acl_anonymous` | BOOLEAN | Tenant-wide / public flags |
| `source_last_modified_at`, `source_refreshed_at` | DATETIME | Snapshot of base row's timestamps; Tier 3 uses these to tell content edits from ACL-only edits |

**Only when source is a SharePoint ragbucket** (`ON RAGBUCKET` over a SharePoint bucket, regardless of `has_acl`):

| Column | Type | Purpose |
|--------|------|---------|
| `deleted_at` | DATETIME | Cascaded tombstone — set by `OntologyRefreshDaemon` when the matching `_raw_files` row is soft-deleted. NULL = alive. See [SOFT_DELETE.md](SOFT_DELETE.md). |

### Step 6: Initial INSERT

Populates the view from the base table:
- Copies the base column value
- Sets `request_id = uuid()`, `status = 'READY'`
- `ON RAGBUCKET`: converts `s3://` URL to HTTPS
- `has_acl=true`: also copies the 5 `acl_*` cols + `last_modified_at` + `refreshed_at` (read uniformly from the base table across all source modes)

### Step 7: Create 3 Scheduled Tasks

#### Task 1: Submit (every `time_interval` seconds, default 10s)
- Task name: `ont_submit_<db>_<table>`
- Selects rows where `status = 'READY'` (limited by `rate_limit`)
- Calls `ontology_redis_submit_udf()` with: request_id, input text, prompt, schema JSON, flags, LLM config, AWS credentials
- Updates rows with response: message, error, tokens, status, result

#### Task 2: Status (every 15s)
- Task name: `ont_status_<db>_<table>`
- Selects rows with non-terminal status (not in: done, READY, deleted, error, not_found)
- Calls `ontology_redis_status_udf()` to poll Redis
- Extracts typed values from the JSON result into user-defined columns using `get_json_string()`, `get_json_int()`, `get_json_double()`, `get_json_bool()`

#### Task 3: Delete Key (every 60s)
- Task name: `ont_dk_<db>_<table>`
- Selects rows where `status = 'done'`
- Calls `ontology_redis_dk_udf()` to remove completed task hashes from Redis
- Resets retry flag to 0

### Step 8: Metadata Registration

Registers an `OntologyViewMeta` object in `OntologyViewMgr` (in-memory registry) and logs to EditLog for journal/image persistence.

## Redis Architecture

### Key Structures

```
ONTOLOGY:<request_id>  (Redis Hash)
  ├── request_id
  ├── status
  ├── result          (JSON string - extracted fields)
  ├── error
  ├── text            (input)
  ├── prompt
  ├── keys            (schema definition)
  ├── is_links
  ├── llm_provider
  ├── llm_model
  ├── output_tokens
  ├── input_tokens
  ├── message
  ├── aws_*           (credentials, if LINKS mode)
  └── timestamps

ONTOLOGY:STREAM  (Redis Stream)
  └── Consumer group: "ontology"
```

- TTL: 24 hours (default, configurable)
- Atomic operations via Lua script (hash creation + TTL + stream enqueue)

## Worker Processing

1. **Worker** polls `ONTOLOGY:STREAM` via Redis consumer group
2. **ACKs** the message immediately
3. **Loads** the full hash from `ONTOLOGY:<request_id>`
4. **Forwards** via HTTP to the FastAPI app at `/ontology_submit_task`
5. **App** processes: resolves links/media if needed, calls LLM, extracts fields per schema
6. **Updates** the Redis hash with result, status, tokens, timestamps

## UDF Integration (via UDF_STRUCTURED)

The three UDFs are defined in `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/ontology.sql` as inline Python functions:

- **Submit UDF**: Creates Redis hash + enqueues to stream (uses Lua script for atomicity). Supports retry flag - if retry=0 and key exists, returns duplicate; if retry=1, overwrites.
- **Status UDF**: Reads Redis hash fields, sanitizes JSON, returns full status object.
- **Delete Key UDF**: Sets TTL to 0 for immediate deletion.

## Querying Ontology Views

```sql
-- List all ontology views
SHOW ONTOLOGY VIEWS;

-- Query extracted data
SELECT * FROM <ontology_view_table> WHERE status = 'done';
```

## Key Files

| Component | Path |
|-----------|------|
| Grammar | `fe/fe-core/src/main/java/com/starrocks/sql/parser/StarRocks.g4` |
| AST Statement | `fe/fe-core/src/main/java/com/starrocks/sql/ast/CreateOntologyViewStmt.java` |
| Column Definition | `fe/fe-core/src/main/java/com/starrocks/sql/ast/OntologyColumnDef.java` |
| Executor | `fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java` |
| Metadata | `fe/fe-core/src/main/java/com/starrocks/catalog/OntologyViewMeta.java` |
| Registry | `fe/fe-core/src/main/java/com/starrocks/server/OntologyViewMgr.java` |
| UDF SQL | `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/ontology.sql` |
| FastAPI Route | `be/UDF_STRUCTURED/routes/ontology.py` |
| Show Command | `fe/fe-core/src/main/java/com/starrocks/sql/ast/ShowOntologyViewsStmt.java` |
| System Table | `fe/fe-core/src/main/java/com/starrocks/catalog/system/information/OntologyViewsSystemTable.java` |
