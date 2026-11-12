# SharePoint RagBucket — Usage

Companion doc to [RAGBUCKET_SHAREPOINT.md](RAGBUCKET_SHAREPOINT.md). Covers everything you do *after* setup is done: registering sites, creating ragbuckets, smoke-testing catalog discovery, querying, and troubleshooting.

For first-time setup (Entra ID, storage volume, crawler deployment), see [RAGBUCKET_SHAREPOINT_SETUP.md](RAGBUCKET_SHAREPOINT_SETUP.md).

---

## 1. Register the SharePoint site

Before any ragbucket can read from SharePoint, the site must be registered. The external crawler reads this registry to know what to walk.

```sql
CREATE SHAREPOINT_SITE demosite
PROPERTIES (
    "site_url"   = "https://akashxsharepoint.sharepoint.com/sites/DemoSite",
    "full_cron"  = "0 2 * * *",         -- nightly authoritative crawl at 02:00
    "delta_cron" = "5 0-1,3-23 * * *",  -- hourly incremental at :05, skipping 02:xx
    "run_now"    = "true"               -- trigger an initial crawl immediately
);

-- See what's registered
SHOW SHAREPOINT_SITES;
```

The crawler picks up the new row on its next poll and starts populating `sharepoint_meta.sys_sharepoint_files` for `site_id = 'demosite'`.

---

## 2. Create a RagBucket bound to that site

### 2.1 Prerequisite: ADLS2 storage volume

The storage volume holds the Entra OAuth2 credentials used by the worker to download file content. Full setup instructions are in [RAGBUCKET_SHAREPOINT_SETUP.md §2](RAGBUCKET_SHAREPOINT_SETUP.md#2-storage-volume-adls2); the working shape is:

```sql
CREATE STORAGE VOLUME sp_creds
TYPE = ADLS2
LOCATIONS = ("adls2://akashxdb")
PROPERTIES (
    "azure.adls2.oauth2_tenant_id"     = "<your-tenant-id>",
    "azure.adls2.oauth2_client_id"     = "<your-client-id>",
    "azure.adls2.oauth2_client_secret" = "<your-client-secret>",
    "azure.adls2.oauth2_client_endpoint" = "https://akashxdb"
);
```

`LOCATIONS` and `oauth2_client_endpoint` values are required by the ADLS2 schema but ignored for SharePoint — pick anything that won't collide with another ragbucket.

### 2.2 Create the bucket

```sql
CREATE RAGBUCKET <bucket_name>
FROM 'sharepoint://<site_id>/'
PROPERTIES (
  'storage_volume'    = '<adls2_volume_name>',
  'source_site_id'    = '<site_id>',
  'metadata_interval' = '300 SECOND',
  'rate_limit'        = '5',
  'llm_provider'      = 'openai',
  'llm_model'         = 'gpt-4o-mini'
  -- 'has_acl' = 'false'   -- optional opt-out; defaults to 'true' for SharePoint
);
```

**Notes:**
- `source_site_id` is required and validated at create time. The `FROM` path is just a logical identifier — the bucket binds by site id, not URL parsing.
- `has_acl` defaults to `true` for SharePoint, `false` for S3/Azure. Explicit `true` on a non-SharePoint source is rejected. `ON RAGBUCKET` ontology views auto-derive their `has_acl` from the bucket.

### Example

```sql
CREATE RAGBUCKET sp_testrag
FROM 'sharepoint://demosite/'
PROPERTIES (
  'storage_volume'      = 'sp_creds',
  'source_site_id'      = 'demosite',
  'metadata_interval'   = '300 SECOND',
  'rate_limit'          = '5',
  'llm_provider'        = 'openai',
  'llm_model'           = 'gpt-4o-mini',
  'dest_storage_volume' = 'minio_dest',                      -- where parquet output goes
  'dest_url'            = 's3://akashx-ragbucket/'           -- (S3/MinIO or wasbs:// for Azure)
);
```

See [RAGBUCKET.md → Destination](RAGBUCKET.md#destination-where-dsrag-writes-parquet-chunks) for the full dest setup (S3 / MinIO / Azure examples).

This creates:

| Object | Purpose |
|---|---|
| `sp_testrag_raw_files` | File inventory. Columns: 16 (S3/Azure), 17 (SharePoint `has_acl=false` — adds `drive_item_id`), 24 (SharePoint `has_acl=true` — adds 5 ACL cols + `last_modified_at` + `refreshed_at`). |
| `sp_testrag_metadata` | Bucket configuration |
| `sp_testrag_chunks` | Processed document chunks with vector embeddings |
| `sp_testrag_intermediate_table` | External parquet table for processed data |
| `sp_testrag_sharepoint_changes` | Staging buffer (per-bucket; PK on `drive_item_id`) |
| 8 scheduled tasks | 4 SharePoint-specific (stage / apply-new / apply-modify / apply-delete) + 4 shared (submit / monitor / chunk_insert / delete_key) |

---

## 2b. Smoke-test catalog discovery

Confirm the crawler is populating the catalog for your site:

```sql
SELECT site_id, COUNT(*)        AS files,
       MIN(refreshed_at)        AS first_seen,
       MAX(refreshed_at)        AS last_refreshed
FROM   sharepoint_meta.sys_sharepoint_files
WHERE  site_id = 'demosite'
GROUP  BY site_id;
```

A row count > 0 means the crawler is working. Inspect a sample:

```sql
SELECT drive_item_id, display_path,
       cardinality(acl_users)     AS direct_users,
       cardinality(acl_groups)    AS groups,
       acl_tenant_everyone, acl_anonymous,
       last_modified_at, refreshed_at
FROM   sharepoint_meta.sys_sharepoint_files
WHERE  site_id = 'demosite' AND is_dir = FALSE
LIMIT  10;
```

If the catalog looks right but `<prefix>_raw_files` is empty for your bucket, the bucket's stage task hasn't fired yet — wait for one `metadata_interval` tick. If `acl_*` columns are all empty, see the troubleshooting section's "files with empty ACLs" probe.

---

## 3. Create an ontology view on the ragbucket

Standard `ON RAGBUCKET` syntax — no SharePoint-specific handling:

```sql
CREATE ONTOLOGY VIEW sp_doc_extraction (
  summary    TEXT AS 'Extract a summary of the document.',
  category   TEXT AS 'Classify: technical, legal, financial, hr, or other.',
  key_topics JSON AS 'Extract a JSON array of key topics.'
)
ON RAGBUCKET sp_testrag
USING 'You are processing a document. Extract all requested fields.'
PROPERTIES (
  'rate_limit'    = '5',
  'time_interval' = '60 SECOND',
  'llm_provider'  = 'openai',
  'llm_model'     = 'gpt-4o-mini'
);
```

---

## 4. Querying

```sql
-- Check SharePoint file discovery
SELECT file_name, file_url, status FROM sp_testrag_raw_files;

-- Check SharePoint identity (drive_item_id is the Graph item id)
SELECT drive_item_id, file_name, status FROM sp_testrag_raw_files;

-- Check the catalog-side delta cursor (managed by the external crawler)
SELECT site_id, drive_id, library_title,
       LENGTH(delta_link) AS cursor_size, last_delta_at
FROM   sharepoint_meta.sharepoint_site_drives
WHERE  site_id = 'demosite';

-- Check what the last delta tick saw
SELECT change_type, COUNT(*)
FROM sp_testrag_sharepoint_changes
GROUP BY change_type;

-- Processing-status summary (Redis task lifecycle)
SELECT status, COUNT(*) FROM sp_testrag_raw_files GROUP BY status;

-- Live vs tombstoned files. `deleted_at` is the SharePoint-side delete
-- state (set by apply_delete); separate from the Redis-task `status` column.
-- Tombstoned rows also trigger chunks cleanup + ontology view cascade —
-- see [SOFT_DELETE.md](SOFT_DELETE.md).
SELECT
  CASE WHEN deleted_at IS NULL THEN 'alive' ELSE 'tombstoned' END AS state,
  COUNT(*)
FROM sp_testrag_raw_files
GROUP BY state;

-- Query document chunks (after processing completes)
SELECT file_name, chunk_text, document_summary FROM sp_testrag_chunks;

-- Ontology-extraction progress
SELECT status, COUNT(*) FROM sp_doc_extraction GROUP BY status;

-- Extracted data
SELECT summary, category, key_topics FROM sp_doc_extraction WHERE status = 'done';
```

---

## 5. Comparison with S3 / Azure ragbuckets

| Aspect | S3 | Azure Blob | SharePoint |
|---|---|---|---|
| `FROM` path | `s3://bucket/path` | `wasbs://container@account/path` | `sharepoint://<site_id>/` |
| Site registration | n/a | n/a | `CREATE SHAREPOINT_SITE` first; bucket binds to `source_site_id` |
| File discovery | `FILES()` table function (BE) | `FILES()` table function (BE) | External crawler daemon writes `sys_sharepoint_files`; bucket stage task reads from there |
| Listing strategy | Full listing per scan + `WHERE NOT IN` dedupe | Same as S3 | Catalog-backed; per-tick watermark on `refreshed_at`; anti-join for deletes |
| Identity key | `file_url` | `file_url` | `drive_item_id` (Graph driveItem.id) |
| ACL + timestamps on `_raw_files` | n/a | n/a | 5 ACL cols + `last_modified_at` + `refreshed_at`, added only when `has_acl=true` |
| `has_acl` default | `false` (explicit `true` rejected) | `false` (explicit `true` rejected) | `true` (ontology views auto-derive) |
| Storage volume | Optional | Optional | **Required** (must be ADLS2; consumed by crawler + worker) |
| Download backend | `S3Storage` | `AzureBlobStorage` | `SharePointStorage` |
| URL scheme | `s3://bucket/key` | `wasbs://container@account/blob` | `sharepoint://driveId/driveItemId/filename` |
| Modify detection | None — re-listed each scan | None | Watermark on `modification_time` (apply-modify) |
| Delete handling | Implicit (file disappears from listing) | Same | Explicit anti-join in stage task; soft-delete via `deleted_at = NOW()` (separate column from `status`, which is the Redis task lifecycle) |
| Scalability | Unlimited (FILES() streams) | Unlimited | Crawler-driven; one Graph quota per cluster regardless of ragbucket count |

For *why* SharePoint uses a different shape, see the design doc — [§1 Pipeline overview](RAGBUCKET_SHAREPOINT_DESIGN.md#1-pipeline-overview).

---

## 6. Troubleshooting

### 6.1 Catalog / crawler

| Symptom | Cause | Fix |
|---|---|---|
| `source_site_id '<id>' is not registered` on CREATE RAGBUCKET | Site not in `sharepoint_meta.sharepoint_sites` | Run `CREATE SHAREPOINT_SITE <id> PROPERTIES(...)` first |
| `SharePoint source requires 'source_site_id' in PROPERTIES` | Missing the property on CREATE RAGBUCKET | Add `"source_site_id" = "<id>"` to PROPERTIES |
| `has_acl=true requires a SharePoint source` on CREATE RAGBUCKET | Explicit `has_acl=true` set on an S3 or Azure bucket | Remove the property or set `has_acl=false` (S3/Azure can't carry per-file ACL) |
| `dest_url and dest_storage_volume PROPERTIES must be set together` | Only one of the two dest PROPERTIES provided | Provide both, or omit both to use the legacy fe.conf fallback |
| `dest_url '...' scheme doesn't match AZBLOB/ADLS2 volume` | URL scheme and storage volume type disagree | Use `wasbs://...` with AZBLOB/ADLS2 volumes, `s3://...` with S3 volumes |
| Only 7 tasks created (missing `chunk_insert`) | `<prefix>_intermediate_table` CREATE failed silently — usually wrong storage volume credentials or unreachable account | Verify `nslookup <account>.blob.core.windows.net` resolves; check `azure.blob.storage_account` matches the real Azure account name |
| `sys_sharepoint_files` empty for your site | Crawler not running, or first crawl in progress | Check crawler container logs; `SHOW SHAREPOINT_SITES` shows `last_full_at` / `last_delta_at`; trigger now via `ALTER SHAREPOINT_SITE <id> TRIGGER` |
| Files with empty ACLs | Crawler-side ACL fetch failed (Sites.Selected not granted on that library) | Grep crawler logs for `ACL_FETCH_FAILED site=<id> list_id=…`; re-grant `Sites.Selected (read)`; trigger a fresh full crawl |

### 6.2 Credentials & FE internal API

| Symptom | Cause | Fix |
|---|---|---|
| Worker download fails: `HTTP 401` from credential API | Token mismatch | `FE_INTERNAL_TOKEN` in dsRAG `.env` must equal `internal_api_token` in `fe.conf` |
| Worker download fails: `HTTP 403` from credential API | `internal_api_token` not set in `fe.conf` | Set it and restart FE |
| `storage volume <name> missing ADLS2 OAuth fields` | Volume created without OAuth properties | `ALTER STORAGE VOLUME` to add the three `azure.adls2.oauth2_*` properties |
| Crawler can't authenticate to Graph | App permissions not granted, or wrong tenant/client/secret | Verify the Entra app has `Sites.Selected (read)` plus admin consent; verify the ADLS2 storage volume's `azure.adls2.oauth2_*` values |

### 6.3 Site / Graph resolution

| Symptom | Cause | Fix |
|---|---|---|
| Crawler logs `Site not found` / 404 from Graph | `site_url` in `sharepoint_sites` row is malformed, or the Entra app lacks access | Verify the URL is exactly `https://<host>/sites/<name>`; re-grant admin consent and `Sites.Selected (read)` on that specific site |
| Site is registered but `sys_sharepoint_files` only has folders, no files | App permission scope too narrow (e.g., site-level grant didn't cascade to all libraries) | Re-grant `Sites.Selected (read)` per affected library, then `ALTER SHAREPOINT_SITE <id> TRIGGER` |

### 6.4 Delta cursor / change handling

| Symptom | Cause | Fix |
|---|---|---|
| Modified files in SharePoint never re-extract | Pre-merge bucket still running the old UDTF-based task SQL | Migrate the bucket — see [design §10](RAGBUCKET_SHAREPOINT_DESIGN.md#10-migration-checklist-pre-merge--post-merge-buckets) |
| Deleted files stay as `status='completed'` | Same as above | Same |
| Stage task succeeds but apply-new inserts nothing | All staged rows already exist in `_raw_files` (steady state) | Normal — most ticks have no new files |
| Stage task is firing but `_sharepoint_changes` is always empty | Crawler hasn't bumped `refreshed_at` since the last stage tick | Check `last_delta_at` in `sharepoint_meta.sharepoint_site_drives` — if it's stale, the crawler is down or stuck |
| ACL changes in SharePoint don't propagate to `_raw_files` between content edits | Known gap **G7** — apply-modify only fires when content changes | Wait for the next content change, or schedule a periodic dummy bump on the file in SharePoint. A dedicated ACL-refresh path is the proper fix |

### 6.5 Worker / download

| Symptom | Cause | Fix |
|---|---|---|
| `provider 'ADLS2' is not supported` (in worker) | Old `ragbucket.py` without SharePoint dispatch | Update `ragbucket.py` — see [design §4](RAGBUCKET_SHAREPOINT_DESIGN.md#4-ragbucketpy-worker-download-path) |
| `download_creds must contain access_key and secret_key` | Old `submit_task_to_redis` UDF | Re-register `ragbucket.sql` (version with `credential_ref` support) |
| Worker downloads fail after a SharePoint rename | Cached `file_url` in flight | The next scan tick will repopulate `file_url` via apply-modify; in-flight tasks fail and retry |
