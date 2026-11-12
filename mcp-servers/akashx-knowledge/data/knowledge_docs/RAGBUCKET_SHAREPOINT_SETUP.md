# SharePoint RagBucket — Setup

Companion doc to [RAGBUCKET_SHAREPOINT.md](RAGBUCKET_SHAREPOINT.md). Covers everything you need to do once before SharePoint ragbuckets work end-to-end: SharePoint app registration, storage volume, FE/BE config, and crawler deployment.

---

## 1. Microsoft Entra ID (Azure AD) app registration

Register an application in Entra ID with the following **Application** (not Delegated) permissions:

| Permission | Type | Purpose |
|---|---|---|
| `Sites.Read.All` | Application | Resolve site IDs, list drives, walk folders |
| `Files.Read.All` | Application | Download file contents during ingestion |

Grant admin consent after adding permissions. Capture the tenant ID, client ID, and client secret — you'll wire them into the storage volume next.

---

## 2. Storage volume (ADLS2)

SharePoint OAuth2 client credentials are stored in a StarRocks Storage Volume of type `ADLS2`. The ADLS2 type is reused because it carries the same `tenant_id` / `client_id` / `client_secret` schema needed for the Graph API client-credentials flow — only the OAuth2 scope at token-request time differs.

```sql
CREATE STORAGE VOLUME sp_creds
TYPE = ADLS2
LOCATIONS = ("adls2://dummy")
PROPERTIES (
  "azure.adls2.oauth2_tenant_id"     = "<your-tenant-id>",
  "azure.adls2.oauth2_client_id"     = "<your-client-id>",
  "azure.adls2.oauth2_client_secret" = "<your-client-secret>",
  "azure.adls2.oauth2_client_endpoint" = "https://dummy"
);
```

`LOCATIONS` and `oauth2_client_endpoint` are required by the ADLS2 schema but ignored for SharePoint — provide dummy values.

---

## 3. FE / BE environment variables

After the crawler removal there's no BE-side Java UDTF that needs `FE_HTTP_URL` / `FE_INTERNAL_TOKEN`. The dsRAG worker still does (see §4).

**`akashxdb/conf/fe.conf`:**
```
internal_api_token = <strong-random-secret>
```

This is consumed by the worker for credential lookups.

---

## 4. dsRAG worker environment

The dsRAG worker downloads SharePoint files at processing time. It uses the same FE internal API for credential resolution.

**`dsRAG/.env`:**
```
FE_HTTP_URL=http://host.docker.internal:8030
FE_INTERNAL_TOKEN=<same secret as fe.conf>
```

`host.docker.internal` is used because the worker runs inside a Docker container and needs to reach the FE on the host. (BE uses `localhost` because BE and FE share a container/host.)

---

## 5. Docker network

The dsRAG container must be able to reach the FE. If they're on separate Docker networks:

```bash
docker network connect dsrag_default akashxdb
```

This must be re-run after every `akashxdb` container restart. Alternatively use `host.docker.internal` in `FE_HTTP_URL` (works on macOS Docker Desktop without `network connect`).

---

## 6. Deploy the external crawler

File discovery happens in the external `akashx-sharepoint-sync` Python daemon, not in the BE. Deploy it once per akashxdb instance — see [`akashx-sharepoint-sync/README.md`](../../../akashx-sharepoint-sync/README.md) for full deployment modes (dev / production-from-git / production-from-ACR-image).

Short form:

```bash
# Apply the catalog DDL (one-time, per akashxdb instance)
mysql -h <fe-host> -P 9030 -u root -e "CREATE DATABASE IF NOT EXISTS sharepoint_meta;"
mysql -h <fe-host> -P 9030 -u root sharepoint_meta < akashx-sharepoint-sync/ddl/001_sys_sharepoint_files.sql
mysql -h <fe-host> -P 9030 -u root sharepoint_meta < akashx-sharepoint-sync/ddl/002_sharepoint_sites.sql
mysql -h <fe-host> -P 9030 -u root sharepoint_meta < akashx-sharepoint-sync/ddl/003_sync_rules.sql
mysql -h <fe-host> -P 9030 -u root sharepoint_meta < akashx-sharepoint-sync/ddl/005_sharepoint_site_drives.sql

# Start the crawler container (uses akashxdb connection + SP OAuth creds from secrets/)
docker compose -f akashx-sharepoint-sync/docker/docker-compose.yml up -d
```

The crawler reads `sharepoint_sites` for registered sites and walks Graph on the configured cron schedule. Per-site registration happens via `CREATE SHAREPOINT_SITE` (see [USAGE](RAGBUCKET_SHAREPOINT_USAGE.md)).

---

## 7. Register the standard RagBucket UDFs

```sql
-- Run: dsRAG/UDF_SQL_SCRIPTS/ragbucket.sql
-- Registers: submit_task_to_redis, check_redis_task_status, ragbucket_redis_dk_udf
```

The `submit_task_to_redis` UDF must be the version that supports `credential_ref` (checks for `credential_ref` before requiring `access_key`/`secret_key`).

No SharePoint-specific UDTF is needed any more — the legacy `sharepoint_files` Java UDTF was removed when discovery moved to the external crawler.

---

## 8. Verify the install

```sql
-- The crawler should have created the catalog table
SHOW CREATE TABLE sharepoint_meta.sys_sharepoint_files;

-- Register a site (replace site_url with your SharePoint URL)
CREATE SHAREPOINT_SITE demosite
PROPERTIES (
    "site_url"   = "https://akashxsharepoint.sharepoint.com/sites/DemoSite",
    "full_cron"  = "0 2 * * *",
    "delta_cron" = "5 0-1,3-23 * * *",
    "run_now"    = "true"
);

-- Wait for the first crawl to land. After it does:
SELECT site_id, COUNT(*) FROM sharepoint_meta.sys_sharepoint_files
WHERE site_id = 'demosite' GROUP BY site_id;
```

If you get a row count, the install is good. If not, check the crawler container logs and see [troubleshooting in the usage doc](RAGBUCKET_SHAREPOINT_USAGE.md#troubleshooting).
