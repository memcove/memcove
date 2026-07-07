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

!!! note "Tenant in the URI is enforced against the caller"
    The `{tenant}` in the URI must match the **authenticated caller's** tenant. Memcove
    resolves the caller's tenant (from the verified identity, exactly as tools do) and
    rejects the request if the URI names a different tenant — so a caller cannot read
    another tenant's metadata by naming it in the URI. Resources are tenant-scoped with the
    same isolation guarantee as tools.
