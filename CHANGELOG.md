# Changelog

All notable changes to Memcove are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once it reaches 1.0.

## [0.11.0] - 2026-07-09

### Changed
- **The data plane no longer buffers large payloads in the pod.** Every "large" path used to
  fully materialize in the control-plane process (risking an OOM); each now bounds peak memory
  to ~one batch:
  - **Export** streams the Trino cursor batch-by-batch straight into a single S3 object via a
    multipart write, instead of building the whole (up to `export_row_cap`) result as an Arrow
    table and serializing it in memory (JSON was triply resident). The one-file →
    one-presigned-URL contract is unchanged.
  - **Flight `DoGet`** returns a `GeneratorStream` sourced from the cursor rather than
    materializing the whole result and wrapping it in a `RecordBatchStream`; `get_flight_info`
    now describes the schema with a `LIMIT 0` probe instead of executing the full query a
    second time.
  - **Flight `DoPut`** commits in a single Iceberg commit while an upload stays under
    `MEMCOVE_DOPUT_SINGLE_COMMIT_MAX_ROWS` (small uploads stay atomic), then flushes in chunks
    past that so a huge upload can't buffer whole — at the cost of a partial table if the
    stream fails mid-way.
  - A shared `trino_client.stream_arrow_batches` powers the streaming reads (export + DoGet).

### Added
- **Ingest size guard** — `s3_parquet` and `upload_handle` ingest now reject an object larger
  than `MEMCOVE_INGEST_BYTES_CAP` (default 1 GiB) via a cheap HEAD before reading it into the
  pod; previously only inline payloads were capped.
- New settings: `MEMCOVE_INGEST_BYTES_CAP`, `MEMCOVE_STREAM_BATCH_ROWS` (default 50,000), and
  `MEMCOVE_DOPUT_SINGLE_COMMIT_MAX_ROWS` (default 1,000,000).

## [0.10.0] - 2026-07-07

### Added
- **Tenancy presets (`MEMCOVE_TENANT_MODE`)** — one setting governs how a caller becomes an
  internal tenant, across both the proxy-header and native-OAuth paths: `shared` (everyone
  → one tenant, via `MEMCOVE_SHARED_TENANT`), `private` (every verified identity → its own
  tenant, derived by hashing the identity so it's deterministic *and* injective — distinct
  users can never collide), `mapped` (explicit `MEMCOVE_TENANT_MAP`, fail-closed), and
  `auto` (the default, fully backward-compatible with prior behavior). `private` gives
  proper per-user isolation with no map to maintain and ignores the client-settable tenant
  header so it can't be spoofed. See `docs/configuration/auth.md`.

## [0.9.0] - 2026-07-07

### Added
- **Bucket sub-paths for uploads/exports** — `MEMCOVE_STAGING_BUCKET` and
  `MEMCOVE_ARTIFACTS_BUCKET` now accept either a bare bucket (`my-bucket`) or a bucket
  with a sub-path (`my-bucket/teams/data-eng`), so a team can scope Memcove's uploads and
  exports to a prefix it owns instead of writing at the bucket root. The path is split
  into `(bucket, key-prefix)` internally (`storage.resolve()`); the upload-handle
  cross-tenant binding check is prefix-aware. The Iceberg warehouse is unaffected (set via
  `MEMCOVE_ICEBERG_WAREHOUSE`).

## [0.8.0] - 2026-07-07

### Added
- **Scratchpad plane** — an optional fast, small, **ephemeral** store backed by DuckDB
  *behind Trino* as a federated catalog (`MEMCOVE_SCRATCH_ENABLED`, needs Trino ≥ 480).
  `remember_dataset` and `derive_dataset` take `target="scratch"`, and a scratch dataset is
  addressed in SQL via the reserved `scratch.<label>` alias — so it can be **JOINed with
  durable lakehouse tables and the reference plane in one `query_memory` call**. The SQL
  guard qualifies the alias to the caller's own scratch schema, so scratch is tenant-isolated
  like everything else. Two operator-selected catalog modes (`MEMCOVE_SCRATCH_CATALOG_MODE`):
  **shared** (one static DuckDB catalog, schema-per-tenant) and **per_tenant** (a
  `scratch_<tenant>` catalog created via Trino dynamic catalog management, file-level
  isolation). New `core/scratch.py`, `SourceKind.SCRATCH`, `MEMCOVE_SCRATCH_*` settings, and
  a `scratch` DuckDB catalog + Trino 480 in the dev stack. Scratch datasets are not tracked
  in the durable registry (no lineage); a TTL/session sweep lands with the session work.

## [0.7.0] - 2026-07-07

### Added
- **Onboarding docs** — a "Run with Docker" getting-started page (published image,
  entry points, env-file), an "Install with Helm" deployment page (OCI chart, BYO-infra
  values, IRSA, native-OAuth values, probes), a "BYO Trino & catalog" page stating exactly
  what Memcove assumes of a self-hosted Trino (≥ 431) and Iceberg REST catalog, a
  "Connect Claude (native OAuth)" section, and a cloud-model option for the demo scripts.
- **Chart: first-class OAuth config** — `config.oauth.*` in the Helm chart renders the
  `MEMCOVE_OAUTH_*` env, so native OAuth is a values toggle rather than raw `extraEnv`.

### Changed
- The Kubernetes and production-checklist docs now lead with the Helm chart and present
  native OAuth alongside the proxy model as first-class auth options.

## [0.6.0] - 2026-07-07

### Added
- **Native OAuth 2.1 resource server** — set `MEMCOVE_OAUTH_ENABLED=true` and Memcove
  validates bearer JWTs itself against the IdP's JWKS (signature, issuer, audience,
  expiry, required scopes) and serves `/.well-known/oauth-protected-resource`, so an MCP
  client like Claude can connect directly instead of through an auth proxy. Returns `401`
  + `WWW-Authenticate` when unauthenticated. Provider-agnostic; a runnable Keycloak worked
  example ships in `deploy/keycloak/`. New `core/oauth.py` (`JWKSTokenVerifier`) and
  `MEMCOVE_OAUTH_*` settings.
- **Tenant from verified claims** — in OAuth mode the tenant is resolved from the signed
  token: mapped through `MEMCOVE_TENANT_MAP` (fail-closed for unmapped identities) or, with
  no map, from a configurable claim (`MEMCOVE_OAUTH_TENANT_CLAIM`, default `sub`).
  `tenancy.resolve_tenant_from_claims()`.

### Fixed
- **Cross-tenant resource read** — the `memcove://{tenant}/…` MCP resources took the tenant
  from the URI and only normalized it, so a caller could read another tenant's dataset
  metadata by naming it. They now require the URI's tenant to match the caller's
  authenticated tenant, matching the tool path.

## [0.5.0] - 2026-07-07

### Added
- **Pluggable registry backend** — the metadata registry now runs on **SQLite**,
  **Postgres**, or **MySQL**, selected by the DSN scheme via a new
  `MEMCOVE_REGISTRY_DSN` (falls back to `MEMCOVE_PG_DSN`). SQLite (embedded, stdlib)
  gives zero-setup local dev with no Postgres container; MySQL needs the `mysql` extra
  (`pip install memcove[mysql]`). Backends live behind `core/registry_backends.py`; the
  `registry.*` API is unchanged. The guarded-write "registry down → metadata_pending"
  contract is preserved per backend.

### Changed
- **Portable tags storage** — object tags moved from a Postgres-only `text[]` column to
  a `memcove_object_tags` child table so tag filtering works on all three backends.
  (No data migration path is provided; this predates any external release.)
- `docker-compose.yml` adds a MySQL service for exercising that backend; CI runs the
  registry suite against Postgres + MySQL + SQLite.

## [0.4.0] - 2026-07-07

### Added
- **Helm chart** (`deploy/charts/memcove`) — installable chart with the control-plane
  server (Deployment + Service, wired to the `/health` and `/ready` probes), the Arrow
  Flight data plane (Deployment + Service, TCP probes), and the reconciler (CronJob).
  Config renders to a ConfigMap; secrets come from an `existingSecret` (recommended) or
  a chart-managed one. Includes optional NetworkPolicy (proxy-only ingress) and Ingress,
  a ServiceAccount for **AWS IRSA**, and non-root pod/container security contexts. The
  release workflow now packages and pushes the chart to GHCR as an OCI artifact.
- **Keyless S3 credentials** — clearing `MEMCOVE_S3_ACCESS_KEY` / `MEMCOVE_S3_SECRET_KEY`
  now falls back to the AWS default credential chain (IRSA / instance profile / env / STS)
  for both the boto3 storage client and PyIceberg, instead of requiring static keys. Adds
  `Settings.static_s3_credentials()`.

## [0.3.4] - 2026-07-07

### Added
- **Container image** — a multi-stage `Dockerfile` (non-root, `python:3.12-slim`,
  deps installed frozen from `uv.lock`) shipping all three entrypoints
  (`memcove-server` default, `memcove-flight`, `memcove-reconcile`). Plus a
  `.dockerignore`.
- **`/health` and `/ready` HTTP endpoints** on the MCP server — liveness always
  returns 200; readiness checks the metadata registry (`SELECT 1`) and Trino
  reachability and returns 503 with per-check detail when a dependency is down.
  Wire these to Kubernetes probes. Adds `registry.ping()`.
- **Release automation** — `.github/workflows/release.yml`: on a `v*` tag, builds a
  multi-arch (amd64+arm64) image and pushes to Docker Hub + GHCR, publishes the wheel
  to PyPI (trusted publishing), and creates a GitHub Release from the CHANGELOG. CI now
  also builds the image and smoke-tests `/health` on every PR.

## [0.3.3] - 2026-07-07

### Added
- **Open-source foundations** — `LICENSE` (Apache-2.0), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, and GitHub issue/PR templates. `pyproject.toml`
  now declares the license, classifiers, and project URLs.
- **CI test/lint gate** — `.github/workflows/ci.yml` runs `ruff check` and the unit
  suite on every PR, plus an integration job against the docker-compose stack. (The
  existing `docs.yml` still handles the docs site.)

### Changed
- **Reproducible installs** — `uv.lock` is now committed (was gitignored).
- **Local-stack reliability** — `docker-compose.yml` gives Trino a real healthcheck so
  `docker compose up -d --wait` blocks until it's ready, replacing the "sleep ~20s"
  guidance in the README and quickstart.

## [0.3.2] - 2026-07-06

### Changed
- **orjson for JSON serialization** — the Flight ticket codec (`data_plane/tickets.py`),
  the structured audit log (`core/audit.py`), and the JSON artifact export
  (`tools/artifacts.py`) now serialize/parse with `orjson` instead of the stdlib `json`
  module. orjson was already installed transitively (via `trino`) and is now a declared
  dependency. Signing is unaffected — HMAC canonicalization uses `orjson.OPT_SORT_KEYS`
  on both the sign and verify paths. One behavior change: JSON exports now render
  `datetime`/`UUID` values as native ISO 8601 (e.g. `2024-01-01T00:00:00`) rather than
  Python `str()`; `Decimal`/`bytes` still fall through to `default=str` unchanged.

## [0.3.1] - 2026-07-05

### Changed
- **Pooled Postgres connections** — the registry now backs its connection helpers with
  a lazily-opened `psycopg_pool` pool instead of opening a fresh connection per op
  (the reconciler and read-repair had raised per-op churn). Connections are health-
  checked on borrow, so a Postgres restart or idle drop no longer hands a dead
  connection to the next caller. Sizing/timeout via `MEMCOVE_PG_POOL_{MIN_SIZE,MAX_SIZE,TIMEOUT}`.
  Write-atomicity is unaffected: pool timeouts subclass `OperationalError`, so a
  saturated/unreachable registry is still swallowed as `metadata_pending`.

## [0.3.0] - 2026-07-05

Write atomicity (M5). A crash or concurrent reader can no longer see a half-written
object, and the registry now self-heals when it drifts from the Iceberg catalog.

### Added
- **Atomic full-table replace** — `mode=replace` uses each engine's native atomic
  swap (PyIceberg `overwrite()` for ingest/Flight, Trino `CREATE OR REPLACE TABLE`
  for derive) instead of drop-then-create. A concurrent reader sees the old rows or
  the new rows, never a missing table, and a mid-write crash no longer loses data.
- **Schema-compatibility guard** — replace and append reject a changed shape
  (added/removed/retyped column) with `SchemaMismatchError` rather than silently
  evolving it, so downstream SQL never breaks. `forget()` then `create()` to reshape.
- **Guarded registry write** — the object row and its lineage commit in one
  transaction; if the registry write fails after the data is committed, the tool
  returns a `metadata_pending` object and logs the drift instead of failing the write.
- **Synchronous read-repair** — `describe_object` on a registry miss backfills a
  `reconciled` row inline when the table exists, so an orphaned object becomes
  visible on the next read (the guarantee holds before the M7 reconcile cron).
- **Reconciler** (`memcove-reconcile`) — sweeps registry vs catalog and repairs both
  directions. Deletion is deliberately timid: fail-safe on an empty listing (never
  wipes a tenant on a transient catalog hiccup), a deletion cap with an absolute
  floor, a two-sweep grace period, and a live re-check just before deleting.

### Changed
- Dev/CI Trino pinned to `431` (the minimum for `CREATE OR REPLACE TABLE` on the
  Iceberg connector); operators bringing their own Trino need `>= 431`.

## [0.2.0] - 2026-07-04

The agent SQL gateway security core and its configuration seams. Every tenant
gets a private namespace plus a shared read-only reference plane, enforced by the
SQL guard and (optionally) the operator's own Trino access control.

### Added
- **Shared reference plane** — `MEMCOVE_SHARED_SCHEMAS` lets every tenant read
  configured read-only schemas (e.g. `ref_market`) while writing only their own.
  The SQL guard resolves shared schemas to themselves and rejects everything else.
- **Configurable Trino principal** — `MEMCOVE_TRINO_IMPERSONATION` connects to
  Trino as the calling tenant so the operator's grant backend applies per tenant
  (defense-in-depth beneath the guard). Off by default (single service identity).
- **Provisioning-map tenant resolution** — map a verified identity (subject/group)
  to an internal tenant id via `MEMCOVE_TENANT_SUBJECT_HEADER` + `MEMCOVE_TENANT_MAP`
  instead of trusting a raw OIDC `sub`. Fail-closed: unmapped identities are rejected.
- **Signed Arrow Flight tickets** — HMAC-signed, expiring tickets/descriptors so a
  gRPC client cannot forge one for another tenant (`MEMCOVE_FLIGHT_TICKET_SECRET`).
- **`discover_reference_data` tool** — a curated view of the shared schemas, since
  agents are denied `information_schema`.
- **Audit log** — structured JSON on the `memcove.audit` logger for every accepted
  read/derive/export, plus configurable Trino resource caps
  (`MEMCOVE_TRINO_SESSION_PROPERTIES`).
- **S3 ingest allowlist** — agent-supplied `s3_parquet` URIs must match
  `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES` (disabled/fail-closed by default).
- **Configuration guide + example deploy manifests** — `docs/CONFIGURATION.md`,
  `deploy/networkpolicy.example.yaml`, `deploy/values.example.yaml`.

### Security
- SQL guard now denies metadata/enumeration schemas (`information_schema`,
  `system`, `SHOW`/`DESCRIBE`/`EXPLAIN`), case-folds identifiers so a quoted
  mixed-case name can't smuggle a foreign schema, and fails closed on any
  FROM-item it can't classify (e.g. polymorphic `TABLE(...)` functions).
- `upload_handle` ingest is bound to the calling tenant's `uploads/{tenant}/` prefix.
- S3 allowlist matches on a path boundary (`s3://bucket` no longer permits
  `s3://bucket-evil`).
