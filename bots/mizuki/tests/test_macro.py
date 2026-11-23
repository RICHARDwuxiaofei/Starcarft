import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sc2.bot_ai import BotAI
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as Up
from sc2.position import Point2

from bots.mizuki import bot as module
from bots.mizuki.bot import CompetitiveBot, visible
from bots.mizuki.policy import (
    Signals,
    air_targets,
    desired_workers,
    expansion_allowed,
    supply_order_count,
)


def test_entrypoint_and_visibility_boundary():
    assert isinstance(CompetitiveBot(), BotAI)
    contact = SimpleNamespace(is_visible=True, is_snapshot=False)
    assert visible(
        [
            contact,
            SimpleNamespace(is_visible=False, is_snapshot=False),
            SimpleNamespace(is_visible=True, is_snapshot=True),
        ]
    ) == [contact]


def test_economy_and_forecast():
    assert desired_workers(1, 1) == 21
    assert desired_workers(6, 12) == 78
    assert supply_order_count(12, 3, 15, 0, 0) == 1
    assert supply_order_count(12, 3, 15, 1, 0) == 0
    assert supply_order_count(110, 1, 111, 0, 7) == 2
    assert supply_order_count(196, 4, 200, 0, 10) == 0


def test_safe_expansion_and_air_mix():
    initial = Signals(130, 1, 8, 0, 0, 0, False)
    assert expansion_allowed(initial)
    assert not expansion_allowed(replace(initial, local_pressure=6))
    assert not expansion_allowed(replace(initial, bases=2))
    assert expansion_allowed(replace(initial, bases=0, local_pressure=6))
    assert air_targets(initial) == (2, 0)
    assert air_targets(replace(initial, army_supply=83, enemy_air=8.5)) == (5, 7)


def test_same_frame_supply_cannot_be_double_spent():
    fake = SimpleNamespace(calculate_supply_cost=lambda _: 2, can_afford=lambda _: True)
    builder = Mock()
    builder.train.return_value = True
    supply = {"left": 3}
    assert CompetitiveBot._train(fake, builder, U.MARAUDER, supply)
    assert not CompetitiveBot._train(fake, builder, U.MARAUDER, supply)
    builder.train.assert_called_once()
    assert supply["left"] == 1


def test_two_bays_do_not_order_the_same_upgrade():
    fake = SimpleNamespace(
        state=SimpleNamespace(upgrades=set()),
        _queued_upgrades=set(),
        unit_tags_received_action=set(),
        already_pending_upgrade=lambda _: 0,
        can_afford=lambda _: True,
    )
    first, second = Mock(), Mock()
    first.research.return_value = second.research.return_value = True
    options = (Up.TERRANINFANTRYWEAPONSLEVEL1, Up.TERRANINFANTRYARMORSLEVEL1)
    assert CompetitiveBot._research(fake, first, options)
    assert CompetitiveBot._research(fake, second, options)
    first.research.assert_called_once_with(options[0], can_afford_check=True)
    second.research.assert_called_once_with(options[1], can_afford_check=True)


def test_skips_blocked_expansion_tile_without_stalling():
    start, natural, third = Point2((0, 0)), Point2((14, 0)), Point2((20, 4))
    worker = Mock(tag=19, position=Point2((1, 1)))
    worker.build.return_value = True
    hall = SimpleNamespace(position=start)
    fake = SimpleNamespace(
        townhalls=[hall],
        start_location=start,
        expansion_locations_list=[third, natural, start],
        _blocked_expansions={},
        _expansion_threats=[],
        enemy_start_locations=[],
        time=121,
        can_place_single=AsyncMock(side_effect=[False, True]),
        _candidate_builders=lambda _: [worker],
        unit_tags_received_action=set(),
        client=SimpleNamespace(query_pathing=AsyncMock(return_value=21)),
    )
    asyncio.run(CompetitiveBot._expand(fake, [hall]))
    assert natural in fake._blocked_expansions
    worker.build.assert_called_once_with(U.COMMANDCENTER, third, can_afford_check=True)


def test_frame_dispatch_only_directs_visible_units(monkeypatch):
    contact = SimpleNamespace(is_visible=True, is_snapshot=False)
    snapshot = SimpleNamespace(is_visible=False, is_snapshot=True)
    signals = Signals(0, 1, 0, 0, 0, 0, False)
    agent = SimpleNamespace(
        enemy_units=[contact, snapshot],
        enemy_structures=[snapshot],
        time=0,
        _signals=Mock(return_value=signals),
        _distribute_economy=AsyncMock(),
        _scout=Mock(),
        _orbitals=Mock(),
        _depot_control=Mock(),
        _macro=AsyncMock(),
        _evacuate_workers=Mock(),
        _track_base_losses=Mock(),
        _update_expansion_risk=Mock(),
    )
    combat = AsyncMock()
    monkeypatch.setattr(module, "control_combat", combat)
    asyncio.run(CompetitiveBot.on_step(agent, 0))
    agent._signals.assert_called_once_with([contact], [])
    agent._scout.assert_called_once_with([contact])
    agent._macro.assert_awaited_once_with(signals)
    agent._evacuate_workers.assert_called_once_with([contact])
    agent._track_base_losses.assert_called_once_with()
    agent._update_expansion_risk.assert_called_once_with([contact], [])
    combat.assert_awaited_once_with(agent, 0)


def test_close_attacker_and_scouted_flyer_drive_distinct_responses():
    start = Point2((2, 2))
    hall = SimpleNamespace(position=start)
    melee = SimpleNamespace(
        tag=8,
        type_id=U.ZEALOT,
        distance_to=lambda _: 9,
        can_attack=True,
        is_flying=False,
        is_armored=False,
        is_cloaked=False,
        is_burrowed=False,
    )
    flyer = SimpleNamespace(
        tag=9,
        type_id=U.BANSHEE,
        distance_to=lambda _: 60,
        can_attack=True,
        is_flying=True,
        is_armored=False,
        is_cloaked=False,
        is_burrowed=False,
    )
    agent = SimpleNamespace(
        time=120,
        townhalls=[hall],
        start_location=start,
        supply_army=8,
        _seen_air={},
        _armor_until=0,
        _cloak_until=0,
        calculate_supply_cost=lambda _: 2,
    )
    state = CompetitiveBot._signals(agent, [melee, flyer], [])
    assert state.local_pressure == 2
    assert state.enemy_air == 1
    assert state.detection_risk
    agent.time += 110
    state = CompetitiveBot._signals(agent, [], [])
    assert state.enemy_air == 0 and not state.detection_risk


def test_addon_reserves_correct_gas_and_minerals_before_followup_orders():
    from sc2.ids.ability_id import AbilityId as A

    building = Mock(position=Point2((10, 10)), return_value=True)
    agent = SimpleNamespace(
        minerals=50,
        vespene=50,
        calculate_cost=Mock(return_value=SimpleNamespace(minerals=50, vespene=50)),
        can_place_single=AsyncMock(return_value=True),
    )
    assert asyncio.run(CompetitiveBot._addon(agent, building, A.BUILD_REACTOR_BARRACKS))
    assert not asyncio.run(
        CompetitiveBot._addon(agent, building, A.BUILD_REACTOR_BARRACKS)
    )
    assert (agent.minerals, agent.vespene) == (0, 0)
    agent.calculate_cost.assert_called_with(U.REACTOR)
    building.assert_called_once_with(A.BUILD_REACTOR_BARRACKS)


def test_threatened_or_recently_lost_pad_is_not_replanted_under_fog():
    from bots.mizuki.bot import Contact

    main, natural = Point2((19, 32)), Point2((41, 19))
    exposed, covered, remote = Point2((38, 60)), Point2((15, 12)), Point2((80, 72))
    halls = [SimpleNamespace(position=p) for p in (main, natural)]
    worker = Mock(tag=7, position=main)
    worker.build.return_value = True
    fake = SimpleNamespace(
        townhalls=halls,
        start_location=main,
        enemy_start_locations=[Point2((110, 90))],
        expansion_locations_list=[main, natural, exposed, covered, remote],
        _blocked_expansions={},
        _expansion_threats=[],
        _seen_halls={12: exposed},
        time=384.95,
        state=SimpleNamespace(dead_units={12}),
        can_place_single=AsyncMock(return_value=True),
        _candidate_builders=lambda _: [worker],
        client=SimpleNamespace(query_pathing=AsyncMock(return_value=23)),
    )
    for i, hall in enumerate(halls):
        hall.tag, hall.is_flying = i + 1, False
    visible_marine = SimpleNamespace(
        can_attack_ground=True, type_id=U.MARINE, position=exposed.offset((6, 2))
    )
    CompetitiveBot._update_expansion_risk(fake, [visible_marine], [])
    CompetitiveBot._track_base_losses(fake)
    assert fake._blocked_expansions[exposed] > 456.38
    fake.time += 16
    CompetitiveBot._update_expansion_risk(fake, [], [])
    assert fake._expansion_threats == [Contact(visible_marine.position, 384.95)]
    assert asyncio.run(CompetitiveBot._expand(fake, halls))
    fake.can_place_single.assert_awaited_once_with(U.COMMANDCENTER, covered)
    worker.build.assert_called_once_with(
        U.COMMANDCENTER, covered, can_afford_check=True
    )
    fake.time += 16
    CompetitiveBot._update_expansion_risk(fake, [], [])
    assert not fake._expansion_threats


def test_research_never_overwrites_order_sent_to_same_lab_this_frame():
    fake = SimpleNamespace(unit_tags_received_action={9})
    lab = Mock(tag=9)
    assert not CompetitiveBot._research(fake, lab, (Up.COMBATSHIELD,))
    lab.research.assert_not_called()


def test_two_base_expansion_needs_workers_and_can_start_before_6_minutes():
    opening = Signals(260, 2, 19, 0, 0, 0, False)
    assert expansion_allowed(opening, saturated=True)
    assert not expansion_allowed(opening)
    assert not expansion_allowed(replace(opening, local_pressure=5), saturated=True)


def test_priority_stim_waits_for_first_tank_and_checks_attachment():
    two_bases = Signals(245, 2, 20, 0, 0, 0, False)
    attached, orphan = SimpleNamespace(tag=5), SimpleNamespace(tag=4)
    labs = SimpleNamespace(ready=SimpleNamespace(idle=[orphan, attached]))
    rax = SimpleNamespace(ready=[SimpleNamespace(add_on_tag=5)])
    fake = SimpleNamespace(
        structures=lambda kind: labs if kind == U.BARRACKSTECHLAB else rax,
        units=lambda _: [],
        _pending=Mock(return_value=0),
        _stim_underway=Mock(return_value=False),
        state=SimpleNamespace(upgrades=set()),
        already_pending_upgrade=lambda _: 0,
        unit_tags_received_action=set(),
    )
    assert CompetitiveBot._priority_bio_upgrade(fake, two_bases) is None
    fake._pending.return_value = 1
    assert CompetitiveBot._priority_bio_upgrade(fake, two_bases) == (
        attached,
        Up.STIMPACK,
    )
    fake.unit_tags_received_action.add(5)
    assert CompetitiveBot._priority_bio_upgrade(fake, two_bases) is None


def test_waiting_for_upgrade_gas_keeps_producing_only_surplus_marines():
    rax = [
        SimpleNamespace(tag=i, orders=[], has_reactor=False, add_on_tag=0)
        for i in (1, 2)
    ]
    fake = SimpleNamespace(
        minerals=170,
        calculate_cost=lambda item: SimpleNamespace(
            minerals=50 if item == U.MARINE else 100
        ),
    )

    def train(*_args, **_kwargs):
        fake.minerals -= 50
        return True

    fake._train = Mock(side_effect=train)
    supply = {"left": 4}
    CompetitiveBot._train_surplus_marines(fake, rax, supply, Up.STIMPACK)
    fake._train.assert_called_once_with(rax[0], U.MARINE, supply, queue=False)
    assert fake.minerals == 120
