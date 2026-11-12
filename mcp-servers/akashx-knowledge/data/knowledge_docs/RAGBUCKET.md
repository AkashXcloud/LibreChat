# RagBucket

## Overview

RagBucket is a document ingestion and retrieval system that automatically scans an S3 bucket for files, processes them (parsing, chunking, embedding), and stores the results in vector-indexed tables for semantic search. It powers the RAG (Retrieval-Augmented Generation) pipeline used by Cognitive SQL.

## SQL Syntax

```sql
CREATE RAGBUCKET <bucket_name> FROM 's3://<user-bucket>/<path>/'
PROPERTIES (
  "aws.s3.access_key" = "...",
  "aws.s3.secret_key" = "...",
  "aws.s3.region" = "us-east-1",
  "llm_provider" = "grok",
  "llm_model" = "grok-4-fast",
  "embedding_provider" = "openai",
  "embedding_model" = "text-embedding-3-small"
);
```

## End-to-End Flow

```
User: CREATE RAGBUCKET test_bucket FROM 's3://my-docs/'
        |
        v
  +-----------------+
  |   SQL Parser    |  AstBuilder.visitCreateRagBucketStatement()
  |  (StarRocks.g4) |  Parses into CreateRagBucketStmt
  +-----------------+
        |
        v
  +-----------------+
  |  StmtExecutor   |  Routes to RagBucketExecutor
  +-----------------+
        |
        v
  +-------------------------------+
  |   RagBucketExecutor           |
  |                               |
  |  1. Validate UDFs exist       |
  |  2. Validate AWS credentials  |
  |  3. Validate S3 bucket access |
  |  4. Create metadata object    |
  |  5. Create 4 tables           |
  |  6. Create 5 scheduled tasks  |
  |  7. Persist to EditLog        |
  +-------------------------------+
        |
        v
  +-------------------------------------------+
  |   5 Scheduled Tasks (continuous pipeline)  |
  |                                            |
  |  S3 Scan     (every 15s)  → raw_files     |
  |  Submit      (every 30s)  → Redis          |
  |  Monitor     (every 10s)  → status check   |
  |  Chunk Load  (every 35s)  → chunks table   |
  |  Cleanup     (every 60s)  → delete keys    |
  +-------------------------------------------+
        |
        v
  +-------------------------------+
  |   External Worker (dsRAG)     |
  |                               |
  |  Download → Parse → Chunk     |
  |  → Embed → Upload Parquet     |
  +-------------------------------+
```

## Tables Created

### 1. `<prefix>_metadata`
Stores ragbucket configuration.

| Column | Description |
|--------|-------------|
| Ragbucket_Name | Logical bucket name |
| Bucket_Name | S3 bucket name |
| s3_path | Full S3 path |
| llm_provider | LLM provider |
| llm_model | LLM model name |
| embedding_provider | Embedding provider |
| embedding_model | Embedding model name |

### 2. `<prefix>_raw_files`
Tracks uploaded files and their processing status.

**Column count by source:**

| Source | Columns | Extras |
|---|---|---|
| S3 / Azure | 15 | (base only) |
| SharePoint, `has_acl=false` | 17 | + `drive_item_id` + `deleted_at` |
| SharePoint, `has_acl=true` (default) | 24 | + `drive_item_id` + 5 ACL + 2 timestamps + `deleted_at` |

**Always present (15):**

| Column | Type | Purpose |
|--------|------|---------|
| `id` | BIGINT | Auto-increment primary key |
| `file_name`, `file_type`, `s3_name` | VARCHAR | File identity |
| `file_url` | VARCHAR | Source URL (S3 / Azure / SharePoint) |
| `raw_text` | STRING | Extracted text content |
| `modification_time` | DATETIME | File mod time |
| `bucket_url` | VARCHAR | Per-ragbucket sink prefix in dsRAG S3 |
| `request_id`, `status_message`, `error_message`, `token_count`, `status`, `retry`, `result` | mixed | Redis task lifecycle + worker output |

**SharePoint adds 2 columns** regardless of `has_acl`:
- `drive_item_id` — Graph driveItem.id, stable identity across renames
- `deleted_at` DATETIME — source tombstone (NULL = alive). Set by `apply_delete`. **Separate from `status`** (Redis lifecycle). Triggers chunks cleanup + ontology view cascade — see [SOFT_DELETE.md](SOFT_DELETE.md). S3/Azure has no soft-delete signal so the column is omitted there.

**When `has_acl=true`** (default for SharePoint), 7 more columns:

| Column | Type | Purpose |
|--------|------|---------|
| `acl_users`, `acl_groups`, `acl_sp_groups` | ARRAY | Allowed user / group OIDs |
| `acl_tenant_everyone`, `acl_anonymous` | BOOLEAN | Tenant-wide / public flags |
| `last_modified_at` | DATETIME | Content-change signal (same value as `modification_time`, exposed under a uniform name) |
| `refreshed_at` | DATETIME | Any-change signal (content or ACL); used by Tier 3 refresh |

### 3. `<prefix>_chunks`
Vector-indexed document chunks for semantic search.

| Column | Type | Description |
|--------|------|-------------|
| file_name | VARCHAR | Source file name |
| chunk_index | BIGINT | Chunk position in document |
| chunk_text | STRING | Chunk text content |
| document_title | STRING | Auto-generated document title |
| section_title | STRING | Auto-generated section title |
| document_summary | STRING | Document-level summary |
| section_summary | STRING | Section-level summary |
| vector | ARRAY\<FLOAT\> | Embedding vector (768 dimensions) |
| chunk_page_start | BIGINT | Start page number |
| chunk_page_end | BIGINT | End page number |
| modification_time | DATETIME | Timestamp |

> This table has an **HNSW vector index** with cosine similarity for fast approximate nearest neighbor search.

### 4. `<prefix>_intermediate_table`
External Parquet table that reads processed output from the dsRAG S3 path. Acts as a bridge between the external worker's output and the chunks table.

## Scheduled Tasks Pipeline

### Task 1: S3 Metadata Insert (every 15s)
- Scans the user's S3 path using the `files()` table function
- Filters out files already in `raw_files` (by file name + modification time)
- Filters out directories (`is_dir` check)
- Inserts new files with `status = 'READY'`

### Task 2: Raw Files Submit (every 30s)
- Selects rows where `status = 'READY'` (limited by `rate_limit`)
- Calls `submit_task_to_redis()` UDF with:
  - request_id, file_url, bucket_url
  - LLM/embedding provider and model config
  - Download credentials (user's S3) and upload credentials (dsRAG S3)
  - Retry flag
- Updates raw_files with response JSON (status, message, error)

### Task 3: Monitor Chunks (every 10s)
- Selects rows with non-terminal status
- Calls `check_redis_task_status()` UDF to poll Redis
- Updates raw_files with: status, raw_text, error_message, token_count

### Task 4: Chunk Insert (every 35s)
- Joins the Parquet intermediate table with raw_files
- Only processes files with `status = 'completed'` or `status = 'deleted'`
- Inserts parsed chunks (with vectors) into the chunks table

### Task 5: Delete Key (every 60s)
- Selects rows where `status = 'completed'`
- Calls `ragbucket_redis_dk_udf()` to remove Redis hashes
- Sets status to `'deleted'`

## Redis Architecture

### Key Structures

```
RAGBUCKET:<request_id>  (Redis Hash)
  ├── request_id
  ├── status
  ├── file_url          (source S3 URL)
  ├── bucket_url        (destination S3 URL)
  ├── llm_provider
  ├── llm_model
  ├── embedding_provider
  ├── embedding_model
  ├── download_creds    (JSON - user's AWS credentials)
  ├── upload_creds      (JSON - dsRAG AWS credentials)
  ├── raw_text          (extracted text after processing)
  ├── error
  ├── token_count
  ├── message
  └── timestamps

ragbucket_stream  (Redis Stream)
  └── Consumer group: "ragbucket"
```

## External Worker Processing (dsRAG)

The worker consumes tasks from `ragbucket_stream` and processes documents:

1. **Download** - Fetches the file from the user's S3 using download credentials
2. **Parse** - Extracts text from PDF, DOCX, TXT, MD files
3. **Semantic Sectioning** - Identifies logical document structure using LLM
4. **Chunking** - Splits into chunks with configurable size
5. **Embedding** - Generates vector embeddings for each chunk (768 dimensions)
6. **Auto-Context** - Generates document and section summaries
7. **Parquet Output** - Serializes chunks + vectors into Parquet format
8. **Upload** - Uploads Parquet to the dsRAG S3 path

The intermediate Parquet table then picks up the output, and the chunk insert task loads it into the chunks table.

## UDF Integration (via UDF_STRUCTURED)

Three UDFs defined in `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/ragbucket.sql`:

- **`submit_task_to_redis()`** - Creates Redis hash with task metadata + enqueues to `ragbucket_stream`. Supports retry flag.
- **`check_redis_task_status()`** - Queries Redis hash, returns full status JSON. Strips sensitive credentials before returning.
- **`ragbucket_redis_dk_udf()`** - Deletes the Redis key after successful completion.

## Configuration

### Destination (where dsRAG writes parquet chunks)

The destination is **per-ragbucket**, configured via two PROPERTIES on `CREATE RAGBUCKET`. Mirrors the source side (URL on `FROM` clause + `storage_volume` PROPERTY for creds):

| PROPERTY | Purpose |
|---|---|
| `dest_storage_volume` | Name of a `CREATE STORAGE VOLUME` that holds upload credentials |
| `dest_url` | Full destination URL (`s3://...`, `wasbs://...`) |

**Both must be set together.** If neither is set, the bucket falls back to legacy `dsrag_aws_s3_*` keys in `fe.conf` (S3/MinIO only — commented out in stock fe.conf).

Supports S3, MinIO, and Azure Blob without any FE config change or restart.

#### Example: MinIO

```sql
CREATE STORAGE VOLUME minio_dest
TYPE = S3
LOCATIONS = ("s3://akashx-ragbucket/")
PROPERTIES (
    "aws.s3.access_key"                = "minioadmin",
    "aws.s3.secret_key"                = "minioadmin123",
    "aws.s3.endpoint"                  = "http://host.docker.internal:9000",
    "aws.s3.enable_path_style_access"  = "true"
);

CREATE RAGBUCKET sp_test FROM 'sharepoint://demosite/' PROPERTIES (
    ...,
    'dest_storage_volume' = 'minio_dest',
    'dest_url'            = 's3://akashx-ragbucket/'
);
```

#### Example: AWS S3

```sql
CREATE STORAGE VOLUME s3_dest
TYPE = S3
LOCATIONS = ("s3://your-bucket-name/")
PROPERTIES (
    "aws.s3.access_key" = "AKIA...",
    "aws.s3.secret_key" = "...",
    "aws.s3.region"     = "us-east-1"
);

CREATE RAGBUCKET s3_test FROM 's3://source-bucket/path/' PROPERTIES (
    ...,
    'dest_storage_volume' = 's3_dest',
    'dest_url'            = 's3://your-bucket-name/'
);
```

#### Example: Azure Blob

```sql
CREATE STORAGE VOLUME azure_dest
TYPE = AZBLOB
LOCATIONS = ("azblob://desttest/")
PROPERTIES (
    "azure.blob.storage_account" = "myaccount",
    "azure.blob.shared_key"      = "<key>"
);

CREATE RAGBUCKET az_test FROM 'sharepoint://demosite/' PROPERTIES (
    ...,
    'dest_storage_volume' = 'azure_dest',
    'dest_url'            = 'wasbs://desttest@myaccount.blob.core.windows.net/'
);
```

Note for Azure: StarRocks only accepts `azblob://<container>/` in the LOCATIONS field of `CREATE STORAGE VOLUME`. The `dest_url` PROPERTY uses the full host form `wasbs://<container>@<account>.blob.core.windows.net/` — this is what the worker uses to upload.

#### Validation at CREATE time

| Check | Error if it fails |
|---|---|
| Both `dest_url` and `dest_storage_volume` set, or both unset | `dest_url and dest_storage_volume PROPERTIES must be set together. Provide both, or omit both to use the legacy fe.conf dsrag_aws_s3_* fallback.` |
| URL scheme matches volume type | `dest_url '...' scheme doesn't match AZBLOB/ADLS2 volume '...'. Expected wasbs://...` (or `Expected s3://...` for S3 volumes) |
| Storage volume exists | `Storage volume not found: ...` |

#### Per-ragbucket prefix

`dest_url` is the **base** sink path. Each `CREATE RAGBUCKET <name>` writes parquet under `<base>/<name>/...` (the bucket's prefix is the lowercased ragbucket name). So `sp_test` with `dest_url = s3://akashx-ragbucket/` lands at `s3://akashx-ragbucket/sp_test/Doc1.parquet`. Same shape applies for Azure: `wasbs://desttest@myaccount.../sp_test/Doc1.parquet`.

This isolates ragbuckets in the sink, prevents filename collisions, and makes per-bucket cleanup easy.

#### Legacy fallback

If you omit both `dest_url` and `dest_storage_volume` on `CREATE RAGBUCKET`, the bucket falls back to four `dsrag_aws_s3_*` keys in `fe.conf` (S3/MinIO only). Kept for migration; commented out in stock fe.conf. New buckets should use the PROPERTY-based model.

### Source Types

`CREATE RAGBUCKET ... FROM '<url>'` accepts three URL schemes:

| Scheme | Source | Detail doc |
|---|---|---|
| `s3://...` | AWS S3 / MinIO / any S3-compatible | This doc |
| `wasbs://...` / `azblob://...` | Azure Blob Storage | This doc |
| `sharepoint://...` | SharePoint via Microsoft Graph | [RAGBUCKET_SHAREPOINT.md](RAGBUCKET_SHAREPOINT.md) |

Source and destination protocols are independent — you can mix any combination (e.g., SharePoint source + Azure dest, or S3 source + MinIO dest).

### Configurable Properties

| Property | Default | Description |
|----------|---------|-------------|
| `rate_limit` | 10 | Batch size per task run |
| `metadata_interval` | 15s | S3 scan frequency |
| `submit_interval` | 30s | Redis submit frequency |
| `monitor_interval` | 10s | Status check frequency |
| `chunk_interval` | 35s | Chunk insert frequency |
| `delete_interval` | 60s | Redis cleanup frequency |
| `llm_provider` | grok | LLM provider |
| `llm_model` | grok-4-fast | LLM model |
| `embedding_provider` | openai | Embedding provider |
| `embedding_model` | text-embedding-3-small | Embedding model |
| `storage_volume` | (none) | **Source** credential reference. Required for SharePoint (must be ADLS2); optional for S3/Azure |
| `source_site_id` | (none) | **SharePoint only**, required. Must match a registered `CREATE SHAREPOINT_SITE` id |
| `has_acl` | `true` for SharePoint, `false` for S3/Azure | Per-file ACL plumbing. Explicit `true` on S3/Azure is rejected. Ontology views auto-derive from this. |
| `dest_storage_volume` | (none) | **Destination** credential reference for parquet sink. Must be set with `dest_url`, or omit both for legacy fallback |
| `dest_url` | (none) | Destination URL (`s3://...` or `wasbs://...`). URL scheme must match `dest_storage_volume` type |

## Querying RagBucket Data

```sql
-- Check file processing status
SELECT file_name, status, error_message FROM <bucket>_raw_files;

-- Semantic search on chunks
SELECT chunk_text, cosine_similarity(vector, @query_vector) AS score
FROM <bucket>_chunks
ORDER BY score DESC
LIMIT 10;
```

## Key Files

| Component | Path |
|-----------|------|
| Grammar | `fe/fe-core/src/main/java/com/starrocks/sql/parser/StarRocks.g4` |
| AST Statement | `fe/fe-core/src/main/java/com/starrocks/sql/ast/CreateRagBucketStmt.java` |
| Executor | `fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java` |
| Catalog Object | `fe/fe-core/src/main/java/com/starrocks/catalog/RagBucket.java` |
| Manager | `fe/fe-core/src/main/java/com/starrocks/catalog/RagBucketMgr.java` |
| Persistence | `fe/fe-core/src/main/java/com/starrocks/persist/CreateRagBucketLog.java` |
| UDF SQL | `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/ragbucket.sql` |
| FastAPI Route | `be/UDF_STRUCTURED/routes/ragbucket.py` |
| dsRAG Worker | `be/UDF_STRUCTURED/Dependencies/ragbucket/dsRAG/` |

---

## PROMPT / ASK - Querying a RagBucket with Natural Language

Once a RagBucket is created and documents are ingested, users can ask natural language questions against it using the `PROMPT` (or `ASK`) command. The system retrieves relevant document chunks via vector search, reranks them, and generates an answer using an LLM.

### SQL Syntax

```sql
PROMPT 'What are the top products by revenue?' ON <ragbucket_name>;
-- or
ASK 'What are the top products by revenue?' ON <ragbucket_name>;
```

### End-to-End Flow

```
User: PROMPT 'What are the top products?' ON my_bucket;
        |
        v
  +-----------------+
  |   SQL Parser    |  AstBuilder.visitPromptStatement()
  |  (StarRocks.g4) |  Parses into PromptStmt (question + bucket)
  +-----------------+
        |
        v
  +-----------------+
  |  StmtExecutor   |  Routes to PromptExecutor
  +-----------------+
        |
        v
  +------------------------------------------------+
  |   PromptExecutor - RAG Pipeline                 |
  |                                                 |
  |  Step 1: Generate query embedding               |
  |     cognitive_embed('openai',                   |
  |       'text-embedding-3-small', @question)      |
  |           |                                     |
  |           v                                     |
  |  Step 2: Vector similarity search               |
  |     SELECT * FROM <bucket>_chunks               |
  |     WHERE cosine_similarity(vector, @qv) > 0.x  |
  |     ORDER BY score DESC LIMIT 200               |
  |           |                                     |
  |           v                                     |
  |  Step 3: Build results JSON array               |
  |     GROUP_CONCAT with metadata                  |
  |     (doc_id, chunk_text, titles, score)         |
  |           |                                     |
  |           v                                     |
  |  Step 4: Rerank with Cohere                     |
  |     dsrag_rerank({Query, Results})              |
  |           |                                     |
  |           v                                     |
  |  Step 5: Extract relevant segments              |
  |     dsrag_rse_extract(reranked_results)         |
  |           |                                     |
  |           v                                     |
  |  Step 6: Generate final answer via LLM          |
  |     dsrag_rse_content({Query, RSE_Result})      |
  |           |                                     |
  |           v                                     |
  |  Step 7: Return {Question, Answer, Content}     |
  +------------------------------------------------+
```

### Detailed Pipeline Steps

#### Step 1: Generate Query Embedding

Converts the user's natural language question into a vector using the `cognitive_embed` function.

```sql
SET @question = 'What are the top products by revenue?';
SET @qv = (SELECT cognitive_embed('openai', 'text-embedding-3-small', @question));
```

- **Backend function**: `cognitive_embed()` in `be/src/exprs/cognitive_functions.cpp`
- **HTTP call**: POST to `COGNITIVE_EMBED_URL` (default: `http://localhost:8002/dsrag/embed`)
- **Returns**: `ARRAY<FLOAT>` vector

#### Step 2: Vector Similarity Search (Top-K Retrieval)

Searches the ragbucket's chunks table using HNSW vector index with cosine similarity.

```sql
WITH topk AS (
    SELECT *, cosine_similarity(vector, @qv) AS score
    FROM <bucket>_chunks
    WHERE cosine_similarity(vector, @qv) > threshold
    ORDER BY score DESC
    LIMIT 200
)
```

- Uses the HNSW index on the `vector` column (768 dimensions)
- Retrieves the top 200 most semantically similar chunks

#### Step 3: Build Results JSON Array

Aggregates the top-k results into a JSON array with metadata for reranking.

```sql
results_json AS (
    SELECT CONCAT('[', GROUP_CONCAT(
        JSON_OBJECT(
            'doc_id', file_name,
            'chunk_text', chunk_text,
            'document_title', document_title,
            'section_title', section_title,
            'score', score
        )
    ), ']') AS results_array_json
    FROM topk
)
```

#### Step 4: Rerank with Cohere

Passes the results through a reranker for better relevance ordering.

```sql
rse AS (
    SELECT dsrag_rse_extract(
        dsrag_rerank(
            JSON_OBJECT('Query', @question, 'Results', results_array_json)
        )
    ) AS RSE_Result
)
```

- `dsrag_rerank()` calls the Cohere reranker API
- Reorders results by true relevance (not just vector similarity)

#### Step 5: Extract Relevant Segments

`dsrag_rse_extract()` parses the reranked results and extracts the most relevant text segments that will form the context for answer generation.

#### Step 6: Generate Final Answer via LLM

```sql
SELECT dsrag_rse_content(
    JSON_OBJECT('Query', @question, 'RSE_Result', rse_segments)
) AS result;
```

- Sends the query + extracted context segments to the LLM
- LLM synthesizes a natural language answer grounded in the retrieved documents
- Returns JSON with: `question`, `answer`, `content` (source evidence)

### PROMPT Key Files

| Component | Path |
|-----------|------|
| PromptStmt AST | `fe/fe-core/src/main/java/com/starrocks/sql/ast/PromptStmt.java` |
| Prompt Executor | `fe/fe-core/src/main/java/com/starrocks/qe/PromptExecutor.java` |
| Cognitive Embed (C++) | `be/src/exprs/cognitive_functions.cpp` |
