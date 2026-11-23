"""Offline checks for the API request budgets added after the disconnect investigation.

No SC2 client is launched: all calls are recorded on in-memory fakes.
"""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bots.hunyuan.bot import (
    BUILD_PLACEMENT_STEP,
    BUILD_QUERY_BUDGET,
    BUILD_QUERY_RADIUS,
    EXPANSION_PLACEMENT_STEP,
    EXPANSION_QUERY_BUDGET,
    EXPANSION_QUERY_RADIUS,
    FAILED_BUILD_COOLDOWN,
    CompetitiveBot,
)
from bots.hunyuan.model import Order
from bots.hunyuan.tests.test_adapter import FakeUnit, FakeUnits, make_bot


def test_expansion_avoids_expand_now_and_pathing_queries():
    bot = make_bot()
    calls: list[dict] = []
    bot.build = _record_build(bot, calls)
    bot.client = SimpleNamespace(
        query_pathing=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pathing query must not be used for expansion")
        )
    )

    asyncio.run(bot._do_expand())

    assert len(calls) == 1
    assert calls[0]["building"] == U.COMMANDCENTER
    assert calls[0]["kwargs"]["max_distance"] == EXPANSION_QUERY_RADIUS
    assert calls[0]["kwargs"]["placement_step"] == EXPANSION_PLACEMENT_STEP
    # Two bounded placement requests, zero pathing requests.
    assert bot._frame_api["placement"] == EXPANSION_QUERY_BUDGET
    assert bot._frame_api["pathing"] == 0


def test_build_uses_bounded_radius_and_one_build_per_step():
    bot = make_bot()
    calls: list[dict] = []
    bot.build = _record_build(bot, calls)

    order = Order(kind="build", unit="SUPPLYDEPOT", near="main")
    asyncio.run(bot._do_build(order))
    asyncio.run(bot._do_build(order))

    assert len(calls) == 1
    assert calls[0]["kwargs"]["max_distance"] == BUILD_QUERY_RADIUS
    assert calls[0]["kwargs"]["placement_step"] == BUILD_PLACEMENT_STEP
    assert bot._frame_api["builds"] == 1
    assert bot._frame_api["placement"] == BUILD_QUERY_BUDGET


def test_identical_move_order_is_not_reissued_in_same_step():
    bot = make_bot()
    marine = bot.all_own_units.find_by_tag(200)
    order = Order(kind="move", tags=(200,), x=60.0, y=60.0)

    asyncio.run(bot._execute(order))
    asyncio.run(bot._execute(order))

    assert len(marine.commands) == 1
    assert bot._frame_api["commands"] == 1

    # Still inside the 0.75s cooldown after half a game second (new step clears
    # the per-frame reservation, as on_step does).
    bot.state.game_loop = 448 + 22.4 * 0.5
    bot._issued_tags.clear()
    asyncio.run(bot._execute(order))
    assert len(marine.commands) == 1

    # After the cooldown the same stance target may be re-issued.
    bot.state.game_loop = 448 + 22.4 * 2
    bot._issued_tags.clear()
    asyncio.run(bot._execute(order))
    assert len(marine.commands) == 2


def test_unit_gets_at_most_one_command_per_step():
    bot = make_bot()
    marine = bot.all_own_units.find_by_tag(200)
    asyncio.run(bot._execute(Order(kind="move", tags=(200,), x=60.0, y=60.0)))
    asyncio.run(bot._execute(Order(kind="attack", tags=(200,), x=150.0, y=150.0)))
    assert len(marine.commands) == 1
    assert marine.commands[0][0] == "move"


def test_non_finite_target_falls_back_to_default():
    bot = make_bot()
    default = Point2((10.0, 10.0))
    assert bot._point(Order(kind="move", x=math.nan, y=10.0), default) == default
    assert bot._point(Order(kind="move", x=math.inf, y=10.0), default) == default
    assert bot._point(Order(kind="move", x=42.0, y=24.0), default) == Point2((42.0, 24.0))


def test_geyser_selection_stays_near_own_base():
    bot = make_bot()
    far = FakeUnit(501, U.VESPENEGEYSER, 140, 140)
    bot.vespene_geyser = FakeUnits([far])
    assert bot._free_geyser(Point2((10.0, 10.0))) is None

    near = FakeUnit(502, U.VESPENEGEYSER, 14, 9)
    bot.vespene_geyser = FakeUnits([far, near])
    assert bot._free_geyser(Point2((10.0, 10.0))).tag == 502


def test_diagnostics_log_is_written_and_is_non_fatal(tmp_path):
    bot = make_bot()
    bot._diag_path = tmp_path / "diag.jsonl"
    bot._diag("probe", value=7)
    asyncio.run(bot.on_step(iteration=0))

    lines = [json.loads(line) for line in bot._diag_path.read_text(encoding="utf-8").splitlines()]
    assert any(line["event"] == "probe" and line["value"] == 7 for line in lines)
    assert any(line["event"] == "frame" and "commands" in line for line in lines)
    assert bot._diag_error == ""


def test_depot_placement_radius_clears_command_center_footprint():
    """The fbe071c radius (4) only probed overlapping tiles, so no depot was ever built."""
    bot = make_bot()
    cc = Point2((10.0, 10.0))
    bot.game_data = SimpleNamespace(
        units={
            U.SUPPLYDEPOT.value: SimpleNamespace(
                creation_ability=SimpleNamespace(id=1)
            )
        }
    )
    # CC is 5x5 and depot 3x3: centres closer than 4 tiles overlap.
    async def placement_query(_ability, positions, ignore_resources=True):
        return [not (abs(p.x - cc.x) < 4 and abs(p.y - cc.y) < 4) for p in positions]

    bot.client = SimpleNamespace(_query_building_placement_fast=placement_query)

    old = asyncio.run(
        bot.find_placement(
            U.SUPPLYDEPOT, near=cc, max_distance=4, placement_step=2,
            random_alternative=False,
        )
    )
    new = asyncio.run(
        bot.find_placement(
            U.SUPPLYDEPOT, near=cc, max_distance=BUILD_QUERY_RADIUS,
            placement_step=BUILD_PLACEMENT_STEP, random_alternative=False,
        )
    )
    assert old is None
    assert new is not None


def test_failed_build_does_not_monopolise_the_single_slot(tmp_path):
    bot = make_bot()
    bot._diag_path = tmp_path / "diag.jsonl"
    calls: list[dict] = []
    bot.build = _record_build(bot, calls, result=False)

    order = Order(kind="build", unit="SUPPLYDEPOT", near="main")
    asyncio.run(bot._do_build(order))
    asyncio.run(bot._do_build(order))
    assert len(calls) == 1

    # A different kind is not blocked by the failed depot's cooldown.
    bot._frame_api["builds"] = 0  # new step, as on_step does
    asyncio.run(bot._do_build(Order(kind="build", unit="BARRACKS", near="main")))
    assert len(calls) == 2

    # The depot may be retried after the cooldown in a later step.
    bot.state.game_loop = 448 + 22.4 * (FAILED_BUILD_COOLDOWN + 0.1)
    bot._frame_api["builds"] = 0
    asyncio.run(bot._do_build(order))
    assert len(calls) == 3


def test_diagnostics_distinguish_queued_from_rejected_build(tmp_path):
    bot = make_bot()
    bot._diag_path = tmp_path / "diag.jsonl"
    order = Order(kind="build", unit="SUPPLYDEPOT", near="main")

    bot.build = _record_build(bot, [], result=False)
    asyncio.run(bot._do_build(order))
    bot.build = _record_build(bot, [], result=True)
    bot.state.game_loop = 448 + 22.4 * (FAILED_BUILD_COOLDOWN + 0.1)
    bot._frame_api["builds"] = 0
    asyncio.run(bot._do_build(order))

    events = [json.loads(line) for line in bot._diag_path.read_text(encoding="utf-8").splitlines()]
    attempts = [e for e in events if e["event"] == "build_attempt"]
    assert len(attempts) == 2
    assert attempts[0]["accepted"] is False
    assert attempts[0]["reason"] == "no_placement_or_worker"
    assert attempts[0]["queued"] == 0
    assert attempts[1]["accepted"] is True
    assert attempts[1]["queued"] == 1


def test_frame_diagnostic_reports_generated_orders_and_queued_actions(tmp_path):
    bot = make_bot()
    bot._diag_path = tmp_path / "diag.jsonl"
    bot.actions.append("queued_command")
    asyncio.run(bot.on_step(iteration=0))
    frame = [
        json.loads(line)
        for line in bot._diag_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "frame"
    ][-1]
    assert frame["actions_queued"] == 1
    assert frame["orders"]
    assert frame["supply"] == "15/38"
    assert frame["workers"] == 1


def _record_build(bot: CompetitiveBot, calls: list[dict], result: bool = True):
    async def build(building, near=None, **kwargs):
        calls.append({"building": building, "near": near, "kwargs": kwargs})
        bot.last_build = (building, near)
        if result:
            bot.actions.append(f"queued:{building.name}")
        return result

    return build
