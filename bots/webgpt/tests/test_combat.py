"""离线战斗场景回归；只验证决策，不声称已连接 SC2 或取得胜绩。"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from bots.webgpt.combat import CombatController, SpatialIndex, can_hit, in_weapon_range
from bots.webgpt.config import BotConfig
from bots.webgpt.model import BattlePlan, IntelReport, Stance
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.effect_id import EffectId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2


@dataclass
class Command:
    unit: object
    ability: object
    target: object = None


class FakeUnit:
    def __init__(
        self,
        tag,
        type_id=U.MARINE,
        pos=(30, 30),
        *,
        flying=False,
        ground_range=5.0,
        air_range=5.0,
        health=45,
        health_max=45,
        cooldown=0.0,
        visible=True,
        attackable=True,
        biological=True,
        structure=False,
        armored=False,
        energy=100,
    ):
        self.tag = tag
        self.type_id = type_id
        self.position = Point2(pos)
        self.is_flying = flying
        self.ground_range = ground_range
        self.air_range = air_range
        self.can_attack_ground = ground_range > 0
        self.can_attack_air = air_range > 0
        self.can_attack = self.can_attack_air or self.can_attack_ground
        self.health = health
        self.health_max = health_max
        self.health_percentage = health / health_max
        self.weapon_cooldown = cooldown
        self.is_visible = visible
        self.can_be_attacked = attackable
        self.is_biological = biological
        self.is_structure = structure
        self.is_armored = armored
        self.energy = energy
        self.radius = 0.5
        self.is_ready = True
        self.detect_range = 11.0
        self.buffs = set()

    def move(self, point):
        return Command(self, "move", point)

    def attack(self, target):
        return Command(self, "attack", target)

    def __call__(self, ability, target=None):
        return Command(self, ability, target)

    def has_buff(self, buff):
        return buff in self.buffs


class FakeBus:
    def __init__(self, abilities=None):
        self.abilities = abilities or {}
        self.issued = set()
        self.commands = []

    def has(self, unit, ability):
        return ability in self.abilities.get(unit.tag, set())

    def issue(self, command, **kwargs):
        assert command.unit.tag not in self.issued, "同一单位收到两个互相覆盖的命令"
        self.commands.append(command)
        self.issued.add(command.unit.tag)
        return True


def world(units, enemies=(), structures=(), effects=(), time=200.0, pathable=True):
    return SimpleNamespace(
        units=list(units),
        enemy_units=list(enemies),
        enemy_structures=list(structures),
        time=time,
        state=SimpleNamespace(effects=effects),
        in_map_bounds=lambda p: 0 <= p.x < 200 and 0 <= p.y < 200,
        in_pathing_grid=lambda p: pathable,
    )


def plan(stance=Stance.ATTACK, objective=(100, 100)):
    return BattlePlan(stance, Point2(objective), Point2((25, 25)), Point2((10, 10)))


def play(bot, bus=None, order=None, controller=None, intel=None):
    controller = controller or CombatController(BotConfig())
    bus = bus or FakeBus()
    asyncio.run(controller.step(bot, bus, order or plan(), intel or IntelReport()))
    return bus


def test_visibility_does_not_make_undetected_cloak_attackable():
    marine = FakeUnit(1)
    cloaked = FakeUnit(2, U.DARKTEMPLAR, (33, 30), attackable=False)
    snapshot = FakeUnit(3, U.SIEGETANK, (34, 30), visible=False)
    bus = play(world([marine], [cloaked, snapshot]))
    assert all(not isinstance(command.target, FakeUnit) for command in bus.commands)


def test_marauder_does_not_attack_air_target():
    marauder = FakeUnit(1, U.MARAUDER, air_range=0)
    mutalisk = FakeUnit(2, U.MUTALISK, (33, 30), flying=True)
    bus = play(world([marauder], [mutalisk]))
    assert all(command.target is not mutalisk for command in bus.commands)


def test_viking_can_attack_ground_colossus_with_air_weapon():
    fighter = FakeUnit(1, U.VIKINGFIGHTER, flying=True, ground_range=0, air_range=9)
    colossus = FakeUnit(2, U.COLOSSUS, (37, 30))
    assert can_hit(fighter, colossus)
    assert in_weapon_range(fighter, colossus)
    bus = play(world([fighter], [colossus]))
    assert bus.commands[0].target is colossus


def test_marine_stims_only_once_in_step_when_healthy_and_fighting():
    marine = FakeUnit(1)
    zergling = FakeUnit(2, U.ZERGLING, (34, 30), ground_range=0.1, air_range=0)
    bus = play(world([marine], [zergling]), FakeBus({1: {A.EFFECT_STIM_MARINE}}))
    assert len(bus.commands) == 1
    assert bus.commands[0].ability == A.EFFECT_STIM_MARINE


def test_weak_marine_shoots_without_spending_health_on_stim():
    marine = FakeUnit(1, health=20)
    zergling = FakeUnit(2, U.ZERGLING, (34, 30), ground_range=0.1, air_range=0)
    bus = play(world([marine], [zergling]), FakeBus({1: {A.EFFECT_STIM_MARINE}}))
    assert bus.commands[0].ability == "attack"
    assert bus.commands[0].target is zergling


def test_stim_is_not_used_to_kill_a_lone_worker():
    marine = FakeUnit(1)
    worker = FakeUnit(2, U.SCV, (34, 30), ground_range=0.1, air_range=0)
    bus = play(world([marine], [worker]), FakeBus({1: {A.EFFECT_STIM_MARINE}}))
    assert bus.commands[0].ability == "attack"


def test_ranged_infantry_kites_melee_during_weapon_cooldown():
    marine = FakeUnit(1, cooldown=8)
    zergling = FakeUnit(2, U.ZERGLING, (33, 30), ground_range=0.1, air_range=0)
    bus = play(world([marine], [zergling]))
    command = bus.commands[0]
    assert command.ability == "move"
    assert command.target.x < marine.position.x


def test_does_not_kite_backwards_indefinitely_against_longer_range_weapon():
    marine = FakeUnit(1, cooldown=8)
    tank = FakeUnit(2, U.SIEGETANKSIEGED, (34, 30), ground_range=13, air_range=0)
    bus = play(world([marine], [tank]))
    assert bus.commands[0].ability == "attack"
    assert bus.commands[0].target == plan().objective


def test_workers_are_not_assigned_combat_orders():
    scv = FakeUnit(1, U.SCV)
    mule = FakeUnit(2, U.MULE)
    bus = play(world([scv, mule], [FakeUnit(3, pos=(31, 30))]))
    assert bus.commands == []


def test_existing_command_owner_is_respected():
    marine = FakeUnit(1)
    bus = FakeBus()
    bus.issued.add(1)
    play(world([marine], [FakeUnit(2, pos=(32, 30))]), bus)
    assert bus.commands == []


def test_tank_sieges_for_ground_contact_and_does_not_move_same_step():
    tank = FakeUnit(1, U.SIEGETANK, ground_range=7, air_range=0, biological=False)
    enemy = FakeUnit(2, pos=(40, 30))
    bus = play(world([tank], [enemy]), FakeBus({1: {A.SIEGEMODE_SIEGEMODE}}))
    assert len(bus.commands) == 1
    assert bus.commands[0].ability == A.SIEGEMODE_SIEGEMODE


def test_tank_does_not_siege_for_air_only_contact():
    tank = FakeUnit(1, U.SIEGETANK, ground_range=7, air_range=0, biological=False)
    enemy = FakeUnit(2, U.MUTALISK, (40, 30), flying=True)
    bus = play(world([tank], [enemy]), FakeBus({1: {A.SIEGEMODE_SIEGEMODE}}))
    assert all(command.ability != A.SIEGEMODE_SIEGEMODE for command in bus.commands)


def test_sieged_tank_unsieges_when_objective_is_out_of_range_after_grace():
    tank = FakeUnit(1, U.SIEGETANKSIEGED, ground_range=13, air_range=0, biological=False)
    enemy = FakeUnit(2, pos=(45.5, 30))
    controller = CombatController(BotConfig())
    controller._tank_contact[1] = 190.0
    bus = play(
        world([tank], [enemy], time=200),
        FakeBus({1: {A.UNSIEGE_UNSIEGE}}),
        controller=controller,
    )
    assert bus.commands[0].ability == A.UNSIEGE_UNSIEGE


def test_tank_morph_hysteresis_prevents_immediate_toggle():
    tank = FakeUnit(1, U.SIEGETANKSIEGED, ground_range=13, air_range=0, biological=False)
    controller = CombatController(BotConfig())
    controller._last_morph[1] = 199.0
    bus = play(
        world([tank], time=200),
        FakeBus({1: {A.UNSIEGE_UNSIEGE}}),
        order=plan(Stance.RETREAT),
        controller=controller,
    )
    assert bus.commands == []


def test_tank_releases_minimum_range_trap():
    tank = FakeUnit(1, U.SIEGETANKSIEGED, ground_range=13, air_range=0, biological=False)
    ling = FakeUnit(2, U.ZERGLING, (31, 30), ground_range=0.1, air_range=0)
    bus = play(world([tank], [ling]), FakeBus({1: {A.UNSIEGE_UNSIEGE}}))
    assert bus.commands[0].ability == A.UNSIEGE_UNSIEGE


def test_medivac_heal_is_not_overridden_by_follow():
    medivac = FakeUnit(1, U.MEDIVAC, flying=True, ground_range=0, air_range=0, biological=False)
    marine = FakeUnit(2, pos=(32, 30), health=10)
    bus = play(world([medivac, marine]), FakeBus({1: {A.MEDIVACHEAL_HEAL}}))
    commands = [command for command in bus.commands if command.unit.tag == 1]
    assert len(commands) == 1
    assert commands[0].ability == A.MEDIVACHEAL_HEAL
    assert commands[0].target is marine


def test_medivac_flees_anti_air_before_healing():
    medivac = FakeUnit(1, U.MEDIVAC, flying=True, ground_range=0, air_range=0, biological=False)
    marine = FakeUnit(2, pos=(32, 30), health=10)
    enemy = FakeUnit(3, pos=(33, 30), air_range=6)
    bus = play(world([medivac, marine], [enemy]), FakeBus({1: {A.MEDIVACHEAL_HEAL}}))
    command = next(command for command in bus.commands if command.unit.tag == 1)
    assert command.ability == "move"
    assert command.target.x < 30


def test_medivac_does_not_call_heal_without_energy():
    medivac = FakeUnit(
        1,
        U.MEDIVAC,
        flying=True,
        ground_range=0,
        air_range=0,
        biological=False,
        energy=0,
    )
    marine = FakeUnit(2, pos=(34, 30), health=10)
    bus = play(world([medivac, marine]), FakeBus({1: {A.MEDIVACHEAL_HEAL}}))
    assert all(command.ability != A.MEDIVACHEAL_HEAL for command in bus.commands)


def test_healers_distribute_attention_when_several_patients_exist():
    medivacs = [
        FakeUnit(tag, U.MEDIVAC, flying=True, ground_range=0, air_range=0, biological=False)
        for tag in (1, 2)
    ]
    marines = [FakeUnit(tag, pos=(31, 30), health=20) for tag in (3, 4)]
    bus = play(
        world(medivacs + marines),
        FakeBus({1: {A.MEDIVACHEAL_HEAL}, 2: {A.MEDIVACHEAL_HEAL}}),
    )
    treated = {
        command.target.tag for command in bus.commands if command.ability == A.MEDIVACHEAL_HEAL
    }
    assert treated == {3, 4}


def effect(effect_id, point=(30, 30), radius=2, enemy=True):
    return SimpleNamespace(id=effect_id, positions={Point2(point)}, radius=radius, is_enemy=enemy)


def test_storm_avoidance_has_priority_over_ready_shot():
    marine = FakeUnit(1)
    enemy = FakeUnit(2, pos=(33, 30))
    bus = play(world([marine], [enemy], effects=[effect(EffectId.PSISTORMPERSISTENT)]))
    assert len(bus.commands) == 1
    assert bus.commands[0].ability == "move"
    assert bus.commands[0].target.distance_to(marine.position) >= 4


def test_own_liberator_zone_does_not_scare_own_army():
    marine = FakeUnit(1)
    enemy = FakeUnit(2, pos=(33, 30))
    bus = play(
        world(
            [marine],
            [enemy],
            effects=[effect(EffectId.LIBERATORTARGETMORPHPERSISTENT, enemy=False)],
        )
    )
    assert bus.commands[0].ability == "attack"
    assert bus.commands[0].target is enemy


def test_air_unit_ignores_ground_only_disruptor_nova():
    fighter = FakeUnit(1, U.VIKINGFIGHTER, flying=True, ground_range=0, air_range=9)
    medivac = FakeUnit(2, U.MEDIVAC, (35, 30), flying=True, ground_range=0, air_range=0)
    nova = FakeUnit(3, U.DISRUPTORPHASED, (30, 30), ground_range=0, air_range=0)
    bus = play(world([fighter], [medivac, nova]))
    assert bus.commands[0].target is medivac


def test_sieged_tank_unsieges_in_storm_instead_of_illegal_move():
    tank = FakeUnit(1, U.SIEGETANKSIEGED, ground_range=13, air_range=0)
    bus = play(
        world([tank], effects=[effect(EffectId.PSISTORMPERSISTENT)]),
        FakeBus({1: {A.UNSIEGE_UNSIEGE}}),
    )
    assert bus.commands[0].ability == A.UNSIEGE_UNSIEGE


def test_ground_movement_never_targets_an_unpathable_endpoint():
    marine = FakeUnit(1)
    bus = play(world([marine], pathable=False))
    assert bus.commands == []


def test_hazard_with_no_legal_exit_does_not_issue_illegal_order():
    marine = FakeUnit(1)
    bus = play(world([marine], effects=[effect(EffectId.PSISTORMPERSISTENT)], pathable=False))
    assert bus.commands == []


def test_air_pathing_is_independent_of_ground_grid():
    raven = FakeUnit(1, U.RAVEN, flying=True, ground_range=0, air_range=0, biological=False)
    marine = FakeUnit(2, pos=(40, 30))
    bus = play(world([raven, marine], pathable=False))
    assert any(command.unit.tag == 1 for command in bus.commands)
    assert not any(command.unit.tag == 2 for command in bus.commands)


def test_viking_stays_airborne_in_presence_of_anti_air():
    viking = FakeUnit(1, U.VIKINGFIGHTER, flying=True, ground_range=0, air_range=9)
    enemy = FakeUnit(2, pos=(34, 30))
    controller = CombatController(BotConfig())
    controller._last_air_seen = 1
    bus = play(
        world([viking], [enemy], time=200),
        FakeBus({1: {A.MORPH_VIKINGASSAULTMODE}}),
        controller=controller,
    )
    assert all(command.ability != A.MORPH_VIKINGASSAULTMODE for command in bus.commands)


def test_ground_viking_morphs_back_for_air_contact():
    viking = FakeUnit(1, U.VIKINGASSAULT, air_range=0)
    enemy = FakeUnit(2, U.MUTALISK, (34, 30), flying=True)
    bus = play(world([viking], [enemy]), FakeBus({1: {A.MORPH_VIKINGFIGHTERMODE}}))
    assert bus.commands[0].ability == A.MORPH_VIKINGFIGHTERMODE


def test_raven_approaches_cloak_without_attacking_it():
    raven = FakeUnit(1, U.RAVEN, flying=True, ground_range=0, air_range=0, biological=False)
    marine = FakeUnit(2, pos=(40, 30))
    dark_templar = FakeUnit(3, U.DARKTEMPLAR, (42, 30), air_range=0, attackable=False)
    bus = play(world([raven, marine], [dark_templar]))
    command = next(command for command in bus.commands if command.unit.tag == 1)
    assert command.ability == "move"
    assert command.target.distance_to(dark_templar.position) < raven.detect_range


@pytest.mark.parametrize(
    "point,radius,tags",
    [((10, 10), 0, {1}), ((9.9, 10), 1, {1, 2}), ((0, 0), 5, set())],
)
def test_spatial_buckets_preserve_cross_cell_neighbors(point, radius, tags):
    units = [
        FakeUnit(1, pos=(10, 10)),
        FakeUnit(2, pos=(9.1, 10)),
        FakeUnit(3, pos=(30, 30)),
    ]
    assert {u.tag for u in SpatialIndex(units).near(Point2(point), radius)} == tags


def test_dead_unit_timers_are_removed():
    controller = CombatController(BotConfig())
    controller._last_morph[99] = 10
    controller._tank_contact[99] = 10
    play(world([]), controller=controller)
    assert not controller._last_morph
    assert not controller._tank_contact


def test_last_flying_viking_continues_search_instead_of_waiting_forever():
    viking = FakeUnit(1, U.VIKINGFIGHTER, flying=True, ground_range=0, air_range=9)
    bus = play(world([viking]), order=plan(Stance.SEARCH))
    assert bus.commands[0].ability == "move"
    assert bus.commands[0].target == plan().objective


def test_real_burnysc2_unit_and_protobuf_observation_produce_real_command():
    """接口测试使用真正的 Unit/UnitCommand；不需要游戏进程。"""
    from s2clientprotocol import data_pb2, raw_pb2, sc2api_pb2
    from sc2.game_data import GameData
    from sc2.unit import Unit
    from sc2.unit_command import UnitCommand

    bot = world([])
    bot.state.game_loop = 4480
    bot.unit_command_uses_self_do = True
    bot.game_data = GameData(
        sc2api_pb2.ResponseData(
            units=[
                data_pb2.UnitTypeData(
                    unit_id=U.MARINE.value,
                    available=True,
                    attributes=[data_pb2.Biological],
                    weapons=[data_pb2.Weapon(type=data_pb2.Weapon.Any, range=5)],
                ),
                data_pb2.UnitTypeData(
                    unit_id=U.ZERGLING.value,
                    available=True,
                    attributes=[data_pb2.Biological],
                    weapons=[data_pb2.Weapon(type=data_pb2.Weapon.Ground, range=0.1)],
                ),
            ]
        )
    )

    def unit(tag, kind, x):
        return Unit(
            raw_pb2.Unit(
                tag=tag,
                unit_type=kind.value,
                display_type=raw_pb2.Visible,
                cloak=raw_pb2.NotCloaked,
                pos={"x": x, "y": 30, "z": 0},
                build_progress=1,
                health=45,
                health_max=45,
                radius=0.5,
            ),
            bot,
            base_build=98310,
        )

    marine, zergling = unit(1, U.MARINE, 30), unit(2, U.ZERGLING, 33)
    bot.units, bot.enemy_units = [marine], [zergling]
    bus = play(bot)
    assert isinstance(bus.commands[0], UnitCommand)
    assert bus.commands[0].ability == A.ATTACK
    assert bus.commands[0].target is zergling
