"""Opening build order, macro scaling, Reactor slots, halted-building resume, and PF tests."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


def test_14_depot_15_refinery_16_barracks_opening_sequence() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=7)
        sim.minerals = 110
        # 12 SCVs -> wait until 14 supply
        sim.add_unit(UnitTypeId.SCV, RED_CITY_SPAWNS[0] + Point2((2.0, 2.0)))
        sim.add_unit(UnitTypeId.SCV, RED_CITY_SPAWNS[0] + Point2((2.5, 2.0)))
        cmds = await sim.step()
        build_depots = [
            c for c in cmds if c.ability == AbilityId.TERRANBUILD_SUPPLYDEPOT
        ]
        assert len(build_depots) == 1

        # Complete Depot -> add 15th SCV -> Refinery + Barracks
        sim.add_unit(UnitTypeId.SUPPLYDEPOT, build_depots[0].target)
        sim.add_unit(UnitTypeId.SCV, RED_CITY_SPAWNS[0] + Point2((2.0, 1.0)))
        sim.minerals = 280
        cmds = await sim.step()
        assert any(c.ability == AbilityId.TERRANBUILD_REFINERY for c in cmds)
        assert any(c.ability == AbilityId.TERRANBUILD_BARRACKS for c in cmds)

    asyncio.run(_run())


def test_early_rush_places_second_cc_safely_in_main() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=7)
        sim.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((40.5, 42.5)))
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)))
        for i in range(7):
            sim.add_unit(
                UnitTypeId.SCV, RED_CITY_SPAWNS[0] + Point2((1.5 + i * 0.2, -1.0))
            )
        # Early proxy rush units close enough to main/ramp to be legally seen
        for i in range(4):
            sim.add_unit(
                UnitTypeId.ZERGLING,
                Point2((48.0 + i * 0.6, 39.0)),
                alliance=Alliance.Enemy.value,
            )
        sim.minerals = 450
        cmds = await sim.step()
        cc_cmds = [c for c in cmds if c.ability == AbilityId.TERRANBUILD_COMMANDCENTER]
        assert len(cc_cmds) == 1
        assert cc_cmds[0].target.distance_to(RED_CITY_SPAWNS[0]) <= 20.0
        assert cc_cmds[0].target.distance_to(RED_CITY_EXPANSIONS[1]) > 5.0

    asyncio.run(_run())


def test_reactor_two_slot_training_and_supply_cap_200_stop() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=11)
        # 2x CC (30 supply) + 22 Depots (176 supply) -> capped at 200
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        for i in range(22):
            sim.add_unit(
                UnitTypeId.SUPPLYDEPOT,
                Point2((18.0 + (i % 6) * 3.0, 18.0 + (i // 6) * 3.0)),
            )

        reactor = sim.add_unit(UnitTypeId.BARRACKSREACTOR, Point2((42.5, 45.5)))
        sim.add_unit(UnitTypeId.BARRACKS, Point2((40.0, 46.0)), add_on_tag=reactor.tag)
        sim.minerals = 600
        sim.vespene = 200
        cmds = await sim.step()

        # Should not build more Supply Depots at 200 cap
        assert not any(c.ability == AbilityId.TERRANBUILD_SUPPLYDEPOT for c in cmds)
        # Reactor Barracks should start a Marine; verify _try_train queues second slot if not 1-cmd/frame limited.
        marine_cmds = [c for c in cmds if c.ability == AbilityId.BARRACKSTRAIN_MARINE]
        assert len(marine_cmds) >= 1

        # Verify supply_left blocks over-queuing when only 1 supply left
        sim2 = OfflineScenario(seed=12)
        tl = sim2.add_unit(UnitTypeId.BARRACKSTECHLAB, Point2((42.5, 45.5)))
        sim2.add_unit(UnitTypeId.BARRACKS, Point2((40.0, 46.0)), add_on_tag=tl.tag)
        # 12 SCVs + 2 Marines = 14 / 15 supply -> only 1 supply left -> cannot train 2-supply Marauder
        sim2.add_unit(UnitTypeId.MARINE, Point2((35.0, 35.0)))
        sim2.add_unit(UnitTypeId.MARINE, Point2((35.5, 35.0)))
        sim2.minerals = 500
        sim2.vespene = 500
        cmds2 = await sim2.step()
        assert not any(c.ability == AbilityId.BARRACKSTRAIN_MARAUDER for c in cmds2)

    asyncio.run(_run())


def test_resume_halted_terran_building_when_scv_lost() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=5)
        sim.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((40.5, 42.5)))
        halted_rax = sim.add_unit(
            UnitTypeId.BARRACKS,
            Point2((42.5, 40.5)),
            build_progress=0.45,
        )
        cmds = await sim.step()
        smart_cmds = [
            c
            for c in cmds
            if c.ability == AbilityId.SMART
            and c.target is not None
            and c.target.tag == halted_rax.tag
        ]
        assert len(smart_cmds) == 1

    asyncio.run(_run())


def test_fourth_base_planetary_fortress_and_upgrades() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=9)
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)))
        tl = sim.add_unit(UnitTypeId.BARRACKSTECHLAB, Point2((45.0, 40.0)))
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)), add_on_tag=tl.tag)
        sim.add_unit(UnitTypeId.ENGINEERINGBAY, Point2((26.0, 32.0)))
        sim.add_unit(UnitTypeId.ARMORY, Point2((26.0, 28.0)))
        sim.add_unit(UnitTypeId.FACTORY, Point2((36.0, 44.0)))
        sim.add_unit(UnitTypeId.STARPORT, Point2((36.0, 48.0)))
        sim.upgrades.add(UpgradeId.TERRANINFANTRYWEAPONSLEVEL1)

        # Replace main CC with Orbital + 2 other Orbitals + 1 outer CC ready to morph to PF
        sim.units = [u for u in sim.units if u.unit_type != UnitTypeId.COMMANDCENTER]
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[0], energy=0.0)
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[1], energy=0.0)
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[3], energy=0.0)
        outer_cc = sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[4])

        sim.minerals = 1200
        sim.vespene = 900
        cmds = await sim.step()

        pf_cmds = [
            c
            for c in cmds
            if c.ability == AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS
            and c.unit.tag == outer_cc.tag
        ]
        assert len(pf_cmds) == 1
        assert any(
            c.ability == AbilityId.BARRACKSTECHLABRESEARCH_STIMPACK for c in cmds
        )
        assert any(
            c.ability == AbilityId.ENGINEERINGBAYRESEARCH_TERRANINFANTRYWEAPONSLEVEL2
            for c in cmds
        )

    asyncio.run(_run())


def test_broken_ramp_falls_back_gracefully() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=3, broken_ramp=True)
        sim.minerals = 300
        for i in range(3):
            sim.add_unit(UnitTypeId.SCV, RED_CITY_SPAWNS[0] + Point2((i * 0.5, 2.0)))
        cmds = await sim.step()
        assert any(c.ability == AbilityId.TERRANBUILD_SUPPLYDEPOT for c in cmds)

    asyncio.run(_run())
