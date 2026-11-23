"""关键场景的离线回归测试。

覆盖：开局建造顺序、补给、被骚扰后恢复采矿、外矿失守取舍、
防守优先级、主动推进与撤退。
"""

from __future__ import annotations

import pytest
from helpers import FakeBotWorld, FakeUnit, FakeUnits, load_bot_module
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

module = load_bot_module()


# ---------------------------------------------------------------------------
# 开局：14 人口补给站 -> 兵营 -> 气矿 -> 二人口 -> 自然分矿
# ---------------------------------------------------------------------------


def test_opening_builds_depot_then_barracks_then_gas():
    world = FakeBotWorld(module)
    world.bot.supply_used = 13
    world.bot.minerals = 200
    world.time_seconds = 5

    assert world.run(world.bot._manage_opening()) is True
    assert world.builds and world.builds[0][0] == UnitTypeId.SUPPLYDEPOT
    assert world.bot._open_depot1 is True

    # 补给站刚开工时不会重复拍第二座，也不会跳过兵营直接开矿。
    world.run(world.bot._manage_opening())
    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]

    world.bot.supply_used = 15
    world.bot.minerals = 200
    world.run(world.bot._manage_opening())
    assert world.builds[-1][0] == UnitTypeId.BARRACKS
    assert world.bot._open_rax is True


def test_opening_takes_natural_expansion_after_tech():
    world = FakeBotWorld(module)
    world.bot._open_depot1 = True
    world.bot._open_rax = True
    world.bot._open_gas = True
    world.bot._open_depot2 = True
    world.bot.supply_used = 24
    world.bot.minerals = 450
    world.bot._natural = Point2((62, 50))
    world.time_seconds = 180

    world.run(world.bot._manage_opening())
    assert world.bot._open_cc is True
    assert world.expansions == [Point2((62, 50))]
    assert world.bot._last_expand == pytest.approx(180, abs=0.1)


def test_opening_does_not_stall_forever_when_minerals_never_reach_400():
    world = FakeBotWorld(module)
    world.bot._open_depot1 = True
    world.bot._open_rax = True
    world.bot._open_gas = True
    world.bot._open_depot2 = True
    world.bot.minerals = 0
    world.time_seconds = 421

    world.run(world.bot._manage_opening())
    assert world.bot._open_cc is True
    assert world.expansions == []


# ---------------------------------------------------------------------------
# 补给
# ---------------------------------------------------------------------------


def test_supply_depot_is_requested_before_becoming_blocked():
    world = FakeBotWorld(module)
    world.bot.supply_used = 14
    world.bot.supply_cap = 15
    world.bot.supply_left = 1
    world.bot.minerals = 200

    world.run(world.bot._manage_supply())
    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]


def test_supply_manager_does_not_overbuild_when_depot_pending():
    world = FakeBotWorld(module)
    world.bot.already_pending = lambda unit_type: 1
    world.bot.supply_used = 14
    world.bot.supply_cap = 15

    world.run(world.bot._manage_supply())
    assert world.builds == []


def test_supply_policy_handles_pending_and_max_supply():
    assert module.supply_depot_needed(14, 15, 0) is True
    assert module.supply_depot_needed(14, 15, 1) is False
    assert module.supply_depot_needed(30, 31, 1) is False
    assert module.supply_depot_needed(198, 200, 0) is False


# ---------------------------------------------------------------------------
# 骚扰后恢复：空闲农民回去采矿、继续补农民
# ---------------------------------------------------------------------------


def test_idle_worker_returns_to_mining_after_harassment():
    world = FakeBotWorld(module)
    world.add_mineral_field((52, 50), assigned_harvesters=0)
    field = world.mineral_fields[0]
    worker = world.bot.workers[0]

    world.bot._rebalance_workers()

    assert len(world.commands) == 1
    command = world.commands[0]
    assert command.kind == "gather"
    assert command.unit is worker
    assert command.target is field


def test_worker_target_and_economy_recovery_policy():
    assert module.worker_target(8, 1) == 19
    assert module.worker_target(16, 4) == 44
    # 农民被打光一半时，不应继续开三矿。
    assert module.desired_base_count(600, 18, 30, 2, 0) == 2
    # 经济恢复后允许三矿。
    assert module.desired_base_count(600, 36, 30, 2, 0) == 3


def test_economy_emergency_is_triggered_by_heavy_worker_loss():
    world = FakeBotWorld(module)
    world.add_mineral_field((52, 50))
    world.add_mineral_field((54, 50))
    world.bot.workers = FakeUnits([])
    world.bot.supply_used = 30
    world.bot.supply_cap = 40
    world.time_seconds = 300

    world.run(world.bot._manage_economy())

    assert world.bot._economy_emergency_until > 300


# ---------------------------------------------------------------------------
# 外矿失守：冷却期内不回头，改去更远的矿点
# ---------------------------------------------------------------------------


def test_lost_natural_is_on_cooldown_then_available_again():
    world = FakeBotWorld(module)
    natural = Point2((62, 50))
    world.bot._lost_bases[(62, 50)] = 100
    world.time_seconds = 150

    assert world.bot._expansion_on_cooldown(natural) is True
    world.time_seconds = 200
    assert world.bot._expansion_on_cooldown(natural) is False


def test_expansion_picker_skips_lost_natural_and_takes_third():
    distances = [0, 20, 40, 60]
    taken = [True, False, False, False]
    blocked = [False, False, False, False]
    cooldown = [False, True, False, False]

    index = module.choose_expansion_index(distances, taken, blocked, cooldown)
    assert index == 2


def test_expansion_picker_skips_enemy_camped_location():
    distances = [0, 20, 40]
    taken = [True, False, False]
    blocked = [False, True, False]
    cooldown = [False, False, False]

    assert module.choose_expansion_index(distances, taken, blocked, cooldown) == 2


# ---------------------------------------------------------------------------
# 防守与推进
# ---------------------------------------------------------------------------


def test_defense_detects_enemy_near_base_and_recalls_attack():
    world = FakeBotWorld(module)
    world.bot.attack_state = "ATTACK"
    world.bot._army_supply = lambda: 10.0
    world.enemy_units = FakeUnits(
        [
            FakeUnit(UnitTypeId.ZERGLING, (52, 50)),
            FakeUnit(UnitTypeId.ZERGLING, (53, 50)),
        ]
    )
    world.bot.enemy_units = world.enemy_units
    world.time_seconds = 400

    world.run(world.bot._manage_defense())

    assert world.bot._threat_level >= 1
    assert world.bot._defense_until > 400
    assert world.bot.attack_state == "GATHER"


def test_attack_waits_for_army_and_medivac():
    world = FakeBotWorld(module)
    world.bot.attack_state = "GATHER"
    world.bot._enemy_army_supply = 5.0
    world.bot._threat_level = 0
    world.time_seconds = 330
    world.bot.calculate_supply_cost = lambda unit_type: 2.0
    for index in range(10):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 60), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (60, 60), is_idle=False)

    world.bot._manage_army()
    assert world.bot.attack_state == "ATTACK"


def test_attack_does_not_launch_without_medivac_before_540():
    assert (
        module.attack_ready(
            army_supply=40, medivacs=0, time=400, enemy_army_supply=5, threat_level=0
        )
        is False
    )
    assert (
        module.attack_ready(
            army_supply=40, medivacs=1, time=400, enemy_army_supply=5, threat_level=0
        )
        is True
    )


def test_attack_is_cancelled_when_outnumbered_or_threatened():
    assert (
        module.attack_ready(
            army_supply=30, medivacs=2, time=500, enemy_army_supply=45, threat_level=0
        )
        is False
    )
    assert (
        module.attack_ready(
            army_supply=40, medivacs=2, time=500, enemy_army_supply=5, threat_level=2
        )
        is False
    )


def test_retreat_when_army_is_wiped_or_outnumbered():
    assert module.should_retreat(5, 5, 20, 10, 20) is True
    assert module.should_retreat(35, 25, 50, 20, 30) is True
    assert module.should_retreat(60, 40, 20, 10, 30) is False
    # 久攻不下且兵力亏空，也应撤退。
    assert module.should_retreat(17, 12, 5, 2, 200) is True


def test_retreat_state_is_entered_when_army_collapses():
    world = FakeBotWorld(module)
    world.bot.attack_state = "ATTACK"
    world.bot._attack_started_at = 300
    world.bot._enemy_army_supply = 30
    world.time_seconds = 340
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    for index in range(5):
        world.add_unit(UnitTypeId.MARINE, (80 + index, 80), is_idle=False)

    world.bot._manage_army()
    assert world.bot.attack_state == "RETREAT"


def test_threat_classification():
    assert module.classify_threat(0, 0, 20) == 0
    assert module.classify_threat(2, 1, 20) == 1
    assert module.classify_threat(12, 3, 20) == 2
    assert module.classify_threat(2, 5, 5) == 2


# ---------------------------------------------------------------------------
# 集成冒烟：中后期 on_step 全流程（不启动 SC2）
# ---------------------------------------------------------------------------


def _build_midgame_world():
    world = FakeBotWorld(module)
    bot = world.bot
    bot.minerals = 800
    bot.vespene = 300
    bot.supply_used = 95
    bot.supply_cap = 120
    bot.supply_left = 25
    bot._opening_done = True
    bot._open_cc = True
    bot._last_expand = 0
    world.time_seconds = 420

    bot.townhalls = FakeUnits(
        [
            FakeUnit(UnitTypeId.COMMANDCENTER, (50, 50)),
            FakeUnit(UnitTypeId.ORBITALCOMMAND, (62, 50)),
        ]
    )
    world.add_mineral_field((52, 50))
    world.add_mineral_field((54, 50))
    world.add_mineral_field((64, 50))
    world.add_mineral_field((66, 50))
    world.add_structure(UnitTypeId.REFINERY, (56, 50), assigned_harvesters=3)
    world.add_structure(UnitTypeId.REFINERY, (68, 50), assigned_harvesters=3, tag=999)

    for index in range(3):
        world.add_structure(
            UnitTypeId.BARRACKS,
            (44 + index * 3, 44),
            add_on_tag=100 + index,
            is_idle=False,
        )
    world.add_structure(UnitTypeId.FACTORY, (40, 46), add_on_tag=200, is_idle=False)
    world.add_structure(UnitTypeId.STARPORT, (40, 40), add_on_tag=300, is_idle=False)
    world.add_structure(UnitTypeId.ENGINEERINGBAY, (46, 40))
    bot.techlab_tags = {100, 200}
    bot.reactor_tags = {101, 102, 300}

    for index in range(10):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 60), is_idle=False)
    world.add_unit(UnitTypeId.MARAUDER, (70, 60), is_idle=False)
    world.add_unit(UnitTypeId.MARAUDER, (71, 60), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (60, 62), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (61, 62), is_idle=False)
    world.add_unit(UnitTypeId.SIEGETANK, (72, 60), is_idle=False)
    world.add_unit(UnitTypeId.SIEGETANKSIEGED, (73, 60), is_idle=False)
    bot.workers = FakeUnits(
        [FakeUnit(UnitTypeId.SCV, (52 + i % 8, 52), tag=500 + i) for i in range(30)]
    )
    return world


def test_midgame_on_step_runs_full_pipeline_offline():
    world = _build_midgame_world()
    world.run(world.bot.on_step(0))
    assert world.commands, "中后期 on_step 应当产生指令"
    assert world.bot.attack_state in {"GATHER", "ATTACK"}

    # 推进 3 分钟后仍能正常决策，不抛异常、不丢失状态。
    world.time_seconds = 600
    world.run(world.bot.on_step(1))
    world.time_seconds = 780
    world.run(world.bot.on_step(2))
    assert world.bot.attack_state in {"GATHER", "ATTACK", "RETREAT"}


def test_midgame_on_step_defends_against_natural_push():
    world = _build_midgame_world()
    world.enemy_units = FakeUnits(
        [
            FakeUnit(UnitTypeId.MARINE, (64, 52)),
            FakeUnit(UnitTypeId.MARINE, (65, 52)),
            FakeUnit(UnitTypeId.MARAUDER, (66, 52)),
            FakeUnit(UnitTypeId.SIEGETANK, (67, 52)),
        ]
    )
    world.bot.enemy_units = world.enemy_units
    world.bot._natural = Point2((62, 50))

    world.run(world.bot.on_step(0))

    assert world.bot._threat_level >= 1
    assert world.bot._defense_until > 420
    assert world.bot.attack_state != "ATTACK"


def test_early_on_step_starts_opening_without_client():
    world = FakeBotWorld(module)
    world.bot.supply_used = 13
    world.bot.supply_cap = 15
    world.bot.supply_left = 2
    world.bot.minerals = 120
    world.bot.workers = FakeUnits(
        [FakeUnit(UnitTypeId.SCV, (51 + i * 0.2, 51), tag=600 + i) for i in range(12)]
    )
    world.add_mineral_field((52, 50))
    world.add_mineral_field((54, 50))
    world.time_seconds = 5

    world.run(world.bot.on_step(0))

    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]
    assert world.bot._open_depot1 is True
