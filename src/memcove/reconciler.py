"""Registry <-> Iceberg reconciler (M5 write-atomicity self-healing).

The registry is a best-effort projection of the Iceberg catalog: a crash between
the data write and the registry write can leave an Iceberg table with no registry
row (invisible to ``list``/``describe``) or a registry row whose table was dropped
(a dangling pointer). This sweep repairs both directions.

Deletion is deliberately timid — the metadata it removes (tags/producing_sql/
lineage) is not reconstructable, so a false delete is unrecoverable:

  * **Fail-safe listing.** A namespace that lists as empty while the registry still
    has rows is treated as "listing unavailable", not "everything was deleted": it
    deletes nothing. A transient catalog hiccup can never wipe a tenant.
  * **Deletion cap.** A sweep that would delete more than ``reconcile_deletion_cap_ratio``
    of a namespace's rows aborts that namespace and alerts instead.
  * **Grace period.** A row must be absent for ``reconcile_min_absent_sweeps``
    consecutive sweeps before it is eligible for deletion (tombstone tracking).
  * **Live re-check.** Immediately before deleting, ``table_exists`` is called again
    to close the TOCTOU window where a table was created after the sweep's snapshot.

Run as a standalone entrypoint (``memcove-reconcile``); M7 schedules it as a CronJob.
Synchronous read-repair (``tools/objects._read_repair``) covers the same drift inline
on the read path so the guarantee holds before the cron exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from memcove.core import catalog, registry
from memcove.core.config import get_settings
from memcove.core.models import SourceKind

logger = logging.getLogger("memcove.reconciler")


@dataclass
class NamespacePlan:
    """The decisions for one namespace, computed from a single sweep snapshot."""

    backfill: list[str] = field(default_factory=list)  # tables missing a registry row
    delete: list[str] = field(default_factory=list)  # rows eligible for deletion now
    tombstone_bump: list[str] = field(default_factory=list)  # absent, not yet past grace
    tombstone_clear: list[str] = field(default_factory=list)  # reappeared -> reset grace
    skipped: bool = False  # fail-safe: suspicious empty listing, deleted nothing
    capped: bool = False  # deletion cap tripped, deleted nothing


@dataclass
class SweepReport:
    backfilled: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    skipped_namespaces: list[str] = field(default_factory=list)
    capped_namespaces: list[str] = field(default_factory=list)
    errored_namespaces: list[str] = field(default_factory=list)


def plan_namespace(
    live_labels: set[str],
    reg_labels: set[str],
    tombstones: dict[str, int],
    *,
    min_absent_sweeps: int,
    cap_ratio: float,
    cap_min: int,
) -> NamespacePlan:
    """Pure planning for one namespace — no I/O, so it is exhaustively unit-testable.

    ``tombstones`` maps label -> how many consecutive prior sweeps it was absent.
    """
    live = set(live_labels)
    reg = set(reg_labels)
    backfill = sorted(live - reg)
    absent = reg - live
    # Clear a tombstone once its row is present again (reappeared), OR once its label
    # is gone from both the registry and the catalog (orphaned — e.g. forget() removed
    # it while a tombstone was pending, so it can never appear in `absent` again and
    # its grace-counter row would otherwise leak forever).
    reappeared = (reg & live) & set(tombstones)
    orphaned = set(tombstones) - reg - live
    clearable = sorted(reappeared | orphaned)

    # Fail-safe: registry has rows but the catalog listed nothing. Treat as an
    # unavailable listing, never as "everything was deleted". Don't clear orphaned
    # tombstones here — with an empty listing we can't tell orphaned from unlisted.
    if reg and not live:
        return NamespacePlan(backfill=backfill, tombstone_clear=sorted(reappeared), skipped=True)

    # Deletion cap: refuse to delete an implausible fraction of a namespace at once.
    # Only applies past cap_min absolute deletions, so a small namespace can still
    # clean up a lone dangling row.
    if len(absent) > cap_min and len(absent) > cap_ratio * len(reg):
        return NamespacePlan(backfill=backfill, tombstone_clear=clearable, capped=True)

    delete: list[str] = []
    bump: list[str] = []
    for label in sorted(absent):
        if tombstones.get(label, 0) + 1 >= min_absent_sweeps:
            delete.append(label)
        else:
            bump.append(label)
    return NamespacePlan(
        backfill=backfill,
        delete=delete,
        tombstone_bump=bump,
        tombstone_clear=clearable,
    )


def _backfill(tenant: str, label: str) -> None:
    registry.record_object(
        tenant,
        label,
        table_ident=f"{get_settings().trino_catalog}.{tenant}.{label}",
        source=SourceKind.RECONCILED.value,
    )


def apply_plan(tenant: str, plan: NamespacePlan, report: SweepReport) -> None:
    """Execute a namespace plan against the real registry/catalog.

    The delete path re-checks ``table_exists`` live immediately before deleting
    (TOCTOU guard): if the table came back between the sweep snapshot and now, the
    row is kept and its tombstone cleared instead.
    """
    for label in plan.backfill:
        _backfill(tenant, label)
        report.backfilled.append((tenant, label))
    for label in plan.tombstone_clear:
        registry.clear_tombstone(tenant, label)
    for label in plan.tombstone_bump:
        registry.bump_tombstone(tenant, label)
    for label in plan.delete:
        if catalog.table_exists(tenant, label):  # TOCTOU: reappeared since the snapshot
            registry.clear_tombstone(tenant, label)
            continue
        registry.delete_object(tenant, label)
        registry.clear_tombstone(tenant, label)
        report.deleted.append((tenant, label))
    if plan.skipped:
        report.skipped_namespaces.append(tenant)
        logger.warning(
            "reconcile: namespace %s listed empty while registry has rows — "
            "skipping deletions (treating as unavailable listing)",
            tenant,
        )
    if plan.capped:
        report.capped_namespaces.append(tenant)
        logger.warning(
            "reconcile: namespace %s would delete over the cap ratio — "
            "aborting deletions and alerting",
            tenant,
        )


def reconcile() -> SweepReport:
    """One full sweep across every tenant namespace. Idempotent; safe to re-run."""
    settings = get_settings()
    report = SweepReport()
    for tenant in catalog.list_namespaces():
        # Isolate each namespace: a catalog/registry error on one tenant must not
        # abort the sweep and starve every tenant after it.
        try:
            plan = plan_namespace(
                live_labels=set(catalog.list_labels(tenant)),
                reg_labels=set(registry.labels_for_tenant(tenant)),
                tombstones=registry.tombstones_for_tenant(tenant),
                min_absent_sweeps=settings.reconcile_min_absent_sweeps,
                cap_ratio=settings.reconcile_deletion_cap_ratio,
                cap_min=settings.reconcile_deletion_cap_min,
            )
            apply_plan(tenant, plan, report)
        except Exception:  # noqa: BLE001 - one bad namespace must not abort the sweep
            report.errored_namespaces.append(tenant)
            logger.warning("reconcile: namespace %s failed mid-sweep, skipping", tenant, exc_info=True)
    logger.info(
        "reconcile sweep: backfilled=%d deleted=%d skipped_ns=%d capped_ns=%d errored_ns=%d",
        len(report.backfilled),
        len(report.deleted),
        len(report.skipped_namespaces),
        len(report.capped_namespaces),
        len(report.errored_namespaces),
    )
    return report


def main() -> None:  # console-script entrypoint (memcove-reconcile)
    logging.basicConfig(level=logging.INFO)
    try:
        registry.init_db()
    except Exception as exc:  # noqa: BLE001 - init is best-effort if tables already exist
        logger.warning("registry init failed (is postgres up?): %s", exc)
    reconcile()


if __name__ == "__main__":
    main()
