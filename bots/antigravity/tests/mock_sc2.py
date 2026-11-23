"""Faithful burnysc2==7.3.0 offline simulation harness for unit and scenario tests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from s2clientprotocol import data_pb2, sc2api_pb2
from sc2.data import Alliance, Attribute, CloakState, DisplayType, Race, Target
from sc2.dicts.generic_redirect_abilities import GENERIC_REDIRECT_ABILITIES
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.game_data import GameData
from sc2.game_info import GameInfo
from sc2.game_state import GameState
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit_command import UnitCommand

from bots.antigravity.bot import CompetitiveBot

# Map-level 4-player RedCityLE-like geometry (160x160).
RED_CITY_SPAWNS: list[Point2] = [
    Point2((32.0, 32.0)),  # Bottom-left (Bot main)
    Point2((128.0, 32.0)),  # Bottom-right
    Point2((128.0, 128.0)),  # Top-right
    Point2((32.0, 128.0)),  # Top-left
]

RED_CITY_EXPANSIONS: list[Point2] = [
    Point2((32.0, 32.0)),  # Main
    Point2((54.0, 34.0)),  # Natural (toward bottom-center)
    Point2((80.0, 34.0)),  # Forward/contested 3rd (toward (128, 32))
    Point2((34.0, 72.0)),  # Safe back/side 3rd (away from (128, 32))
    Point2((34.0, 100.0)),  # Safe 4th
    Point2((128.0, 32.0)),  # Enemy spawn A
    Point2((106.0, 34.0)),  # Enemy A natural
    Point2((128.0, 128.0)),  # Enemy spawn B
    Point2((106.0, 126.0)),  # Enemy B natural
    Point2((32.0, 128.0)),  # Enemy spawn C
    Point2((54.0, 126.0)),  # Enemy C natural
]


def _ability_target_type(ability: AbilityId) -> int:
    name = ability.name
    if any(
        k in name
        for k in (
            "MOVE",
            "ATTACK",
            "PATROL",
            "SMART",
            "RALLY",
            "REPAIR",
            "KD8CHARGE",
            "SCANNERSWEEP",
            "AUTOTURRET",
            "LAND_",
            "HARVEST_GATHER",
            "LOAD_",
            "INTERFERENCEMATRIX",
            "ANTIARMORMISSILE",
            "SUPPLYDROP",
            "CALLDOWNMULE",
        )
    ):
        if any(
            k in name
            for k in (
                "HARVEST_GATHER",
                "LOAD_",
                "SUPPLYDROP",
                "CALLDOWNMULE",
                "INTERFERENCEMATRIX",
            )
        ):
            return Target.Unit.value
        if any(k in name for k in ("KD8CHARGE", "SCANNERSWEEP", "AUTOTURRET", "LAND_")):
            return Target.Point.value
        return Target.PointOrUnit.value
    if name.startswith("TERRANBUILD_REFINERY"):
        return Target.Unit.value
    if name.startswith("TERRANBUILD_"):
        return Target.Point.value
    if "BUILD_TECHLAB" in name or "BUILD_REACTOR" in name:
        return Target.PointOrNone.value
    return getattr(Target, "None").value


@dataclass
class UnitSpec:
    unit_id: UnitTypeId
    minerals: int = 0
    vespene: int = 0
    food_required: float = 0.0
    food_provided: float = 0.0
    max_hp: float = 100.0
    max_shield: float = 0.0
    max_energy: float = 0.0
    armor: float = 0.0
    radius: float = 0.5
    sight_range: float = 9.0
    speed: float = 3.15
    attributes: list[int] = field(default_factory=list)
    ability_id: AbilityId = AbilityId.NULL_NULL
    tech_alias: list[UnitTypeId] = field(default_factory=list)
    weapons: list[tuple[int, float, float, int, float]] = field(
        default_factory=list
    )  # (target_type, damage, speed, attacks, range)
    race: Race = Race.Terran
    cargo_size: int = 1


_T_GROUND = data_pb2.Weapon.TargetType.Value("Ground")
_T_AIR = data_pb2.Weapon.TargetType.Value("Air")
_T_ANY = data_pb2.Weapon.TargetType.Value("Any")

_ATTR_LIGHT = Attribute.Light.value
_ATTR_ARMORED = Attribute.Armored.value
_ATTR_BIO = Attribute.Biological.value
_ATTR_MECH = Attribute.Mechanical.value
_ATTR_STRUCT = Attribute.Structure.value
_ATTR_PSIONIC = Attribute.Psionic.value
_ATTR_MASSIVE = Attribute.Massive.value


UNIT_SPECS: list[UnitSpec] = [
    # Terran Units
    UnitSpec(
        UnitTypeId.SCV,
        50,
        0,
        1.0,
        0,
        45.0,
        radius=0.375,
        sight_range=8.0,
        speed=2.8,
        attributes=[_ATTR_LIGHT, _ATTR_BIO, _ATTR_MECH],
        ability_id=AbilityId.COMMANDCENTERTRAIN_SCV,
        weapons=[(_T_GROUND, 5.0, 1.5, 1, 0.1)],
    ),
    UnitSpec(
        UnitTypeId.MULE,
        0,
        0,
        0.0,
        0,
        60.0,
        radius=0.375,
        sight_range=8.0,
        speed=2.8,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        ability_id=AbilityId.CALLDOWNMULE_CALLDOWNMULE,
    ),
    UnitSpec(
        UnitTypeId.MARINE,
        50,
        0,
        1.0,
        0,
        45.0,
        radius=0.375,
        sight_range=9.0,
        speed=2.25,
        attributes=[_ATTR_LIGHT, _ATTR_BIO],
        ability_id=AbilityId.BARRACKSTRAIN_MARINE,
        weapons=[(_T_ANY, 6.0, 0.86, 1, 5.0)],
    ),
    UnitSpec(
        UnitTypeId.MARAUDER,
        100,
        25,
        2.0,
        0,
        125.0,
        armor=1.0,
        radius=0.5625,
        sight_range=10.0,
        speed=2.25,
        attributes=[_ATTR_ARMORED, _ATTR_BIO],
        ability_id=AbilityId.BARRACKSTRAIN_MARAUDER,
        weapons=[(_T_GROUND, 10.0, 1.5, 2, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.REAPER,
        50,
        50,
        1.0,
        0,
        60.0,
        radius=0.375,
        sight_range=9.0,
        speed=3.75,
        attributes=[_ATTR_LIGHT, _ATTR_BIO],
        ability_id=AbilityId.BARRACKSTRAIN_REAPER,
        weapons=[(_T_GROUND, 4.0, 1.1, 2, 5.0)],
    ),
    UnitSpec(
        UnitTypeId.GHOST,
        150,
        125,
        2.0,
        0,
        125.0,
        radius=0.375,
        sight_range=11.0,
        speed=2.81,
        attributes=[_ATTR_LIGHT, _ATTR_BIO, _ATTR_PSIONIC],
        ability_id=AbilityId.BARRACKSTRAIN_GHOST,
        weapons=[(_T_ANY, 10.0, 1.5, 1, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.HELLION,
        100,
        0,
        2.0,
        0,
        90.0,
        radius=0.625,
        sight_range=10.0,
        speed=4.25,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        ability_id=AbilityId.FACTORYTRAIN_HELLION,
        weapons=[(_T_GROUND, 8.0, 2.5, 1, 5.0)],
    ),
    UnitSpec(
        UnitTypeId.HELLIONTANK,
        100,
        0,
        2.0,
        0,
        135.0,
        radius=0.625,
        sight_range=10.0,
        speed=2.25,
        attributes=[_ATTR_LIGHT, _ATTR_BIO, _ATTR_MECH],
        ability_id=AbilityId.TRAIN_HELLBAT,
        weapons=[(_T_GROUND, 18.0, 2.0, 1, 2.0)],
    ),
    UnitSpec(
        UnitTypeId.WIDOWMINE,
        75,
        25,
        2.0,
        0,
        90.0,
        radius=0.5,
        sight_range=7.0,
        speed=2.8,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        ability_id=AbilityId.FACTORYTRAIN_WIDOWMINE,
    ),
    UnitSpec(
        UnitTypeId.WIDOWMINEBURROWED,
        75,
        25,
        2.0,
        0,
        90.0,
        radius=0.5,
        sight_range=7.0,
        speed=0.0,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        tech_alias=[UnitTypeId.WIDOWMINE],
    ),
    UnitSpec(
        UnitTypeId.SIEGETANK,
        150,
        125,
        3.0,
        0,
        175.0,
        armor=1.0,
        radius=0.875,
        sight_range=11.0,
        speed=2.25,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        ability_id=AbilityId.FACTORYTRAIN_SIEGETANK,
        weapons=[(_T_GROUND, 15.0, 1.04, 1, 7.0)],
    ),
    UnitSpec(
        UnitTypeId.SIEGETANKSIEGED,
        150,
        125,
        3.0,
        0,
        175.0,
        armor=1.0,
        radius=0.875,
        sight_range=11.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        tech_alias=[UnitTypeId.SIEGETANK],
        weapons=[(_T_GROUND, 40.0, 3.0, 1, 13.0)],
    ),
    UnitSpec(
        UnitTypeId.CYCLONE,
        150,
        100,
        3.0,
        0,
        120.0,
        armor=1.0,
        radius=0.75,
        sight_range=11.0,
        speed=3.375,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        ability_id=AbilityId.TRAIN_CYCLONE,
        weapons=[(_T_ANY, 18.0, 1.0, 1, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.THOR,
        300,
        200,
        6.0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=11.0,
        speed=1.875,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_MASSIVE],
        ability_id=AbilityId.FACTORYTRAIN_THOR,
        weapons=[(_T_GROUND, 30.0, 1.28, 2, 7.0), (_T_AIR, 6.0, 3.0, 4, 10.0)],
    ),
    UnitSpec(
        UnitTypeId.VIKINGFIGHTER,
        125,
        75,
        2.0,
        0,
        135.0,
        radius=0.75,
        sight_range=10.0,
        speed=2.75,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        ability_id=AbilityId.STARPORTTRAIN_VIKINGFIGHTER,
        weapons=[(_T_AIR, 10.0, 2.0, 2, 9.0)],
    ),
    UnitSpec(
        UnitTypeId.VIKINGASSAULT,
        125,
        75,
        2.0,
        0,
        135.0,
        radius=0.75,
        sight_range=10.0,
        speed=2.25,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        tech_alias=[UnitTypeId.VIKINGFIGHTER],
        weapons=[(_T_GROUND, 12.0, 1.0, 1, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.MEDIVAC,
        100,
        100,
        2.0,
        0,
        150.0,
        armor=1.0,
        max_energy=200.0,
        radius=0.75,
        sight_range=11.0,
        speed=2.5,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        ability_id=AbilityId.STARPORTTRAIN_MEDIVAC,
    ),
    UnitSpec(
        UnitTypeId.RAVEN,
        100,
        150,
        2.0,
        0,
        140.0,
        armor=1.0,
        max_energy=200.0,
        radius=0.625,
        sight_range=11.0,
        speed=2.75,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        ability_id=AbilityId.STARPORTTRAIN_RAVEN,
    ),
    UnitSpec(
        UnitTypeId.BANSHEE,
        150,
        100,
        3.0,
        0,
        140.0,
        radius=0.75,
        max_energy=200.0,
        sight_range=10.0,
        speed=2.75,
        attributes=[_ATTR_LIGHT, _ATTR_MECH],
        ability_id=AbilityId.STARPORTTRAIN_BANSHEE,
        weapons=[(_T_GROUND, 12.0, 1.25, 2, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.LIBERATOR,
        150,
        150,
        3.0,
        0,
        180.0,
        radius=0.75,
        sight_range=9.0,
        speed=3.375,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        ability_id=AbilityId.STARPORTTRAIN_LIBERATOR,
        weapons=[(_T_AIR, 5.0, 1.8, 2, 5.0)],
    ),
    UnitSpec(
        UnitTypeId.BATTLECRUISER,
        400,
        300,
        6.0,
        0,
        550.0,
        armor=3.0,
        radius=1.25,
        sight_range=12.0,
        speed=1.875,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_MASSIVE],
        ability_id=AbilityId.STARPORTTRAIN_BATTLECRUISER,
        weapons=[(_T_GROUND, 8.0, 0.225, 1, 6.0), (_T_AIR, 5.0, 0.225, 1, 6.0)],
    ),
    # Terran Structures (Patch 5.0.17 costs & supplies)
    UnitSpec(
        UnitTypeId.COMMANDCENTER,
        400,
        0,
        0,
        15.0,
        1500.0,
        armor=1.0,
        radius=2.5,
        sight_range=11.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_COMMANDCENTER,
    ),
    UnitSpec(
        UnitTypeId.COMMANDCENTERFLYING,
        400,
        0,
        0,
        15.0,
        1500.0,
        armor=1.0,
        radius=2.5,
        sight_range=11.0,
        speed=1.875,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        tech_alias=[UnitTypeId.COMMANDCENTER],
    ),
    UnitSpec(
        UnitTypeId.ORBITALCOMMAND,
        550,
        0,
        0,
        15.0,
        1500.0,
        armor=1.0,
        max_energy=200.0,
        radius=2.5,
        sight_range=11.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND,
        tech_alias=[UnitTypeId.COMMANDCENTER],
    ),
    UnitSpec(
        UnitTypeId.ORBITALCOMMANDFLYING,
        550,
        0,
        0,
        15.0,
        1500.0,
        armor=1.0,
        max_energy=200.0,
        radius=2.5,
        sight_range=11.0,
        speed=1.875,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        tech_alias=[UnitTypeId.COMMANDCENTER, UnitTypeId.ORBITALCOMMAND],
    ),
    UnitSpec(
        UnitTypeId.PLANETARYFORTRESS,
        550,
        150,
        0,
        15.0,
        1500.0,
        armor=2.0,
        radius=2.5,
        sight_range=11.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS,
        tech_alias=[UnitTypeId.COMMANDCENTER],
        weapons=[(_T_GROUND, 40.0, 2.0, 1, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.SUPPLYDEPOT,
        100,
        0,
        0,
        8.0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_SUPPLYDEPOT,
    ),
    UnitSpec(
        UnitTypeId.SUPPLYDEPOTLOWERED,
        100,
        0,
        0,
        8.0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        tech_alias=[UnitTypeId.SUPPLYDEPOT],
    ),
    UnitSpec(
        UnitTypeId.REFINERY,
        75,
        0,
        0,
        0,
        500.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_REFINERY,
    ),
    UnitSpec(
        UnitTypeId.BARRACKS,
        150,
        0,
        0,
        0,
        1000.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_BARRACKS,
    ),
    UnitSpec(
        UnitTypeId.BARRACKSTECHLAB,
        50,
        25,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_TECHLAB_BARRACKS,
        tech_alias=[UnitTypeId.TECHLAB],
    ),
    UnitSpec(
        UnitTypeId.BARRACKSREACTOR,
        50,
        50,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_REACTOR_BARRACKS,
        tech_alias=[UnitTypeId.REACTOR],
    ),
    UnitSpec(
        UnitTypeId.FACTORY,
        150,
        100,
        0,
        0,
        1250.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_FACTORY,
    ),
    UnitSpec(
        UnitTypeId.FACTORYTECHLAB,
        50,
        25,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_TECHLAB_FACTORY,
        tech_alias=[UnitTypeId.TECHLAB],
    ),
    UnitSpec(
        UnitTypeId.FACTORYREACTOR,
        50,
        50,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_REACTOR_FACTORY,
        tech_alias=[UnitTypeId.REACTOR],
    ),
    UnitSpec(
        UnitTypeId.STARPORT,
        150,
        100,
        0,
        0,
        1300.0,
        armor=1.0,
        radius=1.5,
        sight_range=10.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_STARPORT,
    ),
    UnitSpec(
        UnitTypeId.STARPORTTECHLAB,
        50,
        25,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_TECHLAB_STARPORT,
        tech_alias=[UnitTypeId.TECHLAB],
    ),
    UnitSpec(
        UnitTypeId.STARPORTREACTOR,
        50,
        50,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.0,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_REACTOR_STARPORT,
        tech_alias=[UnitTypeId.REACTOR],
    ),
    UnitSpec(
        UnitTypeId.ENGINEERINGBAY,
        125,
        0,
        0,
        0,
        850.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_ENGINEERINGBAY,
    ),
    UnitSpec(
        UnitTypeId.ARMORY,
        150,
        100,
        0,
        0,
        750.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_ARMORY,
    ),
    UnitSpec(
        UnitTypeId.BUNKER,
        100,
        0,
        0,
        0,
        400.0,
        armor=1.0,
        radius=1.25,
        sight_range=10.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_BUNKER,
        weapons=[(_T_ANY, 6.0, 0.86, 1, 6.0)],
    ),
    UnitSpec(
        UnitTypeId.MISSILETURRET,
        100,
        0,
        0,
        0,
        250.0,
        radius=1.0,
        sight_range=11.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_MISSILETURRET,
        weapons=[(_T_AIR, 12.0, 0.86, 2, 7.0)],
    ),
    UnitSpec(
        UnitTypeId.SENSORTOWER,
        100,
        50,
        0,
        0,
        200.0,
        radius=0.75,
        sight_range=12.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_SENSORTOWER,
    ),
    UnitSpec(
        UnitTypeId.GHOSTACADEMY,
        150,
        50,
        0,
        0,
        1250.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_GHOSTACADEMY,
    ),
    UnitSpec(
        UnitTypeId.FUSIONCORE,
        150,
        150,
        0,
        0,
        750.0,
        armor=1.0,
        radius=1.5,
        sight_range=9.0,
        speed=0.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.TERRANBUILD_FUSIONCORE,
    ),
    UnitSpec(
        UnitTypeId.TECHLAB,
        50,
        25,
        0,
        0,
        400.0,
        radius=1.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_TECHLAB,
    ),
    UnitSpec(
        UnitTypeId.REACTOR,
        50,
        50,
        0,
        0,
        400.0,
        radius=1.0,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_STRUCT],
        ability_id=AbilityId.BUILD_REACTOR,
    ),
    # Neutral resources / watchtowers
    UnitSpec(
        UnitTypeId.MINERALFIELD,
        max_hp=10000.0,
        radius=0.75,
        attributes=[_ATTR_STRUCT],
        race=Race.NoRace,
    ),
    UnitSpec(
        UnitTypeId.VESPENEGEYSER,
        max_hp=10000.0,
        radius=1.5,
        attributes=[_ATTR_STRUCT],
        race=Race.NoRace,
    ),
    UnitSpec(
        UnitTypeId.XELNAGATOWER,
        max_hp=10000.0,
        radius=1.0,
        attributes=[_ATTR_STRUCT],
        race=Race.NoRace,
    ),
    # Representative Protoss / Zerg enemies for multi-race offline checks
    UnitSpec(
        UnitTypeId.NEXUS,
        400,
        0,
        0,
        15.0,
        1000.0,
        1000.0,
        radius=2.5,
        attributes=[_ATTR_ARMORED, _ATTR_STRUCT],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.GATEWAY,
        150,
        0,
        0,
        0,
        500.0,
        500.0,
        radius=1.5,
        attributes=[_ATTR_ARMORED, _ATTR_STRUCT],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.DARKSHRINE,
        150,
        150,
        0,
        0,
        500.0,
        500.0,
        radius=1.0,
        attributes=[_ATTR_ARMORED, _ATTR_STRUCT],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.ZEALOT,
        100,
        0,
        2.0,
        0,
        100.0,
        50.0,
        radius=0.5,
        speed=2.25,
        attributes=[_ATTR_LIGHT, _ATTR_BIO],
        weapons=[(_T_GROUND, 8.0, 1.2, 2, 0.1)],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.STALKER,
        125,
        50,
        2.0,
        0,
        80.0,
        80.0,
        radius=0.625,
        speed=2.95,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        weapons=[(_T_ANY, 13.0, 1.34, 1, 6.0)],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.DARKTEMPLAR,
        125,
        125,
        2.0,
        0,
        40.0,
        80.0,
        radius=0.375,
        speed=2.81,
        attributes=[_ATTR_LIGHT, _ATTR_BIO, _ATTR_PSIONIC],
        weapons=[(_T_GROUND, 45.0, 1.69, 1, 0.1)],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.VOIDRAY,
        250,
        150,
        4.0,
        0,
        150.0,
        100.0,
        radius=1.0,
        speed=2.75,
        attributes=[_ATTR_ARMORED, _ATTR_MECH],
        weapons=[(_T_ANY, 6.0, 0.5, 1, 6.0)],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.COLOSSUS,
        300,
        200,
        6.0,
        0,
        200.0,
        150.0,
        radius=1.0,
        speed=2.25,
        attributes=[_ATTR_ARMORED, _ATTR_MECH, _ATTR_MASSIVE],
        weapons=[(_T_GROUND, 10.0, 1.07, 2, 7.0)],
        race=Race.Protoss,
    ),
    UnitSpec(
        UnitTypeId.HATCHERY,
        300,
        0,
        0,
        6.0,
        1500.0,
        radius=2.5,
        attributes=[_ATTR_ARMORED, _ATTR_BIO, _ATTR_STRUCT],
        race=Race.Zerg,
    ),
    UnitSpec(
        UnitTypeId.ZERGLING,
        25,
        0,
        0.5,
        0,
        35.0,
        radius=0.375,
        speed=2.95,
        attributes=[_ATTR_LIGHT, _ATTR_BIO],
        weapons=[(_T_GROUND, 5.0, 0.696, 1, 0.1)],
        race=Race.Zerg,
    ),
    UnitSpec(
        UnitTypeId.MUTALISK,
        100,
        100,
        2.0,
        0,
        120.0,
        radius=0.5,
        speed=4.0,
        attributes=[_ATTR_LIGHT, _ATTR_BIO],
        weapons=[(_T_ANY, 9.0, 1.52, 1, 3.0)],
        race=Race.Zerg,
    ),
]

SPECS_BY_TYPE: dict[UnitTypeId, UnitSpec] = {s.unit_id: s for s in UNIT_SPECS}


def build_response_data() -> sc2api_pb2.ResponseData:
    """Build real protobuf GameData covering Terran/enemy units, abilities, and upgrades."""
    resp = sc2api_pb2.ResponseData()

    # Abilities
    for ability in AbilityId:
        if ability.value == 0:
            continue
        ab = resp.abilities.add()
        ab.ability_id = ability.value
        ab.link_name = ability.name
        ab.button_name = ability.name
        ab.friendly_name = ability.name
        ab.available = True
        ab.target = _ability_target_type(ability)
        if ability in GENERIC_REDIRECT_ABILITIES:
            ab.remaps_to_ability_id = GENERIC_REDIRECT_ABILITIES[ability].value

    # Units
    for spec in UNIT_SPECS:
        ud = resp.units.add()
        ud.unit_id = spec.unit_id.value
        ud.name = spec.unit_id.name
        ud.available = True
        ud.mineral_cost = spec.minerals
        ud.vespene_cost = spec.vespene
        ud.food_required = spec.food_required
        ud.food_provided = spec.food_provided
        ud.armor = spec.armor
        ud.sight_range = spec.sight_range
        ud.movement_speed = spec.speed
        ud.race = spec.race.value
        ud.cargo_size = spec.cargo_size
        ud.attributes.extend(spec.attributes)
        if spec.ability_id != AbilityId.NULL_NULL:
            ud.ability_id = spec.ability_id.value
        for ta in spec.tech_alias:
            ud.tech_alias.append(ta.value)
        if spec.unit_id == UnitTypeId.MINERALFIELD:
            ud.has_minerals = True
        if spec.unit_id == UnitTypeId.VESPENEGEYSER:
            ud.has_vespene = True
        for ttype, dmg, spd, attacks, rng in spec.weapons:
            w = ud.weapons.add()
            w.type = ttype
            w.damage = dmg
            w.speed = spd
            w.attacks = attacks
            w.range = rng

    # Upgrades from burnysc2 RESEARCH_INFO
    upgrade_costs: dict[UpgradeId, tuple[int, int]] = {
        UpgradeId.STIMPACK: (100, 100),
        UpgradeId.SHIELDWALL: (100, 100),
        UpgradeId.PUNISHERGRENADES: (50, 50),
        UpgradeId.TERRANINFANTRYWEAPONSLEVEL1: (100, 100),
        UpgradeId.TERRANINFANTRYWEAPONSLEVEL2: (175, 175),
        UpgradeId.TERRANINFANTRYWEAPONSLEVEL3: (250, 250),
        UpgradeId.TERRANINFANTRYARMORSLEVEL1: (100, 100),
        UpgradeId.TERRANINFANTRYARMORSLEVEL2: (175, 175),
        UpgradeId.TERRANINFANTRYARMORSLEVEL3: (250, 250),
        UpgradeId.TERRANVEHICLEWEAPONSLEVEL1: (100, 100),
        UpgradeId.TERRANVEHICLEWEAPONSLEVEL2: (175, 175),
        UpgradeId.TERRANVEHICLEWEAPONSLEVEL3: (250, 250),
        UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL1: (100, 100),
        UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL2: (175, 175),
        UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL3: (250, 250),
        UpgradeId.HISECAUTOTRACKING: (100, 100),
        UpgradeId.TERRANBUILDINGARMOR: (150, 150),
    }
    seen_upgrades: set[UpgradeId] = set()
    for struct_map in RESEARCH_INFO.values():
        for upg_id, info in struct_map.items():
            if upg_id in seen_upgrades:
                continue
            seen_upgrades.add(upg_id)
            ug = resp.upgrades.add()
            ug.upgrade_id = upg_id.value
            ug.name = upg_id.name
            ug.ability_id = info["ability"].value
            m_cost, v_cost = upgrade_costs.get(upg_id, (100, 100))
            ug.mineral_cost = m_cost
            ug.vespene_cost = v_cost
            ug.research_time = 1000.0

    return resp


SHARED_GAME_DATA = GameData(build_response_data())


class MockClient:
    """Async placement & pathing stub matching burnysc2 Client methods used by BotAI."""

    def __init__(self, harness: OfflineScenario) -> None:
        self.harness = harness

    async def _query_building_placement_fast(
        self,
        ability_id: AbilityId,
        positions: list[Point2],
    ) -> list[bool]:
        del ability_id
        results: list[bool] = []
        for pos in positions:
            if not (4.0 <= pos.x <= 156.0 and 4.0 <= pos.y <= 156.0):
                results.append(False)
                continue
            blocked = any(
                u.pos.distance_to(pos) < 2.2
                for u in self.harness.units
                if u.alliance == Alliance.Self.value
                and SPECS_BY_TYPE.get(u.unit_type) is not None
                and _ATTR_STRUCT in SPECS_BY_TYPE[u.unit_type].attributes
            )
            results.append(not blocked)
        return results

    async def query_pathing(self, start: Point2, end: Point2) -> float:
        return start.distance_to(end)


@dataclass
class SimEntity:
    """Mutable entity state converted into raw_pb2.Unit each step."""

    tag: int
    unit_type: UnitTypeId
    pos: Point2
    alliance: int = Alliance.Self.value
    build_progress: float = 1.0
    health: float | None = None
    health_max: float | None = None
    shield: float | None = None
    shield_max: float | None = None
    energy: float = 0.0
    is_flying: bool = False
    is_burrowed: bool = False
    cloak: int = CloakState.NotCloaked.value
    add_on_tag: int = 0
    assigned_harvesters: int = 0
    ideal_harvesters: int = 0
    mineral_contents: int = 0
    vespene_contents: int = 0
    cargo_space_taken: int = 0
    cargo_space_max: int = 0
    weapon_cooldown: float = 0.0
    buff_ids: list[int] = field(default_factory=list)
    orders: list[tuple[AbilityId, int | Point2 | None, float]] = field(
        default_factory=list
    )


class OfflineScenario:
    """Self-contained offline RedCityLE-like 4-player SC2 simulation harness."""

    def __init__(
        self,
        seed: int = 42,
        include_watchtowers: bool = True,
        broken_ramp: bool = False,
        setup_standard_start: bool = True,
        fog_snapshot_resources: bool = True,
    ) -> None:
        self.bot = CompetitiveBot(seed=seed)
        self.bot.distance_calculation_method = 0
        self.units: list[SimEntity] = []
        self._next_tag = 1000
        self.game_loop: int = 0
        self.minerals: int = 50
        self.vespene: int = 0
        self.upgrades: set[UpgradeId] = set()
        self.visible_points: list[tuple[Point2, float]] = []
        self.reveal_all_map: bool = False
        self.fog_snapshot_resources: bool = fog_snapshot_resources
        self.step_commands: list[list[UnitCommand]] = []
        self.broken_ramp = broken_ramp

        self._response_game_info = self._build_game_info_proto()
        self.game_info = GameInfo(self._response_game_info.game_info)
        self.game_info.player_start_location = RED_CITY_SPAWNS[0]
        if broken_ramp:
            # 1 upper point forces Ramp.corner_depots / barracks_correct_placement to raise
            class _BrokenRamp:
                @property
                def top_center(self) -> Point2:
                    return Point2((44.0, 44.0))

                @property
                def bottom_center(self) -> Point2:
                    return Point2((46.0, 46.0))

                @property
                def corner_depots(self) -> set[Point2]:
                    raise RuntimeError("Non-standard ramp geometry")

                @property
                def barracks_correct_placement(self) -> Point2:
                    raise RuntimeError("Non-standard ramp geometry")

            self._custom_ramps = [_BrokenRamp()]
        else:
            # Provide clean standard Terran main ramp near (44, 42)
            class _GoodRamp:
                def __init__(self) -> None:
                    self.upper = {Point2((42.0, 40.0)), Point2((44.0, 42.0))}
                    self.lower = {Point2((47.0, 43.0)), Point2((48.0, 44.0))}
                    self.top_center = Point2((43.0, 41.0))
                    self.bottom_center = Point2((47.5, 43.5))
                    self.corner_depots = {Point2((40.5, 42.5)), Point2((44.5, 38.5))}
                    self.barracks_correct_placement = Point2((42.5, 40.5))

            self._custom_ramps = [_GoodRamp()]
        self.game_info.map_ramps = list(self._custom_ramps)

        self.client = MockClient(self)
        self.bot._prepare_start(
            client=self.client,  # type: ignore[arg-type]
            player_id=1,
            game_info=self.game_info,
            game_data=SHARED_GAME_DATA,
            realtime=False,
            base_build=98745,
        )
        self.bot._expansion_positions_list = list(RED_CITY_EXPANSIONS)
        self.bot._resource_location_to_expansion_position_dict = {}

        if setup_standard_start:
            self.add_base_resources(RED_CITY_EXPANSIONS)
            self.add_unit(
                UnitTypeId.COMMANDCENTER,
                RED_CITY_SPAWNS[0],
                ideal_harvesters=16,
                assigned_harvesters=12,
            )
            for idx in range(12):
                offset = Point2((-3.5 + (idx % 4) * 0.8, 1.5 + (idx // 4) * 0.8))
                scv = self.add_unit(UnitTypeId.SCV, RED_CITY_SPAWNS[0] + offset)
                # Assign initial SCVs to gather main minerals.
                main_minerals = [
                    u
                    for u in self.units
                    if u.unit_type == UnitTypeId.MINERALFIELD
                    and u.pos.distance_to(RED_CITY_SPAWNS[0]) <= 10.0
                ]
                if main_minerals:
                    scv.orders.append(
                        (
                            AbilityId.HARVEST_GATHER,
                            main_minerals[idx % len(main_minerals)].tag,
                            0.0,
                        )
                    )
        else:
            self.add_base_resources(RED_CITY_EXPANSIONS)

        if include_watchtowers:
            self.add_unit(
                UnitTypeId.XELNAGATOWER,
                Point2((80.0, 80.0)),
                alliance=Alliance.Neutral.value,
            )

    def _build_game_info_proto(self) -> sc2api_pb2.Response:
        resp = sc2api_pb2.Response()
        gi = resp.game_info
        gi.map_name = "RedCityLE"
        gi.local_map_path = "RedCityLE.SC2Map"
        p1 = gi.player_info.add()
        p1.player_id = 1
        p1.race_requested = Race.Terran.value
        p1.race_actual = Race.Terran.value
        p2 = gi.player_info.add()
        p2.player_id = 2
        p2.race_requested = Race.Terran.value
        p2.race_actual = Race.Terran.value

        sr = gi.start_raw
        sr.map_size.x = 160
        sr.map_size.y = 160
        sr.playable_area.p0.x = 4
        sr.playable_area.p0.y = 4
        sr.playable_area.p1.x = 156
        sr.playable_area.p1.y = 156

        # Packed 1-bit grids: 160 * 160 / 8 = 3200 bytes all 0xFF (pathable & placeable).
        all_ones = bytes([0xFF] * (160 * 160 // 8))
        sr.pathing_grid.bits_per_pixel = 1
        sr.pathing_grid.size.x = 160
        sr.pathing_grid.size.y = 160
        sr.pathing_grid.data = all_ones

        sr.placement_grid.bits_per_pixel = 1
        sr.placement_grid.size.x = 160
        sr.placement_grid.size.y = 160
        sr.placement_grid.data = all_ones

        sr.terrain_height.bits_per_pixel = 8
        sr.terrain_height.size.x = 160
        sr.terrain_height.size.y = 160
        sr.terrain_height.data = bytes([100] * (160 * 160))

        for loc in RED_CITY_SPAWNS[1:]:
            sl = sr.start_locations.add()
            sl.x = loc.x
            sl.y = loc.y
        return resp

    def next_tag(self) -> int:
        tag = self._next_tag
        self._next_tag += 1
        return tag

    def add_base_resources(self, locations: list[Point2]) -> None:
        for loc in locations:
            for i in range(8):
                angle = math.pi * (0.75 + i * 0.14)
                pos = loc + Point2((math.cos(angle) * 6.5, math.sin(angle) * 6.5))
                self.add_unit(
                    UnitTypeId.MINERALFIELD,
                    pos,
                    alliance=Alliance.Neutral.value,
                    mineral_contents=1500,
                )
            for gx, gy in ((-5.5, -5.0), (5.5, -5.0)):
                self.add_unit(
                    UnitTypeId.VESPENEGEYSER,
                    loc + Point2((gx, gy)),
                    alliance=Alliance.Neutral.value,
                    vespene_contents=2250,
                )

    def add_unit(
        self,
        unit_type: UnitTypeId,
        pos: Point2,
        alliance: int = Alliance.Self.value,
        build_progress: float = 1.0,
        health: float | None = None,
        shield: float | None = None,
        energy: float = 0.0,
        is_flying: bool = False,
        is_burrowed: bool = False,
        cloak: int = CloakState.NotCloaked.value,
        add_on_tag: int = 0,
        assigned_harvesters: int = 0,
        ideal_harvesters: int = 0,
        mineral_contents: int = 0,
        vespene_contents: int = 0,
        cargo_space_taken: int = 0,
        cargo_space_max: int = 0,
        weapon_cooldown: float = 0.0,
        buff_ids: list[BuffId] | None = None,
        orders: list[tuple[AbilityId, int | Point2 | None, float]] | None = None,
    ) -> SimEntity:
        spec = SPECS_BY_TYPE.get(unit_type)
        max_hp = spec.max_hp if spec else 100.0
        max_shield = spec.max_shield if spec else 0.0
        entity = SimEntity(
            tag=self.next_tag(),
            unit_type=unit_type,
            pos=pos,
            alliance=alliance,
            build_progress=build_progress,
            health=max_hp if health is None else health,
            health_max=max_hp,
            shield=max_shield if shield is None else shield,
            shield_max=max_shield,
            energy=energy,
            is_flying=is_flying
            or unit_type
            in {
                UnitTypeId.COMMANDCENTERFLYING,
                UnitTypeId.ORBITALCOMMANDFLYING,
                UnitTypeId.MEDIVAC,
                UnitTypeId.VIKINGFIGHTER,
                UnitTypeId.RAVEN,
                UnitTypeId.BANSHEE,
                UnitTypeId.LIBERATOR,
                UnitTypeId.BATTLECRUISER,
                UnitTypeId.VOIDRAY,
                UnitTypeId.MUTALISK,
            },
            is_burrowed=is_burrowed,
            cloak=cloak,
            add_on_tag=add_on_tag,
            assigned_harvesters=assigned_harvesters,
            ideal_harvesters=ideal_harvesters,
            mineral_contents=mineral_contents,
            vespene_contents=vespene_contents,
            cargo_space_taken=cargo_space_taken,
            cargo_space_max=cargo_space_max,
            weapon_cooldown=weapon_cooldown,
            buff_ids=[b.value for b in (buff_ids or [])],
            orders=list(orders or []),
        )
        self.units.append(entity)
        return entity

    def find_units(
        self,
        unit_type: UnitTypeId | None = None,
        alliance: int | None = None,
    ) -> list[SimEntity]:
        return [
            u
            for u in self.units
            if (unit_type is None or u.unit_type == unit_type)
            and (alliance is None or u.alliance == alliance)
        ]

    def remove_unit(self, tag: int) -> None:
        self.units = [u for u in self.units if u.tag != tag]

    def _compute_supply(self) -> tuple[int, int, int, int]:
        cap = 0.0
        used = 0.0
        workers = 0.0
        army = 0.0
        for u in self.units:
            if u.alliance != Alliance.Self.value:
                continue
            spec = SPECS_BY_TYPE.get(u.unit_type)
            if spec is None:
                continue
            if u.build_progress >= 1.0:
                cap += spec.food_provided
            if spec.food_required > 0:
                used += spec.food_required
                if u.unit_type == UnitTypeId.SCV:
                    workers += spec.food_required
                else:
                    army += spec.food_required
        return min(200, int(cap)), int(used), int(workers), int(army)

    def _build_visibility_bytes(self) -> bytes:
        grid = bytearray(160 * 160)
        if self.reveal_all_map:
            return bytes([2] * (160 * 160))

        sight_sources: list[tuple[Point2, float]] = list(self.visible_points)
        for u in self.units:
            if u.alliance == Alliance.Self.value:
                spec = SPECS_BY_TYPE.get(u.unit_type)
                r = spec.sight_range if spec else 9.0
                sight_sources.append((u.pos, r))

        for pt, radius in sight_sources:
            r_int = math.ceil(radius)
            r_sq = radius * radius
            cx = round(pt.x)
            cy = round(pt.y)
            for dy in range(-r_int, r_int + 1):
                yy = cy + dy
                if not (0 <= yy < 160):
                    continue
                row_off = yy * 160
                dy2 = dy * dy
                for dx in range(-r_int, r_int + 1):
                    xx = cx + dx
                    if 0 <= xx < 160 and (dx * dx + dy2) <= r_sq:
                        grid[row_off + xx] = 2
        return bytes(grid)

    def _build_observation(self) -> sc2api_pb2.ResponseObservation:
        obs_resp = sc2api_pb2.ResponseObservation()
        obs = obs_resp.observation
        obs.game_loop = self.game_loop
        food_cap, food_used, food_workers, food_army = self._compute_supply()
        obs.player_common.player_id = 1
        obs.player_common.minerals = self.minerals
        obs.player_common.vespene = self.vespene
        obs.player_common.food_cap = food_cap
        obs.player_common.food_used = food_used
        obs.player_common.food_workers = food_workers
        obs.player_common.food_army = food_army
        obs.player_common.idle_worker_count = sum(
            1
            for u in self.units
            if u.alliance == Alliance.Self.value
            and u.unit_type == UnitTypeId.SCV
            and not u.orders
        )
        obs.player_common.army_count = sum(
            1
            for u in self.units
            if u.alliance == Alliance.Self.value
            and u.unit_type != UnitTypeId.SCV
            and SPECS_BY_TYPE.get(u.unit_type) is not None
            and SPECS_BY_TYPE[u.unit_type].food_required > 0
        )

        raw = obs.raw_data
        raw.player.upgrade_ids.extend(u.value for u in self.upgrades)

        vis_bytes = self._build_visibility_bytes()
        raw.map_state.visibility.bits_per_pixel = 8
        raw.map_state.visibility.size.x = 160
        raw.map_state.visibility.size.y = 160
        raw.map_state.visibility.data = vis_bytes

        raw.map_state.creep.bits_per_pixel = 1
        raw.map_state.creep.size.x = 160
        raw.map_state.creep.size.y = 160
        raw.map_state.creep.data = bytes(160 * 160 // 8)

        for sim in self.units:
            is_fog_snapshot_resource = False
            if (
                sim.alliance == Alliance.Neutral.value
                and self.fog_snapshot_resources
                and not self.reveal_all_map
                and sim.unit_type
                in {
                    UnitTypeId.MINERALFIELD,
                    UnitTypeId.MINERALFIELD750,
                    UnitTypeId.VESPENEGEYSER,
                }
            ):
                rx = max(0, min(159, round(sim.pos.x)))
                ry = max(0, min(159, round(sim.pos.y)))
                if vis_bytes[ry * 160 + rx] != 2:
                    is_fog_snapshot_resource = True

            # Respect legal fog-of-war for enemies!
            if sim.alliance == Alliance.Enemy.value and not self.reveal_all_map:
                rx = max(0, min(159, round(sim.pos.x)))
                ry = max(0, min(159, round(sim.pos.y)))
                if vis_bytes[ry * 160 + rx] != 2:
                    continue

            pu = raw.units.add()
            pu.display_type = (
                DisplayType.Snapshot.value
                if is_fog_snapshot_resource
                else DisplayType.Visible.value
            )
            pu.alliance = sim.alliance
            pu.tag = sim.tag
            pu.unit_type = sim.unit_type.value
            pu.owner = (
                1
                if sim.alliance == Alliance.Self.value
                else (2 if sim.alliance == Alliance.Enemy.value else 0)
            )
            pu.pos.x = sim.pos.x
            pu.pos.y = sim.pos.y
            pu.pos.z = 10.0
            pu.facing = 0.0
            spec = SPECS_BY_TYPE.get(sim.unit_type)
            pu.radius = spec.radius if spec else 0.5
            pu.build_progress = sim.build_progress
            pu.cloak = sim.cloak
            pu.is_flying = sim.is_flying
            pu.is_burrowed = sim.is_burrowed
            pu.health = (
                sim.health
                if sim.health is not None
                else (spec.max_hp if spec else 100.0)
            )
            pu.health_max = (
                sim.health_max
                if sim.health_max is not None
                else (spec.max_hp if spec else 100.0)
            )
            pu.shield = (
                sim.shield
                if sim.shield is not None
                else (spec.max_shield if spec else 0.0)
            )
            pu.shield_max = (
                sim.shield_max
                if sim.shield_max is not None
                else (spec.max_shield if spec else 0.0)
            )
            pu.energy = sim.energy
            pu.energy_max = spec.max_energy if spec else 0.0
            pu.add_on_tag = sim.add_on_tag
            pu.assigned_harvesters = sim.assigned_harvesters
            pu.ideal_harvesters = sim.ideal_harvesters
            pu.mineral_contents = (
                0 if is_fog_snapshot_resource else sim.mineral_contents
            )
            pu.vespene_contents = (
                0 if is_fog_snapshot_resource else sim.vespene_contents
            )
            pu.cargo_space_taken = sim.cargo_space_taken
            pu.cargo_space_max = sim.cargo_space_max
            pu.weapon_cooldown = sim.weapon_cooldown
            pu.buff_ids.extend(sim.buff_ids)
            for ab_id, tgt, prog in sim.orders:
                po = pu.orders.add()
                po.ability_id = ab_id.value
                po.progress = prog
                if isinstance(tgt, int):
                    po.target_unit_tag = tgt
                elif isinstance(tgt, Point2):
                    po.target_world_space_pos.x = tgt.x
                    po.target_world_space_pos.y = tgt.y
                    po.target_world_space_pos.z = 10.0

        return obs_resp

    async def step(
        self, dt_seconds: float = 0.5, iteration: int | None = None
    ) -> list[UnitCommand]:
        """Advance game loop, prepare burnysc2 state, execute on_step, return commands."""
        step_idx = len(self.step_commands) if iteration is None else iteration
        if step_idx > 0:
            self.game_loop += max(1, round(dt_seconds * 22.4))

        obs_resp = self._build_observation()
        state = GameState(obs_resp)
        self.bot._prepare_step(state, self._response_game_info)
        if not self.bot._initialized:
            self.bot._prepare_first_step()
            self.game_info.map_ramps = list(self._custom_ramps)
            self.bot._expansion_positions_list = list(RED_CITY_EXPANSIONS)
            await self.bot.on_start()

        await self.bot.on_step(step_idx)

        commands = list(self.bot.actions)
        self.step_commands.append(commands)
        self.bot.actions.clear()
        self.bot.unit_tags_received_action.clear()
        self.bot.cache.clear()
        return commands

    def apply_commands_basic(
        self,
        commands: list[UnitCommand],
        dt_seconds: float = 0.5,
    ) -> None:
        """Apply issued commands minimally in long-game simulation tests."""
        by_tag = {u.tag: u for u in self.units}
        advanced_land_tags: set[int] = set()
        for cmd in commands:
            unit = by_tag.get(cmd.unit.tag)
            if unit is None:
                continue
            ab = cmd.ability
            if ab == AbilityId.HARVEST_GATHER and cmd.target is not None:
                tgt_tag = cmd.target.tag if hasattr(cmd.target, "tag") else cmd.target
                unit.orders = [(ab, tgt_tag, 0.0)]
            elif ab in {AbilityId.HARVEST_RETURN, AbilityId.HARVEST_RETURN_SCV}:
                unit.orders = [(AbilityId.HARVEST_RETURN_SCV, None, 0.0)]
            elif ab in {AbilityId.MOVE_MOVE, AbilityId.ATTACK, AbilityId.ATTACK_ATTACK}:
                if isinstance(cmd.target, Point2):
                    unit.orders = [(ab, cmd.target, 0.0)]
                    spec = SPECS_BY_TYPE.get(unit.unit_type)
                    speed = (spec.speed if spec else 3.0) * 1.4
                    if speed > 0:
                        unit.pos = unit.pos.towards(
                            cmd.target,
                            min(speed * dt_seconds, unit.pos.distance_to(cmd.target)),
                        )
                elif hasattr(cmd.target, "tag"):
                    unit.orders = [(ab, cmd.target.tag, 0.0)]
            elif ab == AbilityId.LIFT_ORBITALCOMMAND:
                unit.unit_type = UnitTypeId.ORBITALCOMMANDFLYING
                unit.is_flying = True
                unit.orders = []
            elif ab == AbilityId.LIFT_COMMANDCENTER:
                unit.unit_type = UnitTypeId.COMMANDCENTERFLYING
                unit.is_flying = True
                unit.orders = []
            elif ab == AbilityId.LAND_ORBITALCOMMAND and isinstance(cmd.target, Point2):
                advanced_land_tags.add(unit.tag)
                dist = unit.pos.distance_to(cmd.target)
                if dist <= 1.5:
                    unit.unit_type = UnitTypeId.ORBITALCOMMAND
                    unit.is_flying = False
                    unit.pos = cmd.target
                    unit.orders = []
                else:
                    unit.pos = unit.pos.towards(
                        cmd.target, min(1.875 * dt_seconds, dist)
                    )
                    unit.orders = [(ab, cmd.target, 0.0)]
            elif ab == AbilityId.LAND_COMMANDCENTER and isinstance(cmd.target, Point2):
                advanced_land_tags.add(unit.tag)
                dist = unit.pos.distance_to(cmd.target)
                if dist <= 1.5:
                    unit.unit_type = UnitTypeId.COMMANDCENTER
                    unit.is_flying = False
                    unit.pos = cmd.target
                    unit.orders = []
                else:
                    unit.pos = unit.pos.towards(
                        cmd.target, min(1.875 * dt_seconds, dist)
                    )
                    unit.orders = [(ab, cmd.target, 0.0)]
            elif ab == AbilityId.CANCEL_BUILDINPROGRESS:
                self.remove_unit(unit.tag)
            elif ab == AbilityId.SIEGEMODE_SIEGEMODE:
                unit.unit_type = UnitTypeId.SIEGETANKSIEGED
            elif ab == AbilityId.UNSIEGE_UNSIEGE:
                unit.unit_type = UnitTypeId.SIEGETANK
            elif ab == AbilityId.MORPH_SUPPLYDEPOT_RAISE:
                unit.unit_type = UnitTypeId.SUPPLYDEPOT
            elif ab == AbilityId.MORPH_SUPPLYDEPOT_LOWER:
                unit.unit_type = UnitTypeId.SUPPLYDEPOTLOWERED
            elif ab == AbilityId.MORPH_VIKINGASSAULTMODE:
                unit.unit_type = UnitTypeId.VIKINGASSAULT
                unit.is_flying = False
            elif ab == AbilityId.MORPH_VIKINGFIGHTERMODE:
                unit.unit_type = UnitTypeId.VIKINGFIGHTER
                unit.is_flying = True
            elif ab == AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND:
                unit.unit_type = UnitTypeId.ORBITALCOMMAND
                unit.energy = 50.0
                self.minerals = max(0, self.minerals - 150)
            elif ab == AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS:
                unit.unit_type = UnitTypeId.PLANETARYFORTRESS
                self.minerals = max(0, self.minerals - 150)
                self.vespene = max(0, self.vespene - 150)
            elif ab in {
                AbilityId.TERRANBUILD_SUPPLYDEPOT,
                AbilityId.TERRANBUILD_BARRACKS,
                AbilityId.TERRANBUILD_FACTORY,
                AbilityId.TERRANBUILD_STARPORT,
                AbilityId.TERRANBUILD_ENGINEERINGBAY,
                AbilityId.TERRANBUILD_ARMORY,
                AbilityId.TERRANBUILD_COMMANDCENTER,
                AbilityId.TERRANBUILD_BUNKER,
                AbilityId.TERRANBUILD_MISSILETURRET,
                AbilityId.TERRANBUILD_SENSORTOWER,
            } and isinstance(cmd.target, Point2):
                build_map = {
                    AbilityId.TERRANBUILD_SUPPLYDEPOT: UnitTypeId.SUPPLYDEPOT,
                    AbilityId.TERRANBUILD_BARRACKS: UnitTypeId.BARRACKS,
                    AbilityId.TERRANBUILD_FACTORY: UnitTypeId.FACTORY,
                    AbilityId.TERRANBUILD_STARPORT: UnitTypeId.STARPORT,
                    AbilityId.TERRANBUILD_ENGINEERINGBAY: UnitTypeId.ENGINEERINGBAY,
                    AbilityId.TERRANBUILD_ARMORY: UnitTypeId.ARMORY,
                    AbilityId.TERRANBUILD_COMMANDCENTER: UnitTypeId.COMMANDCENTER,
                    AbilityId.TERRANBUILD_BUNKER: UnitTypeId.BUNKER,
                    AbilityId.TERRANBUILD_MISSILETURRET: UnitTypeId.MISSILETURRET,
                    AbilityId.TERRANBUILD_SENSORTOWER: UnitTypeId.SENSORTOWER,
                }
                ut = build_map[ab]
                spec = SPECS_BY_TYPE[ut]
                if self.minerals >= spec.minerals and self.vespene >= spec.vespene:
                    self.minerals -= spec.minerals
                    self.vespene -= spec.vespene
                    self.add_unit(ut, cmd.target, build_progress=1.0)
            elif ab == AbilityId.TERRANBUILD_REFINERY and cmd.target is not None:
                tgt = by_tag.get(
                    cmd.target.tag if hasattr(cmd.target, "tag") else cmd.target
                )
                if tgt is not None and self.minerals >= 75:
                    self.minerals -= 75
                    self.add_unit(
                        UnitTypeId.REFINERY,
                        tgt.pos,
                        build_progress=1.0,
                        ideal_harvesters=3,
                    )
            else:
                # Check train abilities
                for struct_map in TRAIN_INFO.values():
                    for ut, info in struct_map.items():
                        if info["ability"] == ab and ut in SPECS_BY_TYPE:
                            spec = SPECS_BY_TYPE[ut]
                            if (
                                self.minerals >= spec.minerals
                                and self.vespene >= spec.vespene
                            ):
                                self.minerals -= spec.minerals
                                self.vespene -= spec.vespene
                                self.add_unit(ut, unit.pos + Point2((2.0, -2.0)))
                            break

        # Continue executing persistent flying townhall landing orders across frames.
        for unit in self.units:
            if unit.tag in advanced_land_tags or not unit.orders:
                continue
            ab, tgt, _ = unit.orders[0]
            if ab in {
                AbilityId.LAND_COMMANDCENTER,
                AbilityId.LAND_ORBITALCOMMAND,
            } and isinstance(tgt, Point2):
                dist = unit.pos.distance_to(tgt)
                if dist <= 1.5:
                    unit.unit_type = (
                        UnitTypeId.ORBITALCOMMAND
                        if unit.unit_type == UnitTypeId.ORBITALCOMMANDFLYING
                        else UnitTypeId.COMMANDCENTER
                    )
                    unit.is_flying = False
                    unit.pos = tgt
                    unit.orders = []
                else:
                    unit.pos = unit.pos.towards(tgt, min(1.875 * dt_seconds, dist))
