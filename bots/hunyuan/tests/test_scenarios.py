"""Multi-frame offline scenario simulations (no SC2 client involved)."""

from __future__ import annotations

import math

from bots.hunyuan import planner
from bots.hunyuan.model import Order
from bots.hunyuan.tests.helpers import combat_army, memory, opened_base, snapshot, unit


def _finite(order: Order) -> bool:
    return all(
        v is None or (isinstance(v, (int, float)) and math.isfinite(v))
        for v in (order.x, order.y)
    )


def test_scenario_early_harassment_then_full_recovery():
    """A raid kills workers; the bot defends, replaces SCVs, and resumes macro."""
    m = memory()
    base = opened_base()
    # Harassment: two zerglings in the mineral line at 90 seconds.
    raid = [unit(2001, "ZERGLING", 12, 11), unit(2002, "ZERGLING", 12, 10)]
    snap_raid = snapshot(time=90, units=base + combat_army(5), enemy_visible=raid, supply_left=12)
    raid_orders = planner.plan(snap_raid, m)
    assert m.stance == "DEFEND"
    assert all(_finite(o) for o in raid_orders)

    # After losing 8 workers, economy still keeps SCV production alive.
    depleted = [u for u in base if u.kind != "SCV"] + [u for u in base if u.kind == "SCV"][:10]
    snap_damage = snapshot(time=105, units=depleted + combat_army(5), supply_left=20)
    damage_orders = planner.plan(snap_damage, m)
    assert any(o.kind == "train" and o.unit == "SCV" for o in damage_orders)

    # Raid cleaned, hold expired -> back to macro and eventually attacking.
    planner.plan(snapshot(time=120, units=depleted + combat_army(5)), m)
    assert m.stance in {"MACRO", "ATTACK"}
    planner.plan(snapshot(time=260, units=opened_base() + combat_army(30)), m)
    assert m.stance == "ATTACK"


def test_scenario_failed_push_does_not_stall_production():
    """Wiped first army -> regroup, rebuild, second wave; no all-in behaviour."""
    m = memory()
    planner.plan(snapshot(time=220, units=opened_base() + combat_army(26)), m)
    assert m.stance == "ATTACK"

    # Army dies; only workers and buildings remain.
    bare = opened_base()
    for t in (222, 224, 226, 228, 230):
        orders = planner.plan(snapshot(time=t, units=bare), m)
        assert any(o.kind == "train" for o in orders), f"production stalled at {t}"
        assert all(_finite(o) for o in orders)
    assert m.stance == "REGROUP"

    # Rebuilt army can push again.
    orders = planner.plan(snapshot(time=300, units=opened_base() + combat_army(34)), m)
    assert m.stance == "ATTACK"
    assert m.failed_waves == 1


def test_scenario_three_base_economy_keeps_expanding_and_training():
    """A long macro game issues valid, bounded orders at every sampled frame."""
    m = memory()
    units = opened_base() + combat_army(12)
    cc2 = unit(20, "COMMANDCENTER", 40, 40, hp=1500, hp_max=1500, role="structure")
    cc3 = unit(21, "COMMANDCENTER", 120, 120, hp=1500, hp_max=1500, role="structure")
    units += [cc2, cc3]

    for t in range(60, 600, 10):
        snap = snapshot(
            time=float(t),
            minerals=600 + t,
            vespene=400 + t // 2,
            supply_used=min(190, 30 + t // 4),
            supply_cap=min(200, 38 + t // 3),
            units=units,
            enemy_memory=[],
        )
        orders = planner.plan(snap, m)
        assert all(_finite(o) for o in orders), f"non-finite order at {t}s"
        assert len(orders) < 40, f"order spam at {t}s: {len(orders)}"
        assert any(o.kind == "train" for o in orders), f"no production at {t}s"


def test_scenario_no_units_left_still_emits_scv_orders():
    """Catastrophic army/economy loss: keep trying to rebuild, never crash."""
    m = memory()
    base_only = [
        unit(1, "COMMANDCENTER", 10, 10, role="structure"),
        unit(100, "SCV", 11, 9, role="worker"),
    ]
    for t in (30, 60, 120):
        orders = planner.plan(snapshot(time=t, minerals=300, units=base_only), m)
        assert all(_finite(o) for o in orders)
        assert any(o.kind == "train" and o.unit == "SCV" for o in orders)
