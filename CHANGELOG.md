# Changelog

All notable changes to Memcove are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once it reaches 1.0.

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
