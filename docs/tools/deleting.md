# Deleting

## `forget_dataset`

Permanently delete a dataset from memory (drops the underlying Iceberg table and its
metadata). This **cannot be undone**, and datasets derived from it will lose their source.

Only use when explicitly asked to remove data. To overwrite a dataset with new contents
instead, use [`remember_dataset`](storing.md#remember_dataset) /
[`derive_dataset`](querying.md#derive_dataset) with `mode="replace"`.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name of the dataset to permanently delete. |

**Returns** — `{forgotten: "<name>"}`.

!!! warning
    Deletion drops the Iceberg table and the registry rows. Lineage edges from other
    datasets that referenced it remain recorded, but the source data is gone.
