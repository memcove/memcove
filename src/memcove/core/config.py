"""Runtime configuration, loaded from environment (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMCOVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MCP server
    host: str = "0.0.0.0"
    port: int = 8090

    # Iceberg REST catalog
    iceberg_rest_uri: str = "http://localhost:8181"
    iceberg_warehouse: str = "s3://warehouse/"
    iceberg_catalog_name: str = "memcove"

    # Object store. Access/secret keys default to the local MinIO dev creds. Clear
    # them (empty string) to fall back to the AWS default credential chain — IRSA,
    # instance profile, env vars, or STS — for a keyless deployment. See
    # static_s3_credentials().
    s3_endpoint: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = "minio"
    s3_secret_key: str | None = "minio12345"
    s3_path_style: bool = True
    warehouse_bucket: str = "warehouse"
    staging_bucket: str = "memcove-staging"
    artifacts_bucket: str = "memcove-artifacts"

    # Trino
    trino_host: str = "localhost"
    trino_port: int = 8080
    trino_user: str = "memcove"  # service principal; connect identity when not impersonating
    trino_catalog: str = "iceberg"
    trino_http_scheme: str = "http"  # "https" for TLS-fronted Trino in real deployments
    # When true, each request connects to Trino AS the caller's tenant so the operator's
    # own Trino access control applies per tenant (defense-in-depth beneath the SQL guard).
    # Requires the service principal to hold impersonation rights + a configured grant
    # backend. Off by default so local/dev works with a single identity.
    trino_impersonation: bool = False
    # Session properties applied to every data connection (generic passthrough so the
    # operator sets whatever their Trino version supports — resource caps, etc.), e.g.
    # {"query_max_run_time":"60s","query_max_scan_physical_bytes":"10GB"}.
    trino_session_properties: dict[str, str] = {}

    # Arrow Flight streaming data plane (M3)
    flight_host: str = "0.0.0.0"  # bind address
    flight_port: int = 8815
    flight_advertise_uri: str = "grpc://localhost:8815"  # what clients are told to dial
    # HMAC secret for signing Flight tickets/descriptors so a client cannot forge one
    # to read/write another tenant. MUST be overridden in any real deployment.
    flight_ticket_secret: str = "dev-insecure-change-me"
    flight_ticket_ttl_seconds: int = 300  # signed tickets expire after this many seconds

    # Postgres registry
    pg_dsn: str = "postgresql://memcove:memcove@localhost:5433/memcove"
    # Connection pool: the registry opens a fresh connection per op otherwise, and the
    # reconciler + synchronous read-repair add per-op churn. min_size connections are
    # kept warm; the pool grows to max_size under load. pool_timeout bounds how long a
    # caller waits for a free connection before raising (a subclass of OperationalError,
    # so a saturated/unreachable registry is still swallowed by the guarded-write path).
    # Kept at 10s (not psycopg_pool's 30s default) so an unreachable registry fails
    # closer to the old connect-per-call fast-fail; registry ops are milliseconds, so a
    # >10s wait for a free connection only happens during a real outage, not under load.
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 10
    pg_pool_timeout: float = 10.0

    # Reconciler / read-repair (write-atomicity self-healing). The reconciler diffs the
    # Iceberg catalog against the Postgres registry to backfill missing rows and drop
    # dangling ones. Deletion is fail-safe: an empty/failed namespace listing deletes
    # nothing, a row must be absent across this many consecutive sweeps before deletion,
    # and a sweep that would delete more than the cap ratio of a namespace aborts + alerts.
    reconcile_min_absent_sweeps: int = 2
    reconcile_deletion_cap_ratio: float = 0.25
    # The ratio cap only applies once a sweep would delete more than this many rows,
    # so small namespaces can still clean up a single dangling row (1 of 2 rows is 50%
    # but is not a mass deletion). The cap exists to stop a wipe, not routine cleanup.
    reconcile_deletion_cap_min: int = 3

    # Guardrails
    preview_row_cap: int = 1000
    inline_bytes_cap: int = 8 * 1024 * 1024
    export_row_cap: int = 5_000_000
    presign_ttl_seconds: int = 3600

    # Tenancy. Default: trust a tenant header set by the auth proxy (dev/simple).
    tenant_header: str = "x-memcove-tenant"
    default_tenant: str = "default"

    # Provisioning map (optional). When tenant_subject_header is set, the tenant is
    # resolved by mapping the proxy-provided identity (subject, else a matching group)
    # through tenant_map -> internal tenant id, instead of trusting a raw tenant value.
    # This is the seam for "don't feed a raw OIDC sub straight through".
    # Provisioning mode is fail-closed: when tenant_subject_header is set, an identity
    # absent from tenant_map is rejected (never falls through to the raw tenant header).
    tenant_subject_header: str = ""  # e.g. "x-auth-subject"; empty = direct tenant header
    tenant_group_header: str = ""  # e.g. "x-auth-groups" (comma-separated)
    tenant_map: dict[str, str] = {}  # subject/group -> internal tenant id

    # Shared read-only reference plane (the gateway): schemas every tenant may SELECT
    # from but none may write. These are NOT rewritten to the caller's namespace by the
    # SQL guard; they resolve to themselves. Per-domain schemas contain blast radius.
    shared_schemas: list[str] = ["ref_market"]

    # Ingest allowlist: agent-supplied `s3_parquet` URIs must start with one of these
    # prefixes. Empty list = agent s3_parquet ingest is DISABLED (fail closed) to avoid
    # a confused-deputy read of any bucket the service credential can reach.
    allowed_s3_ingest_prefixes: list[str] = []

    def static_s3_credentials(self) -> tuple[str, str] | None:
        """The explicit S3 access/secret key pair, or ``None`` to defer to the AWS
        default credential chain (IRSA / instance profile / env / STS).

        Both keys must be non-empty to count as static creds; an empty string on
        either clears them, so an IRSA deployment just sets the keys to empty and
        omits static credentials entirely.
        """
        if self.s3_access_key and self.s3_secret_key:
            return self.s3_access_key, self.s3_secret_key
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
