"""Domain models for the Memcove control plane."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceKind(str, Enum):
    """How an object's data first entered Memcove."""

    INLINE = "inline"
    S3_PARQUET = "s3_parquet"
    UPLOAD = "upload_handle"
    STREAM = "stream"
    DERIVED = "derived"


class ColumnSchema(BaseModel):
    name: str
    type: str


class Lineage(BaseModel):
    """Provenance of a derived object."""

    parents: list[str] = Field(default_factory=list)  # parent labels
    producing_sql: str | None = None


class MemoryObject(BaseModel):
    """A labeled, namespaced reference to an Iceberg table.

    Identity is ``<tenant>.<label>`` which maps to the Iceberg table
    ``<catalog>.<tenant>.<label>``.
    """

    tenant: str
    label: str
    table_ident: str  # fully-qualified iceberg identifier, e.g. memcove.t_acme.events
    source: SourceKind
    source_ref: str | None = None  # e.g. the s3 uri or upload handle
    schema_: list[ColumnSchema] = Field(default_factory=list, alias="schema")
    row_count: int | None = None
    size_bytes: int | None = None
    tags: list[str] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"populate_by_name": True}


class PreviewResult(BaseModel):
    """A capped tabular result handed back through the control plane."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int  # number of rows in this preview
    truncated: bool  # True if more rows exist beyond the cap


class ArtifactRef(BaseModel):
    """A materialized export living in object storage."""

    uri: str  # s3:// location
    presigned_url: str  # time-limited GET URL
    format: str  # parquet | csv | json
    row_count: int
    size_bytes: int
    expires_in_seconds: int


class UploadTicket(BaseModel):
    """A presigned slot for out-of-band parquet upload."""

    upload_handle: str  # staging key the client passes back to ingest_object
    presigned_url: str  # presigned PUT URL
    expires_in_seconds: int
