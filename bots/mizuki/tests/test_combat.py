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


def test_a_scattered_army_cannot_launch_based_on_global_count():
    wings = [
        DummyUnit(i, U.MARINE, point(26, 27) if i <= 10 else point(92, 94))
        for i in range(1, 21)
    ]
    bot = dummy_bot(units=wings, now=300)
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.phase is _Phase.GATHER
    assert bot._mizuki_combat_state.core_anchor in {point(26, 27), point(92, 94)}
    assert wings[-1].actions[0][1].distance_to(bot.start_location) < 5


def test_pressure_withdraws_when_distant_reinforcement_does_not_join_force():
    at_front = [DummyUnit(i, U.MARINE, point(70, 70)) for i in range(1, 8)]
    reserve = [
        DummyUnit(100 + i, U.MARINE, position)
        for i, position in enumerate(
            [point(22, 22)] * 6 + [point(26, 90)] * 6 + [point(100, 36)] * 6
        )
    ]
    opponents = [DummyUnit(300 + i, U.ROACH, point(81, 70)) for i in range(15)]
    bot = dummy_bot(units=at_front + reserve, enemies=opponents, now=400)
    bot._mizuki_combat_state = _CombatState(
        phase=_Phase.PRESSURE, core_anchor=point(70, 70)
    )
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.phase is _Phase.RETREAT
    assert at_front[0].actions[0][0] == "move"
    assert at_front[0].actions[0][1].distance_to(bot.start_location) < 5


def test_infantry_that_outruns_tank_moves_back_to_screen():
    front = [DummyUnit(i, U.MARINE, point(52, 40)) for i in range(1, 9)]
    tank = DummyUnit(15, U.SIEGETANK, point(37, 40))
    target = DummyUnit(
        20, U.HATCHERY, point(85, 40), is_structure=True, can_attack_ground=False
    )
    bot = dummy_bot(units=[*front, tank], structures=[target], now=650)
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.phase is _Phase.PRESSURE
    assert front[0].actions[0][0] == "move" and front[0].actions[0][1].x <= 45
    assert tank.actions == [("attack", target.position)]


def test_pressure_does_not_chase_out_of_range_contact_away_from_objective():
    formation = [DummyUnit(i, U.MARINE, point(45, 45)) for i in range(1, 9)]
    enemy = DummyUnit(50, U.ZERGLING, point(38, 45), ground_range=0.1)
    goal = DummyUnit(
        51, U.HATCHERY, point(70, 45), is_structure=True, can_attack_ground=False
    )
    bot = dummy_bot(units=formation, enemies=[enemy], structures=[goal], now=650)
    asyncio.run(control_combat(bot, 0))
    assert formation[0].actions == [("move", point(49, 45))]


def test_viking_sweeps_known_lifted_building_even_with_ground_army_present():
    marine = DummyUnit(1, U.MARINE, point(35, 35), can_attack_air=True)
    viking = DummyUnit(
        2,
        U.VIKINGFIGHTER,
        point(33, 35),
        is_flying=True,
        can_attack_air=True,
        can_attack_ground=False,
    )
    floating = DummyUnit(
        50,
        U.BARRACKSFLYING,
        point(100, 90),
        is_flying=True,
        is_structure=True,
        can_attack_ground=False,
    )
    bot = dummy_bot(units=[marine, viking], structures=[floating], now=800)
    bot.in_pathing_grid = lambda p: p != floating.position
    asyncio.run(control_combat(bot, 0))
    assert viking.actions == [("attack", floating)]
    action, approach = marine.actions[0]
    assert action == "attack" and approach != floating.position
    assert approach.distance_to(floating.position) < 6


def test_escort_does_not_immediately_return_from_anti_air_escape():
    marine = DummyUnit(1, U.MARINE, point(51, 45), health_percentage=0.4)
    medivac = DummyUnit(
        2, U.MEDIVAC, point(45, 45), is_flying=True, can_attack_ground=False
    )
    enemy = DummyUnit(3, U.MARINE, point(49, 45), can_attack_air=True)
    bot = dummy_bot(units=[marine, medivac], enemies=[enemy])
    asyncio.run(control_combat(bot, 0))
    first = medivac.actions[0]
    assert first[0] == "move"
    medivac.position = first[1]
    bot.enemy_units.clear()
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert medivac.actions == [first]


def siege_line():
    return [
        DummyUnit(90, U.SIEGETANKSIEGED, point(33, 20), ground_range=13),
        DummyUnit(91, U.SIEGETANKSIEGED, point(35, 21), ground_range=13),
    ]


def test_unsupported_ground_units_pull_behind_base_during_superior_siege():
    guns = siege_line()
    marine = DummyUnit(1, U.MARINE, point(28, 20))
    marauder = DummyUnit(2, U.MARAUDER, point(27, 20))
    bot = dummy_bot(units=[marine, marauder], enemies=guns, now=410)
    asyncio.run(control_combat(bot, 0))
    for unit in (marine, marauder):
        assert len(unit.actions) == 1
        kind, destination = unit.actions[0]
        assert kind == "move" and destination.x < bot.start_location.x
        assert all(gun.distance_to(destination) > 14 for gun in guns)
    assert marine.actions[0][1] != marauder.actions[0][1]


def test_siege_line_anchors_armored_return_fire_and_sieges_reserve():
    guns = siege_line()
    placed = DummyUnit(1, U.SIEGETANKSIEGED, point(22, 20), ground_range=13)
    reserve = DummyUnit(2, U.SIEGETANK, point(17, 20), ground_range=7)
    # Matching tank counts are not enough while the opposing guns have infantry support.
    opposing_bio = [DummyUnit(100 + i, U.MARINE, point(35, 20)) for i in range(3)]
    bot = dummy_bot(units=[placed, reserve], enemies=[*guns, *opposing_bio], now=410)
    asyncio.run(control_combat(bot, 0))
    assert placed.actions == [("attack", guns[0])]
    assert reserve.actions == [(AbilityId.SIEGEMODE_SIEGEMODE, None)]


def test_safe_guard_cancels_chase_but_fires_on_units_crossing_the_line():
    guns = siege_line()
    marine = DummyUnit(2, U.MARINE, point(16, 20), is_idle=False)
    bot = dummy_bot(units=[marine], enemies=guns, now=410)
    asyncio.run(control_combat(bot, 0))
    assert marine.actions == [(AbilityId.HOLDPOSITION_HOLD, None)]
    advancing = DummyUnit(99, U.MARINE, point(20, 21))
    bot.enemy_units.append(advancing)
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert marine.actions[-1] == ("attack", advancing)


def test_hidden_siege_snapshots_do_not_keep_defensive_guard_active():
    guns = siege_line()
    marine = DummyUnit(2, U.MARINE, point(28, 20))
    bot = dummy_bot(units=[marine], enemies=guns, now=410)
    asyncio.run(control_combat(bot, 0))
    assert marine.actions[-1][0] == "move"
    for gun in guns:
        gun.is_snapshot = True
    advancing = DummyUnit(99, U.MARINE, point(24, 20))
    bot.enemy_units.append(advancing)
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert marine.actions[-1] == ("attack", advancing)


def test_concentrated_superior_armor_can_counterattack():
    guns = siege_line()
    marine = DummyUnit(2, U.MARINE, point(25, 20))
    allied_tanks = [
        DummyUnit(20 + i, U.SIEGETANKSIEGED, point(22, 20 + i), ground_range=13)
        for i in range(3)
    ]
    bot = dummy_bot(units=[marine, *allied_tanks], enemies=guns, now=410)
    asyncio.run(control_combat(bot, 0))
    assert marine.actions == [("attack", guns[0])]


def test_defensive_line_releases_when_one_covering_tank_and_infantry_concentrate():
    guns = siege_line()
    squad = [DummyUnit(i, U.MARINE, point(25, 20)) for i in range(1, 11)]
    tank = DummyUnit(20, U.SIEGETANKSIEGED, point(22, 20), ground_range=13)
    bot = dummy_bot(units=[squad[0], tank], enemies=guns, now=410)
    asyncio.run(control_combat(bot, 0))
    assert squad[0].actions[0][0] == "move"
    bot.units.extend(squad[1:])
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert squad[0].actions[-1] == ("attack", guns[0])


def test_tank_moves_to_natural_post_then_sieges_before_contact():
    tank = DummyUnit(1, U.SIEGETANK, point(21, 20), ground_range=7)
    natural = DummyUnit(901, U.COMMANDCENTER, point(38, 38), is_structure=True)
    bot = dummy_bot(units=[tank])
    bot.townhalls.append(natural)
    front = natural.position.towards(bot.game_info.map_center, 6)
    # The straight post is unavailable: choose a nearby legal side position instead.
    bot.in_pathing_grid = lambda p: p.distance_to(front) >= 0.1
    asyncio.run(control_combat(bot, 0))
    action, post = tank.actions[-1]
    assert action == "move" and bot.in_pathing_grid(post)
    assert 4 <= natural.distance_to(post) <= 8
    tank.position = post
    bot.time += 1
    asyncio.run(control_combat(bot, 2))
    assert tank.actions[-1] == (AbilityId.SIEGEMODE_SIEGEMODE, None)
    tank.type_id = U.SIEGETANKSIEGED
    bot.time += 2
    asyncio.run(control_combat(bot, 4))
    assert len(tank.actions) == 2


def test_contested_outer_keeps_only_a_small_natural_reserve():
    natural = DummyUnit(901, U.COMMANDCENTER, point(34, 34), is_structure=True)
    third = DummyUnit(902, U.COMMANDCENTER, point(63, 63), is_structure=True)
    tank = DummyUnit(30, U.SIEGETANKSIEGED, point(38, 38), ground_range=13)
    field_tank = DummyUnit(31, U.SIEGETANK, point(57, 58), ground_range=7)
    marines = [
        DummyUnit(i, U.MARINE, point(39, 39) if i <= 2 else point(57, 58))
        for i in range(1, 17)
    ]
    attackers = [DummyUnit(100 + i, U.ROACH, point(64, 63)) for i in range(15)]
    bot = dummy_bot(units=[tank, field_tank, *marines], enemies=attackers, now=390)
    bot.townhalls.extend([natural, third])
    asyncio.run(control_combat(bot, 0))
    state = bot._mizuki_combat_state
    assert state.reserve_tank == tank.tag and state.reserve_bio == (1, 2)
    assert not tank.actions and not marines[0].actions
    assert field_tank.actions and marines[-1].actions
    attackers[0].is_snapshot = True
    bot.enemy_units = attackers[:1]
    bot.time += 3
    asyncio.run(control_combat(bot, 2))
    assert not tank.actions
    assert state.outer_contest_until > bot.time


def test_clear_local_advantage_releases_quiet_reserve_to_counterattack():
    natural = DummyUnit(901, U.COMMANDCENTER, point(30, 30), is_structure=True)
    third = DummyUnit(902, U.COMMANDCENTER, point(56, 56), is_structure=True)
    tank = DummyUnit(30, U.SIEGETANKSIEGED, point(34, 34), ground_range=13)
    marines = [DummyUnit(i, U.MARINE, point(53, 53)) for i in range(1, 14)]
    attackers = [DummyUnit(100 + i, U.ROACH, point(57, 56)) for i in range(3)]
    bot = dummy_bot(units=[tank, *marines], enemies=attackers, now=390)
    bot.townhalls.extend([natural, third])
    bot._mizuki_combat_state = _CombatState(
        reserve_tank=tank.tag, outer_contest_until=398
    )
    asyncio.run(control_combat(bot, 0))
    assert tank.actions == [(AbilityId.UNSIEGE_UNSIEGE, None)]
    assert marines[-1].actions[-1][0] == "attack"


def test_quiet_natural_post_does_not_hold_tank_during_clean_pressure():
    natural = DummyUnit(901, U.COMMANDCENTER, point(32, 32), is_structure=True)
    third = DummyUnit(902, U.COMMANDCENTER, point(47, 45), is_structure=True)
    tank = DummyUnit(30, U.SIEGETANKSIEGED, point(36, 36), ground_range=13)
    marines = [DummyUnit(i, U.MARINE, point(42, 40)) for i in range(1, 20)]
    base = DummyUnit(200, U.HATCHERY, point(78, 78), is_structure=True)
    bot = dummy_bot(units=[tank, *marines], structures=[base], now=440)
    bot.townhalls.extend([natural, third])
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.phase is _Phase.PRESSURE
    assert tank.actions == [(AbilityId.UNSIEGE_UNSIEGE, None)]


def test_precontact_post_is_abandoned_if_firing_arc_covers_it():
    natural = DummyUnit(901, U.COMMANDCENTER, point(34, 34), is_structure=True)
    third = DummyUnit(902, U.COMMANDCENTER, point(57, 50), is_structure=True)
    tank = DummyUnit(30, U.SIEGETANK, point(38, 38), ground_range=7)
    guns = [
        DummyUnit(90, U.SIEGETANKSIEGED, point(51, 38), ground_range=13),
        DummyUnit(91, U.SIEGETANKSIEGED, point(52, 39), ground_range=13),
    ]
    bot = dummy_bot(units=[tank], enemies=guns, now=390)
    bot.townhalls.extend([natural, third])
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.reserve_tank is None
    assert tank.actions and tank.actions[0][0] == "move"


def _sole_tank_at_outer_handoff():
    natural = DummyUnit(901, U.COMMANDCENTER, point(32, 32), is_structure=True)
    third = DummyUnit(
        902, U.COMMANDCENTER, point(52, 50), is_structure=True, is_ready=False
    )
    tank = DummyUnit(30, U.SIEGETANKSIEGED, point(36, 36), ground_range=13)
    marines = [DummyUnit(i, U.MARINE, point(44, 41)) for i in range(1, 15)]
    target = DummyUnit(200, U.BARRACKS, point(80, 80), is_structure=True)
    bot = dummy_bot(units=[tank, *marines], structures=[target], now=389)
    bot.townhalls.extend([natural, third])
    asyncio.run(control_combat(bot, 0))
    assert bot._mizuki_combat_state.phase is _Phase.PRESSURE
    assert not tank.actions and marines[0].actions
    third.is_ready = True
    bot.time += 1
    return bot, third, tank, marines


def test_third_completion_does_not_immediately_unseat_sole_posted_tank():
    bot, third, tank, marines = _sole_tank_at_outer_handoff()
    asyncio.run(control_combat(bot, 2))
    assert not tank.actions
    assert bot._mizuki_combat_state.post_handoff_until == 435
    # A destroyed expansion should not immediately stall the field or vacate home.
    bot.townhalls.remove(third)
    bot.time += 40
    asyncio.run(control_combat(bot, 4))
    assert not tank.actions
    assert marines[0].actions[-1] == ("attack", bot.enemy_structures[0].position)
    bot.time += 6
    asyncio.run(control_combat(bot, 6))
    assert tank.actions == [(AbilityId.UNSIEGE_UNSIEGE, None)]


def test_handoff_releases_when_backup_tank_finishes():
    bot, _, tank, _ = _sole_tank_at_outer_handoff()
    asyncio.run(control_combat(bot, 2))
    assert not tank.actions
    bot.units.append(DummyUnit(31, U.SIEGETANK, point(23, 22)))
    bot.time += 5
    asyncio.run(control_combat(bot, 4))
    assert tank.actions == [(AbilityId.UNSIEGE_UNSIEGE, None)]


def test_handoff_releases_when_field_force_already_has_local_advantage():
    bot, _third, tank, marines = _sole_tank_at_outer_handoff()
    asyncio.run(control_combat(bot, 2))
    assert not tank.actions
    for marine in marines:
        marine.position = point(51, 50)
    enemy = DummyUnit(80, U.MARAUDER, point(53, 50))
    bot.enemy_units.append(enemy)
    bot.time += 5
    asyncio.run(control_combat(bot, 4))
    assert tank.actions == [(AbilityId.UNSIEGE_UNSIEGE, None)]
    assert marines[0].actions[-1] == ("attack", enemy)


def test_handoff_does_not_override_unsafe_siege_arc():
    bot, _, tank, _ = _sole_tank_at_outer_handoff()
    asyncio.run(control_combat(bot, 2))
    assert not tank.actions
    guns = [
        DummyUnit(80, U.SIEGETANKSIEGED, point(48, 35), ground_range=13),
        DummyUnit(81, U.SIEGETANKSIEGED, point(49, 35), ground_range=13),
    ]
    bot.enemy_units.extend(guns)
    bot.time += 5
    asyncio.run(control_combat(bot, 4))
    assert bot._mizuki_combat_state.post_handoff_until <= bot.time
    assert tank.actions == [("attack", guns[0])]


def test_handoff_keeps_post_when_natural_attack_starts_outside_tank_range():
    bot, _, tank, marines = _sole_tank_at_outer_handoff()
    asyncio.run(control_combat(bot, 2))
    attacker = DummyUnit(80, U.MARAUDER, point(20, 35))
    bot.enemy_units.append(attacker)
    bot.time += 5
    asyncio.run(control_combat(bot, 4))
    assert not tank.actions
    assert marines[0].actions[-1] == ("attack", attacker.position)
    attacker.position = point(25, 35)
    bot.time += 1
    asyncio.run(control_combat(bot, 6))
    assert tank.actions == [("attack", attacker)]
