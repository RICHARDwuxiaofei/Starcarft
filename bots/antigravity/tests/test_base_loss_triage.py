"""Outer-base loss triage, lift-off, cancel, safe relocation, and later reclaim tests."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bots.antigravity.ag_terran.bases import BaseStatus
from bots.antigravity.ag_terran.tactics import ArmyPosture

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


def test_overwhelmed_outer_base_lifts_evacuates_and_avoids_suicide_defense() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=31)
        # Main, Natural, and contested forward 3rd at (80, 34)
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1], ideal_harvesters=16
        )
        third_oc = sim.add_unit(
            UnitTypeId.ORBITALCOMMAND,
            RED_CITY_EXPANSIONS[2],
            health=900.0,
            ideal_harvesters=16,
        )
        # SCVs working at 3rd
        third_scvs = [
            sim.add_unit(
                UnitTypeId.SCV, RED_CITY_EXPANSIONS[2] + Point2((-1.5 + i * 0.4, 2.0))
            )
            for i in range(6)
        ]
        # Small home army staging at natural (not enough to beat huge enemy push at 3rd)
        for i in range(4):
            sim.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((3.0, -2.0 + i * 0.6)),
            )

        # Confirmed enemy main at (128, 32) + overwhelming enemy army hitting 3rd
        sim.add_unit(
            UnitTypeId.COMMANDCENTER,
            RED_CITY_SPAWNS[1],
            alliance=Alliance.Enemy.value,
        )
        for i in range(4):
            sim.add_unit(
                UnitTypeId.SIEGETANKSIEGED,
                RED_CITY_EXPANSIONS[2] + Point2((8.0, -2.0 + i * 1.2)),
                alliance=Alliance.Enemy.value,
            )
            sim.add_unit(
                UnitTypeId.MARAUDER,
                RED_CITY_EXPANSIONS[2] + Point2((6.0, -1.0 + i * 1.0)),
                alliance=Alliance.Enemy.value,
            )

        cmds = await sim.step()

        # 3rd classified OVERWHELMED, OC lifts, SCVs evacuate, army does NOT trickle into DEFEND.
        assert sim.bot.bases.bases[third_oc.tag].status == BaseStatus.OVERWHELMED
        assert sim.bot.tactics.posture != ArmyPosture.DEFEND
        assert any(
            c.unit.tag == third_oc.tag and c.ability == AbilityId.LIFT_ORBITALCOMMAND
            for c in cmds
        )
        evac_tags = {
            c.unit.tag
            for c in cmds
            if c.ability == AbilityId.HARVEST_GATHER
            and c.target is not None
            and c.target.position.distance_to(RED_CITY_EXPANSIONS[2]) > 15.0
        }
        assert len(evac_tags) == len(third_scvs)

    asyncio.run(_run())


def test_dying_unfinished_outer_cc_cancels_and_lifted_oc_lands_at_safer_base() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=33)
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1], ideal_harvesters=16
        )
        dying_cc = sim.add_unit(
            UnitTypeId.COMMANDCENTER,
            RED_CITY_EXPANSIONS[2],
            build_progress=0.45,
            health=500.0,  # < 45% of 1500
        )
        flying_oc = sim.add_unit(
            UnitTypeId.ORBITALCOMMANDFLYING,
            RED_CITY_EXPANSIONS[2] + Point2((-5.0, 2.0)),
            is_flying=True,
        )
        # Confirm enemy main is at bottom-right (128, 32)
        sim.visible_points.append((RED_CITY_SPAWNS[1], 12.0))
        sim.add_unit(
            UnitTypeId.COMMANDCENTER,
            RED_CITY_SPAWNS[1],
            alliance=Alliance.Enemy.value,
        )
        # Overwhelming force at forward 3rd (80, 34)
        for i in range(5):
            sim.add_unit(
                UnitTypeId.STALKER,
                RED_CITY_EXPANSIONS[2] + Point2((6.0, -2.0 + i)),
                alliance=Alliance.Enemy.value,
            )

        cmds = await sim.step()
        # Dying unfinished CC cancels to save minerals
        assert any(
            c.unit.tag == dying_cc.tag and c.ability == AbilityId.CANCEL_BUILDINPROGRESS
            for c in cmds
        )
        # Flying OC chooses the safe back/side 3rd (34, 72) away from (128, 32), not (80, 34)
        land_cmds = [
            c
            for c in cmds
            if c.unit.tag == flying_oc.tag
            and c.ability == AbilityId.LAND_ORBITALCOMMAND
        ]
        assert len(land_cmds) == 1
        assert land_cmds[0].target.distance_to(RED_CITY_EXPANSIONS[3]) <= 2.0

    asyncio.run(_run())


def test_reclaim_triggered_once_army_outmatches_abandoned_base_threat() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=35)
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        third_oc = sim.add_unit(
            UnitTypeId.ORBITALCOMMAND,
            RED_CITY_EXPANSIONS[2],
            health=900.0,
        )
        # Record enemy threat at (80, 34)
        enemy_tank = sim.add_unit(
            UnitTypeId.SIEGETANKSIEGED,
            RED_CITY_EXPANSIONS[2] + Point2((7.0, 0.0)),
            alliance=Alliance.Enemy.value,
        )
        enemy_mar = sim.add_unit(
            UnitTypeId.MARAUDER,
            RED_CITY_EXPANSIONS[2] + Point2((6.0, 1.0)),
            alliance=Alliance.Enemy.value,
        )
        enemy_mar2 = sim.add_unit(
            UnitTypeId.MARAUDER,
            RED_CITY_EXPANSIONS[2] + Point2((6.0, -1.0)),
            alliance=Alliance.Enemy.value,
        )
        cmds1 = await sim.step()
        sim.apply_commands_basic(cmds1)
        assert (
            round(RED_CITY_EXPANSIONS[2].x),
            round(RED_CITY_EXPANSIONS[2].y),
        ) in sim.bot.bases.abandoned_expansions

        # Move enemy slightly out of direct townhall vision (still remembered in abandoned_expansions)
        enemy_tank.pos = RED_CITY_EXPANSIONS[2] + Point2((8.0, 0.0))
        enemy_mar.pos = RED_CITY_EXPANSIONS[2] + Point2((8.0, 1.0))
        enemy_mar2.pos = RED_CITY_EXPANSIONS[2] + Point2((8.0, -1.0))
        sim.remove_unit(third_oc.tag)  # Assume it lifted and relocated

        # Build strong army at natural -> should trigger RECLAIM toward (80, 34)
        for i in range(16):
            sim.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((2.0 + (i % 4) * 0.5, i // 4)),
            )
        for i in range(3):
            sim.add_unit(
                UnitTypeId.SIEGETANK, RED_CITY_EXPANSIONS[1] + Point2((1.0, 3.0 + i))
            )
        for i in range(2):
            sim.add_unit(
                UnitTypeId.MEDIVAC, RED_CITY_EXPANSIONS[1] + Point2((0.0, 2.0 + i))
            )

        await sim.step()
        assert sim.bot.tactics.posture == ArmyPosture.RECLAIM
        assert sim.bot.tactics.target_position is not None
        assert (
            sim.bot.tactics.target_position.distance_to(RED_CITY_EXPANSIONS[2]) <= 3.0
        )

    asyncio.run(_run())
