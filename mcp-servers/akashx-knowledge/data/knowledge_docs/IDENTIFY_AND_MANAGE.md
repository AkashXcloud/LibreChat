# Identify and Manage RagBuckets and Ontology Views

How to tell which objects in an AkashXDB database are RagBuckets or Ontology Views,
which SQL to use with each, and what lifecycle operations exist today. Every
statement below is taken from the product docs or from behaviour verified against a
live AkashXDB server; anything not supported is stated as not supported.

## Identify RagBuckets (SHOW RAGBUCKET)

List the RagBuckets in the current database:

```sql
SHOW RAGBUCKET;
```

- The command is **singular**: `SHOW RAGBUCKET`. `SHOW RAGBUCKETS` (plural) is a
  **syntax error**.
- It returns one row per logical bucket. The bucket name is in the `ragbucket`
  column and the containing database in `database_name`. (Verified against a live
  AkashXDB server; the Superset AkashXDB engine spec relies on it.)
- The connection's current catalog/database scopes the listing, so run
  `SET CATALOG` / `USE` first if needed.

A RagBucket is a **document corpus, not a table**. Creating one also creates
physical tables named after the bucket — `<bucket>_metadata`, `<bucket>_raw_files`,
`<bucket>_chunks` and `<bucket>_intermediate_table` (plus
`<bucket>_sharepoint_changes` for SharePoint buckets). Those are internal storage,
so `SHOW TABLES` lists them but cannot tell you which bucket they belong to — use
`SHOW RAGBUCKET` to identify the buckets themselves.

## Identify Ontology Views (SHOW ONTOLOGY VIEWS)

List the Ontology Views in the current database:

```sql
SHOW ONTOLOGY VIEWS;
```

- The view name is in the `view_name` column and the containing database in
  `database_name`.
- An Ontology View is backed by a real table, so it also appears in `SHOW TABLES`
  as an ordinary base table — **`SHOW TABLES` cannot distinguish an Ontology View
  from a normal table.** Use `SHOW ONTOLOGY VIEWS`.

## Which SQL to use for each object

| Object | How to query it |
|---|---|
| Table | Normal `SELECT`. |
| Ontology View | Normal `SELECT`; filter `WHERE status = 'done'` for rows whose extraction finished, e.g. `SELECT * FROM <view> WHERE status = 'done';` |
| RagBucket | `ASK '<natural-language question>' ON <bucket>` (managed retrieval). Never `SELECT` from the bucket name — it has no columns. |

## Create a RagBucket

Use `CREATE RAGBUCKET` — see **RagBucket > SQL Syntax** for the full statement and
properties, and **Quick-Start Examples** for complete Azure and SharePoint pathways.

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

## Create an Ontology View (ON COLUMN, ON LINKS, ON RAGBUCKET)

See **Ontology > SQL Syntax**. The source clause takes two forms:

- `ON COLUMN <table>.<column>` and `ON LINKS <table>.<column>` — name the table **and**
  the column holding the text or links.
- `ON RAGBUCKET <bucket>` — the **bare bucket name only**, no `.column`; the source
  text is taken automatically from the bucket's `_raw_files.raw_text`.

```sql
CREATE ONTOLOGY VIEW az_demo_ont (
    Summary TEXT AS 'Summarize the document'
)
ON RAGBUCKET az_demo
USING ''
PROPERTIES (
    "rate_limit"     = "5",
    "time_interval"  = "30 SECOND",
    "llm_provider"   = "openai",
    "llm_model"      = "gpt-4o-mini"
);
```

## Drop, delete or alter a RagBucket (not supported as DDL)

There is **no `DROP RAGBUCKET` and no `ALTER RAGBUCKET` statement** — the grammar only
implements `CREATE RAGBUCKET` (RagBucket SharePoint design, gap G8). Do not write
either statement; it will fail.

To remove a RagBucket, drop its scheduled tasks and tables manually with the
documented **Cleanup template** below. After cleanup the bucket **name cannot be
reused** — the `RagBucketMgr` entry persists — so pick a new name when you recreate it.

## Drop or delete an Ontology View

There is no documented single statement for dropping an Ontology View. Remove one by
dropping its three scheduled tasks and its backing table — the ontology section of
the **Cleanup template** below.

## Cleanup template (remove a RagBucket and its Ontology Views)

From **Quick-Start Examples > Cleanup template**, verbatim (task names are spelled
exactly as in that doc). Replace `<prefix>` with the bucket name, `<db>` with its
database and `<view>` with the Ontology View name. The SharePoint-only lines can be
skipped for S3/Azure buckets (`IF EXISTS` makes them harmless either way).

```sql
-- Tasks (4 shared + 4 SharePoint-only)
DROP TASK IF EXISTS <prefix>_s3_medtadata_insert;
DROP TASK IF EXISTS <prefix>_raw_files_submit_task;
DROP TASK IF EXISTS <prefix>_chunk_insert;
DROP TASK IF EXISTS <prefix>_monitor_chunks;
DROP TASK IF EXISTS <prefix>_delete_key;
DROP TASK IF EXISTS <prefix>_sharepoint_stage;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_new;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_modify;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_delete;

-- Tables
DROP TABLE IF EXISTS <db>.<prefix>_raw_files;
DROP TABLE IF EXISTS <db>.<prefix>_chunks;
DROP TABLE IF EXISTS <db>.<prefix>_metadata;
DROP TABLE IF EXISTS <db>.<prefix>_intermediate_table;
DROP TABLE IF EXISTS <db>.<prefix>_sharepoint_changes;

-- Ontology view tasks + backing table
DROP TASK IF EXISTS ont_submit_<db>_<view>;
DROP TASK IF EXISTS ont_status_<db>_<view>;
DROP TASK IF EXISTS ont_dk_<db>_<view>;
DROP TABLE IF EXISTS <db>.<view>;
```

These statements are irreversible: the ingested documents, chunks and extracted
rows are deleted.

## Refresh / soft-delete cascade for Ontology Views

For SharePoint-backed Ontology Views, the soft-delete cascade is enabled with:

```sql
REFRESH ONTOLOGY VIEW WITH ASYNC MODE ON;
```

This enables the `OntologyRefreshDaemon`. It **resets when the FE restarts**, so re-run
it after every restart. See **Quick-Start Examples > Soft-delete cascade** and the
Soft Delete doc for how `deleted_at` tombstones propagate.
