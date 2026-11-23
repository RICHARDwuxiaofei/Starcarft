"""Multi-phase end-to-end offline simulation across opening, harass, base loss, push, cleanup."""

from __future__ import annotations

import asyncio

from sc2.data import Alliance
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from bots.antigravity.ag_terran.tactics import ArmyPosture

from .mock_sc2 import RED_CITY_EXPANSIONS, RED_CITY_SPAWNS, OfflineScenario


async def _run_full_lifecycle(seed: int) -> tuple[list[str], list[ArmyPosture]]:
    sim = OfflineScenario(seed=seed)
    postures: list[ArmyPosture] = []
    cmd_log: list[str] = []

    def record(cmds: list) -> None:
        postures.append(sim.bot.tactics.posture)
        cmd_log.append(
            ",".join(sorted(f"{c.unit.type_id.name}:{c.ability.name}" for c in cmds))
        )

    # Phase 1: Opening (14-20 supply)
    sim.minerals = 650
    sim.vespene = 150
    for _ in range(8):
        sim.minerals += 55
        sim.vespene += 12
        cmds = await sim.step(dt_seconds=2.0)
        sim.apply_commands_basic(cmds, dt_seconds=2.0)
        record(cmds)

    assert (
        len(sim.find_units(UnitTypeId.SUPPLYDEPOT))
        + len(sim.find_units(UnitTypeId.SUPPLYDEPOTLOWERED))
    ) >= 1
    assert len(sim.find_units(UnitTypeId.BARRACKS)) >= 1

    # Phase 2: 4-player discovery + natural + harass + recovery
    sim.visible_points.append((RED_CITY_SPAWNS[2], 12.0))
    sim.add_unit(
        UnitTypeId.COMMANDCENTER, RED_CITY_SPAWNS[2], alliance=Alliance.Enemy.value
    )
    harasser = sim.add_unit(
        UnitTypeId.HELLION,
        RED_CITY_SPAWNS[0] + Point2((-2.0, 2.5)),
        alliance=Alliance.Enemy.value,
    )
    harasser2 = sim.add_unit(
        UnitTypeId.HELLION,
        RED_CITY_SPAWNS[0] + Point2((-2.5, 3.0)),
        alliance=Alliance.Enemy.value,
    )
    for _ in range(2):
        cmds = await sim.step(dt_seconds=1.0)
        sim.apply_commands_basic(cmds, dt_seconds=1.0)
        record(cmds)

    # Clear harass and verify recovery
    sim.remove_unit(harasser.tag)
    sim.remove_unit(harasser2.tag)
    await sim.bot.on_unit_destroyed(harasser.tag)
    await sim.bot.on_unit_destroyed(harasser2.tag)
    for _ in range(3):
        cmds = await sim.step(dt_seconds=1.5)
        sim.apply_commands_basic(cmds, dt_seconds=1.5)
        record(cmds)

    # Phase 3: Natural occupied, outer 3rd overwhelmed -> triage lift + relocate to safe back 3rd
    if not any(
        u.pos.distance_to(RED_CITY_EXPANSIONS[1]) <= 4.0
        for u in sim.find_units(UnitTypeId.COMMANDCENTER)
        + sim.find_units(UnitTypeId.ORBITALCOMMAND)
    ):
        sim.add_unit(UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[1])
    third_oc = sim.add_unit(
        UnitTypeId.ORBITALCOMMAND, RED_CITY_EXPANSIONS[2], health=850.0
    )
    raiders = [
        sim.add_unit(
            UnitTypeId.SIEGETANKSIEGED,
            RED_CITY_EXPANSIONS[2] + Point2((7.0, i - 1.5)),
            alliance=Alliance.Enemy.value,
        )
        for i in range(4)
    ]
    for _ in range(3):
        cmds = await sim.step(dt_seconds=1.0)
        sim.apply_commands_basic(cmds, dt_seconds=1.0)
        record(cmds)

    assert (
        third_oc.unit_type == UnitTypeId.ORBITALCOMMANDFLYING
        or third_oc.pos.distance_to(RED_CITY_EXPANSIONS[3]) <= 4.0
    )

    # Phase 4: Build decisive push army, clear enemy main, enter cleanup
    for r in raiders:
        sim.remove_unit(r.tag)
        await sim.bot.on_unit_destroyed(r.tag)
    sim.bot.bases.abandoned_expansions.clear()
    sim.upgrades.add(UpgradeId.STIMPACK)
    for i in range(26):
        sim.add_unit(
            UnitTypeId.MARINE,
            RED_CITY_EXPANSIONS[1] + Point2((4.0 + (i % 6) * 0.5, i // 6)),
        )
    for i in range(4):
        sim.add_unit(
            UnitTypeId.SIEGETANK, RED_CITY_EXPANSIONS[1] + Point2((2.0, 2.0 + i))
        )
    for i in range(2):
        sim.add_unit(
            UnitTypeId.MEDIVAC, RED_CITY_EXPANSIONS[1] + Point2((1.0, 2.0 + i))
        )
    sim.add_unit(UnitTypeId.VIKINGFIGHTER, RED_CITY_EXPANSIONS[1] + Point2((2.0, 6.0)))

    cmds_push = await sim.step(dt_seconds=2.0)
    record(cmds_push)
    assert sim.bot.tactics.posture == ArmyPosture.ATTACK
    assert any(c.ability == AbilityId.ATTACK for c in cmds_push)

    # Clear enemy main -> cleanup scout kicks in
    for s in list(sim.find_units(UnitTypeId.COMMANDCENTER, Alliance.Enemy.value)):
        sim.remove_unit(s.tag)
        await sim.bot.on_unit_destroyed(s.tag)
    cmds_clean = await sim.step(dt_seconds=2.0)
    record(cmds_clean)
    assert sim.bot.intel.enemy_main_cleared
    assert len(sim.bot.scout.cleanup_scout_tags) >= 1

    return cmd_log, postures


def test_long_game_lifecycle_and_determinism() -> None:
    log1, postures1 = asyncio.run(_run_full_lifecycle(seed=42))
    log2, postures2 = asyncio.run(_run_full_lifecycle(seed=42))
    assert log1 == log2
    assert postures1 == postures2
    assert ArmyPosture.ATTACK in postures1
