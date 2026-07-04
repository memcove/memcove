# Security & trust boundary

Memcove's isolation model has one assumption you must uphold when you deploy it:
**Memcove trusts the tenant identity in its request headers.** It does not validate OIDC
tokens itself. That is safe only when an authenticating proxy sits in front and nothing
else can reach the service.

Read this before exposing Memcove beyond localhost.

## The trust boundary

In production, an authenticating reverse proxy (your OIDC proxy) authenticates the caller
and sets a trusted header — `MEMCOVE_TENANT_HEADER` (default `x-memcove-tenant`), or an
identity header consumed by the provisioning map. Memcove reads that header and scopes
every operation to the resulting tenant.

This is safe **only if**:

1. **Network isolation** — only the proxy can reach the MCP port (8090) and the Flight
   port (8815). Trino should be similarly restricted so impersonation can't be sidestepped.
   See `deploy/networkpolicy.example.yaml` and [Kubernetes](../deployment/kubernetes.md).
2. **Header hygiene** — the proxy overwrites the tenant header from the verified identity
   and strips any client-supplied copy, so a caller cannot spoof it.
3. **Identity mapping** — don't feed a raw OIDC `sub` straight through as a namespace.
   Map it to an internal tenant id (see [Authentication & tenancy](../configuration/auth.md)).

!!! danger "Default header mode is only for trusted networks"
    With no proxy, anything that can reach the port can set `x-memcove-tenant` to any
    value and read that tenant's data. Fine on localhost, unacceptable on an exposed
    port. For safe local work see [Local development (no proxy)](../configuration/local-dev.md).

## Layers of isolation

Memcove enforces isolation in application code, and can defer to your engine's access
control as an independent second layer.

1. **SQL guard (always on).** Rewrites/validates all SQL so a tenant can only reach its
   own namespace plus the shared read-only plane; metadata schemas and unclassifiable
   references are rejected. See [Tenant isolation & the SQL guard](isolation.md).
2. **Trino principal (optional, recommended for prod).** With
   `MEMCOVE_TRINO_IMPERSONATION=true`, each query connects to Trino *as the tenant*, so
   your Trino access-control backend (file rules, Ranger, OPA, Iceberg REST authz) applies
   per tenant even if the guard were ever bypassed. Memcove doesn't implement grants — you
   choose and configure the backend.

## Provisioning map (fail-closed)

When `MEMCOVE_TENANT_SUBJECT_HEADER` is set, Memcove maps the verified identity
(subject, or a matching group) through `MEMCOVE_TENANT_MAP` to an internal tenant id. This
mode is **fail-closed**: an identity absent from the map is rejected outright — it never
falls back to the client-settable tenant header. This is the seam that keeps a raw OIDC
`sub` from becoming a namespace. Details in [Authentication & tenancy](../configuration/auth.md).

## Streaming: signed tickets

The Arrow Flight gRPC surface is not covered by the MCP-port proxy, so its tickets and
descriptors are **HMAC-signed and short-lived**. The control plane mints a signed ticket
(`tickets.sign`), and the Flight server verifies the signature and expiry
(`tickets.verify`) before serving — a client cannot forge a ticket for another tenant.

Set `MEMCOVE_FLIGHT_TICKET_SECRET` to a strong random value in any real deployment; the
default is intentionally insecure and the Flight server logs a loud warning if it is left
unchanged. Tickets expire after `MEMCOVE_FLIGHT_TICKET_TTL_SECONDS` (default 300).

## The write surface

The entire write surface is two code paths — `catalog.write_arrow` and the Trino CTAS in
`derive_object` — both fed only validated labels and guard-rewritten SQL. Agent-supplied
`s3_parquet` ingest is gated by an allowlist (`MEMCOVE_ALLOWED_S3_INGEST_PREFIXES`,
disabled/fail-closed by default) to prevent a confused-deputy read of arbitrary buckets.

## Production hardening

See the [Production checklist](../deployment/checklist.md) for the full list. In short:
set the Flight ticket secret, enable Trino impersonation with a grant backend, set the S3
ingest allowlist, apply the NetworkPolicy, TLS-front Trino, and strip inbound tenant
headers at the proxy.
