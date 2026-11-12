# RagBucket (Object Storage Sources)

Scope: this doc covers RagBuckets backed by **object storage** — AWS S3, MinIO / S3-compatible, and Azure Blob. SharePoint is handled separately in [RAGBUCKET_SHAREPOINT.md](RAGBUCKET_SHAREPOINT.md). Anything ACL-, drive-item-, or soft-delete-related is SharePoint-only and not present in the tables described here.

## What is a RagBucket?

A RagBucket is a managed ingestion + retrieval pipeline pointed at an object-storage path. From a single `CREATE RAGBUCKET` statement, the engine:

1. Stands up four tables under the current database — one for metadata, one to track each source file, one for chunk+vector storage, and one external Parquet table that bridges in worker output.
2. Schedules five recurring tasks that continuously scan the source path, dispatch new files to an external worker (dsRAG) via Redis, monitor status, load processed chunks, and clean up.
3. Persists the bucket definition through `EditLog` so it survives FE restarts.

Once chunks are loaded, the bucket is queryable two ways: directly via SQL (e.g. `cosine_similarity(vector, …)`) and through the natural-language `PROMPT` / `ASK` syntax described at the bottom of [RAGBUCKET.md](RAGBUCKET.md).

## SQL Syntax

```sql
CREATE RAGBUCKET <bucket_name> FROM '<source-url>'
PROPERTIES (
  -- source credentials (one of these two groups)
  -- ...inline (S3/MinIO):
  "aws.s3.access_key" = "...",
  "aws.s3.secret_key" = "...",
  "aws.s3.region"     = "us-east-1",
  -- ...or a storage volume reference:
  -- "storage_volume" = "my_source_volume",

  -- LLM + embedding
  "llm_provider"       = "grok",
  "llm_model"          = "grok-4-fast",
  "embedding_provider" = "openai",
  "embedding_model"    = "text-embedding-3-small",

  -- parquet sink (must be set together, or both omitted for legacy fallback)
  "dest_storage_volume" = "my_dest_volume",
  "dest_url"            = "s3://my-ragbucket-sink/"
);
```

Accepted source URL schemes (verified in [RagBucketExecutor.java:148-156](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L148-L156)):

| Scheme | Source |
|---|---|
| `s3://...` | AWS S3 / MinIO / any S3-compatible |
| `wasbs://...` or `azblob://...` | Azure Blob Storage |

`has_acl=true` is explicitly **rejected** for these sources — it's a SharePoint-only feature ([RagBucketExecutor.java:164-168](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L164-L168)).

## Tables Created

Four tables are created for every object-storage RagBucket. Each one is prefixed with the lowercased ragbucket name — e.g. a bucket named `my_docs` produces `my_docs_metadata`, `my_docs_raw_files`, `my_docs_chunks`, and `my_docs_intermediate_table`.

### 1. `<prefix>_metadata`

One row per RagBucket, capturing the configuration you provided at `CREATE` time. This is a lean record — destination URL, ACL settings, and task intervals are **not** stored here; they live on the in-memory catalog object only.

Schema ([RagBucketExecutor.java:342-357](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L342-L357)):

| Column | Type | Description |
|---|---|---|
| `Ragbucket_Name` | STRING | Logical bucket name from `CREATE RAGBUCKET <name>` |
| `Bucket_Name` | STRING | Containing database name |
| `s3_path` | STRING | Source URL exactly as provided in the `FROM` clause |
| `Ragbucket_s3_path` | STRING | Per-bucket sink prefix in the dsRAG-managed parquet area |
| `llm_provider` | STRING | LLM provider (e.g. `grok`, `openai`) |
| `llm_model` | STRING | LLM model name |
| `embedding_provider` | STRING | Embedding provider (e.g. `openai`) |
| `embedding_model` | STRING | Embedding model name |

Storage: OLAP, `DUPLICATE KEY(Ragbucket_Name)`, hash-distributed on `Ragbucket_Name` into 8 buckets, `replication_num=1`.

### 2. `<prefix>_raw_files`

One row per file discovered in the source path. Each row tracks the full lifecycle of a file — from "we just saw it in S3" through "the worker finished embedding it" to "Redis key cleaned up."

For object storage sources this table has exactly **15 columns** (all SharePoint extras — `drive_item_id`, the `acl_*` columns, `last_modified_at`, `refreshed_at`, `deleted_at` — are omitted by design).

Schema ([RagBucketExecutor.java:404-432](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L404-L432)):

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT AUTO_INCREMENT | Primary key |
| `file_name` | VARCHAR(2048) | File name (basename) |
| `file_type` | VARCHAR(2048) | File extension or type |
| `s3_name` | VARCHAR(2048) | Object key in the source bucket |
| `file_url` | VARCHAR(2048) | Full source URL (`s3://...` or `wasbs://...`) |
| `raw_text` | VARCHAR(1048576) | Extracted text returned by the worker |
| `modification_time` | DATETIME | File mod time from the source listing |
| `bucket_url` | VARCHAR(2048) | Per-ragbucket destination prefix in the parquet sink |
| `request_id` | VARCHAR(36) NOT NULL | UUID for the Redis task tied to this file |
| `status_message` | VARCHAR(1048576) | Status text from Redis / worker |
| `error_message` | VARCHAR(1048576) | Error text if the worker failed |
| `token_count` | VARCHAR(1024) | Token count reported by the worker |
| `status` | VARCHAR(64) | Redis lifecycle: `READY` → `PROCESSING` → `completed` / `error` → `deleted` |
| `retry` | TINYINT (default 0) | Manual-retry flag — bumped only by human action, never by the pipeline |
| `result` | JSON | Raw response payload from Redis / worker |

Storage: `PRIMARY KEY(id)`, hash-distributed on `id` into 4 buckets, `replication_num=1`.

> **Note on `retry`.** The pipeline tasks never touch this column and never revive `status='error'` rows on their own. Re-runs are a manual operation. Same applies to rows whose `status` is already in a terminal state.

### 3. `<prefix>_chunks`

The semantic-search table. The chunk-insert task loads rows here from the intermediate Parquet table after the worker finishes. Each row is one chunk of one document, with its embedding vector indexed for approximate nearest-neighbor search.

Schema ([RagBucketExecutor.java:803-832](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L803-L832)):

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT | Row id (not a primary key — `DISTRIBUTED BY HASH(id)`) |
| `file_name` | VARCHAR(255) | Source file name (joins to `_raw_files.file_name`) |
| `file_url` | VARCHAR(1024) | Source URL |
| `file_type` | VARCHAR(100) | File type |
| `modification_time` | DATETIME | Mod time from `_raw_files` |
| `document_title` | STRING | LLM-generated document title |
| `section_title` | STRING | LLM-generated section title |
| `chunk_index` | BIGINT | Chunk position within the document |
| `chunk_text` | STRING | The chunk text itself |
| `document_summary` | STRING | LLM-generated document-level summary |
| `section_summary` | STRING | LLM-generated section-level summary |
| `chunk_page_start` | BIGINT | First page covered by this chunk |
| `chunk_page_end` | BIGINT | Last page covered by this chunk |
| `vector` | ARRAY\<FLOAT\> NOT NULL | Embedding vector (768 floats) |

Vector index (declared inline on the table):

```
INDEX vector_idx (vector) USING VECTOR (
  "index_type"        = "hnsw",
  "dim"               = "768",
  "metric_type"       = "cosine_similarity",
  "is_vector_normed"  = "false",
  "M"                 = "32",
  "efconstruction"    = "64"
)
```

Storage: OLAP, hash-distributed on `id` into 1 bucket, `replication_num=1`.

### 4. `<prefix>_intermediate_table`

External `ENGINE=FILE` table pointed at the per-ragbucket parquet sink. The dsRAG worker writes one parquet file per processed document into this path; the chunk-insert task reads from here and writes into `_chunks`.

Schema is **declared, not inferred** (the sink directory is empty at `CREATE` time, so listing would fail) ([RagBucketExecutor.java:865-882](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L865-L882)):

| Column | Type | Description |
|---|---|---|
| `file_name` | STRING | Source file name |
| `document_title` | STRING | LLM-generated |
| `section_title` | STRING | LLM-generated |
| `chunk_index` | BIGINT | Position within document |
| `chunk_text` | STRING | Chunk content |
| `document_summary` | STRING | Document-level summary |
| `section_summary` | STRING | Section-level summary |
| `vector` | ARRAY\<DOUBLE\> | Embedding vector (cast to `ARRAY<FLOAT>` on insert into `_chunks`) |
| `chunk_page_start` | BIGINT | First page |
| `chunk_page_end` | BIGINT | Last page |

The `PROPERTIES` differ by destination provider:

- **S3 / MinIO sink:** `path`, `format=parquet`, `aws.s3.endpoint`, `aws.s3.region`, `aws.s3.access_key`, `aws.s3.secret_key`, `aws.s3.enable_ssl`, `aws.s3.enable_path_style_access`, `strict_mode=false`.
- **Azure Blob sink:** `path`, `format=parquet`, `azure.blob.storage_account`, `azure.blob.shared_key`, `azure.blob.sas_token`, `strict_mode=false`.

## Scheduled Tasks

Five recurring tasks are submitted alongside the tables. Task names (note the typo `medtadata`, which is the actual name in code) and default intervals:

| Task name | Default interval | What it does |
|---|---|---|
| `<prefix>_s3_medtadata_insert` | 15s | Lists the source path via `files()`, filters out files already in `_raw_files`, inserts new rows with `status='READY'` |
| `<prefix>_raw_files_submit_task` | 30s | Picks `status='READY'` rows (up to `rate_limit`), enqueues to Redis via `submit_task_to_redis()` |
| `<prefix>_monitor_chunks` | 10s | Polls Redis for non-terminal rows via `check_redis_task_status()`, updates `status` / `raw_text` / `error_message` / `token_count` |
| `<prefix>_chunk_insert` | 35s | Joins the intermediate parquet table with `_raw_files` (where `status='completed'`) and inserts into `_chunks` |
| `<prefix>_delete_key` | 60s | For `status='completed'` rows, calls `ragbucket_redis_dk_udf()` to remove the Redis hash and sets `status='deleted'` |

Verified task names: [RagBucketExecutor.java:759](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L759), [982](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L982), [1143](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L1143), [885](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L885), [1205](../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java#L1205).

## Properties Reference

| Property | Default | Notes |
|---|---|---|
| `aws.s3.access_key` / `aws.s3.secret_key` | (none) | Required for S3 sources unless `storage_volume` is set |
| `aws.s3.region` | `us-east-1` | |
| `aws.s3.endpoint` | (none) | Required for MinIO / S3-compatible |
| `aws.s3.enable_ssl` | `true` | |
| `aws.s3.enable_path_style` | `true` | |
| `azure.blob.storage_account` / `azure.blob.shared_key` / `azure.blob.sas_token` | (none) | Azure source credentials |
| `storage_volume` | (none) | Alternative to inline credentials — references a `CREATE STORAGE VOLUME` |
| `llm_provider` | `grok` | |
| `llm_model` | `grok-4-fast` | |
| `embedding_provider` | `openai` | |
| `embedding_model` | `text-embedding-3-small` | |
| `dest_storage_volume` | (none) | Parquet sink credentials. Must be set together with `dest_url` |
| `dest_url` | (none) | Parquet sink URL (`s3://...` or `wasbs://...`); scheme must match `dest_storage_volume` type |
| `rate_limit` | `100` | Max files submitted per `submit` tick |
| `metadata_interval` | `15 SECOND` | Source-scan task frequency |
| `submit_interval` | `30 SECOND` | Redis-submit task frequency |
| `monitor_interval` | `10 SECOND` | Status-poll task frequency |
| `chunk_interval` | `35 SECOND` | Chunk-insert task frequency |
| `delete_interval` | `60 SECOND` | Redis-cleanup task frequency |
| `has_acl` | n/a | **Rejected** with explicit error for S3/Azure sources |
