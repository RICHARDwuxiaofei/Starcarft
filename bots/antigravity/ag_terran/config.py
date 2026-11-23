"""Shared constants, unit classifications, and configuration for the Terran bot."""

from __future__ import annotations

from dataclasses import dataclass

from sc2.ids.unit_typeid import UnitTypeId
from sc2.unit import Unit

TOWNHALL_LANDED_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.COMMANDCENTER,
        UnitTypeId.ORBITALCOMMAND,
        UnitTypeId.PLANETARYFORTRESS,
    }
)

TOWNHALL_FLYING_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.COMMANDCENTERFLYING,
        UnitTypeId.ORBITALCOMMANDFLYING,
    }
)

TOWNHALL_ALL_TYPES: frozenset[UnitTypeId] = (
    TOWNHALL_LANDED_TYPES | TOWNHALL_FLYING_TYPES
)

DEPOT_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SUPPLYDEPOT,
        UnitTypeId.SUPPLYDEPOTLOWERED,
    }
)

TANK_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SIEGETANK,
        UnitTypeId.SIEGETANKSIEGED,
    }
)

VIKING_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.VIKINGFIGHTER,
        UnitTypeId.VIKINGASSAULT,
    }
)

LIBERATOR_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.LIBERATOR,
        UnitTypeId.LIBERATORAG,
    }
)

WIDOWMINE_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.WIDOWMINE,
        UnitTypeId.WIDOWMINEBURROWED,
    }
)

WORKER_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SCV,
        UnitTypeId.PROBE,
        UnitTypeId.DRONE,
        UnitTypeId.MULE,
    }
)

OWN_NON_COMBAT_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SCV,
        UnitTypeId.MULE,
        UnitTypeId.LARVA,
        UnitTypeId.OVERLORD,
    }
)

CLOAK_OR_BURROW_ENEMY_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.DARKTEMPLAR,
        UnitTypeId.BANSHEE,
        UnitTypeId.WIDOWMINE,
        UnitTypeId.WIDOWMINEBURROWED,
        UnitTypeId.GHOST,
        UnitTypeId.LURKERMP,
        UnitTypeId.LURKERMPBURROWED,
        UnitTypeId.ROACHBURROWED,
        UnitTypeId.INFESTORBURROWED,
        UnitTypeId.SWARMHOSTBURROWEDMP,
        UnitTypeId.MOTHERSHIP,
        UnitTypeId.OBSERVER,
    }
)

LETHAL_HARASS_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.HELLION,
        UnitTypeId.HELLIONTANK,
        UnitTypeId.BANSHEE,
        UnitTypeId.LIBERATOR,
        UnitTypeId.LIBERATORAG,
        UnitTypeId.WIDOWMINE,
        UnitTypeId.WIDOWMINEBURROWED,
        UnitTypeId.CYCLONE,
        UnitTypeId.ORACLE,
        UnitTypeId.DARKTEMPLAR,
        UnitTypeId.ADEPT,
        UnitTypeId.MUTALISK,
        UnitTypeId.LURKERMP,
        UnitTypeId.LURKERMPBURROWED,
        UnitTypeId.BANELING,
        UnitTypeId.BATTLECRUISER,
    }
)

STATIC_DEFENSE_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.BUNKER,
        UnitTypeId.PLANETARYFORTRESS,
        UnitTypeId.MISSILETURRET,
        UnitTypeId.PHOTONCANNON,
        UnitTypeId.SPINECRAWLER,
        UnitTypeId.SPORECRAWLER,
    }
)

ANTI_AIR_THREAT_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.MARINE,
        UnitTypeId.VIKINGFIGHTER,
        UnitTypeId.CYCLONE,
        UnitTypeId.THOR,
        UnitTypeId.GHOST,
        UnitTypeId.MISSILETURRET,
        UnitTypeId.BUNKER,
        UnitTypeId.STALKER,
        UnitTypeId.PHOENIX,
        UnitTypeId.VOIDRAY,
        UnitTypeId.CARRIER,
        UnitTypeId.TEMPEST,
        UnitTypeId.ARCHON,
        UnitTypeId.PHOTONCANNON,
        UnitTypeId.QUEEN,
        UnitTypeId.HYDRALISK,
        UnitTypeId.MUTALISK,
        UnitTypeId.CORRUPTOR,
        UnitTypeId.SPORECRAWLER,
    }
)

MATRIX_TARGETS: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SIEGETANKSIEGED,
        UnitTypeId.SIEGETANK,
        UnitTypeId.THOR,
        UnitTypeId.BATTLECRUISER,
        UnitTypeId.RAVEN,
        UnitTypeId.BANSHEE,
        UnitTypeId.LIBERATOR,
        UnitTypeId.LIBERATORAG,
        UnitTypeId.COLOSSUS,
        UnitTypeId.DISRUPTOR,
        UnitTypeId.HIGHTEMPLAR,
        UnitTypeId.IMMORTAL,
        UnitTypeId.CARRIER,
        UnitTypeId.TEMPEST,
        UnitTypeId.INFESTOR,
        UnitTypeId.VIPER,
        UnitTypeId.LURKERMPBURROWED,
    }
)

AIR_PRODUCTION_PRIORITY_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.BANSHEE,
        UnitTypeId.BATTLECRUISER,
        UnitTypeId.LIBERATOR,
        UnitTypeId.LIBERATORAG,
        UnitTypeId.MEDIVAC,
        UnitTypeId.RAVEN,
        UnitTypeId.VIKINGFIGHTER,
        UnitTypeId.COLOSSUS,
        UnitTypeId.VOIDRAY,
        UnitTypeId.CARRIER,
        UnitTypeId.TEMPEST,
        UnitTypeId.PHOENIX,
        UnitTypeId.ORACLE,
        UnitTypeId.WARPPRISM,
        UnitTypeId.MOTHERSHIP,
        UnitTypeId.MUTALISK,
        UnitTypeId.CORRUPTOR,
        UnitTypeId.BROODLORD,
        UnitTypeId.VIPER,
    }
)

HEAVY_ARMOR_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.MARAUDER,
        UnitTypeId.SIEGETANK,
        UnitTypeId.SIEGETANKSIEGED,
        UnitTypeId.THOR,
        UnitTypeId.HELLIONTANK,
        UnitTypeId.CYCLONE,
        UnitTypeId.BATTLECRUISER,
        UnitTypeId.STALKER,
        UnitTypeId.IMMORTAL,
        UnitTypeId.COLOSSUS,
        UnitTypeId.ROACH,
        UnitTypeId.RAVAGER,
        UnitTypeId.ULTRALISK,
        UnitTypeId.LURKERMP,
        UnitTypeId.LURKERMPBURROWED,
    }
)

UNIT_COMBAT_VALUE: dict[UnitTypeId, float] = {
    # Terran
    UnitTypeId.SCV: 0.25,
    UnitTypeId.MULE: 0.0,
    UnitTypeId.MARINE: 1.1,
    UnitTypeId.MARAUDER: 2.4,
    UnitTypeId.REAPER: 1.0,
    UnitTypeId.GHOST: 3.5,
    UnitTypeId.HELLION: 1.6,
    UnitTypeId.HELLIONTANK: 2.3,
    UnitTypeId.WIDOWMINE: 2.0,
    UnitTypeId.WIDOWMINEBURROWED: 2.3,
    UnitTypeId.CYCLONE: 3.0,
    UnitTypeId.SIEGETANK: 4.2,
    UnitTypeId.SIEGETANKSIEGED: 5.5,
    UnitTypeId.THOR: 8.5,
    UnitTypeId.VIKINGFIGHTER: 2.5,
    UnitTypeId.VIKINGASSAULT: 1.8,
    UnitTypeId.MEDIVAC: 2.0,
    UnitTypeId.LIBERATOR: 3.6,
    UnitTypeId.LIBERATORAG: 4.2,
    UnitTypeId.RAVEN: 3.0,
    UnitTypeId.BANSHEE: 3.5,
    UnitTypeId.BATTLECRUISER: 10.0,
    UnitTypeId.BUNKER: 5.0,
    UnitTypeId.PLANETARYFORTRESS: 9.0,
    UnitTypeId.MISSILETURRET: 2.2,
    # Protoss
    UnitTypeId.PROBE: 0.25,
    UnitTypeId.ZEALOT: 2.0,
    UnitTypeId.ADEPT: 2.0,
    UnitTypeId.STALKER: 2.6,
    UnitTypeId.SENTRY: 1.8,
    UnitTypeId.HIGHTEMPLAR: 4.0,
    UnitTypeId.DARKTEMPLAR: 4.0,
    UnitTypeId.ARCHON: 7.5,
    UnitTypeId.IMMORTAL: 5.5,
    UnitTypeId.COLOSSUS: 7.5,
    UnitTypeId.DISRUPTOR: 6.0,
    UnitTypeId.PHOENIX: 2.4,
    UnitTypeId.ORACLE: 3.2,
    UnitTypeId.VOIDRAY: 4.5,
    UnitTypeId.CARRIER: 9.5,
    UnitTypeId.TEMPEST: 7.5,
    UnitTypeId.MOTHERSHIP: 10.0,
    UnitTypeId.PHOTONCANNON: 3.0,
    # Zerg
    UnitTypeId.DRONE: 0.25,
    UnitTypeId.ZERGLING: 0.55,
    UnitTypeId.BANELING: 1.4,
    UnitTypeId.QUEEN: 2.2,
    UnitTypeId.ROACH: 2.0,
    UnitTypeId.RAVAGER: 3.0,
    UnitTypeId.HYDRALISK: 2.6,
    UnitTypeId.LURKERMP: 4.5,
    UnitTypeId.LURKERMPBURROWED: 5.5,
    UnitTypeId.MUTALISK: 2.8,
    UnitTypeId.CORRUPTOR: 3.2,
    UnitTypeId.INFESTOR: 4.2,
    UnitTypeId.VIPER: 4.8,
    UnitTypeId.ULTRALISK: 8.5,
    UnitTypeId.BROODLORD: 8.5,
    UnitTypeId.SPINECRAWLER: 2.8,
    UnitTypeId.SPORECRAWLER: 1.8,
}


def unit_combat_value(unit: Unit, include_workers: bool = False) -> float:
    """Return weighted combat value scaled by current health percentage."""
    type_id = unit.type_id
    if type_id in WORKER_TYPES and not include_workers:
        return 0.0
    base = UNIT_COMBAT_VALUE.get(type_id)
    if base is None:
        if unit.is_structure:
            base = 3.0 if (unit.can_attack_ground or unit.can_attack_air) else 0.0
        elif unit.can_attack_ground or unit.can_attack_air:
            base = 1.6
        else:
            base = 0.0
    if base <= 0.0:
        return 0.0
    hp_ratio = max(0.0, min(1.0, float(unit.health_percentage)))
    return base * (0.4 + 0.6 * hp_ratio)


@dataclass(frozen=True)
class BotConfig:
    """Centralized strategy, macro, base triage, and micro parameters."""

    max_workers: int = 70
    timing_push_supply: int = 34
    advantage_push_supply: int = 46
    force_push_army_supply: int = 80
    force_push_total_supply: int = 165
    retreat_power_ratio: float = 0.72
    base_overwhelm_ratio: float = 1.35
    harass_clear_delay: float = 2.0
    abandoned_recheck_seconds: float = 75.0
    siege_engage_range: float = 12.5
    unsiege_clear_range: float = 14.5
    unsiege_clear_delay: float = 4.0
    max_enemy_memory_seconds: float = 45.0
    min_expansion_minerals: int = 300
