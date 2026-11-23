import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bots.mizuki.bot import CompetitiveBot
from bots.mizuki.construction import CommandCenterRecovery
from bots.mizuki.evacuation import MineralLineEvacuation, SiegeZone


class Thing(SimpleNamespace):
    def distance_to(self, other):
        return self.position.distance_to(getattr(other, "position", other))


class Halls(list):
    @property
    def ready(self):
        return [hall for hall in self if hall.is_ready]


def scv(tag, xy, *, target=None, ability=A.SMART):
    worker = Thing(
        tag=tag,
        position=Point2(xy),
        is_collecting=target is None,
        is_idle=False,
        is_constructing_scv=ability == A.TERRANBUILD_COMMANDCENTER,
        smart=Mock(return_value=True),
        move=Mock(return_value=True),
        stop=Mock(return_value=True),
        orders=[],
    )
    if target is not None:
        worker.orders = [
            SimpleNamespace(target=target, ability=SimpleNamespace(id=ability))
        ]
    return worker


def situation():
    # AGY replay geometry, with main-line SCVs outside the old radius of 22.
    cc = Thing(
        tag=100,
        type_id=U.COMMANDCENTER,
        position=Point2((19.5, 134.5)),
        build_progress=0.78,
        is_ready=False,
        is_flying=False,
    )
    main = Thing(
        tag=90,
        type_id=U.ORBITALCOMMAND,
        position=Point2((32, 156)),
        build_progress=1,
        is_ready=True,
        is_flying=False,
    )
    original = scv(
        1, (19, 132), target=cc.position, ability=A.TERRANBUILD_COMMANDCENTER
    )
    near, far = scv(2, (29, 158)), scv(3, (31, 160))
    recovery = CommandCenterRecovery()
    bot = SimpleNamespace(
        time=178,
        workers=[original, near, far],
        townhalls=Halls([main, cc]),
        unit_tags_received_action=set(),
        in_pathing_grid=lambda _: True,
        game_info=SimpleNamespace(
            playable_area=SimpleNamespace(x=0, y=0, width=180, height=180)
        ),
        _evacuation=MineralLineEvacuation(),
        _cc_recovery=recovery,
        _trace_intent=Mock(),
    )
    return bot, recovery, cc, original, near, far


def reaper(xy=(22, 130)):
    return Thing(
        tag=60,
        position=Point2(xy),
        type_id=U.REAPER,
        can_attack_ground=True,
        ground_range=5,
        radius=0.5,
        is_cloaked=False,
    )


def tick(bot, recovery, at, enemies=()):
    bot.time = at
    recovery.update(bot, list(enemies), bot._evacuation, bot._trace_intent)


def test_builder_death_can_resume_from_main_25_tiles_away():
    bot, recovery, cc, original, near, far = situation()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 179)
    near.smart.assert_not_called()
    assert near.distance_to(cc) > 22
    tick(bot, recovery, 180)
    near.smart.assert_called_once_with(cc)
    far.smart.assert_not_called()
    assert recovery.jobs[cc.tag].queued_worker == near.tag
    assert not CompetitiveBot._allows_worker_task(bot, near, Point2((27, 159)))


def test_builder_can_evacuate_direct_reaper_and_no_replacement_enters_fire():
    bot, recovery, cc, original, near, far = situation()
    gun = reaper()
    tick(bot, recovery, 178, [gun])
    waypoint = original.move.call_args.args[0]
    assert gun.distance_to(waypoint) > gun.distance_to(original)
    original.orders.clear()
    tick(bot, recovery, 184, [gun])
    near.smart.assert_not_called()
    far.smart.assert_not_called()
    assert recovery.jobs[cc.tag].stayed_idle
    assert recovery.jobs[cc.tag].last_unsafe == 184


def test_resume_only_after_local_threat_clears_even_if_another_base_is_attacked():
    bot, recovery, cc, original, near, _ = situation()
    tick(bot, recovery, 178, [reaper()])
    bot.workers.remove(original)
    tick(bot, recovery, 245, [reaper((25, 129))])
    distant = reaper((110, 110))
    tick(bot, recovery, 248, [distant])
    near.smart.assert_not_called()
    tick(bot, recovery, 249.1, [distant])
    near.smart.assert_called_once_with(cc)


def test_queued_smart_window_and_observed_substitute_prevent_duplicate_builder():
    bot, recovery, cc, original, near, far = situation()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 180)
    tick(bot, recovery, 180.5)  # same old world snapshot, no acknowledgement yet
    near.smart.assert_called_once_with(cc)
    near.orders = [SimpleNamespace(target=cc.tag, ability=SimpleNamespace(id=A.SMART))]
    near.is_collecting = False
    tick(bot, recovery, 182)
    cc.build_progress += 0.01
    tick(bot, recovery, 183)
    near.smart.assert_called_once_with(cc)
    far.smart.assert_not_called()
    assert any(
        c.args[0] == "cc_construction_restarted"
        for c in bot._trace_intent.call_args_list
    )


def test_destroyed_foundation_releases_queued_worker_and_does_not_requeue():
    bot, recovery, cc, original, near, far = situation()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 180)
    assert recovery.reserved(near.tag)
    bot.townhalls.remove(cc)
    tick(bot, recovery, 180.1)
    assert not recovery.jobs
    assert not recovery.reserved(near.tag)
    assert CompetitiveBot._allows_worker_task(bot, near)
    far.smart.assert_not_called()


def test_visible_ongoing_original_never_hijacked_then_completion_releases_task():
    bot, recovery, cc, original, near, far = situation()
    # Replay DeepSeek's ordinary point build -> uninterrupted progress -> ready.
    tick(bot, recovery, 122.9)
    for at, progress in [(130, 0.82), (160, 0.91), (190, 0.98)]:
        cc.build_progress = progress
        tick(bot, recovery, at, [reaper((112, 106))])
    cc.is_ready, cc.build_progress = True, 1
    tick(bot, recovery, 194.33)
    original.move.assert_not_called()
    near.smart.assert_not_called()
    far.smart.assert_not_called()
    assert not recovery.jobs


def test_gen8_evacuated_workers_and_siege_route_are_not_overridden():
    bot, recovery, cc, original, near, far = situation()
    bot._evacuation.sheltered.add(original.tag)
    bot.unit_tags_received_action.add(original.tag)
    tick(bot, recovery, 178, [reaper()])
    original.move.assert_not_called()
    original.orders.clear()
    bot._evacuation.sheltered.update([near.tag, far.tag])
    bot._evacuation.zones[20] = SiegeZone(cc.position, 15, 178)
    tick(bot, recovery, 182)
    near.smart.assert_not_called()
    far.smart.assert_not_called()
    bot._evacuation.zones.clear()
    bot._evacuation.sheltered.clear()
    bot.unit_tags_received_action.clear()
    tick(bot, recovery, 187)
    near.smart.assert_called_once_with(cc)


def test_resume_without_ack_retries_and_preserves_regular_builders():
    bot, recovery, cc, original, near, far = situation()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 180)
    assert recovery.reserved(near.tag)
    tick(bot, recovery, 181.1)
    far.smart.assert_called_once_with(cc)


def test_replacement_stays_outside_when_reaper_moves_onto_approach():
    bot, recovery, cc, original, near, far = situation()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 180)
    near.orders = [SimpleNamespace(target=cc.tag, ability=SimpleNamespace(id=A.SMART))]
    near.is_collecting = False
    tick(bot, recovery, 182, [reaper((24, 144))])
    near.stop.assert_called_once()
    assert near.smart.call_count == 1
    far.smart.assert_not_called()


def test_economy_allocator_cannot_replace_unconfirmed_worker_order():
    bot, recovery, cc, original, near, _ = situation()
    bot.minerals = bot.vespene = 10
    bot.distribute_workers = AsyncMock()
    bot._distribute_minerals_only = Mock()
    tick(bot, recovery, 178)
    bot.workers.remove(original)
    tick(bot, recovery, 180)
    asyncio.run(CompetitiveBot._distribute_economy(bot))
    bot.distribute_workers.assert_not_awaited()
    bot._distribute_minerals_only.assert_called_once()
    near.orders = [SimpleNamespace(target=cc.tag, ability=SimpleNamespace(id=A.SMART))]
    near.is_collecting = False
    tick(bot, recovery, 181)
    asyncio.run(CompetitiveBot._distribute_economy(bot))
    bot.distribute_workers.assert_awaited_once()
