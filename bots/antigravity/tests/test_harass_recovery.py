"""Harassment defense, evacuation, Orbital scan/turret, depot wall, and post-harass recovery."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance, CloakState
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bots.antigravity.ag_terran.bases import BaseStatus

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


def test_light_harass_pulls_healthy_scvs_and_protects_hurt_scv() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=15)
        # Single Reaper harassing main mineral line with no army.
        sim.add_unit(
            UnitTypeId.REAPER,
            RED_CITY_SPAWNS[0] + Point2((-3.0, 3.0)),
            alliance=Alliance.Enemy.value,
        )
        # Mark one SCV critically injured (< 45% HP)
        scvs = sim.find_units(UnitTypeId.SCV, Alliance.Self.value)
        scvs[0].health = 15.0  # 33% HP

        cmds = await sim.step()
        main_info = next(b for b in sim.bot.bases.bases.values() if b.is_main)
        assert main_info.status == BaseStatus.LIGHT_HARASS
        assert not main_info.evacuate_workers

        attack_scvs = [
            c
            for c in cmds
            if c.unit.type_id == UnitTypeId.SCV and c.ability == AbilityId.ATTACK
        ]
        assert 1 <= len(attack_scvs) <= 4
        assert all(c.unit.tag != scvs[0].tag for c in attack_scvs)

    asyncio.run(_run())


def test_heavy_and_cloaked_harass_evacuates_scans_and_builds_turret() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=17)
        # Replace main CC with Orbital, add safe natural CC + Engineering Bay
        sim.units = [u for u in sim.units if u.unit_type != UnitTypeId.COMMANDCENTER]
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[0], energy=100.0)
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1], ideal_harvesters=16
        )
        sim.add_unit(UnitTypeId.ENGINEERINGBAY, Point2((26.0, 30.0)))

        # Cloaked Banshee harassing main mineral line
        sim.add_unit(
            UnitTypeId.BANSHEE,
            RED_CITY_SPAWNS[0] + Point2((-2.0, 4.0)),
            alliance=Alliance.Enemy.value,
            cloak=CloakState.Cloaked.value,
        )
        sim.minerals = 350
        cmds = await sim.step()

        main_info = next(b for b in sim.bot.bases.bases.values() if b.is_main)
        assert main_info.status == BaseStatus.HEAVY_HARASS
        assert main_info.evacuate_workers

        # Orbital scans the unrevealed Banshee
        assert any(c.ability == AbilityId.SCANNERSWEEP_SCAN for c in cmds)
        # SCVs evacuate to safe natural mineral line
        gather_natural = [
            c
            for c in cmds
            if c.unit.type_id == UnitTypeId.SCV
            and c.ability == AbilityId.HARVEST_GATHER
            and c.target is not None
            and c.target.position.distance_to(RED_CITY_EXPANSIONS[1]) <= 12.0
        ]
        assert len(gather_natural) >= 5
        # Missile Turret queued for defense
        assert any(c.ability == AbilityId.TERRANBUILD_MISSILETURRET for c in cmds)

    asyncio.run(_run())


def test_post_harass_mining_recovery_repair_and_scv_rebuild() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=19)
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1], ideal_harvesters=16
        )
        # Add Refinery at main
        main_geyser = next(
            u
            for u in sim.units
            if u.unit_type == UnitTypeId.VESPENEGEYSER
            and u.pos.distance_to(RED_CITY_SPAWNS[0]) <= 10.0
        )
        sim.add_unit(
            UnitTypeId.REFINERY,
            main_geyser.pos,
            ideal_harvesters=3,
            vespene_contents=2000,
        )

        # Phase 1: Hellion harass hits main -> SCVs evacuate and main CC takes damage
        hellions = [
            sim.add_unit(
                UnitTypeId.HELLION,
                RED_CITY_SPAWNS[0] + Point2((-3.0, 2.0 + i)),
                alliance=Alliance.Enemy.value,
            )
            for i in range(3)
        ]
        main_cc = next(
            u
            for u in sim.units
            if u.unit_type == UnitTypeId.COMMANDCENTER
            and u.pos.distance_to(RED_CITY_SPAWNS[0]) <= 2.0
        )
        main_cc.health = 1000.0  # 66% HP
        await sim.step(dt_seconds=0.5)
        assert next(
            b for b in sim.bot.bases.bases.values() if b.is_main
        ).evacuate_workers

        # Phase 2: Threat eliminated, some SCVs were killed and remaining SCVs became idle
        for h in hellions:
            sim.remove_unit(h.tag)
            await sim.bot.on_unit_destroyed(h.tag)
        scvs = sim.find_units(UnitTypeId.SCV, Alliance.Self.value)
        for dead in scvs[:4]:
            sim.remove_unit(dead.tag)
            await sim.bot.on_unit_destroyed(dead.tag)
        for survivor in sim.find_units(UnitTypeId.SCV, Alliance.Self.value):
            survivor.orders.clear()  # Idle post-harass

        # Advance past harass_clear_delay (2.0s)
        sim.minerals = 300
        cmds_recover = await sim.step(dt_seconds=2.5)
        sim.apply_commands_basic(cmds_recover, dt_seconds=2.0)
        cmds = await sim.step(dt_seconds=2.0)

        main_info = next(b for b in sim.bot.bases.bases.values() if b.is_main)
        assert main_info.status == BaseStatus.SAFE
        assert not main_info.evacuate_workers

        # Remaining SCVs must either have an active order or receive a gather/repair/build command!
        all_cmds = cmds_recover + cmds
        cmd_scvs = {
            c.unit.tag
            for c in all_cmds
            if c.unit.type_id == UnitTypeId.SCV
            and (
                c.ability in {AbilityId.HARVEST_GATHER, AbilityId.EFFECT_REPAIR}
                or c.ability.name.startswith("TERRANBUILD_")
            )
        }
        survivor_tags = {
            u.tag for u in sim.find_units(UnitTypeId.SCV, Alliance.Self.value)
        }
        assert survivor_tags.issubset(cmd_scvs)
        active_or_cmd = {
            u.tag
            for u in sim.find_units(UnitTypeId.SCV, Alliance.Self.value)
            if u.orders
        } | {
            c.unit.tag
            for c in cmds
            if c.unit.type_id == UnitTypeId.SCV
            and (
                c.ability in {AbilityId.HARVEST_GATHER, AbilityId.EFFECT_REPAIR}
                or c.ability.name.startswith("TERRANBUILD_")
            )
        }
        assert survivor_tags.issubset(active_or_cmd)
        # Damaged CC gets repaired and lost SCVs are re-trained
        assert any(c.ability == AbilityId.EFFECT_REPAIR for c in all_cmds)
        assert any(c.ability == AbilityId.COMMANDCENTERTRAIN_SCV for c in all_cmds)

    asyncio.run(_run())


def test_ramp_supply_depot_raises_and_lowers() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=21)
        depot = sim.add_unit(UnitTypeId.SUPPLYDEPOTLOWERED, Point2((40.5, 42.5)))
        ling = sim.add_unit(
            UnitTypeId.ZERGLING,
            Point2((44.5, 42.5)),
            alliance=Alliance.Enemy.value,
        )
        cmds = await sim.step()
        assert any(c.ability == AbilityId.MORPH_SUPPLYDEPOT_RAISE for c in cmds)

        depot.unit_type = UnitTypeId.SUPPLYDEPOT
        sim.remove_unit(ling.tag)
        cmds2 = await sim.step()
        assert any(c.ability == AbilityId.MORPH_SUPPLYDEPOT_LOWER for c in cmds2)

    asyncio.run(_run())
