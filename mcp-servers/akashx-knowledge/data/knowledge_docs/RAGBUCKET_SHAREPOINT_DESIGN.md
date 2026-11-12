# SharePoint RagBucket — Design

Companion doc to [RAGBUCKET_SHAREPOINT.md](RAGBUCKET_SHAREPOINT.md). Covers how the FE wires the SharePoint scan tasks against the catalog table maintained by the external crawler, how ACL flows from catalog to ragbucket to chunks, and what scenarios are/aren't handled.

---

## 1. Pipeline overview

The catalog table `sharepoint_meta.sys_sharepoint_files` is the single source of truth for "what files exist on SharePoint and who can see them." It's populated by the external crawler (`akashx-sharepoint-sync`) which talks to Microsoft Graph and writes one row per file per site. RagBucket reads from this catalog — it does NOT walk Graph itself.

```
                              ┌──────────────────────────────────┐
                              │ akashx-sharepoint-sync (external)│
                              │   walks Graph, writes catalog    │
                              └────────────────┬─────────────────┘
                                               ▼
                       sharepoint_meta.sys_sharepoint_files
                       (per-site partition, per-file row, ACL columns,
                        refreshed_at + last_modified_at timestamps)
                                               │
                ┌──────────────────────────────┴────────────────────────┐
                │ <prefix>_sharepoint_stage  (every metadata_interval)  │
                │   SELECT FROM sys_sharepoint_files WHERE site_id=…    │
                │   UNION ALL  anti-join for deletes                    │
                │   →  upserts _sharepoint_changes (PK drive_item_id)   │
                └────────────────────────────┬──────────────────────────┘
                                             │
                ┌────────────────────────────┼────────────────────────┐
                ▼                            ▼                        ▼
   <prefix>_sharepoint_apply_new   apply_modify              apply_delete
   INSERT new rows + ACL           UPDATE on watermark       SET deleted_at=NOW()
   (drive_item_id NOT IN           (mod_time newer);         (matched by
   _raw_files)                     COALESCE-refresh ACL      drive_item_id)
```

The stage task uses a race-free watermark: it filters `sys_sharepoint_files` by `refreshed_at > MAX(source_refreshed_at)` already in staging. The watermark uses the catalog's own clock, so the bucket DB's `NOW()` doesn't need to be aligned with the crawler's clock.

The two halves of the stage task (upsert + delete) are UNION ALL'd into one INSERT so a single SUBMIT TASK fires both per tick. The PRIMARY KEY on `<prefix>_sharepoint_changes(drive_item_id)` means re-staging the same drive_item_id replaces the previous staging row in place.

---

## 2. RagBucketExecutor wiring

### 2.1 Path detection

```java
boolean isSharepoint = pathLower.startsWith("sharepoint://");
```

**Validation at `CREATE RAGBUCKET` time:**

| Property | Required? | Notes |
|---|---|---|
| `storage_volume` | Required | Must be type `ADLS2` (used by the worker's download path) |
| `source_site_id` | Required | Must match a row in `sharepoint_meta.sharepoint_sites`. Validated via internal SELECT |
| `has_acl` | Optional | Defaults: `true` for SharePoint, `false` for S3/Azure. Explicit `true` on S3/Azure is rejected. Persisted as `RagBucket.hasAcl` |

The site is identified by its registered id, not by URL parsing. Register first with `CREATE SHAREPOINT_SITE <id>`, then reference that id here.

### 2.2 Schema additions for SharePoint buckets

**`<prefix>_raw_files`** column counts:

| Source | Columns | What's added |
|---|---|---|
| S3 / Azure | 15 | (base only) |
| SharePoint, `has_acl=false` | 17 | `drive_item_id` + `deleted_at` |
| SharePoint, `has_acl=true` | 24 | `drive_item_id` + 5 ACL cols + 2 timestamps + `deleted_at` |

| Column | Notes |
|---|---|
| `drive_item_id` | Always added on SharePoint. Graph driveItem.id — stable identity across renames; needed for delete/rename detection regardless of `has_acl` |
| `deleted_at` | Always added on SharePoint. Source tombstone written by `apply_delete` (NULL = alive). **Distinct from `status`** which tracks Redis task lifecycle. S3/Azure has no soft-delete signal, so the column is omitted there. |
| `acl_users`, `acl_groups`, `acl_sp_groups`, `acl_tenant_everyone`, `acl_anonymous` | Added only when `has_acl=true`. Per-file ACL projection |
| `last_modified_at` | Added only when `has_acl=true`. Same value as legacy `modification_time` — exposed under a uniform name so ontology views can read it across base table types |
| `refreshed_at` | Added only when `has_acl=true`. Any-change signal (content or ACL); used by Tier 3 refresh |

**`<prefix>_sharepoint_changes`** (new staging buffer) — created for every SharePoint bucket. Holds the latest catalog event per file. `PRIMARY KEY(drive_item_id)`. Base columns: `drive_item_id`, `change_type`, `file_url`, `file_name`, `modification_time`, `staged_at`, `source_refreshed_at`. With `has_acl=true`, also gets the 5 `acl_*` columns. `source_refreshed_at` mirrors the catalog's `refreshed_at` and serves as the stage-task watermark.

### 2.3 Scan-task pipeline (4 tasks)

| Task | What it does |
|---|---|
| `<prefix>_sharepoint_stage` | Builds `_sharepoint_changes` from `sys_sharepoint_files` in one INSERT. Two halves UNION'd: (a) upserts where catalog `refreshed_at > MAX(source_refreshed_at)` already staged; (b) deletes via anti-join — alive rows in `_raw_files` that disappeared from the catalog. |
| `<prefix>_sharepoint_apply_new` | Inserts staged upserts not yet in `_raw_files` (gated by `drive_item_id NOT IN`). Copies the 5 ACL cols and populates `modification_time`. When `hasAcl=true`, also writes `last_modified_at` + `refreshed_at`. |
| `<prefix>_sharepoint_apply_modify` | When `c.modification_time > r.modification_time` (watermark): re-marks status `READY`, overwrites `file_url` + `file_name` (renames), clears `deleted_at` (resurrection), refreshes ACL via `COALESCE(c.acl_*, r.acl_*)` (preserves existing ACL if staging is NULL). |
| `<prefix>_sharepoint_apply_delete` | Soft-deletes staged deletes by setting `deleted_at = NOW()`. **Does not touch `status`** (that's Redis lifecycle). Idempotent via `WHERE r.deleted_at IS NULL`. |

`apply_modify` and `apply_delete` use `INSERT INTO ... SELECT` (not `UPDATE`) because StarRocks `SUBMIT TASK` rejects `UPDATE`. On a PRIMARY KEY table, partial-column INSERT upserts and preserves untouched columns.

### 2.4 Other shared tasks

`submit`, `monitor`, `chunk_insert`, `delete_key` carry `drive_item_id` through unchanged. For SharePoint rows it's the Graph driveItem.id; for S3/Azure rows it's NULL. No semantic change for non-SharePoint sources.

The `chunk_insert` task has one SharePoint-only WHERE clause: `AND r.deleted_at IS NULL`. This stops it from re-inserting chunks for files the soft-delete cascade just removed (see §2.5).

### 2.5 Soft-delete cascade (SharePoint only)

When `apply_delete` sets `_raw_files.deleted_at`, two downstream effects fire from **`OntologyRefreshDaemon`** (every 30s tick):

- **Chunks cleanup** — hard-DELETE rows in `<prefix>_chunks` for tombstoned files. HNSW gets tombstones; segment compaction reclaims space. The `chunk_insert` task has a matching `AND r.deleted_at IS NULL` guard so deleted chunks aren't re-inserted from parquet.
- **Ontology view cascade** — partial-INSERT sets `deleted_at` on view rows whose `_base_pk` matches a tombstoned `_raw_files` row. Only runs for `ON RAGBUCKET` views backed by a SharePoint bucket. View keeps the LLM extraction for audit; query-side filtering on `WHERE deleted_at IS NULL` hides them.

Both are idempotent. The daemon executes via `StmtExecutor` (not `SUBMIT TASK`, which rejects `DELETE`). The chunks step uses a two-step `SELECT` + `DELETE WHERE ... IN (literal list)` pattern because StarRocks DELETE on DUPLICATE KEY tables rejects subqueries inside IN.

S3/Azure buckets have no `deleted_at` column and no `apply_delete` task — the cascade is a no-op.

See [SOFT_DELETE.md](SOFT_DELETE.md) for the topic-focused walkthrough, schema gating details, and foot-gun list.

---

## 3. Crawler boundary

The external **`akashx-sharepoint-sync`** Python daemon is the only component that talks to Microsoft Graph. RagBucket reads from the catalog it produces; there is no BE-side UDTF, no FE internal endpoint for cursor state, and no per-bucket delta cursor.

### 3.1 What the crawler produces

A row in `sharepoint_meta.sys_sharepoint_files` per file (and folder) per site, with:

| Column | Meaning |
|---|---|
| `site_id` | Registered site id (LIST partition key) |
| `item_id` | SharePoint UniqueId GUID (PK with site_id) |
| `drive_item_id` | Graph driveItem.id — stable through renames/moves within a drive |
| `path` | `sharepoint://<drive_id>/<drive_item_id>/<name>` — the URL the worker uses to download |
| `display_path` | Human-readable path under the site, e.g. `/sites/X/Library/Folder/file.ext` |
| `drive_id`, `list_id`, `list_item_id` | SharePoint addressing IDs (for filtering / debugging) |
| `is_dir` | TRUE for folders; the stage task filters these out |
| `acl_users` / `acl_groups` / `acl_sp_groups` / `acl_tenant_everyone` / `acl_anonymous` | Five-column ACL projection consumed by RLS at query time |
| `last_modified_at` | Content-change signal — only bumped when file content changes |
| `refreshed_at` | Crawler-side change signal — bumped on every upsert (content AND ACL) |

The two timestamps are deliberately separate: ACL-only edits do not bump `last_modified_at`, so the chunks pipeline (which keys on `modification_time`) doesn't re-extract on ACL changes. The stage task watermark uses `refreshed_at` so it sees both flavors of change.

### 3.2 Boundary contract

| Concern | Owner |
|---|---|
| Talking to Graph (auth, delta cursor, 410 recovery) | Crawler |
| `sharepoint_meta.sharepoint_site_drives` (per-drive delta cursor) | Crawler |
| `sharepoint_meta.sharepoint_sites` (site registry, partition lifecycle) | FE (`CREATE/ALTER/DROP SHAREPOINT_SITE` via `SharepointSiteExecutor`) |
| `sharepoint_meta.sys_sharepoint_files` (file catalog + ACL) | Crawler writes, RagBucket + ontology views read |
| `<prefix>_raw_files` and the 4-task pipeline | RagBucket |
| File content download | Worker (`dsRAG/Dependencies/storage.py`, class `SharePointStorage`) |

For crawler internals (delta-paging, 410 fallback, full-crawl harvest, per-drive cursor management), see [`akashx-sharepoint-sync/docs/CODE_FLOW.md`](../../../akashx-sharepoint-sync/docs/CODE_FLOW.md).

### 3.3 Failure modes (RagBucket side)

| Condition | Behavior |
|---|---|
| `source_site_id` not registered at CREATE time | `CREATE RAGBUCKET` fails with a clear "register the site first" error |
| Catalog row's ACL columns are NULL (crawler-side ACL fetch failed) | `apply_modify` preserves existing `_raw_files` ACL via COALESCE; `apply_new` inserts NULL ACL. RLS then fails-closed for affected rows. Operator surface: crawler's `ACL_FETCH_FAILED` logs + the empty-ACL probe SQL in `CODE_FLOW.md` |
| Crawler offline | Stage task tick is a no-op (no new `refreshed_at` to advance the watermark). `_raw_files` remains stale but consistent. Resumes automatically when the crawler comes back |

---

## 4. ragbucket.py worker (download path)

`dsRAG/routes/ragbucket.py` dispatches downloads to `SharePointStorage` when the resolved storage volume is type ADLS2:

```python
elif download_provider == "ADLS2":
    download_sp_creds = {
        "tenant_id":     resolved.get("sp_tenant_id", ""),
        "client_id":     resolved.get("sp_client_id", ""),
        "client_secret": resolved.get("sp_client_secret", ""),
    }
...
if download_sp_creds:
    file_path = await download_from_sharepoint_async(file_url, tmpdir, download_sp_creds)
```

`SharePointStorage` (in `Dependencies/storage.py`) handles:
- Parsing `sharepoint://{drive_id}/{item_id}/{filename}` URLs
- OAuth2 token acquisition via `azure-identity` (`ClientSecretCredential`)
- File download via Graph: `GET /drives/{drive_id}/items/{item_id}/content`

The worker is unaware of `drive_item_id` — it downloads by `file_url`. For SharePoint, `file_url` resolves to the right blob via the Graph `items/{id}/content` endpoint as long as `item_id` is still valid (renames don't break it; deletes do, but apply-delete has tombstoned the row by then).

---

## 5. SharePoint URL format

```
sharepoint://{drive_id}/{item_id}/{filename}
```

Example:
```
sharepoint://b!d56ouVP4SECdYoeN3h6TDz52JYjeL7hJutRXpls11UAFXbMcZRmQTaKaKSNVVUZN/01M5JM26VA3EMXB5T2H5CICBMLMNJVNXBO/Doc1.pdf
```

This URL is:
- Generated by the external crawler from Graph API responses and stored as `path` in `sys_sharepoint_files`
- Copied into `_raw_files.file_url` by the bucket's stage / apply_new tasks
- Parsed by `SharePointStorage.parse_url()` in the worker for download
- **Mutable on rename** — apply-modify rewrites it when SharePoint reports a name change for the same `drive_item_id`. Downstream consumers should not assume `file_url` is stable for the life of a row

---

## 6. End-to-end credential & state flow

```
                        ┌─────────────────────────────┐
                        │  CREATE STORAGE VOLUME       │
                        │  (ADLS2 + SP OAuth2 creds)   │
                        └──────────────┬──────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
                    ▼                                     ▼
         ┌─────────────────────┐               ┌──────────────────┐
         │ File Discovery      │               │ File Download    │
         │ (external crawler — │               │ (dsRAG worker —  │
         │  akashx-sharepoint- │               │  SharePointStorage)│
         │  sync, Python)      │               │                  │
         └──────────┬──────────┘               └────────┬─────────┘
                    │                                   │
                    ▼                                   ▼
         direct DB connection                  FE Internal API
         (aiomysql → akashxdb)                X-Internal-Token
                    │                                   │
                    ▼                                   ▼
   sharepoint_meta.{sys_sharepoint_files,         /api/internal/storage_credential
                    sharepoint_site_drives,
                    sharepoint_sites}
```

**File discovery (external crawler daemon):**
- Runs as a long-running process outside akashxdb (`sharepoint-crawler-daemon`)
- Polls `sharepoint_sites` for registered sites; dispatches full/delta crawls per cron
- Maintains per-drive delta cursor in `sharepoint_site_drives` (one row per site/drive)
- Writes file rows + ACL into `sys_sharepoint_files`
- Credentials resolved from the same ADLS2 storage volume as the worker

**File download (dsRAG worker):**
- Triggered by the standard ragbucket task pipeline → worker → `/extract_row_level_chunks_vectors`
- Reads `file_url` from `_raw_files` (format `sharepoint://<drive_id>/<drive_item_id>/<filename>`)
- Calls FE at `host.docker.internal:8030` (cross-container) for credential resolution
- 5-minute per-worker cache via `credential_resolver.py`

**Credential rotation:**
- `ALTER STORAGE VOLUME sp_creds SET ...`
- Crawler picks up new creds on its next credential refresh cycle (configurable)
- Download picks up new creds within 5 minutes (worker cache TTL)
- For immediate effect, restart the crawler container and the dsRAG worker

---

## 7. Delta sync semantics

### 7.1 Identity model — `drive_item_id`

SharePoint rows in `_raw_files` carry a `drive_item_id` column populated from Graph's `item_id`. apply-modify and apply-delete match on this column rather than on `file_url`, because:

- `file_url` embeds the filename. On rename, `file_url` changes; `item_id` is stable.
- Graph's delete payload contains only `item_id` (no `name`, no `lastModifiedDateTime`), so `file_url` isn't reconstructable for tombstones.

For S3/Azure, `drive_item_id` is NULL — those sources continue to use `file_url` as their natural identity. The column is nullable and the existing S3/Azure scan-task INSERT doesn't reference it, so behavior on those sources is unchanged.

### 7.2 Watermark for modifies

`apply-modify` only fires when `c.modification_time > r.modification_time`. Without this filter every tick would re-mark already-completed files as `READY` and the worker would loop forever. After the worker reprocesses, the row's `modification_time` matches the staging row, comparisons return false, ticks become noops.

### 7.3 Change-type semantics

The stage task emits `change_type='upserted'` for catalog rows whose `refreshed_at` is newer than what's already staged, and `change_type='deleted'` for `_raw_files` rows that no longer appear in the catalog (anti-join). Apply tasks split:
- **apply-new:** `change_type='upserted' AND drive_item_id NOT IN _raw_files`
- **apply-modify:** `change_type='upserted' AND watermark says newer`
- **apply-delete:** `change_type='deleted'`

apply-new and apply-modify intentionally have overlapping `WHERE` clauses so the order of task execution within a tick doesn't matter — each is idempotent.

### 7.4 Scenario coverage

| # | Scenario | Status | Notes |
|---|---|---|---|
| 1 | New file added | ✅ | apply-new picks it up |
| 2 | Modified file (same id, edited content) | ✅ | apply-modify resets `status=READY`; bumps `r.modification_time` |
| 3 | Deleted file | ✅ | matched by `drive_item_id`; soft-deleted via `status=deleted` |
| 4 | Renamed file (same id, new filename) | ✅ | apply-modify updates `file_url` + `file_name` in place |
| 5 | Delete + re-add with new id | ✅ | old row tombstoned via #3; new row inserted via #1 |
| 6 | Resurrection (delete + re-add with same id) | ✅ | apply-modify sees newer mod-time, resets `status` |
| 7 | Moved out of watched folder | 🐛 G1 | scope filter drops the row before staging — `_raw_files` stays stale |
| 8 | Moved into watched folder | ✅ | item appears as a new id in scope; apply-new inserts |

---

## 8. Pros & Cons

### 8.1 Pros
- **Single source of truth.** `sharepoint_meta.sys_sharepoint_files` is written by one process (the external crawler) and read by every consumer (RagBucket, SP_FILES, ontology views). One walk of Graph per site regardless of how many ragbuckets reference it.
- **ACL flows for free.** The crawler populates the five ACL columns once per file; every consumer inherits them, enabling RLS on RagBucket-backed and SP_FILES-backed ontology views uniformly.
- **Bounded I/O.** Staging is one row per file (PK on `drive_item_id`); the catalog is partitioned by site_id so per-site lifecycle is isolated.
- **Bucket-local state stays contained.** All bucket-local tables live in the bucket DB, so dropping the database (or manually dropping the tables) removes them. The shared catalog is untouched.
- **No bucket-local cursor.** No `_sharepoint_state` to manage, no per-bucket delta replay. The race-free watermark uses `refreshed_at` from the catalog directly.

### 8.2 Cons / things to know
- **Site must be registered before bucket create.** Adds a step compared to S3/Azure, but the validation surfaces typos at create time rather than at first task tick.
- **`file_url` is no longer immutable on a SharePoint row.** apply-modify can change it on rename. Any downstream consumer that caches `file_url` needs to tolerate this.
- **More tasks per bucket.** SharePoint creates 4 scan-related tasks instead of 1. Each is cheap, but log volume increases. Drift the cadence longer if it's noisy.
- **Crawler is a separate deploy unit.** A second container/process must be running for SharePoint to work. Compared to the old in-process UDTF, this is more moving parts but also clean ownership boundaries.
- **Mixed-source queries.** Anything that does `WHERE drive_item_id = ...` on `_raw_files` silently drops S3/Azure rows. Fine since `drive_item_id` is SharePoint-only by design.

---

## 9. Known gaps & bugs

Status legend: ✅ resolved · 🟡 mitigated · ⚪ open.

| ID | Gap | Severity | Status / fix |
|---|---|---|---|
| **G1** | **Move-out of folder leaves a stale row.** Previously a UDTF-side scope filter dropped these silently. | n/a | ✅ Resolved by the merge. The crawler owns scope decisions; if a file leaves the catalog, RagBucket's stage-task anti-join detects absence and emits a synthetic `change_type='deleted'` to apply_delete. |
| **G2** | **Renamed-file chunks orphaned under old name.** Chunks pipeline still joins on `file_name`. apply-modify updates the new name on `_raw_files` but old chunks remain. | Medium | ⚪ Open. Chunks pipeline (`insertIntoDsragChunksSql`): switch the join from `r.file_name` to `r.drive_item_id`, and add `drive_item_id` to `_chunks`. |
| **G3** | **NULL `modification_time` would never reprocess.** Watermark comparison evaluates to NULL and apply-modify skips the row. In practice Graph always populates the field. | Low | ⚪ Open (defensive). Relax to `(c.mod_time > r.mod_time OR r.mod_time IS NULL OR c.mod_time IS NULL)`. |
| **G4** | **`token_count` is always NULL** (pre-existing). All sources write `token_count = NULL`. Root cause: dsrag LLM clients don't capture `usage` from API responses. | Low | ⚪ Open. Capture `response.usage` in `Dependencies/ragbucket/dsRAG/dsrag/llm.py`, accumulate per-file in `process_request_task()`, pass through `set_step("completed", token_count=...)`. |
| **G5** | **Backdated upload edge case.** SharePoint returns `lastModifiedDateTime` *earlier* than the stored value (e.g., timestamp-sensitive restore) — resurrection won't trigger. Vanishingly rare. | Negligible | ⚪ Out of scope. |
| **G6** | **Modified-file chunks duplicate / stale.** apply-modify resets `status='READY'` and the worker re-extracts; the chunks insert task's `NOT IN _chunks` guard then BLOCKS new chunks from being inserted at all (the actual behavior is more aggressive than "duplicate" — see below). | Medium | ⚪ Open. Tied to G2 — once the chunks pipeline keys on `drive_item_id`, the modify path should also delete old chunks before re-insert. |
| **G7** | **ACL-only edits don't propagate to `_raw_files` between content changes.** apply-modify only fires when `c.modification_time > r.modification_time`; an ACL-only edit (which doesn't bump `last_modified_at`) is staged but never applied until the next content change. | Medium | ⚪ Open. Add a separate ACL-refresh task that fires when staging ACL differs from `_raw_files` ACL, OR extend apply-modify's WHERE clause with an ACL-diff predicate. |
| **G8** | **No `DROP RAGBUCKET` or `ALTER RAGBUCKET` DDL.** The grammar only exposes `CREATE RAGBUCKET`. Migration, lifecycle operations, and re-binding to a different `source_site_id` all require manual table-level cleanup or schema edits. `hasAcl` is persisted on `RagBucket`; `source_site_id` is not (see §10). | Medium | ⚪ Open. Add the two statements; the executors are mostly drop-everything-by-prefix and persist a single-column change to `RagBucket`. See §10 for current workaround. |

---

## 10. Migration checklist (pre-merge → post-merge buckets)

There is currently no `DROP RAGBUCKET` or `ALTER RAGBUCKET` DDL implemented in the grammar — only `CREATE RAGBUCKET`. That constrains the migration path: in-place is the only realistic option, plus a manual table-level cleanup if you want to start fresh.

Apply per existing SharePoint ragbucket.

### In-place migration (current best option)

> The ACL / timestamp `ADD COLUMN`s below apply only when migrating to a `has_acl=true` bucket. For `has_acl=false`, skip them.

```sql
-- Stop the bucket's tasks first
DROP TASK <prefix>_sharepoint_stage;
DROP TASK <prefix>_sharepoint_apply_new;
DROP TASK <prefix>_sharepoint_apply_modify;
DROP TASK <prefix>_sharepoint_apply_delete;

-- _raw_files: rename column (always)
ALTER TABLE <bucket_db>.<prefix>_raw_files
    RENAME COLUMN source_file_id TO drive_item_id;

-- _raw_files: ACL + timestamps (only for has_acl=true buckets)
ALTER TABLE <bucket_db>.<prefix>_raw_files
    ADD COLUMN acl_users           ARRAY<VARCHAR(128)> NULL,
    ADD COLUMN acl_groups          ARRAY<VARCHAR(128)> NULL,
    ADD COLUMN acl_sp_groups       ARRAY<VARCHAR(64)>  NULL,
    ADD COLUMN acl_tenant_everyone BOOLEAN NULL DEFAULT "false",
    ADD COLUMN acl_anonymous       BOOLEAN NULL DEFAULT "false",
    ADD COLUMN last_modified_at    DATETIME NULL,
    ADD COLUMN refreshed_at        DATETIME NULL;

-- _sharepoint_changes: rename column + add source_refreshed_at (always)
ALTER TABLE <bucket_db>.<prefix>_sharepoint_changes
    RENAME COLUMN source_file_id TO drive_item_id;
ALTER TABLE <bucket_db>.<prefix>_sharepoint_changes
    ADD COLUMN source_refreshed_at DATETIME NULL;

-- _sharepoint_changes: ACL (only for has_acl=true buckets)
ALTER TABLE <bucket_db>.<prefix>_sharepoint_changes
    ADD COLUMN acl_users           ARRAY<VARCHAR(128)> NULL,
    ADD COLUMN acl_groups          ARRAY<VARCHAR(128)> NULL,
    ADD COLUMN acl_sp_groups       ARRAY<VARCHAR(64)>  NULL,
    ADD COLUMN acl_tenant_everyone BOOLEAN NULL DEFAULT "false",
    ADD COLUMN acl_anonymous       BOOLEAN NULL DEFAULT "false";

-- Drop the legacy state table (no longer used; cursor lives in sharepoint_site_drives)
DROP TABLE IF EXISTS <bucket_db>.<prefix>_sharepoint_state;

-- Recreate the four tasks. There is no executor entry point for "regenerate tasks
-- for an existing bucket" yet — paste the new task SQL produced by the updated
-- RagBucketExecutor.processRagBucket() and run it manually. The four SQL bodies
-- live in RagBucketExecutor.java (sharepointStageSql / sharepointApplyNewSql /
-- sharepointApplyModifySql / sharepointApplyDeleteSql).
```

`RagBucket` carries `hasAcl` but not `source_site_id`. Until `ALTER RAGBUCKET` lands, in-place migration produces tasks whose stage SQL has the site_id hardcoded as a string literal (you supply it when generating the task SQL from the template). Treat the site_id as part of the migration script, not as durable bucket state.

The window between DROP TASK and CREATE TASK is the only moment the bucket isn't ingesting new files. Should be minutes, not days. Coordinate with bucket owners.

### Start-fresh path (manual cleanup, then re-create)

If you'd rather discard the bucket's accumulated chunks and start clean:

```sql
-- Manually drop everything the bucket created (no DROP RAGBUCKET DDL today)
DROP TABLE IF EXISTS <bucket_db>.<prefix>_raw_files;
DROP TABLE IF EXISTS <bucket_db>.<prefix>_sharepoint_changes;
DROP TABLE IF EXISTS <bucket_db>.<prefix>_sharepoint_state;
DROP TABLE IF EXISTS <bucket_db>.<prefix>_chunks;
DROP TABLE IF EXISTS <bucket_db>.<prefix>_metadata;
DROP TABLE IF EXISTS <bucket_db>.<prefix>_intermediate_table;
DROP TASK IF EXISTS <prefix>_sharepoint_stage;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_new;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_modify;
DROP TASK IF EXISTS <prefix>_sharepoint_apply_delete;
DROP TASK IF EXISTS <prefix>_raw_files_submit_task;
DROP TASK IF EXISTS <prefix>_monitor_chunks;
DROP TASK IF EXISTS <prefix>_chunk_insert;
DROP TASK IF EXISTS <prefix>_delete_key;

-- Also delete the persisted RagBucket entry — the metadata lives in RagBucketMgr,
-- which has no public SQL to remove rows. Either restart the FE after a manual
-- catalog edit, or wait for DROP RAGBUCKET to be implemented before reusing the
-- bucket name.

-- Then register the site (if not already) and create a fresh bucket
CREATE SHAREPOINT_SITE <site_id> PROPERTIES (...);
CREATE RAGBUCKET <newname>     -- pick a NEW name to avoid the persisted-metadata conflict
FROM 'sharepoint://<site_id>/'
PROPERTIES (
    "storage_volume"  = "sp_creds",
    "source_site_id"  = "<site_id>",
    ...
);
```

### Open follow-up: ship `DROP RAGBUCKET` and `ALTER RAGBUCKET`

The lack of these statements makes migrations clunky. A reasonable follow-up is to add at minimum:

- `DROP RAGBUCKET <name>` — drops all bucket-local tables, tasks, and removes the `RagBucketMgr` entry. Cascades cleanly.
- `ALTER RAGBUCKET <name> SET ("source_site_id" = "...")` — for the in-place migration path so site_id can be persisted on the bucket rather than baked into task SQL.

Tracked as G8 in §9.
