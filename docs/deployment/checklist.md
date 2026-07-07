# Production checklist

Memcove is a generic, self-hostable service — install it with the
[Helm chart](helm.md) or your own manifests, and bring your own object store, Trino,
catalog, registry DB, and (for the proxy model) identity proxy. This checklist is the
hardening you must apply before exposing it beyond a trusted network.

!!! danger "Do not expose Memcove without authentication + network isolation"
    In the default header configuration Memcove trusts the tenant header, so anything that
    can reach the port can read any tenant's data. Either enable
    [native OAuth](../configuration/auth.md#native-oauth-resource-server) or front it with
    an authenticating proxy — and in both cases restrict the network. The items below are
    not optional for an internet- or org-reachable deployment.

## Identity & trust boundary

Pick **one** authentication model and harden it:

**Native OAuth** (clients connect directly):

- [ ] **Enable the resource server** (`MEMCOVE_OAUTH_ENABLED=true`) with your IdP's issuer,
      audience, and required scopes; set `MEMCOVE_PUBLIC_URL` to the public HTTPS URL. See
      [Native OAuth](../configuration/auth.md#native-oauth-resource-server).
- [ ] **Map identities to tenants explicitly** with `MEMCOVE_TENANT_MAP` (fail-closed)
      rather than defaulting the tenant to a raw `sub` claim.

**Proxy / trusted header** (auth terminated at the edge):

- [ ] **Front Memcove with an authenticating proxy.** It authenticates the caller and sets
      the tenant/identity header. Memcove trusts that header.
- [ ] **Strip inbound tenant headers** at the proxy and overwrite from the verified
      identity, so a caller can't spoof `x-memcove-tenant`.
- [ ] **Use the fail-closed provisioning map** (`MEMCOVE_TENANT_SUBJECT_HEADER` +
      `MEMCOVE_TENANT_MAP`) rather than passing a raw OIDC `sub` through. See
      [Authentication & tenancy](../configuration/auth.md).

**Both models:**

- [ ] **Network-isolate the service** so only your proxy (header mode) or your ingress
      (OAuth mode) can reach the MCP port (8090) and the Flight port (8815). See
      [Kubernetes](kubernetes.md).

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

Secrets (S3 keys, registry DSN, ticket secret) should come from your secret manager, not
committed config — the [Helm chart](helm.md) takes them via `secrets.existingSecret`. Every
non-secret knob maps to a `MEMCOVE_*` env var (see the
[settings reference](../configuration/settings.md)).
