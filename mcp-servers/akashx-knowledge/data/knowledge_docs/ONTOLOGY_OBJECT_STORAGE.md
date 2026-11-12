# Ontology View (Object Storage Sources)

Scope: this doc covers Ontology Views whose source path leads back to **object storage** — i.e. `ON COLUMN` / `ON LINKS` over a regular table without ACL columns, or `ON RAGBUCKET` over an S3/MinIO/Azure-backed RagBucket. SharePoint-specific behavior (ACL columns, soft-delete cascade, Tier-3 refresh) is **excluded** here; that lives in [ONTOLOGY.md](ONTOLOGY.md) and the SharePoint docs.

## What is an Ontology View?

An Ontology View is a managed LLM-extraction pipeline. From a single `CREATE ONTOLOGY VIEW` statement, the engine:

1. Creates **one table** in your database with: a copy of the source text, the columns you declared, bookkeeping columns for task state, and (for `ON RAGBUCKET`) two URL columns.
2. Seeds that table with one row per source row, marked `status='READY'`.
3. Schedules **three recurring tasks** that submit extraction requests to Redis, poll status, and clean up.

The external worker pulls each request from Redis, runs the LLM with your global prompt + per-column instructions, returns a JSON object, and the status task fans that JSON out into the typed columns you declared.

## SQL Syntax

```sql
CREATE ONTOLOGY VIEW <view_name>
  (<column_name> <type> AS '<extraction_instruction>', ...)
  ON COLUMN|LINKS|RAGBUCKET <db>.<table>.<column>
  USING '<global_prompt>'
  PROPERTIES (
    "rate_limit"        = "10",
    "time_interval"     = "10",
    "llm_provider"      = "grok",
    "llm_model"         = "grok-4-fast"
    -- optional: "batch_mode" = "true", "batch_limit" = "10"
    -- optional (LINKS only): "aws.access.key" / "aws.secret.key" / "storage_volume"
  );
```

### Source modes

| Mode | Source column contains | What the worker does |
|---|---|---|
| `ON COLUMN` | Raw text | Sends the text straight to the LLM |
| `ON LINKS` | URL or URI | Fetches the URL (S3 supported via AWS creds), then LLM |
| `ON RAGBUCKET` | (auto: uses the ragbucket's `_raw_files.raw_text`) | LLM on `raw_text`; also converts `s3://` → HTTPS |

For object-storage sources, `has_acl` resolves to `false` automatically — for `ON RAGBUCKET` because the underlying S3/Azure bucket has `hasAcl=false`, and for `ON COLUMN`/`ON LINKS` because regular tables don't carry the 5 `acl_*` columns + 2 timestamp columns the executor looks for. Explicit `has_acl=true` is rejected at CREATE time if the source can't supply ACL data.

## The Table That Gets Created

Exactly **one** OLAP table is created, primary-keyed on `id`, `replication_num=1`. Column order below is the exact order produced by the executor ([OntologyViewExecutor.java:340-438](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L340-L438)).

### For `ON COLUMN` / `ON LINKS` (no ACL, no SharePoint, no batch mode)

| # | Column | Type | Notes |
|---|---|---|---|
| 1 | `id` | BIGINT NOT NULL AUTO_INCREMENT | Primary key |
| 2 | `<base_col>` | VARCHAR(1048576) | Copy of the source text (or URL for `ON LINKS`) |
| 3..N | `<user_columns>` | as declared | Your extraction columns; populated by the status task after the LLM returns |
| – | `request_id` | VARCHAR(1048576) | UUID for the Redis task |
| – | `_base_pk` | matches base table's PK type | FK back to the source row (used by refresh logic) |
| – | `message` | VARCHAR(1048576) | Worker / LLM status message |
| – | `error` | VARCHAR(1048576) | Error text if extraction failed |
| – | `input_tokens` | VARCHAR(1048576) | Token count reported by the LLM |
| – | `output_tokens` | VARCHAR(1048576) | Token count reported by the LLM |
| – | `status` | VARCHAR(1048576) | Lifecycle: `READY` → in-flight → `done` / `error` / `not_found` → `deleted` |
| – | `retry` | TINYINT (default 0) | Manual retry flag |
| – | `result` | VARCHAR(1048576) | Full LLM response JSON; column comment is your global prompt |

### For `ON RAGBUCKET` (over an S3/Azure ragbucket)

Same as above, with **two extra columns inserted right after `<base_col>`**:

| Column | Type | Notes |
|---|---|---|
| `file_url` | VARCHAR(1048576) | `s3://...` or `wasbs://...` URL copied from the ragbucket's `_raw_files.file_url` |
| `https_url` | VARCHAR(1048576) | HTTPS form of `file_url`, generated at INSERT time using `aws.s3.region` (default `us-east-1`) |

> Heads up: `https_url` is computed once with a hardcoded region template ([OntologyViewExecutor.java:494-521](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L494-L521)). If your S3 bucket isn't in the region you passed via `aws.s3.region`, the generated HTTPS URL will be wrong. The `file_url` column is always correct.

### Conditional columns (mentioned only for completeness)

These do **not** appear in object-storage-sourced views, but knowing they exist explains some of the table's column order:

- `acl_users`, `acl_groups`, `acl_sp_groups`, `acl_tenant_everyone`, `acl_anonymous` — only when `has_acl=true` (SharePoint).
- `source_last_modified_at`, `source_refreshed_at` — only when `has_acl=true` (SharePoint).
- `batch_id` — only when `batch_mode=true` (independent of source).
- `deleted_at` — only when the source ragbucket is SharePoint-backed (`sharepoint://`). **Not present** for S3/Azure ragbuckets, even if the bucket later changes — because S3/Azure has no soft-delete signal to cascade.

### Reserved column names

Your declared extraction columns and your base column cannot use any of these names — they're built into the schema:

`id`, `status`, `result`, `request_id`, `message`, `error`, `input_tokens`, `output_tokens`, `retry`

The validation pass explicitly rejects the first four ([OntologyViewExecutor.java:201-206, 312-321](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L201-L206)); the rest are reserved implicitly because the executor hardcodes them into the DDL. `file_url`, `https_url`, and `batch_id` are also effectively reserved when those features apply.

### Supported user column types

The executor maps your declared types to the worker's schema like this ([OntologyViewExecutor.java:938-965](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L938-L965)):

| Declared type (any case) | Sent to LLM as |
|---|---|
| `DATE` | `date` |
| `DATETIME`, `TIMESTAMP` | `datetime` |
| `VARCHAR`, `CHAR`, `STRING`, `TEXT` | `text` |
| `JSON` | `json` |
| `BOOLEAN` | `bool` |
| `DECIMAL`, `DOUBLE`, `FLOAT` | `float` |
| any `INT` variant | `int` |
| anything else | `text` (fallback) |

## Initial INSERT

At CREATE time, one row is inserted per source row.

**For `ON RAGBUCKET`** ([OntologyViewExecutor.java:494-521](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L494-L521)):

```sql
INSERT INTO <view> (_base_pk, <base_col>, file_url, https_url, request_id, status)
SELECT <pk>, <base_col>, file_url,
       REPLACE(CONCAT('https://', SPLIT_PART(file_url,'/',3),
                      '.s3.<aws.s3.region>.amazonaws.com/', ...), ' ', '+') AS https_url,
       uuid(), 'READY'
FROM <base_db>.<base_table>;
```

**For `ON COLUMN` / `ON LINKS`** ([OntologyViewExecutor.java:523-536](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L523-L536)):

```sql
INSERT INTO <view> (_base_pk, <base_col>, request_id, status)
SELECT <pk>, <base_col>, uuid(), 'READY'
FROM <base_db>.<base_table>;
```

User-defined columns remain NULL until the status task fills them in. New rows added to the base table later are picked up by the same scheduled-tasks loop.

## Scheduled Tasks

Three tasks ([OntologyViewExecutor.java:623, 723, 754](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyViewExecutor.java#L623)):

| Task name | Interval | What it does |
|---|---|---|
| `ont_submit_<db>_<table>` | `time_interval` property | Picks `status='READY'` rows (up to `rate_limit`), calls `ontology_redis_submit_udf()` with prompt + schema + LLM config + AWS creds, updates `status` / `message` / `error` |
| `ont_status_<db>_<table>` | 15 SECOND | Polls Redis for non-terminal rows via `ontology_redis_status_udf()`, extracts typed fields from the result JSON into your user columns using `get_json_string()` / `get_json_int()` / `get_json_double()` / `get_json_bool()` |
| `ont_dk_<db>_<table>` | 60 SECOND | For `status='done'` rows, calls `ontology_redis_dk_udf()` to drop the Redis hash and resets `retry` to 0 |

Both `<db>` and `<table>` in task names are sanitized — non-alphanumeric characters become underscores.

> Note: the submit interval has **no default in code** — `time_interval` (and `rate_limit`) come straight from your PROPERTIES with no fallback. Omitting them will produce an invalid `SCHEDULE EVERY` clause at CREATE time. [Likely] required in practice.

## Properties Reference

| Property | Default | Notes |
|---|---|---|
| `rate_limit` | (required) | Max rows submitted per submit-task tick |
| `time_interval` | (required) | Submit-task interval, e.g. `"10"` for 10 seconds |
| `llm_provider` | `grok` | |
| `llm_model` | `grok-4-fast` | |
| `batch_mode` | `false` | If `true`, adds a `batch_id` column and switches the worker to batched extraction |
| `batch_limit` | (worker default 10) | Only meaningful when `batch_mode=true` |
| `has_acl` | auto-derived | Resolves to `false` for object-storage sources. Explicit `true` is rejected if the source can't supply ACL data. |
| `aws.s3.region` | `us-east-1` | Used for the `https_url` computation in `ON RAGBUCKET` mode |
| `aws.access.key` / `aws.secret.key` / `aws.session.token` | (none) | Required for `ON LINKS` against S3 unless `storage_volume` is set |
| `aws.s3.endpoint` | (none) | Custom S3/MinIO endpoint for `ON LINKS` |
| `storage_volume` | (none) | Alternative to inline AWS credentials for `ON LINKS` |
