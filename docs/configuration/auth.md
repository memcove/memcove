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

`MEMCOVE_TENANT_MODE` decides how a caller becomes an internal `t_<id>` namespace. It is the
single rule for both entry points — the trusted-proxy-header path and the native-OAuth
claims path — so isolation is set in one place:

| Mode | Every caller becomes… | Isolation | Needs |
| --- | --- | --- | --- |
| `auto` *(default)* | the tenant header (dev) or token claim; the map if one is configured | depends on config | — |
| `shared` | **one** tenant (`MEMCOVE_SHARED_TENANT`, else default) | none — one shared workspace | — |
| `private` | its **own** tenant, hashed from the verified identity | full per-user | a trusted identity |
| `mapped` | a named tenant from `MEMCOVE_TENANT_MAP` | per provisioned tenant | the map |

### `auto` (default) — dev-simple, backward compatible

Trusts `MEMCOVE_TENANT_HEADER` (default `x-memcove-tenant`), falling back to
`MEMCOVE_DEFAULT_TENANT` when absent — right for local development and trusted networks. If
you configure a subject header / map, `auto` behaves like `mapped`. Note the plain-header
path is **not isolated**: a client can set any tenant value. Pick `shared`, `private`, or
`mapped` for real multi-user deployments.

### `shared` — one workspace for everyone

```bash
MEMCOVE_TENANT_MODE=shared
MEMCOVE_SHARED_TENANT=team-alpha    # optional; defaults to MEMCOVE_DEFAULT_TENANT
```

Every caller resolves to the same tenant, whatever they send. Ideal for a personal or
single-team deployment where one shared memory is the point.

### `private` — one tenant per user, automatically

```bash
MEMCOVE_TENANT_MODE=private
MEMCOVE_TENANT_SUBJECT_HEADER=x-auth-subject   # proxy path; OAuth path uses the token claim
```

Each **verified** identity gets its own namespace, derived by hashing the identity — so it's
deterministic (stable across a user's sessions) and *injective* (two different users can
never land in the same namespace, even if their ids would sanitize to the same string). No
map to maintain. The raw, client-settable tenant header is ignored, so isolation can't be
spoofed; a request with no trusted identity is rejected.

### `mapped` — explicit provisioning (fail-closed)

```bash
MEMCOVE_TENANT_MODE=mapped
MEMCOVE_TENANT_SUBJECT_HEADER=x-auth-subject
MEMCOVE_TENANT_GROUP_HEADER=x-auth-groups
MEMCOVE_TENANT_MAP={"oidc|abc123":"acme","team-research":"research"}
```

Memcove maps the verified identity — the subject, else a matching group — through the map to
a named tenant. **Fail-closed**: an identity absent from the map is **rejected**, never
falling back to a client-settable value. Use this when operators assign identities to named,
shared tenants.

!!! tip
    For multi-user, prefer `private` (zero-maintenance per-user isolation) or `mapped`
    (named shared tenants). Never pass a raw `sub` straight through as a namespace — both
    modes derive it safely.

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

**Tenant from claims.** The verified token resolves to a tenant by the same
`MEMCOVE_TENANT_MODE` as proxy mode. In `private` and `auto` (no map), the identity is the
`MEMCOVE_OAUTH_TENANT_CLAIM` claim (default `sub`) — `private` hashes it into a per-user
namespace, `auto` uses it directly (safe because it comes from a signed token, not a
client-settable header). In `mapped` (or `auto` with a map set), the `sub` — or a matching
`groups`/`roles` entry — is mapped through `MEMCOVE_TENANT_MAP` and an unmapped identity is
**rejected**. In `shared`, every token resolves to the one shared tenant.

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
