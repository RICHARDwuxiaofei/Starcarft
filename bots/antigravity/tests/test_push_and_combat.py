"""4-player spawn discovery, proactive pushes, Siege Tank anti-stuck, Bio, Viking, Raven tests."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from bots.antigravity.ag_terran.tactics import ArmyPosture

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


def test_four_player_spawn_discovery_by_elimination_and_parallel_scouts() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=41)
        sim.add_unit(UnitTypeId.BARRACKS, Point2((42.5, 40.5)))
        sim.add_unit(UnitTypeId.REAPER, RED_CITY_SPAWNS[0] + Point2((6.0, 6.0)))
        cmds = await sim.step()

        # SCV and Reaper should scout distinct spawns
        assert sim.bot.scout.scout_worker_tag is not None
        assert sim.bot.scout.reaper_scout_tag is not None
        scv_moves = [
            c.target
            for c in cmds
            if c.unit.tag == sim.bot.scout.scout_worker_tag
            and c.ability == AbilityId.MOVE_MOVE
        ]
        reaper_moves = [
            c.target
            for c in cmds
            if c.unit.tag == sim.bot.scout.reaper_scout_tag
            and c.ability == AbilityId.MOVE_MOVE
        ]
        assert scv_moves and reaper_moves
        assert scv_moves[0].distance_to(reaper_moves[0]) > 20.0

        # Clear 2 of the 3 spawns visibly -> 3rd candidate auto-confirms by elimination!
        sim.visible_points.append((RED_CITY_SPAWNS[1], 12.0))
        sim.visible_points.append((RED_CITY_SPAWNS[2], 12.0))
        await sim.step()
        assert sim.bot.intel.confirmed_enemy_main == RED_CITY_SPAWNS[3]

    asyncio.run(_run())


def test_proactive_timing_advantage_and_max_push_triggers() -> None:
    async def _run() -> None:
        # 1. Stim + 2 Tanks + 2 Medivacs + >= 34 army supply timing push
        sim = OfflineScenario(seed=43)
        sim.upgrades.add(UpgradeId.STIMPACK)
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        for i in range(24):
            sim.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((4.0 + (i % 6) * 0.5, i // 6)),
            )
        for i in range(2):
            sim.add_unit(
                UnitTypeId.SIEGETANK, RED_CITY_EXPANSIONS[1] + Point2((2.0, 2.0 + i))
            )
            sim.add_unit(
                UnitTypeId.MEDIVAC, RED_CITY_EXPANSIONS[1] + Point2((1.0, 2.0 + i))
            )
        sim.visible_points.append((RED_CITY_SPAWNS[2], 12.0))
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[2], alliance=Alliance.Enemy.value
        )

        cmds = await sim.step()
        assert sim.bot.tactics.posture == ArmyPosture.ATTACK
        assert any(c.ability == AbilityId.ATTACK for c in cmds)

        # 2. Force push at near-max even with seen strong enemy
        sim2 = OfflineScenario(seed=44)
        sim2.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        for i in range(60):
            sim2.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((4.0 + (i % 10) * 0.5, i // 10)),
            )
        for i in range(8):
            sim2.add_unit(
                UnitTypeId.SIEGETANK, RED_CITY_EXPANSIONS[1] + Point2((2.0, 1.0 + i))
            )
        await sim2.step()
        assert sim2.bot.tactics.posture == ArmyPosture.ATTACK

    asyncio.run(_run())


def test_siege_tank_sieges_in_combat_and_unsieges_when_clear() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=45)
        sim.upgrades.add(UpgradeId.STIMPACK)
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])
        for i in range(28):
            sim.add_unit(
                UnitTypeId.MARINE,
                RED_CITY_EXPANSIONS[1] + Point2((5.0 + (i % 7) * 0.5, i // 7)),
            )
        tank = sim.add_unit(UnitTypeId.SIEGETANK, Point2((62.0, 34.0)))
        sim.add_unit(UnitTypeId.SIEGETANK, Point2((61.0, 36.0)))
        for i in range(2):
            sim.add_unit(UnitTypeId.MEDIVAC, Point2((60.0, 34.0 + i)))

        enemy_tank = sim.add_unit(
            UnitTypeId.SIEGETANK,
            Point2((72.0, 34.0)),
            alliance=Alliance.Enemy.value,
        )
        sim.visible_points.append((RED_CITY_SPAWNS[1], 12.0))
        sim.add_unit(
            UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[1], alliance=Alliance.Enemy.value
        )

        cmds1 = await sim.step()
        assert any(
            c.unit.tag == tank.tag and c.ability == AbilityId.SIEGEMODE_SIEGEMODE
            for c in cmds1
        )

        # Simulate tank sieged, then enemy killed and area clear for > 4.0s
        tank.unit_type = UnitTypeId.SIEGETANKSIEGED
        sim.remove_unit(enemy_tank.tag)
        await sim.bot.on_unit_destroyed(enemy_tank.tag)
        await sim.step(dt_seconds=1.0)
        cmds_after = await sim.step(dt_seconds=4.5)
        assert any(
            c.unit.tag == tank.tag and c.ability == AbilityId.UNSIEGE_UNSIEGE
            for c in cmds_after
        )

    asyncio.run(_run())


def test_bio_stim_focus_fire_viking_air_ground_and_raven_matrix() -> None:
    async def _run() -> None:
        sim = OfflineScenario(seed=47)
        sim.upgrades.add(UpgradeId.STIMPACK)
        sim.add_unit(UnitTypeId.COMMANDCENTER, RED_CITY_EXPANSIONS[1])

        healthy_marine = sim.add_unit(
            UnitTypeId.MARINE, Point2((60.0, 34.0)), health=45.0
        )
        hurt_marine = sim.add_unit(
            UnitTypeId.MARINE, Point2((60.0, 35.0)), health=12.0
        )  # < 55%
        focus_marine = sim.add_unit(
            UnitTypeId.MARINE, Point2((60.0, 36.0)), health=45.0, weapon_cooldown=0.0
        )
        # Force focus_marine to already have Stimpack? No, verify healthy triggers stim, and test target scoring directly.
        raven = sim.add_unit(UnitTypeId.RAVEN, Point2((58.0, 35.0)), energy=100.0)
        viking = sim.add_unit(UnitTypeId.VIKINGFIGHTER, Point2((61.0, 35.0)))

        enemy_depot = sim.add_unit(
            UnitTypeId.SUPPLYDEPOT, Point2((63.0, 36.0)), alliance=Alliance.Enemy.value
        )
        enemy_tank = sim.add_unit(
            UnitTypeId.SIEGETANKSIEGED,
            Point2((64.0, 36.0)),
            alliance=Alliance.Enemy.value,
        )
        enemy_void = sim.add_unit(
            UnitTypeId.VOIDRAY, Point2((65.0, 35.0)), alliance=Alliance.Enemy.value
        )

        cmds = await sim.step()
        # Healthy marine stims, hurt marine does NOT stim
        assert any(
            c.unit.tag == healthy_marine.tag
            and c.ability == AbilityId.EFFECT_STIM_MARINE
            for c in cmds
        )
        assert not any(
            c.unit.tag == hurt_marine.tag and c.ability == AbilityId.EFFECT_STIM_MARINE
            for c in cmds
        )

        # Raven casts Interference Matrix on enemy Sieged Tank
        assert any(
            c.unit.tag == raven.tag
            and c.ability == AbilityId.EFFECT_INTERFERENCEMATRIX
            and c.target.tag == enemy_tank.tag
            for c in cmds
        )
        # Viking attacks Void Ray in Fighter mode
        assert any(
            c.unit.tag == viking.tag
            and c.ability == AbilityId.ATTACK
            and c.target.tag == enemy_void.tag
            for c in cmds
        )

        # Direct target scoring prefers Sieged Tank over empty Depot
        bot_focus_unit = sim.bot.units.find_by_tag(focus_marine.tag)
        bot_depot = sim.bot.enemy_structures.find_by_tag(enemy_depot.tag)
        bot_tank = sim.bot.enemy_units.find_by_tag(enemy_tank.tag)
        assert (
            bot_focus_unit is not None
            and bot_depot is not None
            and bot_tank is not None
        )
        assert sim.bot.combat._score_bio_target(
            bot_focus_unit, bot_tank
        ) > sim.bot.combat._score_bio_target(bot_focus_unit, bot_depot)

        # Cleanup transition: clear air, clear enemy main -> Viking lands -> then air returns -> Viking lifts
        sim.remove_unit(enemy_void.tag)
        sim.bot.intel.confirmed_enemy_main = RED_CITY_SPAWNS[1]
        sim.bot.intel.enemy_main_cleared = True
        sim.bot.intel.enemy_air_count = 0
        cmds2 = await sim.step()
        assert any(
            c.unit.tag == viking.tag and c.ability == AbilityId.MORPH_VIKINGASSAULTMODE
            for c in cmds2
        )

        viking.unit_type = UnitTypeId.VIKINGASSAULT
        viking.is_flying = False
        sim.add_unit(
            UnitTypeId.MUTALISK, Point2((68.0, 35.0)), alliance=Alliance.Enemy.value
        )
        cmds3 = await sim.step()
        assert any(
            c.unit.tag == viking.tag and c.ability == AbilityId.MORPH_VIKINGFIGHTERMODE
            for c in cmds3
        )

    asyncio.run(_run())
