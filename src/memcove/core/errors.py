"""Typed errors surfaced to MCP callers as structured messages."""

from __future__ import annotations


class MemcoveError(Exception):
    """Base class for all Memcove errors."""


class SqlGuardError(MemcoveError):
    """Raised when submitted SQL violates the safety policy."""


class ObjectNotFoundError(MemcoveError):
    """Raised when a referenced label does not exist for the tenant."""


class ObjectExistsError(MemcoveError):
    """Raised when creating an object whose label already exists."""


class SchemaMismatchError(MemcoveError):
    """Raised when incoming data's schema is incompatible with an existing object.

    A ``replace`` or ``append`` must keep the object's shape; changing columns or
    types is rejected so downstream SQL never breaks silently. To change shape,
    ``forget()`` the object then ``create()`` it fresh.
    """


class IngestError(MemcoveError):
    """Raised when an ingest source cannot be read or written."""


class TenancyError(MemcoveError):
    """Raised when a tenant cannot be resolved."""


class TicketError(MemcoveError):
    """Raised when a Flight ticket/descriptor fails signature or expiry checks."""
