"""Unit tests for the reconciler planner + applier (no infra required).

The planner is pure; the applier's live TOCTOU re-check is tested with fakes.
"""

from __future__ import annotations

from memcove import reconciler
from memcove.reconciler import NamespacePlan, SweepReport, apply_plan, plan_namespace

CAP = 0.25
CAP_MIN = 3
MIN_SWEEPS = 2


def _plan(live, reg, tombstones=None):
    return plan_namespace(
        set(live), set(reg), tombstones or {},
        min_absent_sweeps=MIN_SWEEPS, cap_ratio=CAP, cap_min=CAP_MIN,
    )


def test_backfills_table_missing_registry_row():
    plan = _plan(live=["a", "b"], reg=["a"])
    assert plan.backfill == ["b"]
    assert plan.delete == []


def test_clean_namespace_does_nothing():
    plan = _plan(live=["a", "b"], reg=["a", "b"])
    assert plan == NamespacePlan()  # all empty, not skipped/capped


def test_dangling_row_bumped_before_grace_then_deleted():
    # First time absent: no tombstone yet -> bump, not delete (grace not met).
    p1 = _plan(live=["a"], reg=["a", "gone"])
    assert p1.tombstone_bump == ["gone"]
    assert p1.delete == []
    # Absent again with one prior sweep recorded -> 1+1 >= 2 -> eligible to delete.
    p2 = _plan(live=["a"], reg=["a", "gone"], tombstones={"gone": 1})
    assert p2.delete == ["gone"]
    assert p2.tombstone_bump == []


def test_fail_safe_empty_listing_deletes_nothing():
    # Registry has rows but catalog listed nothing -> treat as unavailable, skip.
    plan = _plan(live=[], reg=["a", "b", "c"], tombstones={"a": 5, "b": 5, "c": 5})
    assert plan.skipped is True
    assert plan.delete == []


def test_deletion_cap_aborts_namespace():
    # 4 of 5 rows absent = 80% > 25% cap AND > cap_min(3) -> abort, delete nothing.
    plan = _plan(
        live=["a"], reg=["a", "w", "x", "y", "z"],
        tombstones={"w": 9, "x": 9, "y": 9, "z": 9},
    )
    assert plan.capped is True
    assert plan.delete == []


def test_small_namespace_can_clean_single_dangling_row():
    # 1 of 2 rows absent = 50% > ratio, but <= cap_min(3): cleanup is allowed.
    plan = _plan(live=["a"], reg=["a", "gone"], tombstones={"gone": 1})
    assert plan.capped is False
    assert plan.delete == ["gone"]


def test_reappeared_row_clears_tombstone():
    plan = _plan(live=["a", "back"], reg=["a", "back"], tombstones={"back": 1})
    assert plan.tombstone_clear == ["back"]
    assert plan.delete == []


def test_orphaned_tombstone_is_cleared():
    # "gone" has a tombstone but is now absent from BOTH reg and live (e.g. forget()
    # removed the row while its grace counter was pending). It can never appear in
    # `absent` again, so its tombstone must be cleared or it leaks forever.
    plan = _plan(live=["a"], reg=["a"], tombstones={"gone": 1})
    assert plan.tombstone_clear == ["gone"]
    assert plan.delete == []


def test_orphaned_tombstone_not_cleared_during_failsafe_skip():
    # With an empty listing we can't distinguish orphaned from unlisted -> leave it.
    plan = _plan(live=[], reg=["a"], tombstones={"gone": 1})
    assert plan.skipped is True
    assert plan.tombstone_clear == []


def test_empty_namespace_no_registry_is_noop():
    # live empty AND reg empty is a genuinely empty namespace -> not skipped.
    plan = _plan(live=[], reg=[])
    assert plan.skipped is False
    assert plan == NamespacePlan()


def test_apply_plan_toctou_recheck_spares_reappeared_table(monkeypatch):
    """A row planned for deletion whose table came back is spared, not deleted."""
    deleted, cleared, backfilled = [], [], []
    # The table exists again at apply time (created after the sweep snapshot).
    monkeypatch.setattr(reconciler.catalog, "table_exists", lambda t, lbl: True)
    monkeypatch.setattr(reconciler.registry, "delete_object", lambda t, lbl: deleted.append(lbl))
    monkeypatch.setattr(reconciler.registry, "clear_tombstone", lambda t, lbl: cleared.append(lbl))
    monkeypatch.setattr(reconciler.registry, "bump_tombstone", lambda t, lbl: None)
    monkeypatch.setattr(reconciler, "_backfill", lambda t, lbl: backfilled.append(lbl))

    plan = NamespacePlan(delete=["gone"])
    report = SweepReport()
    apply_plan("t_acme", plan, report)

    assert deleted == []  # TOCTOU guard: not deleted because table_exists came back True
    assert cleared == ["gone"]  # tombstone cleared instead
    assert report.deleted == []


def test_apply_plan_deletes_confirmed_absent_table(monkeypatch):
    deleted, cleared = [], []
    monkeypatch.setattr(reconciler.catalog, "table_exists", lambda t, lbl: False)
    monkeypatch.setattr(reconciler.registry, "delete_object", lambda t, lbl: deleted.append(lbl))
    monkeypatch.setattr(reconciler.registry, "clear_tombstone", lambda t, lbl: cleared.append(lbl))

    plan = NamespacePlan(delete=["gone"])
    report = SweepReport()
    apply_plan("t_acme", plan, report)

    assert deleted == ["gone"]
    assert report.deleted == [("t_acme", "gone")]
