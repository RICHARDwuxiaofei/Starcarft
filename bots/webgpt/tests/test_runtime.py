"""验证真实 UnitCommand 与预算的一致性；不连接 SC2。"""

import asyncio
from types import SimpleNamespace

import pytest
from bots.webgpt.runtime import Budget, CommandBus
from s2clientprotocol import raw_pb2
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit


class FakeBot:
    unit_command_uses_self_do = True

    def __init__(self, minerals=100, gas=0, supply=10):
        self.minerals, self.vespene, self.supply_left = minerals, gas, supply
        self.time = 0.0
        self.all_own_units = SimpleNamespace(tags={1, 2, 3})
        self.state = SimpleNamespace(game_loop=0)
        self.actions = []
        self.query_count = 0
        self.accept = True

    def calculate_cost(self, item):
        return SimpleNamespace(minerals=50, vespene=0)

    def calculate_supply_cost(self, item):
        return 1

    def do(self, command):
        if self.accept:
            self.actions.append(command)
        return self.accept

    async def get_available_abilities(self, units):
        self.query_count += 1
        return [[A.MOVE_MOVE, A.ATTACK_ATTACK] for _ in units]


def own_unit(bot, tag=1):
    return Unit(
        raw_pb2.Unit(
            tag=tag, unit_type=U.BARRACKS.value, owner=1, build_progress=1, display_type=1
        ),
        bot,
    )


def test_budget_respects_exact_boundaries_and_never_overdraws():
    budget = Budget(100, 25, 2)
    assert not budget.pay(101, 0)
    assert not budget.pay(50, 26)
    assert not budget.pay(50, 0, 3)
    assert budget.pay(100, 25, 2)
    assert (budget.minerals, budget.gas, budget.supply) == (0, 0, 0)
    assert not budget.pay(1, 0)


def test_holds_preserve_expansion_but_do_not_block_free_micro():
    budget = Budget(400, 0, 10, held_minerals=400)
    assert not budget.pay(50, 0)
    assert budget.pay(50, 0, 1, urgent=True)
    assert budget.pay(0, 0)
    assert not budget.pay(1, 0)


@pytest.mark.parametrize("cost", [(-1, 0, 0), (0, -1, 0), (0, 0, -1)])
def test_negative_cost_is_not_a_resource_refund(cost):
    with pytest.raises(ValueError):
        Budget(100, 100, 10).pay(*cost)


def test_simultaneous_buildings_cannot_spend_the_same_fifty_minerals():
    bot = FakeBot(minerals=50)
    bus = CommandBus(bot)
    bus.begin_frame()
    assert bus.issue(own_unit(bot, 1)(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert not bus.issue(own_unit(bot, 2)(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert len(bot.actions) == 1
    assert bus.planned[U.MARINE] == 1
    assert bus.budget.supply == 9
    assert bot.minerals == 50  # 观察快照未被擅自改写。


def test_supply_is_reserved_for_the_first_train_in_this_frame():
    bot = FakeBot(supply=1)
    bus = CommandBus(bot)
    bus.begin_frame()
    assert bus.issue(own_unit(bot, 1)(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert not bus.issue(own_unit(bot, 2)(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)


def test_secondary_move_does_not_overwrite_heal_or_morph():
    bot = FakeBot()
    bus = CommandBus(bot)
    bus.begin_frame()
    unit = own_unit(bot)
    assert bus.issue(unit(A.SIEGEMODE_SIEGEMODE))
    assert not bus.issue(unit.move(Point2((20, 20))))
    assert len(bot.actions) == 1


def test_reactor_can_explicitly_queue_second_train_but_never_third():
    bot = FakeBot(minerals=200)
    bus = CommandBus(bot)
    bus.begin_frame()
    unit = own_unit(bot)
    assert bus.issue(unit(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert bus.issue(unit(A.BARRACKSTRAIN_MARINE, queue=True), cost=U.MARINE, allow_queue=True)
    assert not bus.issue(unit(A.BARRACKSTRAIN_MARINE, queue=True), cost=U.MARINE, allow_queue=True)
    assert bus.budget.minerals == 100
    assert bus.planned[U.MARINE] == 2
    assert bot.actions[1].queue


def test_allow_queue_does_not_override_without_actual_queue_bit():
    bot = FakeBot()
    bus = CommandBus(bot)
    bus.begin_frame()
    unit = own_unit(bot)
    assert bus.issue(unit(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert not bus.issue(unit(A.BARRACKSTRAIN_MARINE), cost=U.MARINE, allow_queue=True)


def test_ability_queries_are_batched_and_cached_within_frame():
    bot = FakeBot()
    bus = CommandBus(bot)
    bus.begin_frame()
    unit1, unit2 = own_unit(bot, 1), own_unit(bot, 2)
    asyncio.run(bus.prime([unit1, unit2]))
    assert A.MOVE_MOVE in asyncio.run(bus.available(unit1))
    assert bus.has(unit2, A.ATTACK_ATTACK)
    assert bot.query_count == 1
    bus.begin_frame()
    assert not bus.has(unit2, A.ATTACK_ATTACK)
    asyncio.run(bus.prime([unit1]))
    assert bot.query_count == 2


def test_duplicate_move_throttle_eventually_retries():
    bot = FakeBot()
    bus = CommandBus(bot)
    unit = own_unit(bot)
    bus.begin_frame()
    assert bus.issue(unit.move(Point2((10, 10))))
    bot.time = 0.2
    bus.begin_frame()
    assert not bus.issue(unit.move(Point2((10, 10))))
    bot.time = 0.8
    bus.begin_frame()
    assert bus.issue(unit.move(Point2((10, 10))))


def test_rejected_command_does_not_spend_or_claim_a_unit():
    bot = FakeBot()
    bot.accept = False
    bus = CommandBus(bot)
    bus.begin_frame()
    assert not bus.issue(own_unit(bot)(A.BARRACKSTRAIN_MARINE), cost=U.MARINE)
    assert bus.budget.minerals == 100
    assert not bus.issued


def test_false_command_is_safe_but_already_executed_true_is_a_contract_error():
    bus = CommandBus(FakeBot())
    assert not bus.issue(False)
    with pytest.raises(RuntimeError, match="unit_command_uses_self_do"):
        bus.issue(True)
