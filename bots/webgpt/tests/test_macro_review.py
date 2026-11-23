"""代码审查找到的运营停滞场景；只构造观察/API，绝不启动游戏。"""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bots.webgpt.config import BotConfig
from bots.webgpt.macro import MacroManager
from bots.webgpt.model import BattlePlan, IntelReport, Stance
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.units import Units


def empty_world():
    bot = SimpleNamespace(time=10.0, start_location=Point2((20, 20)))
    bot.structures = Units([], bot)
    bot.townhalls = Units([], bot)
    bot.workers = Units([], bot)
    bot.all_own_units = Units([], bot)
    bot.already_pending = lambda kind: 0
    return bot


def order():
    point = Point2((20, 20))
    return BattlePlan(Stance.HOLD, point, point, point)


def test_blocked_wall_depot_eventually_falls_back_to_other_placement(monkeypatch):
    bot = empty_world()
    bot.supply_cap = 23
    bot.supply_used = 23
    bus = SimpleNamespace(budget=SimpleNamespace(supply=0), planned=Counter())
    manager = MacroManager(BotConfig(), workers=None)
    wall = Point2((25, 25))
    manager.wall_depots = [wall]
    tried = []

    async def build(bot, bus, kind, near=None, **kwargs):
        assert kind == U.SUPPLYDEPOT
        tried.append(near)
        return near != wall

    monkeypatch.setattr(manager, "_build", build)
    # 首选墙位被阻塞后，至少下一运营周期应该尝试别的位置。
    for _ in range(2):
        asyncio.run(manager._supply(bot, bus))
        bot.time += 2
    assert any(point != wall for point in tried), "不能在同一不可用墙位永久卡人口"


@pytest.mark.parametrize("worker_limit", [12, 13])
def test_worker_cap_below_fourteen_does_not_block_first_depot(monkeypatch, worker_limit):
    bot = empty_world()
    bot.supply_cap = 15
    bot.supply_used = worker_limit
    bus = SimpleNamespace(budget=SimpleNamespace(supply=15 - worker_limit), planned=Counter())
    manager = MacroManager(BotConfig(max_workers=worker_limit), workers=None)
    build = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_build", build)
    asyncio.run(manager._supply(bot, bus))
    build.assert_awaited_once()
    assert build.call_args.args[2] == U.SUPPLYDEPOT


def test_one_surviving_worker_can_rebuild_lost_last_base(monkeypatch):
    bot = empty_world()
    bot.workers = Units([SimpleNamespace(tag=1)], bot)
    manager = MacroManager(BotConfig(), workers=SimpleNamespace())
    target = Point2((40, 40))
    build = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "base_count", lambda bot, bus: 0)
    monkeypatch.setattr(manager, "_next_expansion", AsyncMock(return_value=target))
    monkeypatch.setattr(manager, "_build", build)
    asyncio.run(manager._expansion(bot, SimpleNamespace(), order(), IntelReport()))
    build.assert_awaited_once()
    assert build.call_args.args[2] == U.COMMANDCENTER
    assert build.call_args.kwargs["near"] == target


class ProductionBuilding:
    tag = 100
    type_id = U.BARRACKS
    is_ready = True
    is_idle = True
    add_on_tag = 0
    position = Point2((20, 20))

    def build(self, kind):
        return SimpleNamespace(unit=self, ability=A.BUILD_TECHLAB_BARRACKS)

    def __call__(self, ability, target=None):
        return SimpleNamespace(unit=self, ability=ability, target=target)


def test_available_addon_ability_does_not_hide_blocked_placement():
    bot = empty_world()
    barracks = ProductionBuilding()
    bot.structures = Units([barracks], bot)
    bot.game_data = SimpleNamespace(
        units={
            U.BARRACKSTECHLAB.value: SimpleNamespace(
                creation_ability=SimpleNamespace(
                    id=A.BUILD_TECHLAB_BARRACKS,
                    exact_id=A.BUILD_TECHLAB_BARRACKS,
                )
            ),
        }
    )
    bot.can_place_single = AsyncMock(return_value=False)
    commands = []
    bus = SimpleNamespace(
        issued=set(),
        can_afford=lambda kind: True,
        has=lambda unit, ability: True,
        issue=lambda command, **kwargs: commands.append(command) or True,
    )
    manager = MacroManager(BotConfig(), workers=None)
    manager.blocked_addons[barracks.tag] = 0.0
    asyncio.run(manager._addons(bot, bus, order()))
    assert any(command.ability == A.LIFT_BARRACKS for command in commands), (
        "技能可用不代表落点可用；被堵超过8秒应能进入搬迁恢复"
    )
    assert not any(command.ability == A.BUILD_TECHLAB_BARRACKS for command in commands)
