# Quick-Start Examples

Copy-pastable end-to-end setup for the two common pathways.

Replace `<placeholder>` values with yours. Keep object names (`sp_creds`, `azure_dest`, etc.) consistent across the steps within a pathway.

---

## Pathway A — Azure source → Azure destination

### 1. Source storage volume

```sql
CREATE STORAGE VOLUME az_src
TYPE = AZBLOB
LOCATIONS = ("azblob://<source-container>/")
PROPERTIES (
    "azure.blob.storage_account" = "<source-account>",
    "azure.blob.shared_key"      = "<source-key>"
);
```

### 2. Destination storage volume

```sql
CREATE STORAGE VOLUME az_dest
TYPE = AZBLOB
LOCATIONS = ("azblob://<dest-container>/")
PROPERTIES (
    "azure.blob.storage_account" = "<dest-account>",
    "azure.blob.shared_key"      = "<dest-key>"
);
```

### 3. RagBucket

```sql
CREATE RAGBUCKET az_demo
FROM 'wasbs://<source-container>@<source-account>.blob.core.windows.net/'
PROPERTIES (
    "storage_volume"      = "az_src",
    "metadata_interval"   = "30 SECOND",
    "rate_limit"          = "5",
    "llm_provider"        = "openai",
    "llm_model"           = "gpt-4o-mini",
    "dest_storage_volume" = "az_dest",
    "dest_url"            = "wasbs://<dest-container>@<dest-account>.blob.core.windows.net/"
);
```

### 4. Ontology view

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

### 5. Smoke-check

```sql
SELECT TASK_NAME FROM information_schema.tasks WHERE TASK_NAME LIKE 'az_demo%';   -- expect 5
SELECT file_name, status FROM az_demo_raw_files;
SELECT status, COUNT(*) FROM az_demo_ont GROUP BY status;
```

---

## Pathway B — SharePoint source → Azure destination

### 1. Source storage volume (ADLS2 / OAuth2)

```sql
CREATE STORAGE VOLUME sp_creds
TYPE = ADLS2
LOCATIONS = ("adls2://placeholder/")
PROPERTIES (
    "azure.adls2.oauth2_tenant_id"       = "<tenant-id>",
    "azure.adls2.oauth2_client_id"       = "<client-id>",
    "azure.adls2.oauth2_client_secret"   = "<client-secret>",
    "azure.adls2.oauth2_client_endpoint" = "https://placeholder"
);
```

`LOCATIONS` and `oauth2_client_endpoint` values are unused for SharePoint — any non-colliding placeholder is fine.

### 2. Destination storage volume

```sql
CREATE STORAGE VOLUME azure_dest
TYPE = AZBLOB
LOCATIONS = ("azblob://<dest-container>/")
PROPERTIES (
    "azure.blob.storage_account" = "<dest-account>",
    "azure.blob.shared_key"      = "<dest-key>"
);
```

### 3. Register the SharePoint site

```sql
CREATE SHAREPOINT_SITE demosite
PROPERTIES (
    "site_url"   = "https://<host>.sharepoint.com/sites/<sitename>",
    "full_cron"  = "0 2 * * *",
    "delta_cron" = "5 0-1,3-23 * * *",
    "run_now"    = "true"
);

SHOW SHAREPOINT_SITES;
```

Wait for the crawler to populate `sharepoint_meta.sys_sharepoint_files` for `site_id = 'demosite'`:

```sql
SELECT COUNT(*) FROM sharepoint_meta.sys_sharepoint_files WHERE site_id = 'demosite';
```

### 4. RagBucket

```sql
CREATE RAGBUCKET sp_demo
FROM 'sharepoint://demosite/'
PROPERTIES (
    "storage_volume"      = "sp_creds",
    "source_site_id"      = "demosite",
    "metadata_interval"   = "30 SECOND",
    "rate_limit"          = "5",
    "llm_provider"        = "openai",
    "llm_model"           = "gpt-4o-mini",
    "dest_storage_volume" = "azure_dest",
    "dest_url"            = "wasbs://<dest-container>@<dest-account>.blob.core.windows.net/"
);
```

### 5. Ontology view

```sql
CREATE ONTOLOGY VIEW sp_demo_ont (
    Summary TEXT AS 'Summarize the document'
)
ON RAGBUCKET sp_demo
USING ''
PROPERTIES (
    "rate_limit"     = "5",
    "time_interval"  = "30 SECOND",
    "llm_provider"   = "openai",
    "llm_model"      = "gpt-4o-mini"
);
```

### 6. Soft-delete cascade

```sql
REFRESH ONTOLOGY VIEW WITH ASYNC MODE ON;
```

Enables the `OntologyRefreshDaemon` (resets on FE restart — re-run after every restart).

### 7. Smoke-check

```sql
SELECT TASK_NAME FROM information_schema.tasks WHERE TASK_NAME LIKE 'sp_demo%';   -- expect 8
SELECT file_name, status, deleted_at FROM sp_demo_raw_files;
SELECT status, COUNT(*) FROM sp_demo_ont GROUP BY status;
```

---

## Common gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Only 4 (S3/Azure) or 7 (SharePoint) tasks created | `s3_medtadata_insert` failed at SUBMIT TASK time | Check FE log for the bucket name — usually a wrong storage-volume account name or the source path glob is wrong (drop the trailing `/*` from `FROM`, let FE add the glob) |
| `Failed to glob list blobs` | Storage volume account name doesn't match the URL host | Create a storage volume whose `azure.blob.storage_account` matches the URL's account |
| `No files were found matching the pattern(s)` | Source path glob points to wrong layout | Files at container root → use `wasbs://container@account.../` (no trailing `*`). Files in subfolder → `wasbs://container@account.../<subfolder>/` |
| `dest_url and dest_storage_volume PROPERTIES must be set together` | Only one of the two was provided | Provide both, or omit both (for legacy fe.conf fallback) |
| `dest_url '...' scheme doesn't match AZBLOB/ADLS2 volume` | URL scheme and volume type disagree | `wasbs://` with AZBLOB/ADLS2; `s3://` with S3 |
| `Storage volume not found: <name>` | Typo or volume was never created | `SHOW STORAGE VOLUMES;` to confirm |
| SharePoint: `source_site_id '<id>' is not registered` | Site missing from `sharepoint_meta.sharepoint_sites` | Run `CREATE SHAREPOINT_SITE <id> PROPERTIES(...)` first |

---

## Cleanup template

When dropping a bucket — `RagBucketMgr` entry persists, so the bucket **name is gone for reuse**; pick a new name next time.

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
