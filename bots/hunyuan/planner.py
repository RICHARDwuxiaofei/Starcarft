"""Deterministic, side-effect-free strategic planner for the Hunyuan Terran bot.

The planner receives a :class:`~bots.hunyuan.model.Snapshot` plus mutable
:class:`~bots.hunyuan.model.Memory` and returns a list of
:class:`~bots.hunyuan.model.Order`. It never calls the SC2 API and can be driven
by offline tests with synthetic snapshots.

Strategy summary (original "anvil" style):
  * saturate two bases with workers, keep SCV and supply production continuous;
  * a 1-1-1 opener (barracks / factory / starport) into marine-marauder-tank-medivac;
  * expand by mineral bank, scout with an SCV and with orbital scans;
  * hold a defensive rally until the army reaches an escalating value threshold,
    then attack in waves; a failed wave switches to REGROUP and resumes macro
    instead of throwing units away, so harassment and first-push failure are
    recoverable.
"""

from __future__ import annotations

from .model import (
    ARMORY_KINDS,
    BARRACKS_KINDS,
    CC_KINDS,
    COMBAT_KINDS,
    DEPOT_KINDS,
    ENGINEERING_KINDS,
    FACTORY_KINDS,
    REFINERY_KINDS,
    STARPORT_KINDS,
    UNIT_VALUE,
    Memory,
    Order,
    Snapshot,
    UnitInfo,
)

# ---- tunable constants --------------------------------------------------------
MAX_WORKERS = 72
WORKERS_PER_BASE = 20
GAS_WORKERS_PER_REFINERY = 3
SUPPLY_BUFFER = 5

DEFENSE_RADIUS = 15.0
DEFENSE_HOLD_SECONDS = 8.0

FIRST_ATTACK_SECONDS = 210.0
BASE_ATTACK_VALUE = 620.0
ATTACK_VALUE_GROWTH = 130.0
MAX_ATTACK_VALUE = 1450.0
ATTACK_COLLAPSE_RATIO = 0.4
ATTACK_TIMEOUT_SECONDS = 110.0

EXPANSION_BANK = 430
EXPANSION_COOLDOWN = 25.0
SCOUT_DEADLINE = 55.0
SCAN_INTERVAL = 35.0



# ---- small helpers ------------------------------------------------------------
def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _completed(snap: Snapshot, kinds: set[str]) -> list[UnitInfo]:
    return [u for u in snap.own(kinds) if u.ready]


def _building_count(snap: Snapshot, kinds: set[str]) -> int:
    """Completed structures plus any pending production of the same category."""
    pending = sum(snap.pending.get(k, 0) for k in kinds)
    return len(_completed(snap, kinds)) + pending


def _combat_units(snap: Snapshot) -> list[UnitInfo]:
    return [u for u in snap.units if u.kind in COMBAT_KINDS and u.ready]


def army_value(units: list[UnitInfo]) -> float:
    """Approximate the army's replacement cost, discounted for damage."""
    total = 0.0
    for u in units:
        value = UNIT_VALUE.get(u.kind, 0.0)
        if value and u.hp_max > 0:
            value *= 0.45 + 0.55 * max(0.0, min(1.0, u.hp / u.hp_max))
        total += value
    return total


def attack_threshold(memory: Memory) -> float:
    """Escalate the push requirement after each failed wave."""
    return min(MAX_ATTACK_VALUE, BASE_ATTACK_VALUE + ATTACK_VALUE_GROWTH * memory.failed_waves)


def _nearest(
    point: tuple[float, float], candidates: list[tuple[float, float]]
) -> tuple[float, float] | None:
    if not candidates:
        return None
    return min(candidates, key=lambda p: distance(point, p))


def rally_point(snap: Snapshot) -> tuple[float, float]:
    """Defensive rally between the main and the natural, toward the map centre."""
    main = snap.start
    centre = snap.map_center
    # A short step out of the main along the vector to map centre keeps the
    # army close enough to defend while still massing away from the mineral line.
    dx, dy = centre[0] - main[0], centre[1] - main[1]
    length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
    return (main[0] + dx / length * 7.0, main[1] + dy / length * 7.0)


def enemy_target(snap: Snapshot) -> tuple[float, float] | None:
    """Pick an attack target from remembered enemy structures, else spawn."""
    known_structures = [
        (c.x, c.y) for c in snap.enemy_memory if c.is_structure
    ]
    return _nearest(snap.enemy_start, known_structures) or snap.enemy_start or None


def _threat_centre(threats: list[UnitInfo]) -> tuple[float, float]:
    if not threats:
        return (0.0, 0.0)
    return (
        sum(u.x for u in threats) / len(threats),
        sum(u.y for u in threats) / len(threats),
    )


# ---- stance machine -----------------------------------------------------------
def update_stance(snap: Snapshot, memory: Memory) -> None:
    """Advance MACRO/DEFEND/ATTACK/REGROUP based on vision and army state."""
    threats = snap.threat_units(DEFENSE_RADIUS)
    if threats:
        memory.last_threat_time = snap.time
        memory.defend_until = snap.time + DEFENSE_HOLD_SECONDS

    combat = _combat_units(snap)
    value = army_value(combat)

    if memory.stance == "DEFEND":
        if snap.time >= memory.defend_until and not threats:
            memory.stance = "MACRO"
        return

    if memory.stance == "ATTACK":
        collapsed = value <= memory.attack_launch_value * ATTACK_COLLAPSE_RATIO
        timed_out = snap.time - memory.attack_launch_time > ATTACK_TIMEOUT_SECONDS
        if threats or collapsed or timed_out:
            memory.failed_waves += 1
            memory.stance = "REGROUP"
            memory.regroup_until = snap.time + 12.0
            memory.attack_target = None
        return

    if memory.stance == "REGROUP":
        if snap.time < memory.regroup_until:
            return
        if threats:
            memory.stance = "DEFEND"
            memory.defend_until = snap.time + DEFENSE_HOLD_SECONDS
            return
        if value >= attack_threshold(memory):
            memory.stance = "ATTACK"
            memory.attack_waves += 1
            memory.attack_launch_value = value
            memory.attack_launch_time = snap.time
            memory.attack_target = enemy_target(snap)
        else:
            memory.stance = "MACRO"
        return

    # MACRO
    if threats:
        memory.stance = "DEFEND"
        memory.defend_until = snap.time + DEFENSE_HOLD_SECONDS
        return
    if (
        snap.time >= FIRST_ATTACK_SECONDS
        and value >= attack_threshold(memory)
        and combat
    ):
        memory.stance = "ATTACK"
        memory.attack_waves += 1
        memory.attack_launch_value = value
        memory.attack_launch_time = snap.time
        memory.attack_target = enemy_target(snap)


# ---- economy & supply ---------------------------------------------------------
def plan_economy(snap: Snapshot, memory: Memory) -> list[Order]:
    orders: list[Order] = []
    worker_count = snap.count({"SCV"})
    bases = len(_completed(snap, CC_KINDS))
    desired_workers = min(MAX_WORKERS, max(12, bases * WORKERS_PER_BASE))

    scv_pending = snap.pending.get("SCV", 0)
    if worker_count + scv_pending < desired_workers:
        # One CC per frame maximum keeps command spam low; the adapter picks an idle CC.
        orders.append(Order(kind="train", unit="SCV"))

    # Supply: one depot when close to the cap, more when banked and still capped.
    total_depots = _building_count(snap, DEPOT_KINDS)
    want_more = snap.supply_left <= SUPPLY_BUFFER and snap.supply_cap < 200
    # Proactive depots only when supply is genuinely getting tight and growth
    # room is capped; never spend a large bank on depots we do not need.
    banked = (
        snap.minerals >= 300
        and 5 < snap.supply_left <= 12
        and snap.supply_cap < 120
        and total_depots < 8
    )
    if (want_more or banked) and snap.pending.get("SUPPLYDEPOT", 0) < 2 and snap.minerals >= 100:
        orders.append(Order(kind="build", unit="SUPPLYDEPOT", near="main"))

    # Expansion: bank-driven, with a cooldown so a blocked attempt cannot spam.
    if (
        bases < memory.target_expansions
        and snap.minerals >= EXPANSION_BANK
        and snap.time - memory.last_expand_time >= EXPANSION_COOLDOWN
    ):
        orders.append(Order(kind="expand"))
        memory.last_expand_time = snap.time

    return orders


def plan_tech(snap: Snapshot, _memory: Memory) -> list[Order]:
    """Upgrade the first command centre to an orbital command (scan economy)."""
    orbitals = len(_completed(snap, {"ORBITALCOMMAND"})) + snap.pending.get("ORBITALCOMMAND", 0)
    if orbitals == 0 and _completed(snap, BARRACKS_KINDS) and snap.minerals >= 150:
        return [Order(kind="morph", unit="COMMANDCENTER")]
    return []


def plan_gas(snap: Snapshot, _memory: Memory) -> list[Order]:
    """Request refinery construction when a base has spare geysers."""
    orders: list[Order] = []
    refineries = _building_count(snap, REFINERY_KINDS)
    bases = len(_completed(snap, CC_KINDS))
    max_refineries = max(1, bases * 2)
    if refineries < max_refineries and snap.pending.get("REFINERY", 0) < 1:
        if refineries == 0 and snap.minerals >= 75 and _completed(snap, BARRACKS_KINDS):
            orders.append(Order(kind="build", unit="REFINERY", near="main"))
        elif refineries > 0 and snap.minerals >= 150:
            orders.append(Order(kind="build", unit="REFINERY", near="natural"))
    return orders


# ---- buildings, addons, upgrades ---------------------------------------------
def plan_buildings(snap: Snapshot, _memory: Memory) -> list[Order]:
    """Goal-based production building plan driven by counts, not wall-clock order."""
    orders: list[Order] = []
    m = snap.minerals
    g = snap.vespene
    barracks = _building_count(snap, BARRACKS_KINDS)
    factories = _building_count(snap, FACTORY_KINDS)
    starports = _building_count(snap, STARPORT_KINDS)
    eng_bays = _building_count(snap, ENGINEERING_KINDS)
    armories = _building_count(snap, ARMORY_KINDS)

    first_barracks = barracks < 1 and m >= 150 and _completed(snap, DEPOT_KINDS)
    second_barracks = barracks < 2 and m >= 220 and factories >= 1
    if first_barracks or second_barracks:
        orders.append(Order(kind="build", unit="BARRACKS", near="main"))
    elif barracks < 4 and m >= 400 and len(_completed(snap, CC_KINDS)) >= 2:
        orders.append(Order(kind="build", unit="BARRACKS", near="rally"))

    if factories < 1 and m >= 200 and g >= 90 and _completed(snap, BARRACKS_KINDS):
        orders.append(Order(kind="build", unit="FACTORY", near="main"))
    elif factories < 2 and m >= 350 and g >= 150 and len(_completed(snap, CC_KINDS)) >= 2:
        orders.append(Order(kind="build", unit="FACTORY", near="rally"))

    if starports < 1 and m >= 200 and g >= 100 and _completed(snap, FACTORY_KINDS):
        orders.append(Order(kind="build", unit="STARPORT", near="main"))
    elif starports < 2 and m >= 400 and g >= 200 and len(_completed(snap, CC_KINDS)) >= 3:
        orders.append(Order(kind="build", unit="STARPORT", near="rally"))

    if eng_bays < 1 and m >= 125 and _completed(snap, BARRACKS_KINDS):
        orders.append(Order(kind="build", unit="ENGINEERINGBAY", near="main"))

    if armories < 1 and m >= 220 and g >= 120 and _completed(snap, FACTORY_KINDS):
        orders.append(Order(kind="build", unit="ARMORY", near="rally"))

    return orders


def plan_addons(snap: Snapshot, _memory: Memory) -> list[Order]:
    orders: list[Order] = []
    ready_barracks = _completed(snap, BARRACKS_KINDS)
    ready_factories = _completed(snap, FACTORY_KINDS)
    ready_starports = _completed(snap, STARPORT_KINDS)

    def needs(structures: list[UnitInfo], kind: set[str]) -> bool:
        return any(s.ready and not s.addon and s.kind in kind for s in structures)

    if snap.minerals >= 50 and needs(ready_barracks, BARRACKS_KINDS):
        # First barracks tech lab (stim/marauder), second reactor (marine flood).
        techlabs = sum(1 for s in ready_barracks if s.addon == "techlab")
        reactors = sum(1 for s in ready_barracks if s.addon == "reactor")
        want = "techlab" if techlabs <= reactors else "reactor"
        orders.append(Order(kind="addon", unit="BARRACKS", addon=want))

    if snap.minerals >= 50 and needs(ready_factories, FACTORY_KINDS):
        orders.append(Order(kind="addon", unit="FACTORY", addon="techlab"))

    if snap.minerals >= 50 and needs(ready_starports, STARPORT_KINDS):
        orders.append(Order(kind="addon", unit="STARPORT", addon="reactor"))

    return orders


def plan_upgrades(snap: Snapshot, _memory: Memory) -> list[Order]:
    orders: list[Order] = []
    up = snap.upgrades
    has_barracks_tech = any(
        s.addon == "techlab" for s in _completed(snap, BARRACKS_KINDS)
    )
    has_eng = bool(_completed(snap, ENGINEERING_KINDS))
    has_armory = bool(_completed(snap, ARMORY_KINDS))
    m, g = snap.minerals, snap.vespene

    def want(name: str, cost_m: int, cost_g: int, enabled: bool) -> bool:
        return (
            enabled
            and name not in up
            and m >= cost_m
            and g >= cost_g
            and snap.pending.get(f"UPGRADE:{name}", 0) < 1
        )

    # Names are the burnysc2 7.3.0 UpgradeId enum names. In this build the
    # concussive-shell upgrade is exposed as PUNISHERGRENADES and combat
    # shield as SHIELDWALL (legacy names retained in the protobuf data).
    if want("STIMPACK", 100, 100, has_barracks_tech):
        orders.append(Order(kind="research", upgrade="STIMPACK"))
    elif want("SHIELDWALL", 100, 100, has_barracks_tech and "STIMPACK" in up):
        orders.append(Order(kind="research", upgrade="SHIELDWALL"))
    elif want("PUNISHERGRENADES", 50, 50, has_barracks_tech):
        orders.append(Order(kind="research", upgrade="PUNISHERGRENADES"))

    if want("TERRANINFANTRYWEAPONSLEVEL1", 100, 100, has_eng):
        orders.append(Order(kind="research", upgrade="TERRANINFANTRYWEAPONSLEVEL1"))
    elif want("TERRANINFANTRYARMORSLEVEL1", 100, 100, has_eng):
        orders.append(Order(kind="research", upgrade="TERRANINFANTRYARMORSLEVEL1"))
    if want("TERRANINFANTRYWEAPONSLEVEL2", 150, 150, has_eng and "TERRANINFANTRYWEAPONSLEVEL1" in up):
        orders.append(Order(kind="research", upgrade="TERRANINFANTRYWEAPONSLEVEL2"))

    if want("TERRANVEHICLEWEAPONSLEVEL1", 150, 150, has_armory):
        orders.append(Order(kind="research", upgrade="TERRANVEHICLEWEAPONSLEVEL1"))
    return orders


def plan_production(snap: Snapshot, _memory: Memory) -> list[Order]:
    """Train army units toward a marine/marauder/tank/medivac composition."""
    orders: list[Order] = []
    units = snap.units
    marines = sum(1 for u in units if u.kind == "MARINE")
    marauders = sum(1 for u in units if u.kind == "MARAUDER")
    tanks = sum(1 for u in units if u.kind in {"SIEGETANK", "SIEGETANKSIEGED"})
    medivacs = sum(1 for u in units if u.kind == "MEDIVAC")
    total_ground = marines + marauders + tanks

    barracks_ready = _completed(snap, BARRACKS_KINDS)
    factories_ready = _completed(snap, FACTORY_KINDS)
    starports_ready = _completed(snap, STARPORT_KINDS)

    if snap.minerals >= 50 and snap.supply_left > 0 and barracks_ready:
        if marauders * 3 < max(2, marines) and snap.vespene >= 25:
            orders.append(Order(kind="train", unit="MARAUDER"))
        else:
            orders.append(Order(kind="train", unit="MARINE"))

    if (
        snap.minerals >= 150
        and snap.vespene >= 100
        and snap.supply_left >= 2
        and factories_ready
        and tanks * 8 < max(8, total_ground)
    ):
        orders.append(Order(kind="train", unit="SIEGETANK"))

    if (
        snap.minerals >= 100
        and snap.vespene >= 100
        and snap.supply_left >= 2
        and starports_ready
        and medivacs * 7 < max(7, total_ground)
    ):
        orders.append(Order(kind="train", unit="MEDIVAC"))

    return orders


# ---- scouting & combat --------------------------------------------------------
def plan_scouting(snap: Snapshot, memory: Memory) -> list[Order]:
    orders: list[Order] = []
    scout = next((u for u in snap.units if u.tag == memory.scout_tag), None)

    if memory.scout_stage == 0:
        workers = [u for u in snap.units if u.kind == "SCV" and u.ready]
        if workers:
            # Deterministic choice: the worker nearest the map centre, ties by tag.
            scout = min(workers, key=lambda w: (distance((w.x, w.y), snap.map_center), w.tag))
            memory.scout_tag = scout.tag
            memory.scout_stage = 1
            memory.scout_deadline = snap.time + SCOUT_DEADLINE
            orders.append(Order(kind="scout", tags=(scout.tag,), target="enemy_main"))
    elif memory.scout_stage == 1 and scout is not None:
        if snap.time >= memory.scout_deadline or distance((scout.x, scout.y), snap.enemy_start) < 8:
            memory.scout_stage = 2
            orders.append(Order(kind="scout", tags=(scout.tag,), target="main"))
    elif memory.scout_stage == 2 and scout is not None and distance((scout.x, scout.y), snap.start) < 8:
        memory.scout_stage = 3

    # Orbital scan: legal ability, reveals only through fog via a real scan.
    if (
        memory.scout_stage >= 1
        and snap.time - memory.last_scan_time >= SCAN_INTERVAL
        and snap.time > 150.0
    ):
        orbitals = [u for u in snap.own(CC_KINDS) if u.kind == "ORBITALCOMMAND" and u.energy >= 50]
        if orbitals:
            memory.last_scan_time = snap.time
            orders.append(Order(kind="scan", tags=(orbitals[0].tag,), target="enemy_main"))
    return orders


def plan_combat(snap: Snapshot, memory: Memory) -> list[Order]:
    orders: list[Order] = []
    combat = _combat_units(snap)
    if not combat:
        return orders
    tags = tuple(u.tag for u in combat)

    if memory.stance == "DEFEND":
        threats = snap.threat_units(DEFENSE_RADIUS)
        centre = _threat_centre(threats) if threats else rally_point(snap)
        orders.append(Order(kind="attack", tags=tags, x=centre[0], y=centre[1], target="threat"))
        # Emergency worker pull only against a small, early raid.
        if threats and len(threats) <= 3 and snap.time < 240 and snap.count({"SCV"}) >= 8:
            workers = [
                u for u in snap.own({"SCV"}) if u.ready
                and distance((u.x, u.y), centre) <= DEFENSE_RADIUS
            ]
            if workers:
                orders.append(
                    Order(kind="worker_defend", tags=tuple(w.tag for w in workers[:6]),
                          x=centre[0], y=centre[1])
                )
    elif memory.stance == "ATTACK":
        target = memory.attack_target or enemy_target(snap) or snap.enemy_start
        orders.append(Order(kind="attack", tags=tags, x=target[0], y=target[1], target="enemy_main"))
    else:  # MACRO or REGROUP: hold at the defensive rally.
        point = rally_point(snap)
        orders.append(Order(kind="move", tags=tags, x=point[0], y=point[1], target="rally"))

    return orders


# ---- top-level ----------------------------------------------------------------
def plan(snap: Snapshot, memory: Memory) -> list[Order]:
    """Return all orders for this frame. Pure apart from updating ``memory``."""
    update_stance(snap, memory)
    orders: list[Order] = []
    orders.extend(plan_economy(snap, memory))
    orders.extend(plan_tech(snap, memory))
    orders.extend(plan_gas(snap, memory))
    orders.extend(plan_buildings(snap, memory))
    orders.extend(plan_addons(snap, memory))
    orders.extend(plan_upgrades(snap, memory))
    orders.extend(plan_production(snap, memory))
    orders.extend(plan_scouting(snap, memory))
    orders.extend(plan_combat(snap, memory))
    return orders
