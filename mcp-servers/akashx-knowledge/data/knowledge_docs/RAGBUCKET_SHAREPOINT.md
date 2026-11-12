# SharePoint Integration — RagBucket + Ontology View

SharePoint file processing is a two-system pipeline. The **external crawler** (`akashx-sharepoint-sync`) walks SharePoint via Microsoft Graph and writes file metadata + ACL into the central catalog `sharepoint_meta.sys_sharepoint_files`. **RagBucket** then reads from that catalog for its chunks/embeddings pipeline, and **Ontology View** reads from RagBucket (or from a user base table populated by `SP_FILES()`) to run LLM extraction.

```
SharePoint
   ↓ (Microsoft Graph API)
[crawler daemon — external Python process]
   ↓ writes
sharepoint_meta.sys_sharepoint_files   (per-file row + ACL, partitioned by site_id)
   ↓ (read by both consumers)
   ├──→ RagBucket stage task  →  _raw_files (with ACL)  →  chunks + HNSW
   └──→ SP_FILES() sync rule  →  user base table       →  Ontology View
```

The same catalog feeds both consumer paths. RagBucket-backed ontology views inherit ACL columns; LINKS- and COLUMN-mode views inherit them via the user's sync rule.

---

## Documentation map

| Doc | Read this if you want to… |
|---|---|
| [**RAGBUCKET_SHAREPOINT_SETUP.md**](RAGBUCKET_SHAREPOINT_SETUP.md) | …get a fresh deployment working — Entra ID app, ADLS2 storage volume, crawler setup, BE/FE config |
| [**RAGBUCKET_SHAREPOINT_USAGE.md**](RAGBUCKET_SHAREPOINT_USAGE.md) | …register a site, create ragbuckets, run queries, or diagnose problems |
| [**RAGBUCKET_SHAREPOINT_DESIGN.md**](RAGBUCKET_SHAREPOINT_DESIGN.md) | …understand the implementation — crawler ↔ RagBucket boundary, stage/apply pipeline, ACL flow, scenario coverage |

If you've never touched this before: read **setup** → **usage**, then dip into **design** when something surprises you.

---

## Quick reference

**Register a SharePoint site** (one-time, per site):
```sql
CREATE SHAREPOINT_SITE demosite
PROPERTIES (
    "site_url"   = "https://akashxsharepoint.sharepoint.com/sites/DemoSite",
    "full_cron"  = "0 2 * * *",
    "delta_cron" = "5 0-1,3-23 * * *"
);
```

**Path format for RagBucket logical path:**
```
sharepoint://<site_id>/
```
The `<site_id>` matches the registered site name above. RagBucket binds to the site by id, not by URL parsing.

**Minimal `CREATE RAGBUCKET`:**
```sql
CREATE RAGBUCKET sp_testrag
FROM 'sharepoint://demosite/'
PROPERTIES (
  'storage_volume'      = 'sp_creds',
  'source_site_id'      = 'demosite',
  'metadata_interval'   = '300 SECOND',
  'rate_limit'          = '5',
  'dest_storage_volume' = 'minio_dest',
  'dest_url'            = 's3://akashx-ragbucket/'
  -- 'has_acl' = 'false'   -- optional; defaults 'true' for SharePoint
);
```

**Notes:**
- `source_site_id` is required and validated at create time. Register the site first.
- `has_acl` defaults: `true` for SharePoint, `false` for S3/Azure. Explicit `true` on non-SharePoint is rejected. `ON RAGBUCKET` ontology views auto-derive from the bucket.
- `dest_storage_volume` + `dest_url` together specify where parquet chunks land. See [RAGBUCKET.md → Destination](RAGBUCKET.md#destination-where-dsrag-writes-parquet-chunks) for S3 / MinIO / Azure setup.

**Per-bucket tables created:**

| Table | Purpose |
|---|---|
| `<prefix>_raw_files` | File inventory + status. **16 cols on S3/Azure; 17 with SharePoint `has_acl=false` (adds `drive_item_id`); 24 with SharePoint `has_acl=true` (adds 5 ACL cols + `last_modified_at` + `refreshed_at`).** |
| `<prefix>_sharepoint_changes` | Staging buffer; PK on `drive_item_id` |
| `<prefix>_metadata`, `<prefix>_chunks`, `<prefix>_intermediate_table` | Same as S3/Azure ragbuckets |

**Per-bucket scan tasks (SharePoint-specific):**
- `<prefix>_sharepoint_stage` — reads `sys_sharepoint_files` for this site_id, anti-joins for deletes, writes staging
- `<prefix>_sharepoint_apply_new` — INSERTs new files into `_raw_files` with ACL
- `<prefix>_sharepoint_apply_modify` — UPDATEs on watermark; handles renames; refreshes ACL (COALESCE-guarded)
- `<prefix>_sharepoint_apply_delete` — soft-deletes via `deleted_at = NOW()`

For the why behind this shape, see [design §1 Pipeline overview](RAGBUCKET_SHAREPOINT_DESIGN.md#1-pipeline-overview).

---

## Key files

| Component | Path |
|---|---|
| External crawler (Python daemon) | [`akashx-sharepoint-sync/`](../../../akashx-sharepoint-sync/) |
| Catalog DDL (`sys_sharepoint_files`) | [`akashx-sharepoint-sync/ddl/001_sys_sharepoint_files.sql`](../../../akashx-sharepoint-sync/ddl/001_sys_sharepoint_files.sql) |
| Site registry DDL | [`akashx-sharepoint-sync/ddl/002_sharepoint_sites.sql`](../../../akashx-sharepoint-sync/ddl/002_sharepoint_sites.sql) |
| `CREATE/ALTER/DROP SHAREPOINT_SITE` executor | [`fe/fe-core/src/main/java/com/starrocks/qe/SharepointSiteExecutor.java`](../../fe/fe-core/src/main/java/com/starrocks/qe/SharepointSiteExecutor.java) |
| RagBucket Executor | [`fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java`](../../fe/fe-core/src/main/java/com/starrocks/qe/RagBucketExecutor.java) |
| RagBucket route (worker) | [`dsRAG/routes/ragbucket.py`](../../../dsRAG/routes/ragbucket.py) |
| SharePoint download backend | [`dsRAG/Dependencies/storage.py`](../../../dsRAG/Dependencies/storage.py) (class `SharePointStorage`) |
| FE storage credential endpoint | [`fe/fe-core/src/main/java/com/starrocks/http/rest/StorageCredentialAction.java`](../../fe/fe-core/src/main/java/com/starrocks/http/rest/StorageCredentialAction.java) |

## Related docs

- [RAGBUCKET.md](RAGBUCKET.md) — base RagBucket docs (S3/Azure)
- [ONTOLOGY.md](ONTOLOGY.md) — Ontology View pipeline
- [SOFT_DELETE.md](SOFT_DELETE.md) — `deleted_at` cascade for SharePoint (chunks + OV cleanup)
- [RAGBUCKET_SHAREPOINT_DESIGN.md](RAGBUCKET_SHAREPOINT_DESIGN.md) — implementation reference
- [RAGBUCKET_SHAREPOINT_USAGE.md](RAGBUCKET_SHAREPOINT_USAGE.md) — operator walkthrough
- [Credential Resolution system.pdf](Credential%20Resolution%20system.pdf) — design of the FE-resolved-credentials pattern this builds on
