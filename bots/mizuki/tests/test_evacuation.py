import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bots.mizuki.bot import CompetitiveBot
from bots.mizuki.evacuation import MineralLineEvacuation, SiegeZone


class Object(SimpleNamespace):
    def distance_to(self, other):
        return self.position.distance_to(getattr(other, "position", other))


class Group(list):
    ready = property(lambda self: self)

    def closer_than(self, distance, target):
        return Group(o for o in self if o.distance_to(target) < distance)


def point(x, y):
    return Point2((x, y))


def tank(tag, x, y):
    return Object(
        tag=tag,
        position=point(x, y),
        type_id=U.SIEGETANKSIEGED,
        ground_range=13,
        radius=1,
        is_cloaked=False,
    )


def scv(tag, x, y, target=None):
    return Object(
        tag=tag,
        position=point(x, y),
        order_target=target,
        is_idle=target is None,
        is_collecting=target is not None,
        is_carrying_resource=False,
        move=Mock(return_value=True),
        stop=Mock(return_value=True),
        gather=Mock(return_value=True),
    )


def fixture(workers):
    close = Object(
        tag=40, position=point(156, 41), is_flying=False, surplus_harvesters=-6
    )
    deep = Object(
        tag=41, position=point(143, 18), is_flying=False, surplus_harvesters=-5
    )
    exposed = Object(tag=70, position=point(139, 41), mineral_contents=900)
    lower = Object(tag=71, position=point(142, 16), mineral_contents=900)
    other = Object(tag=72, position=point(146, 16), mineral_contents=900)
    return SimpleNamespace(
        time=602,
        workers=Group(workers),
        townhalls=Group([close, deep]),
        gas_buildings=Group(),
        mineral_field=Group([exposed, lower, other]),
        unit_tags_received_action=set(),
        game_info=SimpleNamespace(
            playable_area=SimpleNamespace(x=0, y=0, width=180, height=170)
        ),
        in_pathing_grid=Mock(return_value=True),
        _trace_intent=Mock(),
        state=SimpleNamespace(dead_units=set()),
        _evacuation=MineralLineEvacuation(),
    )


def test_mineral_line_gets_warning_even_if_hall_is_outside_trigger_ring():
    gun = tank(501, 137, 52)
    workers = [scv(i + 1, 139 + (i % 3) * 0.25, 41, 70) for i in range(37)]
    unaffected = scv(100, 144, 16, 71)
    bot = fixture([*workers, unaffected])
    assert gun.distance_to(bot.townhalls[0]) > 21
    CompetitiveBot._evacuate_workers(bot, [gun])
    assert bot._evacuation.sheltered == {w.tag for w in workers}
    escape = {w.move.call_args.args[0] for w in workers}
    assert len(escape) >= 2
    assert all(
        gun.distance_to(p) > gun.distance_to(w)
        for w in workers
        for p in [w.move.call_args.args[0]]
    )
    unaffected.move.assert_not_called()
    unaffected.gather.assert_not_called()
    for w in workers:
        w.gather.assert_not_called()
    bot._trace_intent.assert_called_once_with(
        "evacuate_workers", orders=37, protected=37
    )


def test_safe_endpoint_across_siege_fire_is_not_a_safe_assignment():
    evac = MineralLineEvacuation(zones={1: SiegeZone(point(25, 25), 15, 500)})
    assert evac.margin(point(45, 25)) > 0
    assert not evac.avoids_fire(point(5, 25), point(45, 25))
    assert evac.avoids_fire(point(5, 25), point(5, 48))


def test_visibility_hysteresis_protects_orders_and_then_allows_stock_allocator():
    worker = scv(1, 139, 41, 70)
    bot = fixture([worker])
    gun = tank(501, 137, 52)
    CompetitiveBot._evacuate_workers(bot, [gun])
    bot.time += 0.5
    CompetitiveBot._evacuate_workers(bot, [])
    worker.move.assert_called_once()
    bot.time += 8
    bot.state.dead_units = {worker.tag}
    bot.workers.clear()  # same protected tag can reappear after leaving a refinery
    CompetitiveBot._evacuate_workers(bot, [])
    assert worker.tag in bot._evacuation.sheltered
    worker.position = point(144, 17)
    assert not CompetitiveBot._allows_worker_task(bot, worker, point(142, 16))
    bot.time += 1
    CompetitiveBot._evacuate_workers(bot, [])
    assert CompetitiveBot._allows_worker_task(bot, worker, point(142, 16))

    bot.minerals, bot.vespene = 200, 60
    bot.distribute_workers = AsyncMock()
    bot._distribute_minerals_only = Mock()
    bot._evacuation.update([gun], set(), bot.time)
    asyncio.run(CompetitiveBot._distribute_economy(bot))
    bot._distribute_minerals_only.assert_called_once()
    bot.distribute_workers.assert_not_awaited()
    gun.type_id = U.SIEGETANK
    bot._evacuation.update([gun], set(), bot.time + 1)
    asyncio.run(CompetitiveBot._distribute_economy(bot))
    bot.distribute_workers.assert_awaited_once()


def test_already_commanded_worker_not_reassigned_and_pathing_bounds_checked():
    worker = scv(1, 1, 7, 70)
    bot = fixture([worker])
    gun = tank(501, 5, 7)
    bot._evacuation.update([gun], set(), bot.time)
    bot.unit_tags_received_action.add(worker.tag)
    assert not bot._evacuation.react(bot, bot.time)
    assert worker.tag in bot._evacuation.sheltered
    worker.move.assert_not_called()
    bot.unit_tags_received_action.clear()

    def blocked(tile):
        assert 0 <= tile.x < 180 and 0 <= tile.y < 170
        return False

    bot.in_pathing_grid.side_effect = blocked
    assert not bot._evacuation.react(bot, bot.time + 1)
    worker.move.assert_not_called()


def test_transfer_can_mine_safe_alternative_without_permitting_an_unsafe_rebuild():
    worker = scv(1, 146, 29, 70)
    bot = fixture([worker])
    gun = tank(500, 137, 52)
    CompetitiveBot._evacuate_workers(bot, [gun])
    assert worker.gather.call_args.args[0].tag in (71, 72)
    assert not CompetitiveBot._candidate_builders(bot, point(147, 19))
    fresh = scv(2, 144, 18)
    bot.workers.append(fresh)
    assert not CompetitiveBot._candidate_builders(bot, point(139, 41))
    assert CompetitiveBot._candidate_builders(bot, point(147, 19)) == [fresh]


def test_distant_tanks_do_not_disable_safe_economy():
    bot = fixture([scv(1, 139, 41)])
    CompetitiveBot._evacuate_workers(bot, [tank(501, 25, 25)])
    assert not bot._evacuation.zones


def test_construction_order_toward_siege_is_intercepted_before_worker_enters():
    worker = scv(1, 146, 29, point(139, 44))
    bot = fixture([worker])
    gun = tank(500, 137, 52)
    assert gun.distance_to(worker) > 17
    CompetitiveBot._evacuate_workers(bot, [gun])
    assert worker.tag in bot._evacuation.sheltered
    worker.gather.assert_called_once()


def test_refuge_patch_nearest_to_an_unsafe_dropoff_is_rejected():
    worker = scv(1, 146, 29, point(137, 52))
    bot = fixture([worker])
    safe_hall = Object(tag=1, position=point(155, 36), is_flying=False)
    unsafe_hall = Object(tag=2, position=point(148, 44), is_flying=False)
    unsafe_patch = Object(tag=4, position=point(152, 41), mineral_contents=900)
    bot.townhalls = Group([safe_hall, unsafe_hall])
    bot.mineral_field = Group([unsafe_patch])
    bot._evacuation.zones = {1: SiegeZone(point(137, 52), 15.7, bot.time)}
    assert bot._evacuation.avoids_fire(worker.position, unsafe_patch.position)
    assert unsafe_hall.distance_to(unsafe_patch) < safe_hall.distance_to(unsafe_patch)
    bot._evacuation.react(bot, bot.time)
    worker.gather.assert_not_called()
    worker.move.assert_called_once()


def test_safe_mining_unlocks_worker_even_while_original_siege_remains():
    worker = scv(1, 139, 41, 70)
    bot = fixture([worker])
    gun = tank(501, 137, 52)
    CompetitiveBot._evacuate_workers(bot, [gun])
    worker.position, worker.order_target = point(143, 18), 71
    bot.time += 5
    CompetitiveBot._evacuate_workers(bot, [gun])
    assert not CompetitiveBot._allows_worker_task(bot, worker, point(144, 19))
    bot.time += 8
    CompetitiveBot._evacuate_workers(bot, [gun])
    assert CompetitiveBot._allows_worker_task(bot, worker, point(144, 19))
    assert not CompetitiveBot._allows_worker_task(bot, worker, point(139, 41))
