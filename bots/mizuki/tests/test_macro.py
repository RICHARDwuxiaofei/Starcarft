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
        time=121,
        can_place_single=AsyncMock(side_effect=[False, True]),
        _available_builder=lambda _: worker,
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
        distribute_workers=AsyncMock(),
        _scout=Mock(),
        _orbitals=Mock(),
        _depot_control=Mock(),
        _macro=AsyncMock(),
    )
    combat = AsyncMock()
    monkeypatch.setattr(module, "control_combat", combat)
    asyncio.run(CompetitiveBot.on_step(agent, 0))
    agent._signals.assert_called_once_with([contact], [])
    agent._scout.assert_called_once_with([contact])
    agent._macro.assert_awaited_once_with(signals)
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
