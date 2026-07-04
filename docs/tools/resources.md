# Resources

Besides tools, Memcove exposes two MCP **resources** — read-only, addressable views of a
tenant's catalog.

## `memcove://{tenant}/{name}`

Metadata for a single dataset (schema, source, tags, lineage) — the same shape
[`inspect_dataset`](reading.md#inspect_dataset) returns.

- `tenant` — the tenant namespace id (from the URI).
- `name` — the dataset label.

## `memcove://{tenant}/_catalog`

Lists all datasets for a tenant — `{datasets: [...]}`, the same shape as
[`list_memory`](reading.md#list_memory).

!!! note "Tenant in the URI"
    Resources take the tenant from the URI path rather than from the request headers the
    way tools do. Binding resource access to the authenticated caller is tracked as
    hardening work; treat resources as convenience views and rely on tools for
    tenant-scoped access in security-sensitive setups.
