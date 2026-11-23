"""Regression tests for Round-Robin 2026-11-24 replay findings."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bots.antigravity.ag_terran.tactics import ArmyPosture

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


def _clear_default_workers_and_halls(sim: OfflineScenario) -> None:
    sim.units = [
        u
        for u in sim.units
        if u.unit_type not in {UnitTypeId.SCV, UnitTypeId.COMMANDCENTER}
    ]


def test_oversaturated_workers_do_not_spam_gather_or_ping_pong() -> None:
    async def _run() -> None:
        # Scenario A: 2 saturated bases with 52 SCVs (mirrors deepseek-vs-antigravity at ~6:30).
        sim = OfflineScenario(seed=101)
        sim.reveal_all_map = True
        _clear_default_workers_and_halls(sim)
        for base_idx in (0, 1):
            base_pos = RED_CITY_EXPANSIONS[base_idx]
            sim.add_unit(
                UnitTypeId.ORBITALCOMMAND,
                base_pos,
                energy=0.0,
                ideal_harvesters=16,
            )
            geysers = [
                u
                for u in sim.units
                if u.unit_type == UnitTypeId.VESPENEGEYSER
                and u.pos.distance_to(base_pos) <= 10.5
            ]
            refs = [
                sim.add_unit(
                    UnitTypeId.REFINERY,
                    g.pos,
                    ideal_harvesters=3,
                    vespene_contents=2000,
                )
                for g in geysers[:2]
            ]
            patches = [
                u
                for u in sim.units
                if u.unit_type == UnitTypeId.MINERALFIELD
                and u.pos.distance_to(base_pos) <= 10.5
            ]
            # 12 actively gathering minerals + 4 returning minerals + 4 extra (> ideal 16)
            for i in range(16):
                sim.add_unit(
                    UnitTypeId.SCV,
                    base_pos + Point2((-2.0 + (i % 4) * 0.6, 1.5)),
                    orders=[
                        (AbilityId.HARVEST_GATHER, patches[i % len(patches)].tag, 0.0)
                    ],
                )
            for i in range(4):
                sim.add_unit(
                    UnitTypeId.SCV,
                    base_pos + Point2((2.0, -1.0 + i * 0.5)),
                    buff_ids=[BuffId.CARRYMINERALFIELDMINERALS],
                    orders=[(AbilityId.HARVEST_RETURN_SCV, None, 0.0)],
                )
            # 4 actively gathering gas + 2 returning gas
            for i in range(4):
                sim.add_unit(
                    UnitTypeId.SCV,
                    refs[i % 2].pos + Point2((0.5, 0.5)),
                    orders=[(AbilityId.HARVEST_GATHER, refs[i % 2].tag, 0.0)],
                )
            for i in range(2):
                sim.add_unit(
                    UnitTypeId.SCV,
                    refs[i % len(refs)].pos.towards(base_pos, 2.5),
                    buff_ids=[BuffId.CARRYHARVESTABLEVESPENEGEYSERGAS],
                    orders=[(AbilityId.HARVEST_RETURN_SCV, None, 0.0)],
                )

        # Sufficient depots so supply blocks do not trigger extra builds.
        for i in range(12):
            sim.add_unit(
                UnitTypeId.SUPPLYDEPOT,
                Point2((14.0 + (i % 6) * 2.5, 14.0 + (i // 6) * 2.5)),
            )

        sim.bot.scout._scv_scout_done = True
        sim.minerals = 30
        sim.vespene = 200

        for _ in range(10):
            cmds = await sim.step(dt_seconds=0.5)
            worker_harvest_cmds = [
                c
                for c in cmds
                if c.unit.type_id == UnitTypeId.SCV
                and c.ability in {AbilityId.HARVEST_GATHER, AbilityId.HARVEST_RETURN}
            ]
            assert worker_harvest_cmds == []
            sim.apply_commands_basic(cmds, dt_seconds=0.5)

        # Scenario B: 1 base with 28 SCVs + high gas throttle (mirrors antigravity-vs-codex).
        sim1 = OfflineScenario(seed=102)
        sim1.reveal_all_map = True
        _clear_default_workers_and_halls(sim1)
        base0 = RED_CITY_EXPANSIONS[0]
        sim1.add_unit(UnitTypeId.ORBITALCOMMAND, base0, energy=0.0, ideal_harvesters=16)
        geysers0 = [
            u
            for u in sim1.units
            if u.unit_type == UnitTypeId.VESPENEGEYSER
            and u.pos.distance_to(base0) <= 10.5
        ]
        refs0 = [
            sim1.add_unit(
                UnitTypeId.REFINERY,
                g.pos,
                ideal_harvesters=3,
                vespene_contents=2000,
            )
            for g in geysers0[:2]
        ]
        patches0 = [
            u
            for u in sim1.units
            if u.unit_type == UnitTypeId.MINERALFIELD
            and u.pos.distance_to(base0) <= 10.5
        ]
        for i in range(18):
            sim1.add_unit(
                UnitTypeId.SCV,
                base0 + Point2((-1.5 + (i % 5) * 0.5, 2.0)),
                orders=[
                    (AbilityId.HARVEST_GATHER, patches0[i % len(patches0)].tag, 0.0)
                ],
            )
        for i in range(4):
            sim1.add_unit(
                UnitTypeId.SCV,
                base0 + Point2((2.0, 1.0 + i * 0.4)),
                buff_ids=[BuffId.CARRYMINERALFIELDMINERALS],
                orders=[(AbilityId.HARVEST_RETURN_SCV, None, 0.0)],
            )
        for i in range(4):
            sim1.add_unit(
                UnitTypeId.SCV,
                refs0[i % 2].pos + Point2((0.5, 0.5)),
                orders=[(AbilityId.HARVEST_GATHER, refs0[i % 2].tag, 0.0)],
            )
        carrying_gas_scvs = []
        for i in range(2):
            scv = sim1.add_unit(
                UnitTypeId.SCV,
                refs0[i % len(refs0)].pos.towards(base0, 2.5),
                buff_ids=[BuffId.CARRYHARVESTABLEVESPENEGEYSERGAS],
                orders=[(AbilityId.HARVEST_RETURN_SCV, None, 0.0)],
            )
            carrying_gas_scvs.append(scv.tag)
        for i in range(6):
            sim1.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((16.0 + i * 2.5, 16.0)))

        sim1.bot.scout._scv_scout_done = True
        sim1.minerals = 30
        sim1.vespene = 200
        for _ in range(10):
            cmds1 = await sim1.step(dt_seconds=0.5)
            assert not any(
                c.unit.type_id == UnitTypeId.SCV
                and c.ability in {AbilityId.HARVEST_GATHER, AbilityId.HARVEST_RETURN}
                for c in cmds1
            )

        # Trigger gas throttle (950 gas / 80 minerals) -> carrying vespene SCVs must not be interrupted.
        sim1.vespene = 950
        sim1.minerals = 80
        cmds_throttle = await sim1.step(dt_seconds=0.5)
        interrupted = {
            c.unit.tag
            for c in cmds_throttle
            if c.unit.tag in carrying_gas_scvs
            and c.ability in {AbilityId.HARVEST_GATHER, AbilityId.HARVEST_RETURN}
        }
        assert interrupted == set()

    asyncio.run(_run())


def test_oversaturated_transfer_only_when_new_base_has_deficit() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=103)
        sim.reveal_all_map = True
        _clear_default_workers_and_halls(sim)
        main_pos = RED_CITY_EXPANSIONS[0]
        nat_pos = RED_CITY_EXPANSIONS[1]
        sim.add_unit(
            UnitTypeId.ORBITALCOMMAND, main_pos, energy=0.0, ideal_harvesters=16
        )
        sim.add_unit(UnitTypeId.COMMANDCENTER, nat_pos, ideal_harvesters=16)

        main_patches = [
            u
            for u in sim.units
            if u.unit_type == UnitTypeId.MINERALFIELD
            and u.pos.distance_to(main_pos) <= 10.5
        ]
        nat_patch_tags = {
            u.tag
            for u in sim.units
            if u.unit_type == UnitTypeId.MINERALFIELD
            and u.pos.distance_to(nat_pos) <= 10.5
        }
        for i in range(18):
            sim.add_unit(
                UnitTypeId.SCV,
                main_pos + Point2((-1.0 + (i % 5) * 0.4, 2.0)),
                orders=[
                    (
                        AbilityId.HARVEST_GATHER,
                        main_patches[i % len(main_patches)].tag,
                        0.0,
                    )
                ],
            )
        for i in range(4):
            sim.add_unit(
                UnitTypeId.SCV,
                main_pos + Point2((2.0, -1.0 + i * 0.4)),
                buff_ids=[BuffId.CARRYMINERALFIELDMINERALS],
                orders=[(AbilityId.HARVEST_RETURN_SCV, None, 0.0)],
            )
        for i in range(4):
            sim.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((16.0 + i * 2.5, 16.0)))

        sim.bot.scout._scv_scout_done = True
        sim.minerals = 30

        cmds1 = await sim.step(dt_seconds=0.5)
        gather1 = [c for c in cmds1 if c.ability == AbilityId.HARVEST_GATHER]
        assert len(gather1) == 6
        assert all(
            c.target is not None and c.target.tag in nat_patch_tags for c in gather1
        )

        sim.apply_commands_basic(cmds1, dt_seconds=0.5)
        cmds2 = await sim.step(dt_seconds=0.5)
        assert not any(
            c.ability in {AbilityId.HARVEST_GATHER, AbilityId.HARVEST_RETURN}
            for c in cmds2
        )

    asyncio.run(_run())


def test_fog_of_war_snapshot_natural_and_third_expansion_selection() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=104, fog_snapshot_resources=True)
        sim.bot.scout._scv_scout_done = True
        sim.minerals = 30
        await sim.step(dt_seconds=0.5)

        nat_patches = [
            m
            for m in sim.bot.mineral_field
            if m.distance_to(RED_CITY_EXPANSIONS[1]) <= 10.5
        ]
        assert nat_patches
        assert all(m.is_snapshot and m.mineral_contents == 0 for m in nat_patches)

        choice1 = sim.bot.bases.choose_next_expansion(
            sim.bot, sim.bot.intel, allow_natural_priority=True
        )
        assert choice1 == RED_CITY_EXPANSIONS[1]

        # Occupy natural & reveal enemy main at (128, 32) -> 3rd picks safe fogged (34, 72).
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        sim.visible_points.append((RED_CITY_SPAWNS[1], 12.0))
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[1], alliance=Alliance.Enemy.value
        )
        await sim.step(dt_seconds=0.5)

        choice2 = sim.bot.bases.choose_next_expansion(
            sim.bot, sim.bot.intel, allow_natural_priority=True
        )
        assert choice2 == RED_CITY_EXPANSIONS[3]

        # Reveal and deplete (34, 72) -> must skip visibly empty expansion.
        for u in list(sim.units):
            if (
                u.unit_type == UnitTypeId.MINERALFIELD
                and u.pos.distance_to(RED_CITY_EXPANSIONS[3]) <= 10.5
            ):
                sim.remove_unit(u.tag)
        sim.visible_points.append((RED_CITY_EXPANSIONS[3], 12.0))
        await sim.step(dt_seconds=0.5)

        choice3 = sim.bot.bases.choose_next_expansion(
            sim.bot, sim.bot.intel, allow_natural_priority=True
        )
        assert choice3 is not None
        assert choice3 != RED_CITY_EXPANSIONS[3]

    asyncio.run(_run())


def test_flying_cc_with_remapped_land_order_does_not_oscillate_to_hover() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=105, fog_snapshot_resources=True)
        _clear_default_workers_and_halls(sim)
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[0], energy=0.0)
        flying_cc = sim.add_unit(
            UnitTypeId.COMMANDCENTERFLYING,
            RED_CITY_EXPANSIONS[0] + Point2((3.0, 1.0)),
            is_flying=True,
            orders=[(AbilityId.LAND_COMMANDCENTER, RED_CITY_EXPANSIONS[1], 0.0)],
        )
        sim.visible_points.append((RED_CITY_SPAWNS[1], 12.0))
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[1], alliance=Alliance.Enemy.value
        )
        sim.bot.scout._scv_scout_done = True
        sim.minerals = 30

        move_cmds_issued = 0
        for step_idx in range(15):
            cmds = await sim.step(dt_seconds=1.0)
            if step_idx == 0:
                bot_fly = sim.bot.townhalls.find_by_tag(flying_cc.tag)
                assert bot_fly is not None
                assert bot_fly.orders[0].ability.id == AbilityId.LAND
            for c in cmds:
                if c.unit.tag == flying_cc.tag and c.ability == AbilityId.MOVE_MOVE:
                    move_cmds_issued += 1
            sim.apply_commands_basic(cmds, dt_seconds=1.0)
            if flying_cc.unit_type == UnitTypeId.COMMANDCENTER:
                break

        assert move_cmds_issued == 0
        assert flying_cc.unit_type == UnitTypeId.COMMANDCENTER
        assert flying_cc.pos.distance_to(RED_CITY_EXPANSIONS[1]) <= 2.0

    asyncio.run(_run())


def test_bunker_placement_never_blocks_natural_cc() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=106)
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)))
        sim.add_unit(UnitTypeId.FACTORY, Point2((38.0, 44.0)))
        sim.bot.scout._scv_scout_done = True
        sim.minerals = 30
        await sim.step(dt_seconds=0.5)

        nat = sim.bot.bases.natural_location
        assert nat is not None
        bunker_pos = await sim.bot.placement.find_bunker_position(
            sim.bot, sim.bot.bases, sim.bot.intel
        )
        assert bunker_pos is not None
        assert bunker_pos.distance_to(nat) >= 5.0

        sim.add_unit(UnitTypeId.BUNKER, bunker_pos)
        await sim.step(dt_seconds=0.5)
        assert await sim.bot.can_place_single(UnitTypeId.COMMANDCENTER, nat)

    asyncio.run(_run())


def test_barracks_builds_techlab_and_researches_stimpack_before_concussive_and_marines() -> (
    None
):
    async def _run() -> None:
        # Phase A: Bare idle Barracks builds TechLab before training bare Marines once Reaper exists.
        sim = OfflineScenario(seed=107)
        sim.reveal_all_map = True
        sim.units = [u for u in sim.units if u.unit_type != UnitTypeId.COMMANDCENTER]
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[0], energy=0.0)
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)))
        sim.add_unit(UnitTypeId.FACTORY, Point2((36.0, 44.0)))
        sim.add_unit(UnitTypeId.STARPORT, Point2((36.0, 48.0)))
        sim.add_unit(UnitTypeId.ENGINEERINGBAY, Point2((26.0, 32.0)))
        sim.add_unit(UnitTypeId.REAPER, RED_CITY_SPAWNS[0] + Point2((5.0, 5.0)))
        for i in range(4):
            sim.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((18.0 + i * 2.5, 18.0)))
        sim.bot.scout._scv_scout_done = True
        sim.minerals = 55
        sim.vespene = 30

        cmds = await sim.step(dt_seconds=0.5)
        assert any(c.ability == AbilityId.BUILD_TECHLAB_BARRACKS for c in cmds)
        assert not any(c.ability == AbilityId.BARRACKSTRAIN_MARINE for c in cmds)

        # Phase B & C: With idle ready TechLab, hold minerals at 75m/100g instead of buying
        # Concussive Shells or Marines, then start Stimpack at 110m/100g.
        sim2 = OfflineScenario(seed=108)
        sim2.reveal_all_map = True
        sim2.units = [u for u in sim2.units if u.unit_type != UnitTypeId.COMMANDCENTER]
        sim2.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[0], energy=0.0)
        sim2.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        tl = sim2.add_unit(UnitTypeId.BARRACKSTECHLAB, Point2((45.0, 40.0)))
        sim2.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)), add_on_tag=tl.tag)
        ftl = sim2.add_unit(UnitTypeId.FACTORYTECHLAB, Point2((38.5, 44.0)))
        sim2.add_unit(UnitTypeId.FACTORY, Point2((36.0, 44.0)), add_on_tag=ftl.tag)
        sim2.add_unit(UnitTypeId.STARPORT, Point2((36.0, 48.0)))
        sim2.add_unit(UnitTypeId.BUNKER, Point2((60.0, 34.0)))
        for i in range(4):
            sim2.add_unit(UnitTypeId.SUPPLYDEPOT, Point2((18.0 + i * 2.5, 18.0)))
        sim2.bot.scout._scv_scout_done = True
        sim2.game_loop = int(200 * 22.4)
        sim2.minerals = 75
        sim2.vespene = 100

        cmds_b = await sim2.step(dt_seconds=0.5)
        assert not any(c.ability == AbilityId.RESEARCH_CONCUSSIVESHELLS for c in cmds_b)
        assert not any(c.ability == AbilityId.BARRACKSTRAIN_MARINE for c in cmds_b)

        sim2.minerals = 110
        cmds_c = await sim2.step(dt_seconds=0.5)
        assert any(
            c.ability == AbilityId.BARRACKSTECHLABRESEARCH_STIMPACK for c in cmds_c
        )

    asyncio.run(_run())


def test_endgame_closeout_keeps_attacking_rebuilt_undefended_cc() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=109)
        sim.bot.scout._scv_scout_done = True
        sim.visible_points.append((RED_CITY_SPAWNS[2], 14.0))
        enemy_cc = sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[2], alliance=Alliance.Enemy.value
        )
        for i in range(20):
            sim.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((2.0 + (i % 5) * 0.5, i // 5)),
            )

        # Step 1: confirm enemy main at spawns[2].
        await sim.step(dt_seconds=0.5)
        assert sim.bot.intel.confirmed_enemy_main == RED_CITY_SPAWNS[2]
        assert not sim.bot.intel.enemy_main_cleared

        # Step 2: destroy enemy main CC -> cleared & ever_cleared set.
        sim.remove_unit(enemy_cc.tag)
        await sim.bot.on_unit_destroyed(enemy_cc.tag)
        await sim.step(dt_seconds=0.5)
        assert sim.bot.intel.enemy_main_cleared
        assert sim.bot.intel.enemy_main_ever_cleared
        assert sim.bot.tactics.posture == ArmyPosture.ATTACK

        # Step 3: enemy rebuilds an undefended CC at spawns[2] with 0 combat units -> stay ATTACK.
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[2], alliance=Alliance.Enemy.value
        )
        await sim.step(dt_seconds=0.5)
        assert not sim.bot.intel.enemy_main_cleared
        assert sim.bot.intel.enemy_main_ever_cleared
        assert sim.bot.tactics.posture == ArmyPosture.ATTACK
        assert sim.bot.tactics.target_position is not None
        assert sim.bot.tactics.target_position.distance_to(RED_CITY_SPAWNS[2]) <= 2.0

    asyncio.run(_run())
