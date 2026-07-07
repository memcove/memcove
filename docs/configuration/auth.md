# Authentication & tenancy

Memcove supports **two authentication models**, and in both cases resolves the request to a
single internal tenant namespace that everything downstream keys on. Read
[Security & trust boundary](../concepts/security.md) first for the assumptions.

1. **Trusted-header / proxy** (default) — Memcove sits behind an authenticating proxy that
   validates OIDC and sets trusted headers; Memcove maps those to a tenant. Simplest, and
   right when you already terminate auth at the edge.
2. **Native OAuth** — Memcove itself validates bearer JWTs against your IdP's JWKS, so an
   MCP client (Claude, MCP Inspector) connects **directly**, no proxy required. See
   [Native OAuth](#native-oauth-resource-server) below.

## How the tenant is resolved

In proxy mode, `tenancy.resolve_tenant(headers)` decides the tenant once per request, in two
modes:

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

## Native OAuth (resource server)

Set `MEMCOVE_OAUTH_ENABLED=true` and Memcove becomes an OAuth 2.1 **resource server**: it
serves `/.well-known/oauth-protected-resource`, validates the bearer JWT on every request
(signature via the IdP's JWKS, plus issuer, audience, expiry, and required scopes), and
returns a `401` with a `WWW-Authenticate` challenge when the token is missing or invalid.
The authorization dance (login, consent, client registration) stays with your IdP —
Memcove only verifies the token it issues.

```bash
MEMCOVE_OAUTH_ENABLED=true
MEMCOVE_OAUTH_ISSUER=https://keycloak.example.com/realms/memcove
MEMCOVE_OAUTH_AUDIENCE=memcove          # expected `aud`; empty to skip the check
MEMCOVE_OAUTH_REQUIRED_SCOPES=["memcove.use"]
MEMCOVE_PUBLIC_URL=https://memcove.example.com   # advertised resource identifier
```

The JWKS URI is discovered from the issuer's OIDC metadata; set `MEMCOVE_OAUTH_JWKS_URI` to
pin it explicitly. It's provider-agnostic — any OIDC IdP works. See
[`deploy/keycloak/`](https://github.com/memcove/memcove/tree/main/deploy/keycloak) for a
runnable Keycloak worked example (the default) with a realm, audience mapper, and a smoke
test.

**Tenant from claims.** The verified token maps to a tenant the same fail-closed way as
proxy mode: if `MEMCOVE_TENANT_MAP` is set, the token's `sub` (or a matching `groups`/
`roles` entry) is mapped through it and an unmapped identity is **rejected**. With no map,
the tenant comes from a single configurable claim (`MEMCOVE_OAUTH_TENANT_CLAIM`, default
`sub`) — safe because it's from a signed token, not a client-settable header.

The Arrow Flight data plane is unchanged: it stays secured by the HMAC-signed, short-TTL
tickets the (now OAuth-authenticated) control plane mints — clients get tickets, never
tokens.

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

In **proxy mode**, the proxy must **overwrite** the identity/tenant header from the verified
identity and **strip** any client-supplied copy, so a caller cannot spoof it. Pair this with
network isolation so only the proxy can reach Memcove — see
[Kubernetes](../deployment/kubernetes.md). In **native OAuth mode** the tenant comes from the
signed token rather than a header, so this concern doesn't apply — but still restrict who can
reach Memcove at the network layer.
