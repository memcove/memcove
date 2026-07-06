# Changelog

All notable changes to Memcove are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once it reaches 1.0.

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
