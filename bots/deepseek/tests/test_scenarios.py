"""关键场景的离线回归测试。

覆盖：开局建造顺序、补给、被骚扰后恢复采矿、外矿失守取舍、
防守优先级、主动推进与撤退。
"""

from __future__ import annotations

import pytest
from helpers import FakeBotWorld, FakeUnit, FakeUnits, load_bot_module
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
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

    # 1 秒内的重复调用不会重复下单（放置限速），也不会跳过兵营直接开矿。
    world.run(world.bot._manage_opening())
    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]

    world.time_seconds += 1.5
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
    world.add_structure(UnitTypeId.SUPPLYDEPOT, (48, 50))
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
    world.add_structure(UnitTypeId.SUPPLYDEPOT, (48, 50))
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
    # 骚扰级（2 人口）不再召回主力；这里用 8 人口的正面推进验证回防路径。
    world.enemy_units = FakeUnits(
        [FakeUnit(UnitTypeId.ZERGLING, (52 + i, 50)) for i in range(8)]
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
            army_supply=40,
            medivacs=0,
            time=400,
            enemy_army_supply=5,
            threat_blocking=False,
        )
        is False
    )
    assert (
        module.attack_ready(
            army_supply=40,
            medivacs=1,
            time=400,
            enemy_army_supply=5,
            threat_blocking=False,
        )
        is True
    )


def test_attack_is_cancelled_when_outnumbered_or_threatened():
    assert (
        module.attack_ready(
            army_supply=30,
            medivacs=2,
            time=500,
            enemy_army_supply=45,
            threat_blocking=False,
        )
        is False
    )
    assert (
        module.attack_ready(
            army_supply=40,
            medivacs=2,
            time=500,
            enemy_army_supply=5,
            threat_blocking=True,
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
        [FakeUnit(UnitTypeId.MARINE, (64 + i * 0.5, 52)) for i in range(10)]
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


# ---------------------------------------------------------------------------
# 掉线排查相关：推进步长、附件放置、重复指令
# ---------------------------------------------------------------------------


def test_on_start_keeps_client_step_and_sets_natural():
    world = FakeBotWorld(module)
    world.run(world.bot.on_start())

    # 不能私自改推进步长：同局两个客户端必须同节奏推帧。
    assert world.bot.client.game_step == 4
    assert world.bot._natural == Point2((62, 50))


def test_addon_is_issued_once_then_throttled():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    rax = world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=101, is_idle=True)

    world.run(world.bot._manage_addons())

    builds = [c for c in world.commands if c.kind == "build"]
    assert len(builds) == 1
    assert builds[0].unit is rax
    assert builds[0].ability == UnitTypeId.BARRACKSTECHLAB

    # 同一帧/紧邻帧不会重复提交放置请求。
    world.run(world.bot._manage_addons())
    assert len([c for c in world.commands if c.kind == "build"]) == 1


def test_addon_skipped_when_slot_blocked():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    rax = world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=102, is_idle=True)
    slot = rax.position.offset((2.5, -0.5))
    world.blocked_slots.add((round(slot.x, 1), round(slot.y, 1)))

    world.run(world.bot._manage_addons())

    assert [c for c in world.commands if c.kind == "build"] == []
    assert world.bot._addon_blocked_until[rax.tag] > 150


def test_addon_queues_behind_current_production():
    """生产中的兵营也要排附件，不能一直等空闲。

    8:16 局 4 座兵营全程 0 附件：没有科技实验室就没有兴奋剂/盾牌与掠夺者，
    没有反应堆产能也只有一半。旧实现要求 `is_idle`，而兵营几乎一直在训练。
    """
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    rax = world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=103, is_idle=False)

    world.run(world.bot._manage_addons())

    builds = [c for c in world.commands if c.kind == "build"]
    assert [c.unit for c in builds] == [rax]
    assert [c.ability for c in builds] == [UnitTypeId.BARRACKSTECHLAB]


def test_addon_waits_when_production_queue_is_deep():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    rax = world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=104, is_idle=False)
    rax.orders = [object(), object()]

    world.run(world.bot._manage_addons())

    assert [c for c in world.commands if c.kind == "build"] == []


def test_addon_order_rejection_backs_off():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot.do = lambda command: False
    rax = world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=105, is_idle=False)

    world.run(world.bot._manage_addons())

    # 客户端拒绝后 10 秒内不再重试，避免回到“每帧提交放置请求”的老问题。
    assert world.bot._addon_blocked_until[rax.tag] == pytest.approx(160)
    world.time_seconds = 155
    world.run(world.bot._manage_addons())
    assert world.bot._addon_blocked_until[rax.tag] == pytest.approx(160)


def test_issue_skips_identical_command_within_interval():
    world = FakeBotWorld(module)
    world.time_seconds = 100
    worker = world.bot.workers[0]
    world.add_mineral_field((52, 50))
    field = world.mineral_fields[0]

    assert world.bot._issue(worker, worker.gather(field), min_interval=2.0) is True
    assert world.bot._issue(worker, worker.gather(field), min_interval=2.0) is False
    assert world.bot._duplicates_skipped == 1

    world.time_seconds = 103
    assert world.bot._issue(worker, worker.gather(field), min_interval=2.0) is True


def test_same_barracks_is_not_given_two_addons():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=111, is_idle=True)

    world.run(world.bot._manage_addons())
    world.time_seconds = 153
    world.run(world.bot._manage_addons())

    builds = [c for c in world.commands if c.kind == "build"]
    assert [c.ability for c in builds] == [UnitTypeId.BARRACKSTECHLAB]


def test_second_barracks_gets_reactor_after_techlab_is_ordered():
    world = FakeBotWorld(module)
    world.time_seconds = 150
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=121, is_idle=True)
    world.add_structure(UnitTypeId.BARRACKS, (48, 44), tag=122, is_idle=True)

    world.run(world.bot._manage_addons())
    world.time_seconds = 152
    world.run(world.bot._manage_addons())

    abilities = [c.ability for c in world.commands if c.kind == "build"]
    assert abilities == [UnitTypeId.BARRACKSTECHLAB, UnitTypeId.BARRACKSREACTOR]


def test_barracks_gets_reactor_once_techlab_exists():
    world = FakeBotWorld(module)
    world.time_seconds = 400
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot.techlab_tags = {9000}
    world.add_structure(UnitTypeId.BARRACKS, (44, 44), tag=113, is_idle=True)

    world.run(world.bot._manage_addons())

    builds = [c for c in world.commands if c.kind == "build"]
    assert [c.ability for c in builds] == [UnitTypeId.BARRACKSREACTOR]


def test_long_run_keeps_command_rate_bounded_and_stable():
    """连续推进 80 帧：不抛异常，且每帧指令量不失控。"""
    world = _build_midgame_world()
    for step in range(80):
        world.time_seconds = 420 + step * 4
        world.bot.minerals = 600 + step * 5
        world.bot.vespene = 250
        world.run(world.bot.on_step(step))

    assert world.bot._commands_peak <= 80, f"每帧指令过多: {world.bot._commands_peak}"
    average = world.bot._commands_total / max(1, world.bot._steps_seen)
    assert average <= 60, f"平均每帧指令过多: {average:.1f}"
    assert world.bot._duplicates_skipped >= 0


# ---------------------------------------------------------------------------
# 本轮复盘驱动：骚扰不再拦阻推进 / 开局建筑静默失败
# ---------------------------------------------------------------------------


def test_threat_blocks_attack_thresholds():
    # 单个死神 / 机枪兵绕后骚扰：不拦。
    assert module.threat_blocks_attack(1, 1, 40) is False
    assert module.threat_blocks_attack(4, 3, 40) is False
    # 一支能吃掉分矿的部队：拦。
    assert module.threat_blocks_attack(8, 4, 40) is True
    # 数量多而家里没兵：拦。
    assert module.threat_blocks_attack(5, 5, 10) is True
    # 数量多但家里有兵：不拦，交给守军。
    assert module.threat_blocks_attack(5, 5, 30) is False


def test_harass_does_not_recall_the_army():
    world = FakeBotWorld(module)
    world.bot.attack_state = "ATTACK"
    world.bot._army_supply = lambda: 30.0
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    world.bot._natural = Point2((62, 50))
    world.enemy_units = FakeUnits([FakeUnit(UnitTypeId.REAPER, (58, 52))])
    world.bot.enemy_units = world.enemy_units
    world.time_seconds = 400

    world.run(world.bot._manage_defense())

    assert world.bot._threat_level == 1
    assert world.bot._threat_blocking is False
    # 关键：不再续期防守、不再强制回家。
    assert world.bot._defense_until == 0
    assert world.bot.attack_state == "ATTACK"


def test_push_still_recalls_the_army():
    world = FakeBotWorld(module)
    world.bot.attack_state = "ATTACK"
    world.bot._army_supply = lambda: 30.0
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    world.bot._natural = Point2((62, 50))
    world.enemy_units = FakeUnits(
        [FakeUnit(UnitTypeId.MARINE, (60 + i, 52)) for i in range(10)]
    )
    world.bot.enemy_units = world.enemy_units
    world.time_seconds = 400

    world.run(world.bot._manage_defense())

    assert world.bot._threat_blocking is True
    assert world.bot._defense_until > 400
    assert world.bot.attack_state == "GATHER"


def test_army_leaves_home_while_enemy_harasses():
    world = FakeBotWorld(module)
    world.bot.calculate_supply_cost = lambda unit_type: 2.0
    world.time_seconds = 420
    world.bot._prev_step_time = 400
    for index in range(10):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 60), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (60, 61), is_idle=False)
    world.enemy_units = FakeUnits([FakeUnit(UnitTypeId.REAPER, (58, 52))])
    world.bot.enemy_units = world.enemy_units

    world.run(world.bot._manage_defense())
    world.bot._manage_army()

    assert world.bot.attack_state == "ATTACK"
    assert world.bot._first_attack_at == 420

    world.time_seconds = 430
    world.bot._manage_army()
    assert world.bot._attack_seconds == pytest.approx(10, abs=0.5)


def test_retreat_holds_army_for_regroup_before_attacking_again():
    world = FakeBotWorld(module)
    world.bot.calculate_supply_cost = lambda unit_type: 2.0
    world.bot.attack_state = "RETREAT"
    world.bot._retreat_at = 500
    world.time_seconds = 505
    for index in range(10):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 60), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (60, 61), is_idle=False)

    world.bot._manage_army()
    assert world.bot.attack_state == "GATHER"

    # 20 秒重整期内不再出门，避免 ATTACK/RETREAT 反复横跳。
    world.bot._manage_army()
    assert world.bot.attack_state == "GATHER"

    world.time_seconds = 530
    world.bot._manage_army()
    assert world.bot.attack_state == "ATTACK"


def test_opening_reissues_barracks_when_structure_never_appeared():
    world = FakeBotWorld(module)
    world.bot.supply_used = 19
    world.bot.minerals = 400
    world.bot._open_depot1 = True
    world.add_structure(UnitTypeId.SUPPLYDEPOT, (48, 50))
    world.bot._open_rax = True
    world.bot._open_rax_at = 100
    world.time_seconds = 130

    world.run(world.bot._manage_opening())

    # 标记被重置并重新下单，而不是整局以为兵营已经建好。
    assert [b[0] for b in world.builds] == [UnitTypeId.BARRACKS]
    assert world.builds[0][1]["random_alternative"] is True


def test_opening_reissues_barracks_when_construction_stuck_at_zero():
    world = FakeBotWorld(module)
    world.bot.supply_used = 19
    world.bot.minerals = 400
    world.bot._open_depot1 = True
    world.add_structure(UnitTypeId.SUPPLYDEPOT, (48, 50))
    world.bot._open_rax = True
    world.bot._open_rax_at = 100
    world.add_structure(
        UnitTypeId.BARRACKS, (44, 44), is_ready=False, build_progress=0.0
    )
    world.time_seconds = 140

    world.run(world.bot._manage_opening())

    assert [b[0] for b in world.builds] == [UnitTypeId.BARRACKS]


def test_opening_keeps_barracks_flag_when_construction_is_progressing():
    world = FakeBotWorld(module)
    world.bot.supply_used = 19
    world.bot.minerals = 400
    world.bot._open_depot1 = True
    world.add_structure(UnitTypeId.SUPPLYDEPOT, (48, 50))
    world.bot._open_rax = True
    world.bot._open_rax_at = 100
    world.add_structure(
        UnitTypeId.BARRACKS, (44, 44), is_ready=False, build_progress=0.4
    )
    world.time_seconds = 140

    world.run(world.bot._manage_opening())

    assert world.builds == []
    assert world.bot._open_rax is True


def test_structure_manager_ignores_zero_progress_barracks():
    world = FakeBotWorld(module)
    world.bot.minerals = 400
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.add_structure(
        UnitTypeId.BARRACKS, (44, 44), is_ready=False, build_progress=0.0
    )
    world.time_seconds = 200

    world.run(world.bot._manage_structures())

    assert [b[0] for b in world.builds] == [UnitTypeId.BARRACKS]


def test_structures_build_barracks_even_under_push_when_none_ready():
    world = FakeBotWorld(module)
    world.bot.minerals = 400
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot._threat_blocking = True
    world.time_seconds = 400

    world.run(world.bot._manage_structures())

    # 越挨打越要出兵营，不能因为威胁就把生产建筑停掉。
    assert [b[0] for b in world.builds] == [UnitTypeId.BARRACKS]


def test_army_resumes_attack_after_push_retreats():
    """正面推进撤走后，主力要能重新出门，而不是被防守状态永久锁住。"""
    world = FakeBotWorld(module)
    world.bot.calculate_supply_cost = lambda unit_type: 2.0
    world.bot._natural = Point2((62, 50))
    for index in range(10):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 60), is_idle=False)
    world.add_unit(UnitTypeId.MEDIVAC, (60, 61), is_idle=False)
    world.time_seconds = 420
    world.bot._prev_step_time = 415

    world.run(world.bot._manage_defense())
    world.bot._manage_army()
    assert world.bot.attack_state == "ATTACK"

    # 正面推进到家：全军回防。
    world.time_seconds = 430
    world.enemy_units = FakeUnits(
        [FakeUnit(UnitTypeId.MARINE, (60 + i, 52)) for i in range(10)]
    )
    world.bot.enemy_units = world.enemy_units
    world.run(world.bot._manage_defense())
    world.bot._manage_army()
    assert world.bot.attack_state == "GATHER"
    assert world.bot._defense_until == pytest.approx(438)

    # 推进撤走：防守窗口结束（不续期）后恢复推进。
    world.time_seconds = 445
    world.enemy_units = FakeUnits([])
    world.bot.enemy_units = world.enemy_units
    world.run(world.bot._manage_defense())
    world.bot._manage_army()
    assert world.bot.attack_state == "ATTACK"
    assert world.bot._defense_until == pytest.approx(438)


# ---------------------------------------------------------------------------
# 本轮复盘驱动（Sol 6.2 两场）：集结点离开分矿本体 / 防守姿态
# ---------------------------------------------------------------------------


def test_staging_point_moves_away_from_base_towards_enemy():
    base = Point2((62, 50))
    enemy_start = Point2((150, 150))

    point = module.staging_point(base, enemy_start, distance=10)

    assert point.distance_to(base) == pytest.approx(10, abs=0.1)
    assert point != base
    # 朝对手方向前出，且不超过基地到对手的一半。
    assert point.distance_to(enemy_start) < base.distance_to(enemy_start)
    assert point.distance_to(base) < base.distance_to(enemy_start) / 2
    # 距离为 0 或非法时退化为基地本身，不产生 NaN。
    assert module.staging_point(base, base, distance=10) == base


def test_rally_holds_army_away_from_natural_townhall():
    world = FakeBotWorld(module)
    world.time_seconds = 420
    world.bot._natural = Point2((62, 50))
    world.bot.townhalls = FakeUnits(
        [
            FakeUnit(UnitTypeId.COMMANDCENTER, (50, 50), tag=1),
            FakeUnit(UnitTypeId.ORBITALCOMMAND, (62, 50), tag=2),
        ]
    )
    for index in range(5):
        world.add_unit(UnitTypeId.MARINE, (62, 50), is_idle=True, tag=700 + index)
    army = world.bot._army_units()

    world.bot._rally(army)

    moves = [c for c in world.commands if c.kind == "move"]
    assert len(moves) == 5
    expected = module.staging_point(Point2((62, 50)), Point2((150, 150)), distance=10)
    for command in moves:
        assert command.target != Point2((62, 50))
        assert command.target.distance_to(expected) < 0.5


def test_defense_stims_bio_when_enemy_pushes():
    world = FakeBotWorld(module)
    world.time_seconds = 400
    world.bot.state.upgrades = {UpgradeId.STIMPACK}
    world.bot._natural = Point2((62, 50))
    world.bot._defense_until = 420
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    for index in range(6):
        world.add_unit(UnitTypeId.MARINE, (61 + index * 0.4, 52), is_idle=False)
    world.enemy_units = FakeUnits(
        [FakeUnit(UnitTypeId.MARINE, (64 + i, 52)) for i in range(8)]
    )
    world.bot.enemy_units = world.enemy_units

    world.bot._manage_army()

    stims = [
        c
        for c in world.commands
        if c.kind == "ability" and c.ability == AbilityId.EFFECT_STIM_MARINE
    ]
    assert stims, "防守时也应该打兴奋剂，而不是只在推进时打"


def test_defense_focuses_sieged_tank_over_closer_marine():
    world = FakeBotWorld(module)
    world.time_seconds = 400
    world.bot._natural = Point2((62, 50))
    world.bot._defense_until = 420
    world.bot._prev_step_time = 399
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    for index in range(4):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 52), is_idle=False)
    tank = FakeUnit(UnitTypeId.SIEGETANKSIEGED, (74, 52))
    close_marine = FakeUnit(UnitTypeId.MARINE, (61, 52))
    world.enemy_units = FakeUnits([tank, close_marine])
    world.bot.enemy_units = world.enemy_units

    world.bot._manage_army()

    attacks = [c for c in world.commands if c.kind == "attack"]
    assert attacks
    assert all(c.target is tank for c in attacks)


def test_defense_skips_flying_targets_for_ground_bio():
    world = FakeBotWorld(module)
    world.time_seconds = 400
    world.bot._natural = Point2((62, 50))
    world.bot._defense_until = 420
    world.bot._prev_step_time = 399
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    for index in range(4):
        world.add_unit(UnitTypeId.MARINE, (60 + index, 52), is_idle=False)
    world.enemy_units = FakeUnits(
        [FakeUnit(UnitTypeId.MEDIVAC, (64, 52), is_flying=True)]
    )
    world.bot.enemy_units = world.enemy_units

    world.bot._manage_army()

    # 陆战队打不到运输机：只移动，不把攻击指令下给打不到的目标。
    assert [c for c in world.commands if c.kind == "attack"] == []


def test_defense_holds_stim_until_enemies_are_close():
    world = FakeBotWorld(module)
    world.time_seconds = 400
    world.bot.state.upgrades = {UpgradeId.STIMPACK}
    world.bot._natural = Point2((62, 50))
    world.bot._defense_until = 420
    world.bot._prev_step_time = 399
    world.bot.calculate_supply_cost = lambda unit_type: 1.0
    for index in range(4):
        world.add_unit(UnitTypeId.MARINE, (60 + index * 0.3, 52), is_idle=False)
    world.enemy_units = FakeUnits([FakeUnit(UnitTypeId.MARINE, (95, 52))])
    world.bot.enemy_units = world.enemy_units

    world.bot._manage_army()

    # 敌人在半个屏幕外，先不扣血打兴奋剂。
    stims = [
        c
        for c in world.commands
        if c.kind == "ability" and c.ability == AbilityId.EFFECT_STIM_MARINE
    ]
    assert stims == []


# ---------------------------------------------------------------------------
# 本轮复盘驱动（Sol 6.2 两场）：人口上限不能卡在首战
# ---------------------------------------------------------------------------


def test_supply_headroom_follows_production_width():
    # 61/69、5 座出兵建筑：预留给下一波兵的人口，现在就该开始补站。
    assert module.supply_depot_needed(61, 69, 0, reserved_supply=10) is True
    # 旧行为（不预留）会一直等到 65/69 才动，首战就卡在 69。
    assert module.supply_depot_needed(61, 69, 0) is False


def test_supply_manager_prebuilds_when_production_is_wide():
    world = FakeBotWorld(module)
    world.time_seconds = 380
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot.supply_used = 61
    world.bot.supply_cap = 69
    world.bot.minerals = 400
    for index in range(5):
        world.add_structure(UnitTypeId.BARRACKS, (44 + index * 2, 44))

    world.run(world.bot._manage_supply())

    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]


def test_supply_manager_does_not_prebuild_without_production():
    world = FakeBotWorld(module)
    world.time_seconds = 380
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot.supply_used = 61
    world.bot.supply_cap = 69
    world.bot.minerals = 400

    world.run(world.bot._manage_supply())

    # 没有出兵建筑时不提前堆补给站，避免早期浪费。
    assert world.builds == []


def test_supply_manager_allows_third_depot_when_banked():
    world = FakeBotWorld(module)
    world.time_seconds = 380
    world.bot._opening_done = True
    world.bot._open_cc = True
    world.bot.supply_used = 73
    world.bot.supply_cap = 61
    world.bot.minerals = 500
    world.bot.already_pending = lambda unit_type: 2

    world.run(world.bot._manage_supply())
    assert [b[0] for b in world.builds] == [UnitTypeId.SUPPLYDEPOT]

    # 没存款时仍最多排两座。
    world_poor = FakeBotWorld(module)
    world_poor.time_seconds = 380
    world_poor.bot._opening_done = True
    world_poor.bot._open_cc = True
    world_poor.bot.supply_used = 73
    world_poor.bot.supply_cap = 61
    world_poor.bot.minerals = 150
    world_poor.bot.already_pending = lambda unit_type: 2

    world_poor.run(world_poor.bot._manage_supply())
    assert world_poor.builds == []
