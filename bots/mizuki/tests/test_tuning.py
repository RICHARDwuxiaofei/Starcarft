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
        distribute_workers=AsyncMock(side_effect=ValueError("only local details")),
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
