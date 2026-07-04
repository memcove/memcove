# Local development (no proxy)

You do **not** need an OIDC proxy to develop against Memcove. The proxy is the production
trust boundary, not a runtime dependency — Memcove never talks to it or validates tokens.
In development you set the tenant header yourself, or omit it.

## Default mode just works

With `MEMCOVE_TENANT_SUBJECT_HEADER` empty (the default), `resolve_tenant` trusts
`x-memcove-tenant` and falls back to `MEMCOVE_DEFAULT_TENANT` (`default`) when it's absent.
So:

- **Single-tenant play** — run the server, send no tenant header, everything lands in
  `t_default`. Zero setup.
- **Multi-tenant play** — set the header per request to simulate different callers:

  ```text
  x-memcove-tenant: acme   ->  t_acme
  x-memcove-tenant: beta   ->  t_beta
  ```

  The header *is* the tenant in dev. You can prove the isolation guard works just by
  flipping the header and watching a cross-tenant `SELECT` get rejected.

## Prod-only knobs stay off locally

The production hardening settings all default to off, so local dev needs no auth
infrastructure:

- `MEMCOVE_TRINO_IMPERSONATION=false` — a single Trino service identity, no grant backend.
- `MEMCOVE_TENANT_SUBJECT_HEADER=""` — no provisioning map; the direct header is used.
- `MEMCOVE_FLIGHT_TICKET_SECRET` — uses the dev default (logs a warning, still works).
- `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES=[]` — agent `s3_parquet` ingest is disabled; use
  `inline` or `upload_handle` sources while playing.

## Exercising the production identity path — still no proxy

To test the fail-closed provisioning map without a real proxy, just set the headers
yourself:

```bash
MEMCOVE_TENANT_SUBJECT_HEADER=x-auth-subject
MEMCOVE_TENANT_MAP={"alice":"acme"}
```

Then send `x-auth-subject: alice` → `t_acme`. Send an unmapped subject → the request is
rejected (fail closed). No proxy involved.

## The one caveat

!!! danger "Don't expose the port without a proxy"
    In default header mode, anything that can reach the MCP port can set
    `x-memcove-tenant` to any value and read that tenant's data. That is fine on
    `localhost`. It is **not** fine on an exposed port — which is exactly what the
    production proxy plus a NetworkPolicy enforce. See
    [Security & trust boundary](../concepts/security.md) and
    [Kubernetes](../deployment/kubernetes.md).
