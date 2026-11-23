"""Pure data models for the Hunyuan Terran bot decision core.

Nothing in this module imports ``sc2`` and nothing here touches a live game.
Unit kinds are stored as ``UnitTypeId`` *names* (strings); the BotAI adapter in
``bot.py`` is the only place that translates them to real sc2 identifiers.
That keeps the strategic logic fully exercisable in offline tests.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class UnitInfo:
    """A lightweight observation of one friendly or enemy unit."""

    tag: int
    kind: str
    x: float
    y: float
    hp: float = 0.0
    hp_max: float = 0.0
    progress: float = 1.0
    energy: float = 0.0
    addon: str | None = None
    role: str = ""            # "worker" | "army" | "structure" | ""
    idle: bool = False
    order_abilities: tuple[int, ...] = ()

    @property
    def ready(self) -> bool:
        return self.progress >= 0.99


@dataclass
class Contact:
    """Last-seen information about an enemy unit, remembered under fog of war."""

    kind: str
    x: float
    y: float
    last_seen: float
    is_structure: bool = False
    tag: int = 0


@dataclass
class Snapshot:
    """Per-frame view handed to the planner. All values derive from normal vision."""

    time: float
    minerals: int
    vespene: int
    supply_used: int
    supply_cap: int
    supply_left: int
    units: list[UnitInfo] = field(default_factory=list)
    enemy_visible: list[UnitInfo] = field(default_factory=list)
    enemy_memory: list[Contact] = field(default_factory=list)
    expansion_points: list[tuple[float, float]] = field(default_factory=list)
    start: tuple[float, float] = (0.0, 0.0)
    enemy_start: tuple[float, float] = (0.0, 0.0)
    map_center: tuple[float, float] = (0.0, 0.0)
    pending: dict[str, int] = field(default_factory=dict)
    upgrades: set[str] = field(default_factory=set)
    alerts: set[str] = field(default_factory=set)

    # ---- convenience queries -------------------------------------------------
    def own(self, kinds: set[str] | str, ready_only: bool = False) -> list[UnitInfo]:
        wanted = {kinds} if isinstance(kinds, str) else set(kinds)
        return [
            u
            for u in self.units
            if u.kind in wanted and (not ready_only or u.ready)
        ]

    def count(self, kinds: set[str] | str, ready_only: bool = False) -> int:
        return len(self.own(kinds, ready_only))

    def threat_units(self, radius: float = 15.0) -> list[UnitInfo]:
        bases = self.own(CC_KINDS, ready_only=True)
        result: list[UnitInfo] = []
        for e in self.enemy_visible:
            if e.kind in NON_THREAT_ENEMY:
                continue
            for base in bases:
                if (e.x - base.x) ** 2 + (e.y - base.y) ** 2 <= radius * radius:
                    result.append(e)
                    break
        return result


# ---- unit category aliases ----------------------------------------------------
CC_KINDS = {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYCOMMAND", "COMMANDCENTERFLYING"}
DEPOT_KINDS = {"SUPPLYDEPOT", "SUPPLYDEPOTLOWERED"}
REFINERY_KINDS = {"REFINERY"}
BARRACKS_KINDS = {"BARRACKS", "BARRACKSFLYING"}
FACTORY_KINDS = {"FACTORY", "FACTORYFLYING"}
STARPORT_KINDS = {"STARPORT", "STARPORTFLYING"}
TECHLAB_KINDS = {
    "BARRACKSTECHLAB", "FACTORYTECHLAB", "STARPORTTECHLAB",
    "TECHLAB", "GHOSTACADEMY",  # academy is not an addon but counts as tech source for some
}
REACTOR_KINDS = {"BARRACKSREACTOR", "FACTORYREACTOR", "STARPORTREACTOR", "REACTOR"}
ENGINEERING_KINDS = {"ENGINEERINGBAY"}
ARMORY_KINDS = {"ARMORY"}

WORKER_KINDS = {"SCV"}
COMBAT_KINDS = {"MARINE", "MARAUDER", "SIEGETANK", "SIEGETANKSIEGED", "MEDIVAC", "THOR", "LIBERATOR"}
GROUND_COMBAT_KINDS = {"MARINE", "MARAUDER", "SIEGETANK", "SIEGETANKSIEGED", "THOR"}
AIR_COMBAT_KINDS = {"MEDIVAC", "LIBERATOR"}

NON_THREAT_ENEMY = {
    "OVERLORD", "OVERSEER", "DRONE", "SCV", "PROBE", "MOTHership",
    "LARVA", "EGG", "CREEP_TUMOR_BURROWED", "CREEP_TUMOR",
}

UNIT_VALUE = {
    "MARINE": 50.0,
    "MARAUDER": 100.0,
    "SIEGETANK": 150.0,
    "SIEGETANKSIEGED": 150.0,
    "MEDIVAC": 100.0,
    "THOR": 250.0,
    "LIBERATOR": 150.0,
}


@dataclass(frozen=True)
class Order:
    """A single directive emitted by the planner and executed by the adapter."""

    kind: str
    unit: str | None = None          # unit kind to train/build, or ability/morph subject
    amount: int = 1
    near: str | None = None          # symbolic placement: "main" | "natural" | "rally"
    addon: str | None = None         # "techlab" | "reactor"
    upgrade: str | None = None
    target: str | None = None        # symbolic target: "enemy_main" | "rally" | "threat" | "auto"
    x: float | None = None
    y: float | None = None
    tags: tuple[int, ...] = ()
    meta: str = ""


@dataclass
class Memory:
    """Mutable cross-frame state owned by one bot instance."""

    seed: int
    rng: random.Random = field(default_factory=lambda: random.Random(0))

    stance: str = "MACRO"            # MACRO | DEFEND | ATTACK | REGROUP
    scout_tag: int = 0
    scout_stage: int = 0             # 0 none, 1 outbound, 2 returning, 3 done
    scout_deadline: float = 0.0

    attack_waves: int = 0
    failed_waves: int = 0
    attack_launch_value: float = 0.0
    attack_launch_time: float = 0.0
    regroup_until: float = 0.0
    attack_target: tuple[float, float] | None = None

    last_threat_time: float = -1e9
    defend_until: float = 0.0
    last_expand_time: float = -1e9
    last_scan_time: float = -1e9
    target_expansions: int = 3

    def __post_init__(self) -> None:
        self.rng.seed(self.seed)
