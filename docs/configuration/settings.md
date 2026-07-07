# Settings reference

All configuration is environment variables prefixed **`MEMCOVE_`**, loaded from the
environment or a `.env` file. The env var is `MEMCOVE_` + the field name uppercased
(e.g. `port` → `MEMCOVE_PORT`). `dict` and `list` fields are parsed as **JSON** (e.g.
`MEMCOVE_SHARED_SCHEMAS=["ref_market"]`).

The defaults target the local Docker stack, so local development needs no changes.

## MCP server

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_HOST` | str | `0.0.0.0` | bind address |
| `MEMCOVE_PORT` | int | `8090` | MCP Streamable HTTP port |

## Iceberg REST catalog

| Setting | Type | Default |
| --- | --- | --- |
| `MEMCOVE_ICEBERG_REST_URI` | str | `http://localhost:8181` |
| `MEMCOVE_ICEBERG_WAREHOUSE` | str | `s3://warehouse/` |
| `MEMCOVE_ICEBERG_CATALOG_NAME` | str | `memcove` |

## Object store (S3 / MinIO)

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_S3_ENDPOINT` | str | `http://localhost:9000` | MinIO locally; real S3 in prod |
| `MEMCOVE_S3_REGION` | str | `us-east-1` | |
| `MEMCOVE_S3_ACCESS_KEY` | str | `minio` | |
| `MEMCOVE_S3_SECRET_KEY` | str | `minio12345` | |
| `MEMCOVE_S3_PATH_STYLE` | bool | `true` | |
| `MEMCOVE_WAREHOUSE_BUCKET` | str | `warehouse` | |
| `MEMCOVE_STAGING_BUCKET` | str | `memcove-staging` | upload staging; accepts `bucket` or `bucket/sub/path` to scope uploads to a prefix |
| `MEMCOVE_ARTIFACTS_BUCKET` | str | `memcove-artifacts` | exports; accepts `bucket` or `bucket/sub/path` to scope exports to a prefix |

## Trino (read / derive / export engine)

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_TRINO_HOST` | str | `localhost` | |
| `MEMCOVE_TRINO_PORT` | int | `8080` | |
| `MEMCOVE_TRINO_USER` | str | `memcove` | service principal (connect identity when not impersonating) |
| `MEMCOVE_TRINO_CATALOG` | str | `iceberg` | |
| `MEMCOVE_TRINO_HTTP_SCHEME` | str | `http` | set `https` for a TLS-fronted Trino |
| `MEMCOVE_TRINO_IMPERSONATION` | bool | `false` | connect **as the tenant** so your Trino access control applies per tenant. See [Authentication & tenancy](auth.md#trino-impersonation-defense-in-depth). |
| `MEMCOVE_TRINO_SESSION_PROPERTIES` | dict | `{}` | resource caps etc., e.g. `{"query_max_run_time":"60s"}` |

## Arrow Flight (streaming data plane)

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_FLIGHT_HOST` | str | `0.0.0.0` | bind address |
| `MEMCOVE_FLIGHT_PORT` | int | `8815` | |
| `MEMCOVE_FLIGHT_ADVERTISE_URI` | str | `grpc://localhost:8815` | what clients are told to dial |
| `MEMCOVE_FLIGHT_TICKET_SECRET` | str | `dev-insecure-change-me` | **HMAC signing secret — override in any real deployment.** |
| `MEMCOVE_FLIGHT_TICKET_TTL_SECONDS` | int | `300` | signed ticket lifetime |

!!! warning "Change the ticket secret before you expose the Flight port"
    `MEMCOVE_FLIGHT_TICKET_SECRET` defaults to a known, insecure value. Anyone who can
    reach the Flight port with the default secret can forge tenant-scoped tickets. Set a
    strong random value in any real deployment; the Flight server logs a loud warning if
    you don't.

## Postgres registry

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_PG_DSN` | str | `postgresql://memcove:memcove@localhost:5433/memcove` | host port 5433 avoids clashing with a local pg on 5432 |

## Guardrails

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_PREVIEW_ROW_CAP` | int | `1000` | max rows returned by `query_memory` / preview |
| `MEMCOVE_INLINE_BYTES_CAP` | int | `8388608` | 8 MiB max for inline ingest |
| `MEMCOVE_EXPORT_ROW_CAP` | int | `5000000` | safety cap for artifact export |
| `MEMCOVE_PRESIGN_TTL_SECONDS` | int | `3600` | presigned URL lifetime |

## Tenancy

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_TENANT_MODE` | str | `auto` | how a caller becomes a tenant: `auto` / `shared` / `private` / `mapped` (see [tenancy modes](auth.md#how-the-tenant-is-resolved)) |
| `MEMCOVE_TENANT_HEADER` | str | `x-memcove-tenant` | `auto` dev path: client-settable tenant header |
| `MEMCOVE_DEFAULT_TENANT` | str | `default` | fallback when the header is absent |
| `MEMCOVE_SHARED_TENANT` | str | `""` | `shared` mode target; empty falls back to `DEFAULT_TENANT` |
| `MEMCOVE_TENANT_SUBJECT_HEADER` | str | `""` | e.g. `x-auth-subject`; the trusted identity for `mapped` / `private` on the proxy path |
| `MEMCOVE_TENANT_GROUP_HEADER` | str | `""` | e.g. `x-auth-groups` (comma-separated) |
| `MEMCOVE_TENANT_MAP` | dict | `{}` | `mapped` mode: subject/group → internal tenant id |

See [Authentication & tenancy](auth.md) for how these interact.

## Shared reference plane

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_SHARED_SCHEMAS` | list | `["ref_market"]` | read-only schemas every tenant may query; never rewritten to a tenant namespace |

## Ingest allowlist

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES` | list | `[]` | agent `s3_parquet` URIs must match one of these prefixes; **empty = disabled (fail closed)** |

## Full `.env.example`

The shipped example, annotated:

```bash title=".env.example"
--8<-- ".env.example"
```
