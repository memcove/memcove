# Security Policy

Memcove enforces a per-tenant isolation boundary (SQL guard, signed Arrow Flight
tickets, a trusted-header / OAuth trust boundary). Vulnerabilities in that boundary
are taken seriously.

## Reporting a vulnerability

**Please do not report security issues in public GitHub issues, discussions, or
pull requests.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   (or [Security Advisories](https://github.com/memcove/memcove/security/advisories/new)).
2. Describe the issue, affected version/commit, and a reproduction if you have one.

We aim to acknowledge a report within a few business days and will coordinate a
fix and disclosure timeline with you.

## Scope

High-value areas to consider when reporting:

- **Tenant isolation** — any way to read, write, or stream another tenant's data
  (the SQL guard in `core/sql_guard.py`, tenant resolution in `core/tenancy.py`,
  the `memcove://` resources, or the Arrow Flight data plane).
- **Flight ticket forgery / replay** — bypassing the HMAC-signed, short-TTL ticket
  checks on the gRPC surface (`data_plane/tickets.py`).
- **SQL guard bypass** — executing non-read-only or cross-namespace/catalog SQL.
- **Credential or presigned-URL leakage.**

## Supported versions

Memcove is pre-1.0; security fixes land on `main` and the latest release. Pin to a
released version and upgrade promptly for fixes.
