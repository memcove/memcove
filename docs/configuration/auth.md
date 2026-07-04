# Authentication & tenancy

Memcove does **not** validate OIDC tokens. It sits behind an authenticating proxy that
sets trusted headers, and turns those headers into an internal tenant namespace. This page
covers how that resolution works and how to wire it. Read
[Security & trust boundary](../concepts/security.md) first for the assumptions.

## How the tenant is resolved

`tenancy.resolve_tenant(headers)` decides the tenant once per request, in two modes:

### Direct header (default)

If `MEMCOVE_TENANT_SUBJECT_HEADER` is empty, Memcove trusts `MEMCOVE_TENANT_HEADER`
(default `x-memcove-tenant`), falling back to `MEMCOVE_DEFAULT_TENANT` when it's absent.
Simple, and right for local development and trusted networks. The header value is
normalized to `t_<id>`.

### Provisioning map (fail-closed)

For production, set `MEMCOVE_TENANT_SUBJECT_HEADER` (e.g. `x-auth-subject`) and provide
`MEMCOVE_TENANT_MAP`. Memcove maps the verified identity — the subject, or a matching
group from `MEMCOVE_TENANT_GROUP_HEADER` — through the map to an internal tenant id:

```bash
MEMCOVE_TENANT_SUBJECT_HEADER=x-auth-subject
MEMCOVE_TENANT_GROUP_HEADER=x-auth-groups
MEMCOVE_TENANT_MAP={"oidc|abc123":"acme","team-research":"research"}
```

This mode is **fail-closed**: an identity absent from the map is **rejected** — it never
falls back to the raw tenant header. That's what stops a raw OIDC `sub` from being used
directly as a namespace, and stops a caller from self-selecting a tenant.

!!! tip
    Use the provisioning map (or map identity → tenant at the proxy) rather than passing a
    raw `sub` through. Keep the mapping small and explicit.

## Trino impersonation (defense-in-depth)

The tenant resolution above plus the [SQL guard](../concepts/isolation.md) are the primary
isolation layer. You can add a second, independent layer with
`MEMCOVE_TRINO_IMPERSONATION=true`: each data query then connects to Trino **as the
tenant**, so Trino's own access control applies per tenant even if the guard were ever
bypassed.

Memcove does not implement grants — you choose and configure the backend (Trino file
rules, Ranger, OPA, or Iceberg REST authz). Requirements:

- The service principal (`MEMCOVE_TRINO_USER`) must be allowed to impersonate tenants.
- Each tenant principal needs read on its own schema and the shared schemas, and write on
  its own schema (derive's CTAS runs as the tenant).
- Schema creation stays on the service principal (provisioning is privileged).

With impersonation off (the default), Memcove uses the single service identity — right for
local/dev where Trino has no authz.

## Header hygiene

Whichever mode you use, the proxy must **overwrite** the identity/tenant header from the
verified identity and **strip** any client-supplied copy, so a caller cannot spoof it. Pair
this with network isolation so only the proxy can reach Memcove — see
[Kubernetes](../deployment/kubernetes.md).
