# Configuring & Deploying Memcove

Memcove is a generic, self-hostable service, not one organization's deployment.
Everything environment-specific is configuration; you bring your own object store,
Trino, catalog, identity proxy, and orchestration. This guide covers the knobs and
the security contract you are responsible for wiring.

All settings are environment variables prefixed `MEMCOVE_` (see `.env.example` for
the full list with defaults). Lists are JSON (`MEMCOVE_SHARED_SCHEMAS=["ref_market"]`).

## The trust boundary (read this first)

Memcove does **not** validate OIDC tokens itself. It trusts a request's tenant the
way a service behind an authenticating reverse proxy does: the proxy authenticates
the caller and sets a trusted header (`MEMCOVE_TENANT_HEADER`, default
`x-memcove-tenant`). **This is only safe if nothing can reach Memcove except the
proxy.** You must enforce that at the network layer, and the proxy must strip any
inbound copy of that header from the client so it cannot be spoofed.

Concretely, you are responsible for:

1. **Network isolation** — only the identity proxy can reach the MCP port, the Flight
   gRPC port, and (ideally) Trino. See `deploy/networkpolicy.example.yaml`.
2. **Header hygiene** — the proxy overwrites `x-memcove-tenant` from the verified
   identity and drops any client-supplied value.
3. **Tenant mapping** — don't feed a raw OIDC `sub` straight through as a namespace.
   Either map identity → internal tenant id at the proxy, or let Memcove do it: set
   `MEMCOVE_TENANT_SUBJECT_HEADER` (and optionally `MEMCOVE_TENANT_GROUP_HEADER`) plus
   `MEMCOVE_TENANT_MAP` (JSON `{"identity":"tenant_id"}`), and Memcove resolves the
   tenant through that map. This mode is fail-closed: an unmapped identity is rejected,
   never allowed to fall back to the client-settable tenant header. Left unset, Memcove
   trusts `MEMCOVE_TENANT_HEADER` directly (dev/simple).

## Layers of isolation

Memcove enforces tenant isolation in application code (the SQL guard), and can defer
to your engine's access control as a second, independent layer.

### 1. SQL guard (always on)

`core/sql_guard.py` rewrites/validates every submitted SELECT: bare and own-schema
references resolve to the caller's namespace; `MEMCOVE_SHARED_SCHEMAS` resolve to
themselves (read-only reference plane); everything else — other tenants, foreign
catalogs, `information_schema`/`system`, `SHOW`/`DESCRIBE`/`EXPLAIN` — is rejected.
Writes never come in as SQL; materialization is API-driven (`derive`, `export`).

### 2. Trino principal (optional, recommended for prod)

Set `MEMCOVE_TRINO_IMPERSONATION=true` and Memcove connects to Trino **as the
caller's tenant** for every data query, so your Trino access-control backend applies
per tenant even if the guard were ever bypassed. Memcove does not implement grants —
you choose and configure the backend (file-based rules, Ranger, OPA, or Iceberg REST
authz). Requirements:

- The service principal (`MEMCOVE_TRINO_USER`) must be allowed to impersonate tenants.
- Each tenant principal needs read on its own schema + the shared schemas, and write
  on its own schema (Memcove's CTAS materialization runs as the tenant).
- Schema creation stays on the service principal (provisioning is privileged).

With impersonation off (the default) Memcove uses the single service identity — fine
for local/dev where Trino has no authz.

## Shared reference plane

`MEMCOVE_SHARED_SCHEMAS` lists schemas every tenant may read but none may write —
your consolidated market/reference data. Use **per-domain** schemas (e.g.
`ref_market`, `ref_reference`) rather than one catch-all, to contain blast radius.
Loading data into them is a privileged, out-of-band publisher step (not an agent
path); grant tenants `SELECT` and nothing else.

## Agent-supplied ingest

`s3_parquet` ingest reads a URI the agent provides. To avoid a confused-deputy read
of any bucket the service credential can reach, it is **disabled unless**
`MEMCOVE_ALLOWED_S3_INGEST_PREFIXES` lists permitted `s3://` prefixes.

## Arrow Flight tickets

Streaming tickets/descriptors are HMAC-signed and short-lived so a gRPC client cannot
forge one for another tenant. **Set `MEMCOVE_FLIGHT_TICKET_SECRET`** to a strong
random value in every real deployment (the default is intentionally insecure), and
tune `MEMCOVE_FLIGHT_TICKET_TTL_SECONDS` (default 300).

## Observability

Every accepted read/derive/export emits one structured JSON line on the
`memcove.audit` logger (tenant, rewritten SQL, row count). Route that logger to your
audit sink. Apply Trino-side resource caps via `MEMCOVE_TRINO_SESSION_PROPERTIES`
(a generic passthrough, e.g. `{"query_max_run_time":"60s"}`).

Agents can't read `information_schema`, so the `discover_reference_data` tool gives
them a curated view of the shared reference schemas and their columns.

## Deployment

Memcove ships no opinionated cluster manifest. `deploy/` contains **examples** to
adapt:

- `deploy/networkpolicy.example.yaml` — the proxy-only ingress contract above.
- `deploy/values.example.yaml` — the config surface as a values file, for wiring into
  your own Helm chart / ArgoCD Application / Kustomize base.

Point these at your object store, Trino, Postgres registry, and identity proxy, then
deploy with whatever your platform uses.
