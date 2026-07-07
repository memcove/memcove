# Keycloak — native OAuth worked example

Memcove can act as an OAuth 2.1 **resource server**: instead of trusting headers from an
auth proxy, it validates bearer JWTs itself against your IdP's JWKS. This lets an MCP
client (Claude, MCP Inspector) connect **directly**. Keycloak is the default worked
example here; any OIDC-compliant provider (Auth0, Okta, Google, …) works the same way —
only the issuer URL and how tokens are minted differ.

## 1. Start Keycloak

```bash
docker compose -f deploy/keycloak/docker-compose.keycloak.yml up -d
```

This imports a `memcove` realm with:
- a client scope **`memcove.use`**,
- an **audience mapper** stamping `aud=memcove` into access tokens,
- a confidential client **`memcove-test`** (client-credentials) for smoke tests.

Admin console: <http://localhost:8081> (`admin` / `admin`). Issuer:
`http://localhost:8081/realms/memcove`.

## 2. Point Memcove at it

```bash
MEMCOVE_OAUTH_ENABLED=true
MEMCOVE_OAUTH_ISSUER=http://localhost:8081/realms/memcove
MEMCOVE_OAUTH_AUDIENCE=memcove
MEMCOVE_OAUTH_REQUIRED_SCOPES=["memcove.use"]
# Client-credentials tokens have a UUID `sub`; key the tenant off the client id instead:
MEMCOVE_OAUTH_TENANT_CLAIM=azp
```

The JWKS URI is discovered from the issuer automatically; set `MEMCOVE_OAUTH_JWKS_URI`
to skip discovery.

## 3. Smoke-test

```bash
# Mint a token
TOKEN=$(curl -s http://localhost:8081/realms/memcove/protocol/openid-connect/token \
  -d grant_type=client_credentials \
  -d client_id=memcove-test -d client_secret=test-secret \
  -d scope=memcove.use | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Unauthenticated → 401 with a WWW-Authenticate challenge
curl -i http://localhost:8090/mcp -X POST -H 'Accept: application/json, text/event-stream'

# Authenticated → passes the auth layer
curl -i http://localhost:8090/mcp -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json, text/event-stream'
```

## Connecting a real client (Claude)

Claude discovers the authorization server from Memcove's
`/.well-known/oauth-protected-resource` metadata and runs the OAuth flow against your
IdP directly. For that you need an **interactive** client in the realm (standard
authorization-code flow, Claude's redirect URI) rather than the client-credentials test
client above, and — if the client should self-register — Keycloak's Dynamic Client
Registration enabled. The full end-to-end "Connect Claude" walkthrough lands with the
onboarding docs (Phase 5).

## Production notes

- Front Keycloak and Memcove with TLS; set `MEMCOVE_PUBLIC_URL` to the public `https://`
  URL so the resource metadata advertises the right identifier.
- Rotate the test client secret / remove the test client in real deployments.
- To map identities to internal tenants explicitly (instead of a raw claim), set
  `MEMCOVE_TENANT_MAP` — it takes precedence and is fail-closed for unmapped identities.
