"""复盘改动的真实库联动验证；输入为合成观察，不是重新跑过三场录像。

沿用既有离线 RPC fixture，以真实 Observation / GameState / ScoreDetails
驱动 CompetitiveBot.on_step，再检查真实 UnitCommand 与自己的遥测。
此处的计分和部队位置是针对风险构造的场景，不声称还原实际交战轨迹。
"""

from __future__ import annotations

import asyncio

from bots.webgpt.model import Stance
from bots.webgpt.tests.test_bot_integration import (
    image_fixture,
    raw_unit,
    records,
)
from bots.webgpt.tests.test_bot_integration import (
    prepared_bot as prepared_bot,
)
from s2clientprotocol import sc2api_pb2, score_pb2
from sc2.game_state import GameState
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit_command import UnitCommand


def army(*, gathered=0, replacements=0):
    """五十枪兵加一坦克，替换 tag 可以模拟损失被新兵补齐的观察。"""
    units = [
        raw_unit(U.COMMANDCENTER, 1, (16, 16)),
        raw_unit(U.COMMANDCENTER, 2, (100, 100), enemy=True),
    ]
    for index in range(50):
        base = 18 if index < gathered else 65
        tag = 100 + index + (1000 if index < replacements else 0)
        units.append(raw_unit(U.MARINE, tag, (base + index % 3 * 0.2, base)))
    tank_position = (20, 20) if gathered == 50 else (65, 65)
    units.append(raw_unit(U.SIEGETANKSIEGED, 500, tank_position))
    return units


def frame(bot, units, seconds, **totals):
    """把累计分数放进正常协议观察；不替换策略、战斗或 ScoreDetails。"""
    response = sc2api_pb2.ResponseObservation()
    observation = response.observation
    observation.game_loop = round(seconds * 22.4)
    observation.player_common.player_id = 1
    observation.player_common.food_army = 53
    observation.player_common.food_used = 53
    observation.player_common.food_cap = 200
    observation.raw_data.units.extend(units)
    observation.raw_data.map_state.visibility.CopyFrom(image_fixture(2))
    observation.raw_data.map_state.creep.CopyFrom(image_fixture(0, bits=True))
    observation.score.score_type = score_pb2.Score.Melee
    details = observation.score.score_details
    details.lost_minerals.army = 0
    details.killed_minerals.army = 0
    for name, value in totals.items():
        prefix, resource, category = name.split("_", 2)
        setattr(getattr(details, f"{prefix}_{resource}"), category, value)
    proto_info = sc2api_pb2.Response()
    proto_info.game_info.start_raw.pathing_grid.CopyFrom(image_fixture(255, bits=True))
    bot.actions.clear()
    bot.unit_tags_received_action.clear()
    bot._prepare_step(GameState(response), proto_info)


def begin(bot):
    bot.client.abilities[U.SIEGETANKSIEGED] = [A.UNSIEGE_UNSIEGE]
    frame(bot, army(), 500)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    assert bot.plan.stance == Stance.ATTACK


def suffer_bad_trade(bot):
    frame(bot, army(replacements=14), 510, lost_minerals_army=700)
    asyncio.run(bot.on_step(1))
    assert bot.plan.stance == Stance.RETREAT


def test_score_loss_with_replacement_supply_reaches_retreat_commands_and_telemetry(prepared_bot):
    bot = prepared_bot
    begin(bot)
    initial_supply = bot.supply_army
    suffer_bad_trade(bot)

    assert bot.supply_army == initial_supply == 53
    assert not bot.match_error
    assert all(isinstance(action, UnitCommand) for action in bot.actions)
    marine_actions = [action for action in bot.actions if action.unit.type_id == U.MARINE]
    assert len(marine_actions) == 50
    assert all(action.ability == A.MOVE_MOVE for action in marine_actions)
    assert all(action.target == bot.plan.fallback for action in marine_actions)
    assert any(
        action.unit.tag == 500 and action.ability == A.UNSIEGE_UNSIEGE for action in bot.actions
    )
    assert len({action.unit.tag for action in bot.actions}) == len(bot.actions)

    retreat = next(
        record
        for record in records(bot)
        if record["event"] == "stance" and record["stance"] == "retreat"
    )
    diagnostic = retreat["engagement"]
    assert diagnostic["trade_available"]
    assert diagnostic["army_loss"] == 700
    assert diagnostic["enemy_gain"] == 0
    assert diagnostic["bad_trade"]
    assert diagnostic["bad_trade_retreats"] == 1
    assert diagnostic["regroup_active"]
    assert diagnostic["regroup_point"] == list(bot.plan.fallback)
    assert diagnostic["last_bad_trade"]["army_loss"] == 700
    snapshot = [record for record in records(bot) if record["event"] == "snapshot"][-1]
    assert snapshot["engagement"]["bad_trade_retreats"] == 1
    assert snapshot["engagement"]["regroup_fraction"] == 0


def test_real_economy_and_technology_kill_credit_preserves_profitable_push(prepared_bot):
    bot = prepared_bot
    begin(bot)
    frame(
        bot,
        army(replacements=14),
        510,
        lost_minerals_army=700,
        killed_minerals_economy=350,
        killed_minerals_technology=350,
    )
    asyncio.run(bot.on_step(1))

    assert bot.plan.stance == Stance.ATTACK
    assert bot.strategist.trade_report.loss == 700
    assert bot.strategist.trade_report.gain == 700
    assert bot.strategist.diagnostics()["bad_trade_retreats"] == 0
    marine_actions = [action for action in bot.actions if action.unit.type_id == U.MARINE]
    assert marine_actions
    assert all(action.ability == A.ATTACK for action in marine_actions)
    assert all(action.target == bot.plan.objective for action in marine_actions)


def test_elapsed_time_does_not_bypass_regroup_and_old_losses_do_not_retrigger(prepared_bot):
    bot = prepared_bot
    begin(bot)
    suffer_bad_trade(bot)
    locked_point = bot.plan.fallback

    # 旧实现的24秒超时已经过去，但主力仍在远方；仅等时间不能重新出门。
    frame(bot, army(replacements=14), 540, lost_minerals_army=700)
    asyncio.run(bot.on_step(2))
    assert bot.plan.stance == Stance.RETREAT
    assert bot.plan.fallback == locked_point
    assert bot.strategist.diagnostics()["regroup_active"]

    frame(bot, army(gathered=50, replacements=14), 550, lost_minerals_army=700)
    asyncio.run(bot.on_step(3))
    assert bot.plan.stance == Stance.ATTACK
    assert not bot.strategist.diagnostics()["regroup_active"]

    frame(bot, army(gathered=50, replacements=14), 551, lost_minerals_army=700)
    asyncio.run(bot.on_step(4))
    assert bot.plan.stance == Stance.ATTACK
    assert bot.strategist.diagnostics()["bad_trade_retreats"] == 1
    assert bot.strategist.trade_report.loss == 0


def test_regroup_keeps_minimum_duration_even_when_army_is_already_home(prepared_bot):
    bot = prepared_bot
    begin(bot)
    suffer_bad_trade(bot)
    frame(bot, army(gathered=50, replacements=14), 515, lost_minerals_army=700)
    asyncio.run(bot.on_step(2))
    assert bot.plan.stance == Stance.RETREAT
    assert bot.strategist.diagnostics()["regroup_fraction"] == 1

    frame(bot, army(gathered=50, replacements=14), 523, lost_minerals_army=700)
    asyncio.run(bot.on_step(3))
    assert bot.plan.stance == Stance.ATTACK


def test_home_defense_has_priority_then_returns_to_unfinished_regroup(prepared_bot):
    bot = prepared_bot
    begin(bot)
    suffer_bad_trade(bot)
    attackers = [
        raw_unit(U.MARINE, 900 + index, (18 + index, 18), enemy=True) for index in range(3)
    ]
    frame(bot, army(replacements=14) + attackers, 515, lost_minerals_army=700)
    asyncio.run(bot.on_step(2))
    assert bot.plan.stance == Stance.DEFEND
    assert bot.strategist.diagnostics()["regroup_active"]

    frame(bot, army(replacements=14), 516, lost_minerals_army=700)
    asyncio.run(bot.on_step(3))
    assert bot.plan.stance == Stance.DEFEND

    frame(bot, army(replacements=14), 523, lost_minerals_army=700)
    asyncio.run(bot.on_step(4))
    assert bot.plan.stance == Stance.RETREAT
    assert bot.strategist.diagnostics()["regroup_active"]


def test_new_base_does_not_move_locked_regroup_destination(prepared_bot):
    bot = prepared_bot
    begin(bot)
    suffer_bad_trade(bot)
    locked_point = bot.plan.fallback
    assert locked_point == Point2((16, 16))

    # 新矿比旧主矿更远离目标；若每帧重算最安全基地，fallback本会跳动。
    units = army(replacements=14) + [raw_unit(U.COMMANDCENTER, 3, (5, 5))]
    frame(bot, units, 520, lost_minerals_army=700)
    asyncio.run(bot.on_step(2))
    assert bot.plan.stance == Stance.RETREAT
    assert bot.plan.fallback == locked_point
    assert bot.strategist.diagnostics()["regroup_point"] == list(locked_point)
