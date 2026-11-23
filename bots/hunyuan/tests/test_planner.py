"""Offline decision-core tests: no SC2 client, no real game, no network."""

from __future__ import annotations

from bots.hunyuan import planner
from bots.hunyuan.model import Contact, Order
from bots.hunyuan.tests.helpers import (
    combat_army,
    memory,
    opened_base,
    snapshot,
    unit,
)


def kinds(orders: list[Order]) -> list[str]:
    return [o.kind for o in orders]


def units_requested(orders: list[Order], kind: str) -> list[Order]:
    return [o for o in orders if o.kind == "train" and o.unit == kind]


# ---------------------------------------------------------------------------
# entry / determinism
# ---------------------------------------------------------------------------
def test_entry_import_and_no_arg_instantiation():
    from sc2.bot_ai import BotAI

    from bots.hunyuan.bot import CompetitiveBot

    bot = CompetitiveBot()
    assert isinstance(bot, BotAI)
    assert bot.seed == 20261123


def test_planner_is_deterministic_with_fixed_seed():
    snap = snapshot(time=100, units=opened_base() + combat_army(10))
    m1 = memory(seed=42)
    m2 = memory(seed=42)
    o1 = planner.plan(snap, m1)
    o2 = planner.plan(snap, m2)
    assert [(o.kind, o.unit, o.target) for o in o1] == [(o.kind, o.unit, o.target) for o in o2]
    assert m1.stance == m2.stance


# ---------------------------------------------------------------------------
# opening / economy / supply
# ---------------------------------------------------------------------------
def test_opening_orders_depot_barracks_and_workers():
    units = [
        unit(1, "COMMANDCENTER", 10, 10, role="structure"),
        unit(2, "SUPPLYDEPOT", 14, 12, role="structure"),  # prerequisite for barracks
        *[unit(100 + i, "SCV", 11, 9, role="worker") for i in range(13)],
    ]
    snap = snapshot(time=20, minerals=400, vespene=100, supply_used=13, supply_cap=23, units=units)
    orders = planner.plan(snap, memory())
    assert units_requested(orders, "SCV"), "SCV production must never idle at the opening"
    assert any(o.kind == "build" and o.unit == "SUPPLYDEPOT" for o in orders)
    assert any(o.kind == "build" and o.unit == "BARRACKS" for o in orders)


def test_supply_block_triggers_depot():
    units = opened_base()
    snap = snapshot(
        time=120, minerals=600, supply_used=37, supply_cap=38, units=units,
        pending={"SUPPLYDEPOT": 0},
    )
    orders = planner.plan(snap, memory())
    depots = [o for o in orders if o.kind == "build" and o.unit == "SUPPLYDEPOT"]
    assert depots, "near supply cap must request a depot"


def test_expansion_triggers_from_bank_and_respects_cooldown():
    units = opened_base()
    snap = snapshot(time=150, minerals=900, units=units)
    m = memory()
    first = planner.plan(snap, m)
    assert any(o.kind == "expand" for o in first)
    again = planner.plan(snap, m)
    assert not any(o.kind == "expand" for o in again), "cooldown must prevent expand spam"


def test_orbital_morph_emitted_once():
    units = opened_base()
    snap = snapshot(time=120, minerals=600, units=units)
    orders = planner.plan(snap, memory())
    assert any(o.kind == "morph" for o in orders)
    units_with_orbital = units + [unit(50, "ORBITALCOMMAND", 12, 12, role="structure")]
    orders2 = planner.plan(snapshot(time=130, units=units_with_orbital), memory())
    assert not any(o.kind == "morph" for o in orders2)


# ---------------------------------------------------------------------------
# production / addons / upgrades
# ---------------------------------------------------------------------------
def test_addons_and_upgrades_are_planned():
    # Add one addon-less factory so an addon order is actually needed.
    units = opened_base() + [unit(80, "FACTORY", 24, 16, role="structure")]
    snap = snapshot(time=200, minerals=800, vespene=800, units=units, upgrades=set())
    orders = planner.plan(snap, memory())
    assert any(o.kind == "addon" for o in orders)
    upgrades = {o.upgrade for o in orders if o.kind == "research"}
    assert "STIMPACK" in upgrades


def test_army_mix_produces_marines_and_support():
    units = opened_base()
    snap = snapshot(time=200, minerals=1200, vespene=800, supply_left=30, units=units)
    orders = planner.plan(snap, memory())
    trained = {o.unit for o in orders if o.kind == "train"}
    assert "MARINE" in trained or "MARAUDER" in trained
    assert "SIEGETANK" in trained
    assert "MEDIVAC" in trained


# ---------------------------------------------------------------------------
# scouting
# ---------------------------------------------------------------------------
def test_scout_is_selected_and_recalled():
    snap = snapshot(time=10, units=opened_base())
    m = memory()
    orders = planner.plan(snap, m)
    scouts = [o for o in orders if o.kind == "scout"]
    assert scouts and scouts[0].target == "enemy_main"
    assert m.scout_stage == 1

    # Move the selected scout next to the enemy base -> it should be recalled home.
    scout_tag = m.scout_tag
    units = opened_base()
    for u in units:
        if u.tag == scout_tag:
            u.x, u.y = 150.0, 150.0
    orders2 = planner.plan(snapshot(time=30, units=units), m)
    assert any(o.kind == "scout" and o.target == "main" for o in orders2)


def test_orbital_scan_uses_normal_ability_with_energy():
    units = opened_base() + [unit(60, "ORBITALCOMMAND", 12, 12, energy=60, role="structure")]
    m = memory()
    m.scout_stage = 1
    m.last_scan_time = -100
    orders = planner.plan(snapshot(time=200, units=units), m)
    assert any(o.kind == "scan" for o in orders)


# ---------------------------------------------------------------------------
# defense, attack waves, and recovery
# ---------------------------------------------------------------------------
def test_early_harassment_switches_to_defend_and_pulls_workers():
    units = opened_base() + combat_army(6)
    raiders = [
        unit(2001, "ZERGLING", 12, 11, role="army"),
        unit(2002, "ZERGLING", 13, 11, role="army"),
    ]
    snap = snapshot(time=95, units=units, enemy_visible=raiders, supply_left=10)
    m = memory()
    orders = planner.plan(snap, m)
    assert m.stance == "DEFEND"
    assert any(o.kind == "attack" and o.target == "threat" for o in orders)
    assert any(o.kind == "worker_defend" for o in orders), "small early raid should pull workers"


def test_defense_releases_after_threat_disappears():
    units = opened_base() + combat_army(8)
    m = memory()
    raider = unit(2001, "ZERGLING", 12, 11)
    planner.plan(snapshot(time=95, units=units, enemy_visible=[raider]), m)
    assert m.stance == "DEFEND"
    # Threat gone and hold timer elapsed -> back to macro.
    planner.plan(snapshot(time=115, units=units, enemy_visible=[]), m)
    assert m.stance in {"MACRO", "ATTACK"}


def test_attack_launches_only_when_army_value_and_time_ready():
    small = opened_base() + combat_army(3)
    m1 = memory()
    planner.plan(snapshot(time=250, units=small), m1)
    assert m1.stance == "MACRO"

    big = opened_base() + combat_army(24)
    m2 = memory()
    orders = planner.plan(snapshot(time=250, units=big), m2)
    assert m2.stance == "ATTACK"
    assert any(o.kind == "attack" and o.target == "enemy_main" for o in orders)


def test_failed_first_attack_enters_regroup_then_resumes_macro():
    units = opened_base() + combat_army(24)
    m = memory()
    planner.plan(snapshot(time=250, units=units), m)
    assert m.stance == "ATTACK"

    # The attacking army is mostly wiped; only a few damaged units remain.
    remnants = opened_base() + combat_army(3)
    for u in remnants:
        if u.role == "army":
            u.hp = 10.0
    orders = planner.plan(snapshot(time=260, units=remnants), m)
    assert m.stance == "REGROUP"
    assert m.failed_waves == 1
    assert any(o.kind == "move" and o.target == "rally" for o in orders)

    # Economy/production continue while regrouping -- no all-in throwaway.
    assert units_requested(orders, "SCV") or any(o.kind == "train" for o in orders)

    # After the timer and with a rebuilt army the bot attacks again.
    rebuilt = opened_base() + combat_army(30)
    planner.plan(snapshot(time=290, units=rebuilt), m)
    assert m.stance == "ATTACK"
    assert m.failed_waves == 1


def test_failed_attack_raises_next_wave_threshold():
    m = memory()
    before = planner.attack_threshold(m)
    m.failed_waves = 2
    after = planner.attack_threshold(m)
    assert after > before


def test_attack_escapes_vision_remembered_target_is_used():
    contacts = [
        Contact("COMMANDCENTER", 140, 140, 100.0, True, 999),
        Contact("MARINE", 60, 60, 99.0, False, 998),
    ]
    snap = snapshot(time=250, enemy_memory=contacts, units=opened_base() + combat_army(24))
    m = memory()
    planner.plan(snap, m)
    assert m.attack_target == (140.0, 140.0)


# ---------------------------------------------------------------------------
# math helpers
# ---------------------------------------------------------------------------
def test_army_value_discounts_damaged_units():
    healthy = [unit(1, "MARINE", hp=50, hp_max=50)]
    hurt = [unit(2, "MARINE", hp=10, hp_max=50)]
    assert planner.army_value(healthy) > planner.army_value(hurt) > 0


def test_memory_seed_controls_rng():
    a = memory(seed=7)
    b = memory(seed=7)
    c = memory(seed=8)
    assert [a.rng.random() for _ in range(3)] == [b.rng.random() for _ in range(3)]
    assert a.rng.random() != c.rng.random()
