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

    # Object store
    s3_endpoint: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio12345"
    s3_path_style: bool = True
    warehouse_bucket: str = "warehouse"
    staging_bucket: str = "memcove-staging"
    artifacts_bucket: str = "memcove-artifacts"

    # Trino
    trino_host: str = "localhost"
    trino_port: int = 8080
    trino_user: str = "memcove"
    trino_catalog: str = "iceberg"

    # Arrow Flight streaming data plane (M3)
    flight_host: str = "0.0.0.0"  # bind address
    flight_port: int = 8815
    flight_advertise_uri: str = "grpc://localhost:8815"  # what clients are told to dial

    # Postgres registry
    pg_dsn: str = "postgresql://memcove:memcove@localhost:5433/memcove"

    # Guardrails
    preview_row_cap: int = 1000
    inline_bytes_cap: int = 8 * 1024 * 1024
    export_row_cap: int = 5_000_000
    presign_ttl_seconds: int = 3600

    # Tenancy (auth deferred)
    tenant_header: str = "x-memcove-tenant"
    default_tenant: str = "default"


@lru_cache
def get_settings() -> Settings:
    return Settings()
