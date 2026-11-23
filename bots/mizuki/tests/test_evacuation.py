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
        "evacuate_workers", orders=37, protected=37, infantry_lines=0, siege_sources=1
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


def infantry(tag, x, y, kind=U.MARINE):
    return Object(
        tag=tag,
        position=point(x, y),
        type_id=kind,
        is_ready=True,
        health_percentage=1,
        is_cloaked=False,
    )


def test_overwhelming_bio_push_relocates_worker_before_hall_is_destroyed():
    workers = [scv(i + 1, 146, 34 + i * 0.3, 70) for i in range(6)]
    safe = scv(25, 143, 18, 71)
    bot = fixture([*workers, safe])
    bot.mineral_field[0].position = point(148, 42)
    bot.units = [infantry(99, 154, 38, U.SIEGETANKSIEGED)]
    for hall in bot.townhalls:
        hall.health_percentage = 0.95
    attackers = [infantry(90 + i, 143 + i * 0.6, 43, U.MARAUDER) for i in range(7)]
    CompetitiveBot._evacuate_workers(bot, attackers)
    natural = bot.townhalls[0]
    assert -natural.tag in bot._evacuation.zones
    assert {w.tag for w in workers} <= bot._evacuation.sheltered
    assert all(w.move.called or w.gather.called for w in workers)
    safe.move.assert_not_called()
    safe.gather.assert_not_called()
    bot.time += 4
    CompetitiveBot._evacuate_workers(bot, [])
    assert not CompetitiveBot._allows_worker_task(bot, workers[0], point(139, 41))
    bot.time += 2
    CompetitiveBot._evacuate_workers(bot, [])
    assert not bot._evacuation.zones
    assert not bot._evacuation.sheltered


def test_healthy_well_covered_natural_keeps_mining_during_small_bio_contact():
    worker = scv(1, 147, 38, 70)
    bot = fixture([worker])
    bot.mineral_field[0].position = point(148, 42)
    bot.units = [infantry(i, 149, 39) for i in range(15)]
    for hall in bot.townhalls:
        hall.health_percentage = 1
    attackers = [infantry(50 + i, 146 + i / 2, 43) for i in range(5)]
    CompetitiveBot._evacuate_workers(bot, attackers)
    assert not bot._evacuation.zones
    worker.move.assert_not_called()
    worker.gather.assert_not_called()


def test_brief_bio_zone_does_not_expire_live_siege_zone():
    bot = fixture([])
    bot.mineral_field[0].position = point(148, 42)
    bot.units = []
    for hall in bot.townhalls:
        hall.health_percentage = 0.72
    gun = tank(99, 137, 52)
    attackers = [infantry(50 + i, 141 + i / 2, 43) for i in range(8)]
    CompetitiveBot._evacuate_workers(bot, [gun, *attackers])
    assert set(bot._evacuation.zones) == {99, -bot.townhalls[0].tag}
    bot.time += 6
    CompetitiveBot._evacuate_workers(bot, [])
    assert set(bot._evacuation.zones) == {99}


def test_evacuated_worker_resumes_mining_at_main_after_natural_is_lost():
    worker = scv(1, 147, 39, 70)
    bot = fixture([worker])
    bot.mineral_field[0].position = point(148, 42)
    bot.units = []
    for hall in bot.townhalls:
        hall.health_percentage = 0.7
    attackers = [infantry(50 + i, 143 + i, 43, U.MARAUDER) for i in range(7)]
    CompetitiveBot._evacuate_workers(bot, attackers)
    assert worker.move.called
    natural = bot.townhalls.pop(0)
    bot.state.dead_units = {natural.tag}
    worker.position = point(143, 29)
    worker.order_target = None
    worker.is_idle = True
    worker.is_collecting = False
    bot.time += 2
    CompetitiveBot._evacuate_workers(bot, attackers)
    worker.gather.assert_called_once()
    assert worker.gather.call_args.args[0].tag in (71, 72)
    assert -natural.tag in bot._evacuation.zones


def test_escape_finishes_before_switching_to_gather_after_crossing_edge():
    from bots.mizuki.evacuation import EvacOrder

    worker = scv(1, 142, 33, point(142, 24))
    bot = fixture([worker])
    bot._evacuation.zones = {501: SiegeZone(point(142, 50), 15.7, bot.time)}
    bot._evacuation.sheltered.add(worker.tag)
    bot._evacuation.orders[worker.tag] = EvacOrder(
        "move", point(142, 24), bot.time - 1.1
    )
    # At the edge, an available mineral alone must not interrupt the active leg.
    assert bot._evacuation.react(bot, bot.time) == 0
    worker.gather.assert_not_called()
    worker.move.assert_not_called()
    worker.position = point(142, 24.8)
    assert bot._evacuation.react(bot, bot.time + 1) == 1
    assert worker.gather.call_args.args[0].tag in (71, 72)


def test_new_fire_on_escape_route_allows_immediate_replan():
    from bots.mizuki.evacuation import EvacOrder

    worker = scv(1, 142, 32, point(142, 24))
    bot = fixture([worker])
    evac = bot._evacuation
    evac.zones = {501: SiegeZone(point(142, 23), 5, bot.time)}
    evac.sheltered.add(worker.tag)
    evac.orders[worker.tag] = EvacOrder("move", point(142, 24), bot.time - 0.4)
    assert evac.react(bot, bot.time) == 1
    waypoint = worker.move.call_args.args[0]
    assert waypoint != point(142, 24)
    assert evac.avoids_fire(worker.position, waypoint)


def test_unacknowledged_retreat_orders_do_not_stall_recovery():
    from bots.mizuki.evacuation import EvacOrder

    worker = scv(1, 142, 31)
    bot = fixture([worker])
    evac = bot._evacuation
    evac.zones = {501: SiegeZone(point(142, 50), 15.7, bot.time)}
    evac.sheltered.add(worker.tag)
    evac.orders[worker.tag] = EvacOrder("move", point(142, 24), bot.time - 0.9)
    assert evac.react(bot, bot.time) == 1
    worker.gather.assert_called_once()
