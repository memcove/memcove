# Production checklist

Memcove ships no opinionated cluster manifest — it's a generic, self-hostable service. You
bring your own object store, Trino, catalog, Postgres, and identity proxy. This checklist
is the hardening you must apply before exposing it beyond a trusted network.

## Identity & trust boundary

- [ ] **Front Memcove with an authenticating proxy.** It authenticates the caller and sets
      the tenant/identity header. Memcove trusts that header.
- [ ] **Network-isolate the service** so *only* the proxy can reach the MCP port (8090) and
      the Flight port (8815). See [Kubernetes](kubernetes.md).
- [ ] **Strip inbound tenant headers** at the proxy and overwrite from the verified
      identity, so a caller can't spoof `x-memcove-tenant`.
- [ ] **Use the fail-closed provisioning map** (`MEMCOVE_TENANT_SUBJECT_HEADER` +
      `MEMCOVE_TENANT_MAP`) rather than passing a raw OIDC `sub` through. See
      [Authentication & tenancy](../configuration/auth.md).

## Engine access control

- [ ] **Enable Trino impersonation** (`MEMCOVE_TRINO_IMPERSONATION=true`) and configure a
      grant backend (file rules / Ranger / OPA / Iceberg REST authz) so Trino re-checks
      access per tenant beneath the guard.
- [ ] **Restrict Trino** so only Memcove can reach it (otherwise impersonation is
      sidesteppable).
- [ ] **TLS-front Trino** and set `MEMCOVE_TRINO_HTTP_SCHEME=https`.
- [ ] Consider `MEMCOVE_TRINO_SESSION_PROPERTIES` resource caps (e.g.
      `query_max_run_time`) to bound runaway queries.

## Data-plane secrets

- [ ] **Set `MEMCOVE_FLIGHT_TICKET_SECRET`** to a strong random value (the default is
      insecure; the Flight server warns if unchanged).
- [ ] **Use real S3 credentials / IAM**, not the MinIO dev defaults, and least-privilege
      bucket policies for the warehouse / staging / artifacts buckets.
- [ ] **Secure the Postgres DSN** (`MEMCOVE_PG_DSN`) and pull it from a secret manager.

## Ingest & buckets

- [ ] **Set `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES`** if you want to allow agent
      `s3_parquet` ingest (empty = disabled). Scope prefixes tightly.
- [ ] Review the guardrail caps (`MEMCOVE_PREVIEW_ROW_CAP`, `MEMCOVE_EXPORT_ROW_CAP`,
      `MEMCOVE_INLINE_BYTES_CAP`, `MEMCOVE_PRESIGN_TTL_SECONDS`) for your workload.

## Observability

- [ ] Route the `memcove.audit` logger (structured JSON, one line per accepted
      read/derive/export) to your audit sink.

Secrets (S3 keys, PG password, ticket secret) should come from your secret manager, not
committed config. The `deploy/values.example.yaml` file maps every knob to a
`MEMCOVE_*` env var for wiring into Helm / ArgoCD / Kustomize.
