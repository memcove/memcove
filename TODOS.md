# TODOs

Deferred work, captured with enough context to pick up cold.

## Reconciler: incremental / bounded scan (M6)
- **What:** Replace the reconciler's O(all-datasets) full catalog-vs-registry scan with an
  incremental one (only namespaces/tables changed since the last sweep, or a bounded batch).
- **Why:** At M5 scale (few tenants) a full scan per sweep is fine. M6 runs the same sweeper
  frequently for TTL/session GC across many sessions and tenants, where a full scan every run
  becomes the dominant cost.
- **Context:** The reconciler lands in M5 as a standalone idempotent entrypoint (see the M5
  write-atomicity design doc, task T8). It diffs Iceberg tables against `memcove_objects`. The
  targeted drift signal already makes the common ingest-failure case cheap; this TODO is about
  the periodic sweep, not the targeted path. Designing incremental scan now is premature.
- **Depends on:** reconciler landing (T8); best co-built with the M6 GC sweeper.

## Derive replace: guard column TYPES, not just names
- **What:** `tools/derive.py:_assert_replace_keeps_shape` compares only the set of column
  *names* between the existing table and the SELECT's output. A replace that keeps the names
  but changes a column's type (e.g. `id` bigint -> varchar) passes and silently reshapes.
- **Why:** The ingest path's guard (`catalog._assert_schema_compatible`) compares types, so the
  two replace paths disagree on what "same shape" means, and the derive path can still break the
  "downstream SQL never breaks" promise it exists to keep.
- **Context:** Fixing needs the probe (`SELECT ... WHERE 1=0`) to return column *types*, not just
  names, from Trino result metadata, then compare with the same normalization as the ingest guard.
  Found by adversarial review of the M5 diff (confidence 6). Names cover add/remove/rename (the
  common corruption); type-only-same-name changes are the residual gap.
- **Depends on:** exposing column types from `trino_client.execute` result metadata.

## Reconciler: tighten partial-listing safety + single-execution probe
- **What:** Two smaller reconciler hardenings from the same review. (a) The deletion cap uses
  `len(absent) > cap_min` (cap_min=3), so exactly 3 absent rows are neither capped nor caught by
  the fully-empty fail-safe — a partial catalog listing that transiently drops <=3 tables could
  delete up to 3 real registry rows (metadata is unrecoverable). Consider a partial-listing
  heuristic beyond "listing is completely empty". (b) `derive` replace runs the SELECT twice
  (the `WHERE 1=0` probe, then the CTAS), doubling cost and double-evaluating non-deterministic
  SQL; the probe also `set()`-dedupes column names so a `SELECT *` join with duplicate names
  mis-compares.
- **Why:** Both are low-probability given the existing layers (two-sweep grace + live TOCTOU
  re-check gate every delete; probes are cheap), so they're hardening, not fixes.
- **Context:** Found by adversarial review of the M5 diff (confidence 4-5). Left as-is because
  the grace period + TOCTOU re-check already make a wrongful delete require a *correlated,
  persistent* flake across two sweeps AND the live re-check.
- **Depends on:** nothing.
