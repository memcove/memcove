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
| `MEMCOVE_S3_ACCESS_KEY` | str \| None | `minio` | **clear (empty) to use the AWS default credential chain** — IRSA / instance profile / STS |
| `MEMCOVE_S3_SECRET_KEY` | str \| None | `minio12345` | clear alongside the access key for keyless auth |
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

## Registry (Postgres / SQLite / MySQL)

The metadata registry runs on any of three backends, selected by the DSN scheme. See
[registry backends](../concepts/architecture.md) for the trade-offs.

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_REGISTRY_DSN` | str \| None | `None` | takes precedence over `PG_DSN`. Scheme selects the backend: `postgresql://…`, `sqlite:///memcove.db` (or `sqlite://` in-memory), `mysql://…` (needs the `mysql` extra) |
| `MEMCOVE_PG_DSN` | str | `postgresql://memcove:memcove@localhost:5433/memcove` | fallback when `REGISTRY_DSN` is unset; host port 5433 avoids clashing with a local pg on 5432 |
| `MEMCOVE_PG_POOL_MIN_SIZE` | int | `1` | connections kept warm (Postgres pool) |
| `MEMCOVE_PG_POOL_MAX_SIZE` | int | `10` | pool grows to this under load |
| `MEMCOVE_PG_POOL_TIMEOUT` | float | `10.0` | seconds to wait for a free connection before erroring |

## Reconciler / self-healing

The reconciler diffs the Iceberg catalog against the registry to backfill missing rows and
drop dangling ones; deletion is fail-safe. Tune the guardrails:

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_RECONCILE_MIN_ABSENT_SWEEPS` | int | `2` | a row must be absent across this many consecutive sweeps before deletion |
| `MEMCOVE_RECONCILE_DELETION_CAP_RATIO` | float | `0.25` | a sweep that would delete more than this fraction of a namespace aborts + alerts |
| `MEMCOVE_RECONCILE_DELETION_CAP_MIN` | int | `3` | the ratio cap only applies once a sweep would delete more than this many rows |

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
| `MEMCOVE_TENANT_HEADER` | str | `x-memcove-tenant` | direct-mode tenant header |
| `MEMCOVE_DEFAULT_TENANT` | str | `default` | fallback when the header is absent |
| `MEMCOVE_TENANT_SUBJECT_HEADER` | str | `""` | e.g. `x-auth-subject`; enables the fail-closed provisioning map |
| `MEMCOVE_TENANT_GROUP_HEADER` | str | `""` | e.g. `x-auth-groups` (comma-separated) |
| `MEMCOVE_TENANT_MAP` | dict | `{}` | subject/group → internal tenant id |

See [Authentication & tenancy](auth.md) for how these interact.

## Native OAuth 2.1 (resource server)

Off by default (trusted-header / proxy model). When enabled, Memcove validates bearer JWTs
itself against the IdP's JWKS so a client like Claude can connect directly — see
[Native OAuth](auth.md#native-oauth-resource-server).

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_OAUTH_ENABLED` | bool | `false` | turn on the native resource server |
| `MEMCOVE_OAUTH_ISSUER` | str | `""` | IdP issuer URL, e.g. `https://keycloak.example.com/realms/memcove` |
| `MEMCOVE_OAUTH_JWKS_URI` | str | `""` | optional; derived from the issuer's OIDC discovery when empty |
| `MEMCOVE_OAUTH_AUDIENCE` | str | `""` | expected `aud`; **empty = skip the audience check.** Set to match your IdP's token audience |
| `MEMCOVE_OAUTH_REQUIRED_SCOPES` | list | `[]` | scopes every token must carry, e.g. `["memcove.use"]` |
| `MEMCOVE_OAUTH_ALGORITHMS` | list | `["RS256"]` | accepted JWT signing algorithms |
| `MEMCOVE_OAUTH_TENANT_CLAIM` | str | `sub` | claim used as the tenant id when the identity isn't in `TENANT_MAP` (map wins, fail-closed, when set) |
| `MEMCOVE_PUBLIC_URL` | str | `""` | this server's public URL (OAuth resource id); falls back to `http://host:port` |

## Shared reference plane

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_SHARED_SCHEMAS` | list | `["ref_market"]` | read-only schemas every tenant may query; never rewritten to a tenant namespace |

## Scratchpad plane

An optional fast, small, **ephemeral** store backed by DuckDB *behind Trino* — see
[Scratchpad](../concepts/scratchpad.md). Off by default; needs Trino ≥ 480.

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_SCRATCH_ENABLED` | bool | `false` | enable `target=scratch` and the `scratch.<label>` SQL alias |
| `MEMCOVE_SCRATCH_CATALOG_MODE` | str | `shared` | `shared` (one static DuckDB catalog, schema-per-tenant) or `per_tenant` (a `scratch_<tenant>` catalog via Trino dynamic catalog management) |
| `MEMCOVE_SCRATCH_CATALOG` | str | `scratch` | catalog name in `shared` mode |
| `MEMCOVE_SCRATCH_CATALOG_PREFIX` | str | `scratch` | `per_tenant`: catalog is `<prefix>_<tenant>` |
| `MEMCOVE_SCRATCH_DUCKDB_DIR` | str | `/data/scratch` | `per_tenant`: Trino-side dir (on a shared volume) for the `<tenant>.duckdb` files |

## Ingest allowlist

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES` | list | `[]` | agent `s3_parquet` URIs must match one of these prefixes; **empty = disabled (fail closed)** |

## Full `.env.example`

The shipped example, annotated:

```bash title=".env.example"
--8<-- ".env.example"
```
