"""Combat decisions exercised without a StarCraft II game client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2, Rect

from bots.mizuki.combat import (
    _CombatState,
    _explore,
    _next_phase,
    _Phase,
    control_combat,
)


def point(x, y):
    return Point2((x, y))


@dataclass
class DummyUnit:
    tag: int
    type_id: U
    position: Point2
    is_flying: bool = False
    is_structure: bool = False
    is_ready: bool = True
    is_visible: bool = True
    is_snapshot: bool = False
    is_cloaked: bool = False
    is_revealed: bool = False
    is_armored: bool = False
    health_percentage: float = 1
    shield_percentage: float = 0
    can_attack_ground: bool = True
    can_attack_air: bool = False
    ground_range: float = 5
    air_range: float = 5
    radius: float = 0.5
    weapon_cooldown: float = 0
    energy: float = 100
    is_idle: bool = True
    orders: list = field(default_factory=list)
    buffs: set = field(default_factory=set)
    actions: list = field(default_factory=list)

    def distance_to(self, target):
        return self.position.distance_to(getattr(target, "position", target))

    def target_in_range(self, target, bonus_distance=0):
        can_hit = self.can_attack_air if target.is_flying else self.can_attack_ground
        reach = self.air_range if target.is_flying else self.ground_range
        return (
            can_hit
            and self.distance_to(target)
            <= reach + self.radius + target.radius + bonus_distance
        )

    def has_buff(self, buff):
        return buff in self.buffs

    def attack(self, target):
        self.actions.append(("attack", target))

    def move(self, target):
        self.actions.append(("move", target))

    def __call__(self, ability, target=None):
        self.actions.append((ability, target))


def dummy_bot(*, units=(), enemies=(), structures=(), upgrades=(), now=100):
    start = point(20, 20)
    return SimpleNamespace(
        units=list(units),
        townhalls=[
            DummyUnit(
                900, U.COMMANDCENTER, start, is_structure=True, can_attack_ground=False
            )
        ],
        enemy_units=list(enemies),
        enemy_structures=list(structures),
        game_info=SimpleNamespace(
            playable_area=Rect((0, 0, 120, 120)), map_center=point(60, 60)
        ),
        start_location=start,
        enemy_start_locations=[point(99, 99)],
        expansion_locations_list=[point(30, 30), point(90, 90)],
        state=SimpleNamespace(upgrades=set(upgrades)),
        time=now,
        is_visible=lambda _: False,
        in_pathing_grid=lambda _: True,
    )


def test_base_defense_ignores_hidden_enemy_and_leaves_reaper_to_scout():
    marine = DummyUnit(1, U.MARINE, point(22, 22))
    reaper = DummyUnit(2, U.REAPER, point(21, 22))
    visible = DummyUnit(3, U.ZERGLING, point(24, 22))
    hidden = DummyUnit(4, U.BANELING, point(22, 21), is_snapshot=True)
    cloaked = DummyUnit(5, U.DARKTEMPLAR, point(22, 21), is_cloaked=True)
    bot = dummy_bot(units=[marine, reaper], enemies=[hidden, visible, cloaked])
    asyncio.run(control_combat(bot, 0))
    assert marine.actions == [("attack", visible)]
    assert not reaper.actions


def test_retreat_cooldown_does_not_clear_when_contact_drops_out_of_vision():
    state = _CombatState()
    assert _next_phase(state, 80, 20, 0, 0, 1) is _Phase.PRESSURE
    assert _next_phase(state, 81, 10, 20, 20, 1) is _Phase.RETREAT
    assert _next_phase(state, 85, 18, 0, 15, 1) is _Phase.RETREAT
    assert _next_phase(state, 93, 18, 0, 15, 1) is _Phase.RETREAT
    assert _next_phase(state, 94, 14, 0, 10, 1) is _Phase.GATHER
    assert _next_phase(state, 95, 20, 0, 10, 1) is _Phase.PRESSURE


def test_tank_sieges_while_a_healthy_infantry_only_stims_once_per_buff():
    tank = DummyUnit(1, U.SIEGETANK, point(20, 20))
    marine = DummyUnit(2, U.MARINE, point(25, 20))
    marauder = DummyUnit(3, U.MARAUDER, point(25, 20), health_percentage=0.5)
    enemies = [
        DummyUnit(4, U.ROACH, point(28, 20)),
        DummyUnit(5, U.ZERGLING, point(29, 20)),
    ]
    bot = dummy_bot(
        units=[tank, marine, marauder], enemies=enemies, upgrades=[UpgradeId.STIMPACK]
    )
    asyncio.run(control_combat(bot, 0))
    assert tank.actions == [(AbilityId.SIEGEMODE_SIEGEMODE, None)]
    assert marine.actions == [(AbilityId.EFFECT_STIM_MARINE, None)]
    assert marauder.actions[0][0] == "attack"
    marine.buffs.add(BuffId.STIMPACK)
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert marine.actions[-1][0] == "attack"
    assert len(tank.actions) == 1


def test_medivac_heals_then_prioritizes_getting_out_of_anti_air():
    marine = DummyUnit(1, U.MARINE, point(24, 23), health_percentage=0.4)
    medivac = DummyUnit(
        2, U.MEDIVAC, point(23, 23), is_flying=True, can_attack_ground=False
    )
    bot = dummy_bot(units=[marine, medivac])
    asyncio.run(control_combat(bot, 0))
    assert medivac.actions == [(AbilityId.MEDIVACHEAL_HEAL, marine)]
    enemy = DummyUnit(3, U.MARINE, point(27, 23), can_attack_air=True)
    bot.enemy_units.append(enemy)
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    action, destination = medivac.actions[-1]
    assert action == "move"
    assert destination.distance_to(enemy.position) > medivac.distance_to(enemy)


def test_air_sweep_does_not_replace_ground_goal_with_an_unpathable_tile():
    bot = dummy_bot()
    start = point(20, 20)
    ground_goal = point(30, 30)
    air_goal = point(21, 21)
    bot.in_pathing_grid = lambda p: p != air_goal
    state = _CombatState(route=(ground_goal, air_goal))
    assert _explore(bot, state, 0, start, can_fly=False) == ground_goal
    assert _explore(bot, state, 0, start, can_fly=True) == air_goal
    assert _explore(bot, state, 1, start, can_fly=False) == ground_goal


def test_late_last_viking_targets_visible_lifted_building_across_map():
    viking = DummyUnit(
        1,
        U.VIKINGFIGHTER,
        point(20, 20),
        is_flying=True,
        can_attack_air=True,
        can_attack_ground=False,
    )
    target = DummyUnit(
        2,
        U.BARRACKSFLYING,
        point(95, 95),
        is_flying=True,
        is_structure=True,
        can_attack_ground=False,
    )
    bot = dummy_bot(units=[viking], structures=[target], now=800)
    asyncio.run(control_combat(bot, 0))
    assert viking.actions == [("attack", target)]


def test_visible_storm_preempts_attacking_and_splits_the_pair():
    from sc2.ids.effect_id import EffectId

    first = DummyUnit(11, U.MARINE, point(24, 23))
    second = DummyUnit(12, U.MARINE, point(24, 23))
    enemy = DummyUnit(13, U.ZEALOT, point(25, 23))
    bot = dummy_bot(units=[first, second], enemies=[enemy])
    bot.state.effects = [
        SimpleNamespace(
            id=EffectId.PSISTORMPERSISTENT,
            positions={point(24, 23)},
            radius=1.5,
            is_enemy=True,
        )
    ]
    asyncio.run(control_combat(bot, 0))
    assert first.actions[0][0] == second.actions[0][0] == "move"
    assert first.actions[0][1] != second.actions[0][1]
    assert all(u.actions[0][1].distance_to(u.position) >= 2.25 for u in (first, second))
