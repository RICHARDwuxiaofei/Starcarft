"""真实 burnysc2 对象的离线联动验证。

使用真实 protobuf、GameData、GameState、Unit/Units、UnitCommand、BotAI 帧准备。
只有服务器查询由 OfflineClient 替代；不启动 SC2、不访问用户电脑。
合成地图/单位数据用于接口验证，不能作为实际战斗、正式平衡数据或胜率证据。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from bots.webgpt.bot import CompetitiveBot, Telemetry
from s2clientprotocol import data_pb2, raw_pb2, sc2api_pb2
from sc2.data import Race, Result
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.game_data import GameData
from sc2.game_state import GameState
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.pixel_map import PixelMap
from sc2.position import Point2, Rect
from sc2.unit import Unit
from sc2.unit_command import UnitCommand

# 仅为测试构造一组自洽的花费/人口；不以此声称某补丁的正式数值。
SPECS = {
    U.SCV: (50, 0, 1),
    U.COMMANDCENTER: (400, 0, 0),
    U.ORBITALCOMMAND: (550, 0, 0),
    U.SUPPLYDEPOT: (100, 0, 0),
    U.BARRACKS: (150, 0, 0),
    U.REFINERY: (75, 0, 0),
    U.FACTORY: (150, 100, 0),
    U.STARPORT: (150, 100, 0),
    U.ENGINEERINGBAY: (125, 0, 0),
    U.ARMORY: (150, 100, 0),
    U.MISSILETURRET: (100, 0, 0),
    U.BUNKER: (100, 0, 0),
    U.BARRACKSTECHLAB: (50, 25, 0),
    U.BARRACKSREACTOR: (50, 50, 0),
    U.FACTORYTECHLAB: (50, 25, 0),
    U.FACTORYREACTOR: (50, 50, 0),
    U.STARPORTTECHLAB: (50, 25, 0),
    U.STARPORTREACTOR: (50, 50, 0),
    U.MARINE: (50, 0, 1),
    U.MARAUDER: (100, 25, 2),
    U.SIEGETANK: (150, 125, 3),
    U.SIEGETANKSIEGED: (150, 125, 3),
    U.MEDIVAC: (100, 100, 2),
    U.VIKINGFIGHTER: (125, 75, 2),
    U.RAVEN: (100, 150, 2),
    U.MINERALFIELD: (0, 0, 0),
    U.VESPENEGEYSER: (0, 0, 0),
    U.ZERGLING: (25, 0, 0.5),
}
STRUCTURES = {kind for kind, (_, _, supply) in SPECS.items() if supply == 0} - {
    U.MINERALFIELD,
    U.VESPENEGEYSER,
}


def data_fixture() -> GameData:
    response = sc2api_pb2.ResponseData()
    for ability in A:
        if ability.value:
            response.abilities.add(
                ability_id=ability.value,
                link_name=ability.name,
                friendly_name=ability.name,
                available=True,
            )
    creations = {}
    for producer in (U.SCV, U.COMMANDCENTER, U.BARRACKS, U.FACTORY, U.STARPORT):
        for kind, details in TRAIN_INFO[producer].items():
            creations[kind] = details["ability"]
    creations.update(
        {
            U.BARRACKSTECHLAB: A.BUILD_TECHLAB_BARRACKS,
            U.BARRACKSREACTOR: A.BUILD_REACTOR_BARRACKS,
            U.FACTORYTECHLAB: A.BUILD_TECHLAB_FACTORY,
            U.FACTORYREACTOR: A.BUILD_REACTOR_FACTORY,
            U.STARPORTTECHLAB: A.BUILD_TECHLAB_STARPORT,
            U.STARPORTREACTOR: A.BUILD_REACTOR_STARPORT,
            U.ZERGLING: A.LARVATRAIN_ZERGLING,
        }
    )
    for kind, (minerals, gas, supply) in SPECS.items():
        attributes = [data_pb2.Mechanical]
        if kind in STRUCTURES:
            attributes.append(data_pb2.Structure)
        if kind in {U.SCV, U.MARINE, U.MARAUDER, U.ZERGLING}:
            attributes.append(data_pb2.Biological)
        item = response.units.add(
            unit_id=kind.value,
            name=kind.name,
            available=True,
            mineral_cost=minerals,
            vespene_cost=gas,
            food_required=supply,
            race=Race.Terran.value,
            attributes=attributes,
            ability_id=creations[kind].value if kind in creations else 0,
            has_minerals=kind == U.MINERALFIELD,
            has_vespene=kind in {U.REFINERY, U.VESPENEGEYSER},
        )
        if kind == U.ORBITALCOMMAND:
            item.tech_alias.append(U.COMMANDCENTER.value)
        if kind in {
            U.MARINE,
            U.MARAUDER,
            U.ZERGLING,
            U.SCV,
            U.SIEGETANK,
            U.SIEGETANKSIEGED,
        }:
            item.weapons.add(
                type=3 if kind == U.MARINE else 1,
                damage=6,
                attacks=1,
                range=5 if kind == U.MARINE else 0.1,
                speed=1,
            )
    return GameData(response)


class OfflineClient:
    """替代查询 RPC；实际 BotAI 内部逻辑和指令仍执行。"""

    def __init__(self):
        self.game_step = None
        self.abilities = {U.COMMANDCENTER: [A.COMMANDCENTERTRAIN_SCV]}
        self.query_calls = 0
        self.placement_calls = 0

    async def query_available_abilities(self, units, ignore_resource_requirements=False):
        self.query_calls += 1
        return [self.abilities.get(unit.type_id, []) for unit in units]

    async def _query_building_placement_fast(self, ability, positions):
        self.placement_calls += 1
        return [True] * len(positions)

    async def query_pathing(self, start, end):
        return start.distance_to(end)


def image_fixture(value, *, bits=False):
    image = sc2api_pb2.ResponseGameInfo().start_raw.pathing_grid
    image.size.x = image.size.y = 128
    image.bits_per_pixel = 1 if bits else 8
    image.data = bytes([value]) * (128 * 128 // (8 if bits else 1))
    return image


def raw_unit(kind, tag, position=(16, 16), *, progress=1.0, enemy=False, neutral=False, **kwargs):
    unit = raw_pb2.Unit(
        tag=tag,
        unit_type=kind.value,
        display_type=raw_pb2.Visible,
        alliance=raw_pb2.Neutral if neutral else raw_pb2.Enemy if enemy else raw_pb2.Self,
        owner=0 if neutral else 2 if enemy else 1,
        build_progress=progress,
        health=45,
        health_max=45,
        shield=0,
        shield_max=0,
        cloak=raw_pb2.NotCloaked,
        radius=0.5,
        is_powered=True,
        **kwargs,
    )
    unit.pos.x, unit.pos.y = position
    return unit


def load_frame(bot, units, *, game_loop=0, workers=12, minerals=50, gas=0, army_supply=0, cap=15):
    response = sc2api_pb2.ResponseObservation()
    observation = response.observation
    observation.game_loop = game_loop
    observation.player_common.player_id = 1
    observation.player_common.minerals = minerals
    observation.player_common.vespene = gas
    observation.player_common.food_workers = workers
    observation.player_common.food_army = army_supply
    observation.player_common.food_used = workers + int(army_supply)
    observation.player_common.food_cap = cap
    observation.raw_data.units.extend(units)
    observation.raw_data.map_state.visibility.CopyFrom(image_fixture(2))
    observation.raw_data.map_state.creep.CopyFrom(image_fixture(0, bits=True))
    proto_info = sc2api_pb2.Response()
    proto_info.game_info.start_raw.pathing_grid.CopyFrom(image_fixture(255, bits=True))
    bot.actions.clear()
    bot.unit_tags_received_action.clear()
    bot._prepare_step(GameState(response), proto_info)


@pytest.fixture
def prepared_bot(tmp_path):
    bot = CompetitiveBot(seed=123, telemetry_path=tmp_path / "telemetry.jsonl")
    info = SimpleNamespace(
        player_races={1: Race.Terran.value, 2: Race.Terran.value},
        player_start_location=Point2((16, 16)),
        start_locations=[Point2((100, 100))],
        map_center=Point2((64, 64)),
        map_name="OFFLINE_SYNTHETIC_FIXTURE",
        map_ramps=[],
        playable_area=Rect((0, 0, 128, 128)),
        placement_grid=PixelMap(image_fixture(255, bits=True), in_bits=True),
    )
    bot._prepare_start(OfflineClient(), 1, info, data_fixture(), base_build=98310)
    bot._expansion_positions_list = [
        Point2((16, 16)),
        Point2((36, 36)),
        Point2((100, 100)),
    ]
    return bot


def opening_units(count=12):
    units = [raw_unit(U.COMMANDCENTER, 1, assigned_harvesters=count, ideal_harvesters=16)]
    units += [
        raw_unit(U.SCV, 10 + index, (18 + index % 3, 18 + index // 3)) for index in range(count)
    ]
    units += [
        raw_unit(
            U.MINERALFIELD,
            1000 + index,
            (22, 12 + index),
            neutral=True,
            mineral_contents=1500,
        )
        for index in range(8)
    ]
    return units


def records(bot):
    return [
        json.loads(line) for line in bot.telemetry.path.read_text(encoding="utf-8").splitlines()
    ]


def test_constructor_does_not_need_game_installation_or_spawn_client(monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("constructing a bot must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    bot = CompetitiveBot(seed=42)
    assert bot.config.seed == 42
    assert bot.plan is None
    assert bot.unit_command_uses_self_do
    assert not hasattr(bot, "client")


def test_full_opening_step_runs_real_unit_commands_and_workers(prepared_bot):
    bot = prepared_bot
    load_frame(bot, opening_units())
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    assert bot.client.game_step == bot.config.game_step
    assert bot.match_error is None
    assert bot._step_count == 1
    assert len(bot.workers) == 12
    assert all(isinstance(unit, Unit) for unit in bot.workers)
    assert all(isinstance(action, UnitCommand) for action in bot.actions)
    assert any(action.ability == A.COMMANDCENTERTRAIN_SCV for action in bot.actions)
    assert any(action.ability == A.HARVEST_GATHER for action in bot.actions)
    assert bot.bus.budget.minerals == 0
    assert bot.minerals == 50
    assert [record["event"] for record in records(bot)] == ["start", "snapshot"]


def test_second_frame_uses_actual_cache_and_does_not_duplicate_worker_orders(
    prepared_bot,
):
    bot = prepared_bot
    units = opening_units()
    load_frame(bot, units)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    for unit in units:
        if unit.unit_type == U.SCV.value:
            unit.orders.add(ability_id=A.HARVEST_GATHER.value, target_unit_tag=1000 + unit.tag % 8)
    units[0].orders.add(ability_id=A.COMMANDCENTERTRAIN_SCV.value, progress=0.1)
    load_frame(bot, units, game_loop=16, minerals=0)
    asyncio.run(bot.on_step(1))
    assert bot.match_error is None
    assert not bot.actions
    assert bot.already_pending(U.SCV) == 1


def test_supply_construction_and_mining_do_not_overwrite_same_worker(prepared_bot):
    bot = prepared_bot
    load_frame(bot, opening_units(14), workers=14, minerals=150)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    build = [action for action in bot.actions if action.ability == A.TERRANBUILD_SUPPLYDEPOT]
    assert len(build) == 1
    assert sum(action.unit.tag == build[0].unit.tag for action in bot.actions) == 1
    assert bot.bus.budget.minerals == 0
    assert bot.match_error is None


def test_interrupted_building_is_resumed_without_double_counting_pending(prepared_bot):
    bot = prepared_bot
    units = opening_units()
    units.append(raw_unit(U.SUPPLYDEPOT, 500, (22, 23), progress=0.4))
    load_frame(bot, units, minerals=0)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    resumed = [
        action
        for action in bot.actions
        if action.ability == A.SMART and getattr(action.target, "tag", None) == 500
    ]
    assert len(resumed) == 1
    assert bot.macro.count(bot, bot.bus, U.SUPPLYDEPOT) == 1
    assert bot.match_error is None


def test_no_remaining_units_has_no_phantom_actions(prepared_bot):
    bot = prepared_bot
    load_frame(bot, [], workers=0, minerals=0, cap=0)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    assert not bot.actions
    assert not bot.match_error


def test_last_worker_spends_last_400_minerals_on_rebuilding_command_center(
    prepared_bot,
):
    bot = prepared_bot
    load_frame(bot, [raw_unit(U.SCV, 10, (18, 18))], workers=1, minerals=400, cap=0)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    assert len(bot.actions) == 1
    assert bot.actions[0].ability == A.TERRANBUILD_COMMANDCENTER
    assert bot.actions[0].unit.tag == 10
    assert bot.bus.budget.minerals == 0
    assert bot.bus.planned[U.COMMANDCENTER] == 1
    assert bot.match_error is None


def test_real_enemy_observation_reaches_combat_and_returns_unit_command(prepared_bot):
    bot = prepared_bot
    own = raw_unit(U.MARINE, 50, (50, 50))
    enemy = raw_unit(U.ZERGLING, 60, (53, 50), enemy=True)
    load_frame(bot, [own, enemy], workers=0, minerals=0, army_supply=1, cap=15)
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    attacks = [action for action in bot.actions if action.ability == A.ATTACK]
    assert len(attacks) == 1
    assert attacks[0].target.tag == 60
    assert 60 in bot.intelligence.contacts
    assert bot.match_error is None


def test_on_start_failure_is_visible_to_runner_and_telemetry(prepared_bot):
    bot = prepared_bot
    load_frame(bot, opening_units())
    bot.race = Race.Protoss
    with pytest.raises(ValueError, match="Race.Terran"):
        asyncio.run(bot.on_start())
    assert bot.match_error.startswith("start: ValueError")
    assert records(bot)[-1]["event"] == "error"


def test_failed_rpc_is_recorded_and_not_swallowed(prepared_bot):
    bot = prepared_bot
    load_frame(bot, opening_units())
    asyncio.run(bot.on_start())

    async def broken(*args, **kwargs):
        raise ConnectionError("offline injected disconnect")

    bot.client.query_available_abilities = broken
    with pytest.raises(ConnectionError, match="injected"):
        asyncio.run(bot.on_step(0))
    assert "step: ConnectionError" in bot.match_error
    assert records(bot)[-1]["event"] == "error"


def test_official_result_and_destruction_callback_are_recorded(prepared_bot):
    bot = prepared_bot
    load_frame(bot, opening_units())
    asyncio.run(bot.on_start())
    asyncio.run(bot.on_step(0))
    bot.workers_manager.jobs[9999] = 1000
    bot.macro.blocked_addons[9999] = 1.0
    asyncio.run(bot.on_unit_destroyed(9999))
    assert 9999 not in bot.workers_manager.jobs
    assert 9999 not in bot.macro.blocked_addons
    asyncio.run(bot.on_end(Result.Victory))
    assert bot.game_result == "Victory"
    assert records(bot)[-1]["result"] == "Victory"
    assert records(bot)[-1]["match_error"] is None


def test_telemetry_io_failure_disables_logging_without_breaking_bot(tmp_path):
    telemetry = Telemetry(tmp_path)
    telemetry.write("snapshot", 12.0, minerals=50)
    assert telemetry.error
    telemetry.write("snapshot", 13.0, minerals=100)
