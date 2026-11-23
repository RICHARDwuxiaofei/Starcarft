import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bots.mizuki.bot import CompetitiveBot
from bots.mizuki.policy import Signals
from bots.mizuki.trace import DecisionTrace


def point(x, y):
    return Point2((x, y))


class NoRamp:
    _wall_anchors = None

    @property
    def main_base_ramp(self):
        raise ValueError("no compatible ramp")


def test_wall_geometry_has_safe_cached_fallback_on_map_without_ramp():
    agent = NoRamp()
    anchor = CompetitiveBot._anchors(agent)
    assert anchor == (None, None, None)
    assert CompetitiveBot._anchors(agent) is anchor


def test_selects_free_gathering_worker_when_closest_was_already_commanded():
    close = SimpleNamespace(
        tag=1, is_idle=True, is_collecting=False, distance_to=lambda _: 1
    )
    busy = SimpleNamespace(
        tag=2, is_idle=False, is_collecting=False, distance_to=lambda _: 2
    )
    free = SimpleNamespace(
        tag=3, is_idle=False, is_collecting=True, distance_to=lambda _: 3
    )
    agent = SimpleNamespace(workers=[close, busy, free], unit_tags_received_action={1})
    assert CompetitiveBot._available_builder(agent, point(2, 2)) is free


def test_placement_uses_addon_search_and_never_occupies_reserved_pad():
    near = point(10.5, 10.5)
    pad_owner = point(19.5, 10.5)
    pad = pad_owner.offset((2.5, -0.5))
    fallback = point(22.5, 16.5)
    worker = Mock(tag=20)
    worker.build.return_value = True
    find = AsyncMock(side_effect=[pad, fallback])
    agent = SimpleNamespace(
        time=55,
        _next_build={},
        can_afford=lambda _: True,
        workers=[worker],
        _reserved_addons={pad_owner: 75},
        structures=lambda _: [],
        find_placement=find,
        _available_builder=lambda _: worker,
        unit_tags_received_action=set(),
        _trace_intent=Mock(),
    )
    assert asyncio.run(CompetitiveBot._build(agent, U.FACTORY, near, addon=True))
    assert find.await_count == 2 and all(
        call.kwargs["addon_place"] for call in find.await_args_list
    )
    worker.build.assert_called_once_with(U.FACTORY, fallback, can_afford_check=True)
    assert fallback in agent._reserved_addons


def test_two_unexplained_own_worker_losses_nearby_prompt_scan_even_at_full_health_before_death():
    p = point(4, 4)
    q = point(5, 4)
    base = SimpleNamespace(position=point(0, 0))
    agent = SimpleNamespace(
        time=500,
        workers=[SimpleNamespace(tag=2, health=45, position=q)],
        _worker_sightings={1: (45, p), 2: (45, q)},
        _unexplained_hits=[],
    )
    assert CompetitiveBot._missing_worker_contact(agent, [], [base], False) is None
    agent.time += 1
    agent.workers.clear()
    assert (
        CompetitiveBot._missing_worker_contact(agent, [], [base], False).position == q
    )


def test_visible_attacker_disqualifies_unexplained_loss():
    p = point(4, 4)
    base = SimpleNamespace(position=point(0, 0))
    enemy = SimpleNamespace(can_attack_ground=True, distance_to=lambda _: 4)
    agent = SimpleNamespace(
        time=500, workers=[], _worker_sightings={1: (45, p)}, _unexplained_hits=[]
    )
    assert CompetitiveBot._missing_worker_contact(agent, [enemy], [base], True) is None


def test_trace_is_bounded_samples_phase_changes_and_omits_unrecorded_time():
    trace = DecisionTrace(sample_interval_s=10, sample_limit=2, decision_limit=2)
    trace.sample(0, "GATHER", army_supply=0)
    trace.sample(2, "GATHER", army_supply=1)
    trace.sample(3, "PRESSURE", army_supply=12)
    trace.sample(4, "RETREAT", army_supply=6)
    out = trace.export()
    assert [r["t"] for r in out["samples"]] == [0, 3]
    assert out["dropped_samples"] == out["dropped_decisions"] == 1


def test_frame_error_does_not_look_like_completed_step():
    trace = DecisionTrace()
    fake = SimpleNamespace(
        _trace=trace,
        time=4.5,
        enemy_units=[],
        enemy_structures=[],
        _signals=Mock(return_value=Signals(4.5, 1, 0, 0, 0, 0, False)),
        _distribute_economy=AsyncMock(side_effect=ValueError("only local details")),
    )
    with pytest.raises(ValueError):
        asyncio.run(CompetitiveBot.on_step(fake, 0))
    assert trace.steps_completed == 0
    assert trace.export()["last_error"] == {
        "t": 4.5,
        "iteration": 0,
        "type": "ValueError",
    }
    assert "only local details" not in repr(trace.export())


def test_first_macro_frame_runs_without_wall_geometry_and_queues_worker():
    class Group(list):
        @property
        def ready(self):
            return self

        @property
        def amount(self):
            return len(self)

    home = point(16, 15)
    hall = SimpleNamespace(tag=99, position=home, is_idle=True, type_id=U.COMMANDCENTER)
    train = Mock()
    build = AsyncMock(
        return_value=False
    )  # Supply Depot is not affordable at 50 starting minerals.
    agent = SimpleNamespace(
        time=0,
        minerals=50,
        supply_workers=12,
        _natural_retry_at=0,
        _protected_support=lambda *_: None,
        supply_left=3,
        supply_used=12,
        supply_cap=15,
        workers=Group([object()] * 12),
        townhalls=Group([hall]),
        _anchors=Mock(return_value=(None, None, None)),
        structures=lambda _: Group(),
        _working_refineries=lambda: 0,
        start_location=home,
        game_info=SimpleNamespace(map_center=point(48, 48)),
        unit_tags_received_action=set(),
        _queued_upgrades=set(),
        _last_macro_mode=None,
        _pending=lambda _: 0,
        _plan_total=lambda _: 0,
        _train=train,
        _build=build,
        _tech=AsyncMock(),
        _addons_and_army=AsyncMock(),
        _trace_intent=Mock(),
        _resume_construction=Mock(),
        _staff_refineries=Mock(),
    )
    asyncio.run(CompetitiveBot._macro(agent, Signals(0, 1, 0, 0, 0, 0, False)))
    assert train.call_args.args[0] is hall
    assert train.call_args.args[1] is U.SCV
    assert build.await_args.args[0] is U.SUPPLYDEPOT


def test_gas_workers_entering_refinery_do_not_trigger_false_cloak_alert():
    near = point(5, 1)
    hall = SimpleNamespace(position=point(0, 0))
    gas = SimpleNamespace(position=near, tag=99)
    agent = SimpleNamespace(
        time=68,
        workers=[],
        gas_buildings=[gas],
        _worker_sightings={1: (45, point(8, 2)), 2: (45, point(3, 1))},
        _gas_worker_sightings={1},
        _unexplained_hits=[],
    )
    assert CompetitiveBot._missing_worker_contact(agent, [], [hall], False) is None
    assert agent._unexplained_hits == []


def test_opening_reserve_persists_past_old_cutoff_and_drops_under_real_pressure():
    from dataclasses import replace

    from bots.mizuki.policy import desired_refineries, protect_natural

    row = Signals(186, 1, 3, 0, 0, 0, False)
    assert protect_natural(row, barracks_ready=True, pending=False)
    assert protect_natural(replace(row, time=270), barracks_ready=True, pending=False)
    assert not protect_natural(
        replace(row, local_pressure=5), barracks_ready=True, pending=False
    )
    assert not protect_natural(row, barracks_ready=True, pending=True)
    assert not protect_natural(row, barracks_ready=True, pending=False, retry_ok=False)
    assert desired_refineries(1, 50, factory_ready=True) == 1
    assert desired_refineries(2, 532, factory_ready=True) == 1
    assert desired_refineries(2, 100, factory_ready=True) == 2


@pytest.mark.parametrize(
    ("money", "bases", "candidate", "expected_mode", "first_action"),
    [
        (325, 1, None, "bank_natural", None),
        (400, 1, None, "normal", "expand"),
        (100, 2, U.SIEGETANK, "bank_siegetank", None),
        (150, 2, U.SIEGETANK, "normal", "train_SIEGETANK"),
    ],
)
def test_macro_spends_on_expansion_or_first_tank_before_optional_army(
    money, bases, candidate, expected_mode, first_action
):
    class Group(list):
        ready = property(lambda self: self)
        amount = property(len)

    home = point(16, 15)
    hall = SimpleNamespace(
        tag=99, position=home, is_idle=True, type_id=U.ORBITALCOMMAND
    )
    events = []
    army = AsyncMock(side_effect=lambda *_: events.append("army"))
    tech = AsyncMock(side_effect=lambda *_: events.append("tech"))
    expand = AsyncMock(side_effect=lambda *_: (events.append("expand"), True)[1])
    factory = SimpleNamespace(tag=3)
    f = SimpleNamespace(
        time=186,
        minerals=money,
        vespene=300,
        supply_workers=22,
        supply_left=10,
        supply_used=33,
        supply_cap=43,
        workers=Group([object()]),
        townhalls=Group([hall]),
        _anchors=lambda: (None, None, None),
        structures=lambda t: Group([object()]) if t == U.BARRACKS else Group(),
        _working_refineries=lambda: 1,
        start_location=home,
        game_info=SimpleNamespace(map_center=point(48, 48)),
        unit_tags_received_action=set(),
        _queued_upgrades=set(),
        _last_macro_mode=None,
        _pending=lambda _: 0,
        _plan_total=lambda t: int(t == U.BARRACKS),
        _expand=expand,
        _next_build={},
        _natural_retry_at=0,
        _protected_support=lambda *_: (candidate, factory) if candidate else None,
        can_afford=lambda t: money >= (400 if t == U.COMMANDCENTER else 150),
        _train=lambda _, unit, __: (events.append(f"train_{unit.name}"), True)[1],
        _build=AsyncMock(),
        _gas=AsyncMock(),
        _tech=tech,
        _addons_and_army=army,
        _trace_intent=Mock(),
        _resume_construction=Mock(),
        _staff_refineries=Mock(),
        _repair=Mock(),
    )
    s = Signals(186, bases, 7, 0, 0, 0, False)
    asyncio.run(CompetitiveBot._macro(f, s))
    assert f._last_macro_mode == expected_mode
    if first_action:
        assert events[0] == first_action
    else:
        assert not events
        tech.assert_not_awaited()
        army.assert_not_awaited()


def test_ready_factory_tech_lab_has_priority_over_first_starport():
    class Group(list):
        ready = property(lambda self: self)

        def find_by_tag(self, tag):
            return next((u for u in self if u.tag == tag), None)

    factory = SimpleNamespace(is_idle=True, tag=1, add_on_tag=2)
    lab = SimpleNamespace(tag=2)
    structures = {U.FACTORY: Group([factory]), U.FACTORYTECHLAB: Group([lab])}
    agent = SimpleNamespace(
        time=260,
        vespene=300,
        _support_retry_at=0,
        _pending=lambda _: 0,
        structures=lambda u: structures.get(u, Group()),
        units=lambda _: [],
        _plan_total=lambda _: 0,
        calculate_cost=lambda _: SimpleNamespace(vespene=125),
        unit_tags_received_action=set(),
    )
    signals = Signals(260, 2, 12, 0, 0, 0, False)
    assert CompetitiveBot._protected_support(agent, signals, 6) == (
        U.SIEGETANK,
        factory,
    )
    agent.units = lambda u: (
        [object()] if isinstance(u, set) and U.SIEGETANK in u else []
    )
    assert CompetitiveBot._protected_support(agent, signals, 6) == (U.STARPORT, None)


def test_surplus_gas_returns_idle_cargo_free_workers_to_local_minerals():
    patch = Mock(tag=20, distance_to=lambda _: 1)
    gather = Mock()
    worker = SimpleNamespace(
        tag=1, order_target=42, is_carrying_resource=False, gather=gather
    )
    agent = SimpleNamespace(
        time=350,
        _next_refinery_staff=0,
        vespene=532,
        minerals=40,
        gas_buildings=SimpleNamespace(tags={42}),
        workers=[worker],
        unit_tags_received_action=set(),
        mineral_field=SimpleNamespace(closer_than=lambda *_: [patch]),
    )
    CompetitiveBot._staff_refineries(agent)
    gather.assert_called_once_with(patch)


def test_unobserved_command_center_order_cools_down_only_that_site():
    from bots.mizuki.bot import Contact

    site = point(42, 40)
    agent = SimpleNamespace(
        time=110,
        townhalls=[],
        _expansion_intent=Contact(site, 109),
        _blocked_expansions={},
        _trace_intent=Mock(),
    )
    assert CompetitiveBot._expansion_pending(agent, False)
    assert not agent._blocked_expansions
    agent.time = 112
    assert not CompetitiveBot._expansion_pending(agent, False)
    assert agent._blocked_expansions == {site: 130}
    agent._trace_intent.assert_called_once_with("expand_unconfirmed", x=42, y=40)


def test_observed_command_center_order_does_not_blacklist_the_site():
    from bots.mizuki.bot import Contact

    site = point(42, 40)
    agent = SimpleNamespace(
        time=128,
        townhalls=[],
        _expansion_intent=Contact(site, 109),
        _blocked_expansions={},
    )
    assert CompetitiveBot._expansion_pending(agent, True)
    assert agent._expansion_intent is None and not agent._blocked_expansions


def test_high_gas_does_not_allow_stock_worker_allocator_to_refill_refinery():
    agent = SimpleNamespace(
        time=330,
        minerals=40,
        vespene=532,
        distribute_workers=AsyncMock(),
        _distribute_minerals_only=Mock(),
    )
    asyncio.run(CompetitiveBot._distribute_economy(agent))
    agent._distribute_minerals_only.assert_called_once()
    agent.distribute_workers.assert_not_awaited()
    agent.vespene = 80
    asyncio.run(CompetitiveBot._distribute_economy(agent))
    agent.distribute_workers.assert_awaited_once()


def test_high_gas_mineral_allocator_moves_overflow_to_new_base_without_refilling_gas():
    base_a = SimpleNamespace(
        tag=7, is_flying=False, surplus_harvesters=1, distance_to=lambda _: 2
    )
    base_b = SimpleNamespace(
        tag=8, is_flying=False, surplus_harvesters=-2, distance_to=lambda _: 12
    )
    first = SimpleNamespace(tag=30, mineral_contents=700, distance_to=lambda _: 3)
    second = SimpleNamespace(tag=31, mineral_contents=900, distance_to=lambda _: 3)
    minerals = {7: [first], 8: [second]}
    overflow = SimpleNamespace(
        tag=1,
        is_idle=False,
        order_target=30,
        is_carrying_resource=False,
        gather=Mock(return_value=True),
    )
    idle = SimpleNamespace(
        tag=2,
        is_idle=True,
        order_target=None,
        is_carrying_resource=False,
        gather=Mock(return_value=True),
    )
    gas = SimpleNamespace(
        tag=3, is_idle=False, order_target=77, is_carrying_resource=False, gather=Mock()
    )
    agent = SimpleNamespace(
        townhalls=SimpleNamespace(ready=[base_a, base_b]),
        workers=[overflow, idle, gas],
        mineral_field=SimpleNamespace(closer_than=lambda _, base: minerals[base.tag]),
        unit_tags_received_action=set(),
    )
    CompetitiveBot._distribute_minerals_only(agent)
    overflow.gather.assert_called_once_with(second)
    idle.gather.assert_called_once_with(second)
    gas.gather.assert_not_called()
