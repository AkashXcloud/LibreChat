# Soft Delete (SharePoint)

When SharePoint deletes a file, we need to:
1. Mark it as deleted in the ragbucket without losing the row (for audit / replay).
2. Stop returning its chunks from vector search.
3. Stop returning its rows from any ontology view built on the bucket.

This is **SharePoint-only**. S3 and Azure ragbuckets don't have a delete signal — files just stop showing up in the listing — so the cascade is a no-op there.

## Where the signal lives

| Layer | Column | Set by |
|---|---|---|
| `<bucket>_raw_files` | `deleted_at DATETIME` (NULL = alive) | `<bucket>_sharepoint_apply_delete` task |
| `<bucket>_chunks` | (no column — rows are hard-deleted) | `OntologyRefreshDaemon` |
| Ontology view backing table | `deleted_at DATETIME` (NULL = alive) | `OntologyRefreshDaemon` |

`deleted_at` is **separate from `status`**. `status` tracks the Redis task lifecycle (`READY` / `submitted` / `completed` / `error`). Don't conflate them.

## End-to-end flow

```
SharePoint file deleted
        │
        ▼  (crawler removes row from sys_sharepoint_files)
<bucket>_sharepoint_stage task
        │  picks up missing drive_item_id via anti-join → emits change_type='deleted'
        ▼
<bucket>_sharepoint_apply_delete task
        │  sets _raw_files.deleted_at = NOW()
        ▼
OntologyRefreshDaemon (every 30s)
        ├─► hard-DELETE rows in <bucket>_chunks for the deleted file
        │   (HNSW index gets tombstones; segment compaction reclaims space)
        │
        └─► partial-INSERT to set ontology_view.deleted_at = NOW()
            for every view row whose _base_pk matches a tombstoned _raw_files row
```

Each step is idempotent. The daemon doesn't track what it's already cleaned — it just re-runs the same SQL every tick. After the first run, follow-up ticks find nothing to do.

## Why the daemon, not a SUBMIT TASK

StarRocks `SUBMIT TASK` rejects `UPDATE` and `DELETE` in the task body — only `INSERT`, `CTAS`, and `DATACACHE SELECT` are accepted. The daemon executes via `StmtExecutor` directly, which has no such restriction.

## Schema gating

**`<bucket>_raw_files.deleted_at`** — only added when the source is SharePoint (`bucket.isSharepoint() = true`). S3/Azure buckets don't get the column at all.

**Ontology view `deleted_at`** — only added when the view is `ON RAGBUCKET <bucket>` and the bucket is SharePoint. `ON LINKS` / `ON COLUMN` views don't get the column (their base tables have no `deleted_at` to cascade from).

**`<bucket>_chunks`** — no `deleted_at` column. Rows are hard-deleted; the file_name in the deletion list comes from `_raw_files.deleted_at IS NOT NULL`. The `<bucket>_chunk_insert` task has a matching `AND r.deleted_at IS NULL` guard so chunks for tombstoned files aren't re-inserted from parquet.

## Daemon implementation

[OntologyRefreshDaemon.java](../fe/fe-core/src/main/java/com/starrocks/qe/OntologyRefreshDaemon.java) — two methods called per tick before the existing PAUSED/ACTIVE handling:

```java
runChunksDeleteCascade();   // hard-deletes chunks for tombstoned files
runOvDeleteCascade(meta);   // partial-INSERTs deleted_at on view rows
```

The chunks cascade uses a **two-step** pattern because StarRocks DELETE on DUPLICATE KEY tables rejects subqueries in IN predicates ("Child of in predicate should be value"):

```java
// Step 1: read deleted file_names
SELECT DISTINCT file_name FROM <bucket>_raw_files WHERE deleted_at IS NOT NULL;

// Step 2: DELETE with inline literal IN-list
DELETE FROM <bucket>_chunks WHERE file_name IN ('Doc1.pdf', 'Doc2.pdf', ...);
```

Single quotes in file names are escaped.

The OV cascade is a single partial-INSERT upsert on the PRIMARY KEY:

```sql
INSERT INTO <view> (id, deleted_at)
SELECT v.id, NOW()
FROM <view> v
JOIN <bucket>_raw_files r ON v._base_pk = r.id
WHERE r.deleted_at IS NOT NULL AND v.deleted_at IS NULL;
```

The `v.deleted_at IS NULL` guard makes it idempotent.

## How to enable

The daemon defaults to OFF. Turn it on once per FE lifetime:

```sql
REFRESH ONTOLOGY VIEW WITH ASYNC MODE ON;
```

It resets to OFF on FE restart — re-enable after any restart.

## How to test without SharePoint

You can simulate a delete without touching SharePoint:

```sql
UPDATE <bucket>_raw_files SET deleted_at = NOW() WHERE id = <some_id>;

-- Wait ≥30s for the next daemon tick, then:
SELECT COUNT(*) FROM <bucket>_chunks WHERE file_name = (
    SELECT file_name FROM <bucket>_raw_files WHERE id = <some_id>
);
-- Expect 0

SELECT deleted_at FROM <view> WHERE _base_pk = <some_id>;
-- Expect non-NULL
```

Confirm the daemon ticked at all:

```bash
grep "(OntologyRefreshDaemon|" /Users/jayshree/workspace/4.0.1_cognitive_sql/akashxdb/output/fe/log/fe.log | tail -5
```

## Common foot-guns

| Symptom | Cause | Fix |
|---|---|---|
| Cascade silently skips a SharePoint bucket | Bucket was created before `RagBucket.sourceUrl` field existed → `bucket.isSharepoint()` returns false | Drop the bucket's tables/tasks, drop the OV, recreate everything with a new bucket name (the `RagBucketMgr` entry persists across DROP TABLE) |
| Cascade SQL with subquery fails: `Child of in predicate should be value` | DELETE on DUPLICATE KEY tables doesn't accept subqueries in IN | Already handled — daemon uses two-step pattern. If you see this in the log, the JVM is running stale bytecode (FE restart didn't take). |
| `Streaming diff-insert` runs but cascade doesn't | Daemon is alive but cascade methods aren't called | Most likely the bucket's `sourceUrl` is NULL (legacy bucket). Recreate. |
| OV `deleted_at` set with timestamp **before** `_raw_files.deleted_at` | User updated `_raw_files.deleted_at` more than once; the cascade only fires on the first transition NULL → non-NULL | Expected behavior. The cascade is idempotent and won't re-set after the first run. |

## Out of scope (for now)

- `ON LINKS` / `ON COLUMN` ontology views can't be cascaded — the base table has no `deleted_at` column. Deletes there manifest as the row disappearing, which is a different detection pattern.
- Parquet retention. Old parquets in the dsRAG sink stick around forever. Deleting chunks doesn't delete parquets. A separate retention process is out of scope.
