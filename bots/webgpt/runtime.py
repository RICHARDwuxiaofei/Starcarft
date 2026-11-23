"""每帧统一预算与指令仲裁。

SC2 的观察是帧初快照。同一帧三个兵营都看到 50 矿，并不代表能造三只陆战队员。
这里按已接受指令扣除本地预算，下一帧再以游戏回报为准；不会修改游戏资源。
所有 Unit 方法只构造 UnitCommand，再由本模块统一提交。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI
    from sc2.ids.ability_id import AbilityId
    from sc2.ids.upgrade_id import UpgradeId
    from sc2.unit import Unit
    from sc2.unit_command import UnitCommand


@dataclass(slots=True)
class Budget:
    minerals: float
    gas: float
    supply: float
    held_minerals: float = 0.0
    held_gas: float = 0.0

    def can_pay(
        self, minerals: float, gas: float, supply: float = 0, *, urgent: bool = False
    ) -> bool:
        if min(minerals, gas, supply) < 0:
            raise ValueError("资源支出不能为负")
        free_minerals = max(0, self.minerals - (0 if urgent else self.held_minerals))
        free_gas = max(0, self.gas - (0 if urgent else self.held_gas))
        return free_minerals >= minerals and free_gas >= gas and self.supply >= supply

    def pay(self, minerals: float, gas: float, supply: float = 0, *, urgent: bool = False) -> bool:
        if not self.can_pay(minerals, gas, supply, urgent=urgent):
            return False
        self.minerals -= minerals
        self.gas -= gas
        self.supply -= supply
        return True


class CommandBus:
    """一个单位一帧一个主要命令；反应堆第二个生产槽必须显式排队。"""

    def __init__(self, bot: BotAI):
        self.bot = bot
        self.budget = Budget(0, 0, 0)
        self.issued: set[int] = set()
        self.planned: Counter = Counter()
        self.metrics: Counter = Counter()
        self._abilities: dict[int, set[AbilityId]] = {}
        self._last_command: dict[int, tuple[tuple, float]] = {}
        self._queue_count: Counter = Counter()

    def begin_frame(self):
        self.budget = Budget(self.bot.minerals, self.bot.vespene, max(0, self.bot.supply_left))
        self.issued.clear()
        self.planned.clear()
        self._abilities.clear()
        self._queue_count.clear()
        # 单位死亡后释放记忆；不读取任何敌方进程。
        live_tags = self.bot.all_own_units.tags
        self._last_command = {tag: v for tag, v in self._last_command.items() if tag in live_tags}

    async def prime(self, units):
        """批量查询，避免为每名陆战队员单独进行网络往返。"""
        missing = [u for u in units if u.tag not in self._abilities]
        if missing:
            answers = await self.bot.get_available_abilities(missing)
            for unit, abilities in zip(missing, answers, strict=True):
                self._abilities[unit.tag] = set(abilities)

    async def available(self, unit: Unit) -> set[AbilityId]:
        await self.prime([unit])
        return self._abilities[unit.tag]

    def has(self, unit: Unit, ability: AbilityId) -> bool:
        return ability in self._abilities.get(unit.tag, set())

    def can_afford(
        self, item: UnitTypeId | UpgradeId, *, urgent: bool = False, supply: float | None = None
    ) -> bool:
        cost = self.bot.calculate_cost(item)
        if supply is None:
            supply = (
                max(0, self.bot.calculate_supply_cost(item)) if isinstance(item, UnitTypeId) else 0
            )
        return self.budget.can_pay(cost.minerals, cost.vespene, supply, urgent=urgent)

    @staticmethod
    def _key(command: UnitCommand) -> tuple:
        target = command.target
        if isinstance(target, Point2):
            target = (round(target.x, 1), round(target.y, 1))
        elif target is not None:
            target = target.tag
        return (command.ability.value, target, command.queue)

    def issue(
        self,
        command: UnitCommand | bool,
        *,
        cost: UnitTypeId | UpgradeId | None = None,
        supply: float | None = None,
        allow_queue: bool = False,
        force: bool = False,
        urgent: bool = False,
    ) -> bool:
        """接受指令时一次性记账，避免后续经理覆盖；True 不代表服务器已执行成功。"""
        if isinstance(command, bool):
            # Unit.train 在该版本数据没有 creation_ability 时会返回 False。
            if command:
                raise RuntimeError("Bot 必须开启 unit_command_uses_self_do")
            return False
        tag = command.unit.tag
        queued_second = allow_queue and command.queue and self._queue_count[tag] < 2
        if tag in self.issued and not queued_second:
            self.metrics["conflicting_commands_prevented"] += 1
            return False
        key = self._key(command)
        previous = self._last_command.get(tag)
        if (
            cost is None
            and not force
            and previous is not None
            and previous[0] == key
            and self.bot.time - previous[1] < 0.65
        ):
            self.metrics["duplicate_commands_prevented"] += 1
            return False
        mineral_cost = gas_cost = 0.0
        supply_cost = 0.0 if supply is None else supply
        if cost is not None:
            price = self.bot.calculate_cost(cost)
            mineral_cost, gas_cost = price.minerals, price.vespene
            if supply is None and isinstance(cost, UnitTypeId):
                supply_cost = max(0, self.bot.calculate_supply_cost(cost))
        if not self.budget.can_pay(mineral_cost, gas_cost, supply_cost, urgent=urgent):
            self.metrics["overspend_prevented"] += 1
            return False
        # 不再让 BotAI.do 重复扣费：预算属于此总线，游戏快照始终保留原值。
        if not self.bot.do(command):
            return False
        self.budget.pay(mineral_cost, gas_cost, supply_cost, urgent=urgent)
        self.issued.add(tag)
        self._queue_count[tag] += 1
        self._last_command[tag] = (key, self.bot.time)
        if cost is not None:
            self.planned[cost] += 1
        self.metrics["commands_submitted"] += 1
        return True
