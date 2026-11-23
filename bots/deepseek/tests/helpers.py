"""离线测试用的轻量假对象。

不启动 SC2、不依赖客户端，只用鸭子类型模拟 BotAI 在决策时需要的最小接口。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"


def load_bot_module():
    """按文件路径加载 bot.py，避免依赖 bots 包结构。"""
    if "deepseek_bot_under_test" in sys.modules:
        return sys.modules["deepseek_bot_under_test"]
    spec = importlib.util.spec_from_file_location("deepseek_bot_under_test", BOT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deepseek_bot_under_test"] = module
    spec.loader.exec_module(module)
    return module


class FakeCommand:
    """记录机器人发出的指令，供断言检查。"""

    def __init__(self, kind: str, unit, target=None, ability=None):
        self.kind = kind
        self.unit = unit
        self.target = target
        self.ability = ability

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FakeCommand({self.kind}, {self.unit.tag}, {self.target})"


class FakeUnit:
    def __init__(
        self,
        type_id: UnitTypeId,
        position=(50, 50),
        tag: int = 1,
        *,
        is_ready: bool = True,
        is_idle: bool = True,
        is_flying: bool = False,
        can_attack: bool = True,
        health: float = 100,
        shield: float = 0,
        assigned_harvesters: int = 0,
        ideal_harvesters: int = 16,
        energy: float = 0,
        cargo_used: int = 0,
        add_on_tag: int = 0,
    ):
        self.type_id = type_id
        self.position = Point2(position)
        self.tag = tag
        self.is_ready = is_ready
        self.is_idle = is_idle
        self.is_flying = is_flying
        self.can_attack = can_attack
        self.health = health
        self.shield = shield
        self.assigned_harvesters = assigned_harvesters
        self.ideal_harvesters = ideal_harvesters
        self.energy = energy
        self.cargo_used = cargo_used
        self.add_on_tag = add_on_tag
        self.order_target = None

    def distance_to(self, other) -> float:
        target = other.position if hasattr(other, "position") else other
        return self.position.distance_to(target)

    def gather(self, target):
        self.order_target = getattr(target, "tag", None)
        return FakeCommand("gather", self, target)

    def move(self, target):
        return FakeCommand("move", self, target)

    def attack(self, target):
        return FakeCommand("attack", self, target)

    def has_buff(self, _buff) -> bool:
        return False

    def __call__(self, ability, target=None):
        return FakeCommand("ability", self, target, ability)


class FakeUnits:
    def __init__(self, units=()):
        self._units = list(units)

    def __iter__(self):
        return iter(self._units)

    def __len__(self) -> int:
        return len(self._units)

    def __bool__(self) -> bool:
        return bool(self._units)

    def __getitem__(self, item):
        return self._units[item]

    @property
    def amount(self) -> int:
        return len(self._units)

    @property
    def first(self):
        return self._units[0]

    @property
    def ready(self):
        return FakeUnits([u for u in self._units if u.is_ready])

    @property
    def idle(self):
        return FakeUnits([u for u in self._units if u.is_idle])

    @property
    def center(self):
        if not self._units:
            return Point2((0, 0))
        x = sum(u.position.x for u in self._units) / len(self._units)
        y = sum(u.position.y for u in self._units) / len(self._units)
        return Point2((x, y))

    def filter(self, predicate):
        return FakeUnits([u for u in self._units if predicate(u)])

    def closest_to(self, position):
        if not self._units:
            raise ValueError("empty units")
        target = position.position if hasattr(position, "position") else position
        return min(self._units, key=lambda u: u.position.distance_to(target))

    def sorted_by_distance_to(self, position):
        target = position.position if hasattr(position, "position") else position
        return FakeUnits(
            sorted(self._units, key=lambda u: u.position.distance_to(target))
        )

    def __call__(self, unit_types):
        if isinstance(unit_types, UnitTypeId):
            unit_types = {unit_types}
        return FakeUnits([u for u in self._units if u.type_id in unit_types])


class FakeBotWorld:
    """把 CompetitiveBot 实例装配成一个可控的假战场。"""

    def __init__(self, module):
        self.module = module
        self.bot = module.CompetitiveBot()
        self.commands: list[FakeCommand] = []
        self.builds: list[tuple[UnitTypeId, dict]] = []
        self.expansions: list[object] = []
        self.structures_by_type: dict[UnitTypeId, list[FakeUnit]] = defaultdict(list)
        self.units_by_type: dict[UnitTypeId, list[FakeUnit]] = defaultdict(list)
        self.enemy_units = FakeUnits([])
        self.enemy_structures = FakeUnits([])
        self.mineral_fields = FakeUnits([])
        self.geysers = FakeUnits([])
        self._configure()
        self.time_seconds = 0.0

    # ---- 装配 -----------------------------------------------------------

    def _configure(self) -> None:
        bot = self.bot
        bot.state = SimpleNamespace(game_loop=0, upgrades=set())
        bot.client = SimpleNamespace(game_step=2)
        bot.race = Race.Terran
        bot.enemy_race = Race.Zerg
        bot.game_info = SimpleNamespace(
            map_name="RedCityLE",
            player_start_location=Point2((50, 50)),
            start_locations=[Point2((150, 150))],
        )
        # BotAI 的扩张点来自私有字段，直接注入以模拟 RedCityLE 的四个矿点。
        bot._expansion_positions_list = [
            Point2((50, 50)),
            Point2((62, 50)),
            Point2((74, 50)),
            Point2((86, 50)),
        ]
        bot.minerals = 200
        bot.vespene = 100
        bot.supply_used = 14
        bot.supply_cap = 15
        bot.supply_left = 1

        bot.townhalls = FakeUnits([FakeUnit(UnitTypeId.COMMANDCENTER, (50, 50), tag=1)])
        bot.workers = FakeUnits([FakeUnit(UnitTypeId.SCV, (50, 50), tag=10)])
        bot.mineral_field = self.mineral_fields
        bot.vespene_geyser = self.geysers
        bot.gas_buildings = FakeUnits([])
        bot.enemy_units = self.enemy_units
        bot.enemy_structures = self.enemy_structures

        bot.structures = lambda unit_type=None: self._structures(unit_type)
        bot.units = lambda unit_type=None: self._units(unit_type)
        bot.train = lambda *args, **kwargs: 1
        bot.research = lambda *args, **kwargs: False
        bot.can_afford = lambda *args, **kwargs: True
        bot.already_pending = lambda *args, **kwargs: 0
        bot.already_pending_upgrade = lambda *args, **kwargs: 0
        bot.calculate_supply_cost = lambda unit_type: 1.0
        bot.do = self._do
        bot.build = self._build
        bot.expand_now = self._expand_now

    @property
    def time_seconds(self) -> float:
        return self._time_seconds

    @time_seconds.setter
    def time_seconds(self, value: float) -> None:
        self._time_seconds = value
        self.bot.state.game_loop = int(value * 22.4)

    @staticmethod
    def _types(unit_type):
        if isinstance(unit_type, UnitTypeId):
            return {unit_type}
        return set(unit_type)

    def _structures(self, unit_type=None):
        if unit_type is None:
            everything = [
                u for units in self.structures_by_type.values() for u in units
            ]
            return FakeUnits(everything)
        wanted = self._types(unit_type)
        return FakeUnits(
            [
                u
                for t, units in self.structures_by_type.items()
                if t in wanted
                for u in units
            ]
        )

    def _units(self, unit_type=None):
        if unit_type is None:
            everything = [u for units in self.units_by_type.values() for u in units]
            return FakeUnits(everything)
        wanted = self._types(unit_type)
        return FakeUnits(
            [u for t, units in self.units_by_type.items() if t in wanted for u in units]
        )

    def _do(self, command):
        self.commands.append(command)
        return True

    async def _build(self, building, near=None, **kwargs):
        self.builds.append((building, {"near": near, **kwargs}))
        return True

    async def _expand_now(self, location=None, **kwargs):
        self.expansions.append(location)

    # ---- 场景辅助 -------------------------------------------------------

    def add_structure(self, unit_type, position=(50, 50), **kwargs):
        tag = kwargs.pop("tag", len(self.commands) + 1000)
        unit = FakeUnit(unit_type, position, tag=tag, **kwargs)
        self.structures_by_type[unit_type].append(unit)
        return unit

    def add_unit(self, unit_type, position=(50, 50), **kwargs):
        tag = kwargs.pop("tag", len(self.commands) + 2000)
        unit = FakeUnit(unit_type, position, tag=tag, **kwargs)
        self.units_by_type[unit_type].append(unit)
        return unit

    def add_mineral_field(self, position=(52, 50), **kwargs):
        field = FakeUnit(UnitTypeId.MINERALFIELD, position, **kwargs)
        self.mineral_fields = FakeUnits(list(self.mineral_fields) + [field])
        self.bot.mineral_field = self.mineral_fields
        return field

    def run(self, coroutine):
        return asyncio.run(coroutine)
