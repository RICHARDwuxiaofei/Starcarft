"""Macro production, build order, adaptive composition, upgrades, and expansion control."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit

from .bases import BaseManager, BaseStatus
from .config import (
    DEPOT_TYPES,
    TANK_TYPES,
    TOWNHALL_ALL_TYPES,
    TOWNHALL_LANDED_TYPES,
    VIKING_TYPES,
    BotConfig,
)
from .economy import EconomyManager
from .intel import IntelTracker
from .placement import PlacementManager

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


class MacroManager:
    """Drives resilient Terran macro from 1-1-1 opener through 4+ base late game."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._issued_this_step: Counter[UnitTypeId] = Counter()

    def _count_planned(
        self,
        bot: BotAI,
        unit_types: UnitTypeId | set[UnitTypeId] | frozenset[UnitTypeId],
        build_ability: AbilityId,
    ) -> int:
        """Count existing (ready + in-progress) structures plus workers en route to build."""
        type_set = (
            {unit_types} if isinstance(unit_types, UnitTypeId) else set(unit_types)
        )
        existing = list(bot.structures(type_set))
        en_route = 0
        for w in bot.workers:
            for order in w.orders:
                if order.ability.id != build_ability:
                    continue
                target = order.target
                if isinstance(target, Point2) and any(
                    not s.is_ready and s.distance_to(target) <= 2.0 for s in existing
                ):
                    continue
                en_route += 1
                break
        extra = sum(self._issued_this_step[t] for t in type_set)
        return len(existing) + en_route + extra

    def _count_addons(
        self,
        bot: BotAI,
        parents: list[Unit],
        addon_type: UnitTypeId,
        ability_ids: set[AbilityId],
    ) -> int:
        """Count completed/building addons plus parent orders starting one."""
        count = len(bot.structures(addon_type))
        for p in parents:
            if any(o.ability.id in ability_ids for o in p.orders):
                count += 1
        return count + self._issued_this_step[addon_type]

    def _try_train(self, bot: BotAI, structure: Unit, unit_type: UnitTypeId) -> bool:
        """Train a unit while updating burnysc2 supply counters immediately."""
        if structure.tag in bot.unit_tags_received_action:
            return False
        if not bot.can_afford(unit_type):
            return False
        supply_cost = bot.calculate_supply_cost(unit_type)
        if supply_cost > 0 and bot.supply_left < supply_cost:
            return False
        result = structure.train(unit_type)
        if result is False:
            return False
        if supply_cost > 0:
            bot.supply_left -= supply_cost
            bot.supply_used += supply_cost
        return True

    def _try_build_addon(
        self,
        bot: BotAI,
        structure: Unit,
        addon_generic: UnitTypeId,
        addon_specific: UnitTypeId,
        ability: AbilityId,
    ) -> bool:
        """Build a TechLab or Reactor on a parent structure and deduct its exact cost."""
        if structure.tag in bot.unit_tags_received_action:
            return False
        if not bot.can_afford(addon_generic, check_supply_cost=False):
            return False
        cost = bot.calculate_cost(addon_generic)
        structure(ability, subtract_cost=False)
        bot.minerals -= cost.minerals
        bot.vespene -= cost.vespene
        self._issued_this_step[addon_specific] += 1
        return True

    async def _try_build_structure(
        self,
        bot: BotAI,
        economy: EconomyManager,
        building_type: UnitTypeId,
        pos: Point2 | Unit | None,
        scout_worker_tag: int | None,
        allow_evacuated: bool = False,
    ) -> bool:
        """Order a free healthy SCV to build a structure at pos (or geyser Unit)."""
        if pos is None or not bot.can_afford(building_type, check_supply_cost=False):
            return False
        near = pos.position if isinstance(pos, Unit) else pos
        worker = economy.select_builder(
            bot, near, scout_worker_tag, allow_evacuated=allow_evacuated
        )
        if worker is None:
            return False
        # Replace any gather/return order queued earlier in this same frame.
        if worker.tag in bot.unit_tags_received_action:
            bot.actions = [a for a in bot.actions if a.unit.tag != worker.tag]
            bot.unit_tags_received_action.discard(worker.tag)
        if isinstance(pos, Unit):
            worker.build_gas(pos)
        else:
            worker.build(building_type, pos)
        self._issued_this_step[building_type] += 1
        return True

    async def update(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        placement: PlacementManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Execute build order, workers, expansions, production, addons, upgrades, and army."""
        self._issued_this_step.clear()

        await self._manage_supply_depots(
            bot, base_mgr, placement, economy, scout_worker_tag
        )
        total_ccs = self._count_planned(
            bot, TOWNHALL_ALL_TYPES, AbilityId.TERRANBUILD_COMMANDCENTER
        )
        if total_ccs < 2 and bot.minerals >= 400:
            await self._manage_core_build_and_expansions(
                bot, intel, base_mgr, placement, economy, scout_worker_tag
            )
            self._morph_townhalls(bot)
            self._train_workers(bot, base_mgr)
        else:
            self._morph_townhalls(bot)
            self._train_workers(bot, base_mgr)
            await self._manage_core_build_and_expansions(
                bot, intel, base_mgr, placement, economy, scout_worker_tag
            )
        await self._manage_urgent_turrets(
            bot, intel, base_mgr, placement, economy, scout_worker_tag
        )
        if self._allow_non_cc_spend(bot, intel):
            self._train_army(bot, intel)
            self._manage_upgrades(bot)
            await self._manage_refineries(bot, base_mgr, economy, scout_worker_tag)
            await self._manage_production_and_tech(
                bot, intel, base_mgr, placement, economy, scout_worker_tag
            )
            await self._manage_addons(bot, intel)

    def _allow_non_cc_spend(self, bot: BotAI, intel: IntelTracker) -> bool:
        """Avoid draining saved minerals right before 2nd Command Center at 19+ supply."""
        if intel.enemy_is_rushing:
            return True
        total_ccs = self._count_planned(
            bot, TOWNHALL_ALL_TYPES, AbilityId.TERRANBUILD_COMMANDCENTER
        )
        total_rax = self._count_planned(
            bot, UnitTypeId.BARRACKS, AbilityId.TERRANBUILD_BARRACKS
        )
        return not (
            total_ccs < 2
            and total_rax >= 1
            and bot.supply_used >= 19
            and bot.minerals < 450
        )

    async def _manage_supply_depots(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        placement: PlacementManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        total_depots = self._count_planned(
            bot, DEPOT_TYPES, AbilityId.TERRANBUILD_SUPPLYDEPOT
        )
        if total_depots == 0:
            if bot.supply_used >= 14 and bot.can_afford(
                UnitTypeId.SUPPLYDEPOT, check_supply_cost=False
            ):
                pos = await placement.find_depot_position(bot, base_mgr)
                await self._try_build_structure(
                    bot, economy, UnitTypeId.SUPPLYDEPOT, pos, scout_worker_tag
                )
            return

        if bot.supply_cap >= 200:
            return

        pending_depots = total_depots - len(bot.structures(DEPOT_TYPES).ready)

        if bot.supply_cap + pending_depots * 8 >= 200:
            return

        prod_count = (
            len(bot.structures(UnitTypeId.BARRACKS))
            + len(bot.structures(UnitTypeId.FACTORY))
            + len(bot.structures(UnitTypeId.STARPORT))
        )
        buffer_target = min(14, 3 + max(1, prod_count) * 2)
        max_parallel = (
            1 if bot.supply_used < 45 else (2 if bot.supply_used < 100 else 3)
        )

        # Ensure second depot starts around 20-22 supply to complete ramp wall & avoid block.
        if total_depots == 1 and len(bot.townhalls) >= 2 and bot.supply_left <= 4:
            buffer_target = max(buffer_target, 5)

        # Save minerals for 2nd Command Center before optional buffer depot unless near supply block.
        total_ccs = self._count_planned(
            bot, TOWNHALL_ALL_TYPES, AbilityId.TERRANBUILD_COMMANDCENTER
        )
        total_rax = self._count_planned(
            bot, UnitTypeId.BARRACKS, AbilityId.TERRANBUILD_BARRACKS
        )
        if (
            total_ccs < 2
            and total_rax >= 1
            and bot.supply_used >= 19
            and bot.supply_left >= 2
            and bot.minerals >= 340
        ):
            return

        if (
            bot.supply_left <= buffer_target
            and pending_depots < max_parallel
            and bot.can_afford(UnitTypeId.SUPPLYDEPOT, check_supply_cost=False)
        ):
            pos = await placement.find_depot_position(bot, base_mgr)
            await self._try_build_structure(
                bot, economy, UnitTypeId.SUPPLYDEPOT, pos, scout_worker_tag
            )

    def _morph_townhalls(self, bot: BotAI) -> None:
        """Upgrade first 3 CCs to Orbitals and 4th+ CCs to Planetary Fortresses."""
        ccs = [
            th
            for th in bot.townhalls
            if th.type_id == UnitTypeId.COMMANDCENTER
            and th.is_ready
            and th.is_idle
            and th.tag not in bot.unit_tags_received_action
        ]
        if not ccs:
            return

        oc_and_pf_count = len(
            bot.townhalls(
                {
                    UnitTypeId.ORBITALCOMMAND,
                    UnitTypeId.ORBITALCOMMANDFLYING,
                    UnitTypeId.PLANETARYFORTRESS,
                }
            )
        )
        ebay_ready = bot.tech_requirement_progress(UnitTypeId.PLANETARYFORTRESS) >= 1.0
        anchor = bot.start_location

        # Morph oldest/closest CCs first.
        ccs.sort(key=lambda c: c.distance_to(anchor))
        for cc in ccs:
            is_outer_fourth = oc_and_pf_count >= 3 and cc.distance_to(anchor) > 15.0
            if (
                is_outer_fourth
                and ebay_ready
                and bot.can_afford(
                    UnitTypeId.PLANETARYFORTRESS, check_supply_cost=False
                )
            ):
                cc(
                    AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS,
                    subtract_cost=True,
                )
                oc_and_pf_count += 1
                continue
            if bot.tech_requirement_progress(
                UnitTypeId.ORBITALCOMMAND
            ) >= 1.0 and bot.can_afford(
                UnitTypeId.ORBITALCOMMAND, check_supply_cost=False
            ):
                cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND, subtract_cost=True)
                oc_and_pf_count += 1

    def _train_workers(self, bot: BotAI, base_mgr: BaseManager) -> None:
        """Keep SCV production continuous up to safe active base saturation."""
        safe_mineral_bases = sum(
            1
            for b in base_mgr.bases.values()
            if b.status != BaseStatus.OVERWHELMED and b.remaining_minerals >= 300
        )
        ready_refs = len(bot.gas_buildings.ready)
        target_workers = min(
            self.config.max_workers,
            max(22, safe_mineral_bases * 18 + ready_refs * 3 + 2),
        )
        current_workers = len(bot.workers) + int(bot.already_pending(UnitTypeId.SCV))
        if current_workers >= target_workers:
            return

        # Yield 1st CC briefly once Barracks finishes so Orbital morph or 2nd CC fires cleanly.
        rax_ready = bool(bot.structures(UnitTypeId.BARRACKS).ready)
        ccs_ready = len(bot.townhalls(UnitTypeId.COMMANDCENTER).ready)
        ocs_total = len(
            bot.townhalls({UnitTypeId.ORBITALCOMMAND, UnitTypeId.ORBITALCOMMANDFLYING})
        )
        total_ccs = self._count_planned(
            bot, TOWNHALL_ALL_TYPES, AbilityId.TERRANBUILD_COMMANDCENTER
        )
        saving_for_nat_cc = (
            total_ccs < 2
            and bool(bot.structures(UnitTypeId.BARRACKS))
            and bot.supply_used >= 19
            and 340 <= bot.minerals < 460
        )
        if saving_for_nat_cc:
            return

        for th in bot.townhalls:
            if (
                th.type_id not in TOWNHALL_LANDED_TYPES
                or not th.is_ready
                or not th.is_idle
            ):
                continue
            info = base_mgr.bases.get(th.tag)
            if info is not None and info.status == BaseStatus.OVERWHELMED:
                continue
            if (
                th.type_id == UnitTypeId.COMMANDCENTER
                and rax_ready
                and ocs_total == 0
                and ccs_ready == 1
                and bot.minerals >= 100
            ):
                continue
            self._try_train(bot, th, UnitTypeId.SCV)

    async def _resolve_cc_placement(
        self, bot: BotAI, preferred: Point2
    ) -> Point2 | None:
        if await bot.can_place_single(UnitTypeId.COMMANDCENTER, preferred):
            return preferred
        return await bot.find_placement(
            UnitTypeId.COMMANDCENTER,
            near=preferred,
            max_distance=6,
            random_alternative=False,
            placement_step=1,
        )

    async def _manage_core_build_and_expansions(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        placement: PlacementManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        total_rax = self._count_planned(
            bot, UnitTypeId.BARRACKS, AbilityId.TERRANBUILD_BARRACKS
        )
        total_ccs = self._count_planned(
            bot,
            {
                UnitTypeId.COMMANDCENTER,
                UnitTypeId.COMMANDCENTERFLYING,
                UnitTypeId.ORBITALCOMMAND,
                UnitTypeId.ORBITALCOMMANDFLYING,
                UnitTypeId.PLANETARYFORTRESS,
            },
            AbilityId.TERRANBUILD_COMMANDCENTER,
        )
        total_fact = self._count_planned(
            bot, UnitTypeId.FACTORY, AbilityId.TERRANBUILD_FACTORY
        )
        total_sp = self._count_planned(
            bot, UnitTypeId.STARPORT, AbilityId.TERRANBUILD_STARPORT
        )
        total_bunkers = self._count_planned(
            bot, UnitTypeId.BUNKER, AbilityId.TERRANBUILD_BUNKER
        )

        # 1. First Barracks after first Depot.
        if total_rax == 0 and bot.tech_requirement_progress(UnitTypeId.BARRACKS) >= 1.0:
            if bot.can_afford(UnitTypeId.BARRACKS, check_supply_cost=False):
                pos = await placement.find_production_position(
                    bot, UnitTypeId.BARRACKS, base_mgr
                )
                await self._try_build_structure(
                    bot, economy, UnitTypeId.BARRACKS, pos, scout_worker_tag
                )
            return

        # 2. Natural expansion (2nd CC) — build safely in main if early rush detected.
        if total_ccs < 2 and total_rax >= 1 and bot.supply_used >= 19:
            if bot.can_afford(UnitTypeId.COMMANDCENTER, check_supply_cost=False):
                target = None
                if not intel.enemy_is_rushing:
                    cand = base_mgr.choose_next_expansion(
                        bot, intel, allow_natural_priority=True
                    )
                    if cand is not None:
                        target = await self._resolve_cc_placement(bot, cand)
                if target is None:
                    target = await bot.find_placement(
                        UnitTypeId.COMMANDCENTER,
                        near=base_mgr.main_location or bot.start_location,
                        max_distance=18,
                        random_alternative=False,
                        placement_step=1,
                    )
                if target is not None and await self._try_build_structure(
                    bot, economy, UnitTypeId.COMMANDCENTER, target, scout_worker_tag
                ):
                    base_mgr.reserve_expansion(target, float(bot.time) + 12.0)
            if bot.minerals < 420 and not intel.enemy_is_rushing:
                return

        # 3. First Factory.
        if (
            total_fact == 0
            and bot.tech_requirement_progress(UnitTypeId.FACTORY) >= 1.0
            and (total_ccs >= 2 or intel.enemy_is_rushing)
            and bot.can_afford(UnitTypeId.FACTORY, check_supply_cost=False)
        ):
            pos = await placement.find_production_position(
                bot, UnitTypeId.FACTORY, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.FACTORY, pos, scout_worker_tag
            )

        # 4. Defensive natural Bunker early.
        if (
            total_bunkers == 0
            and total_ccs >= 2
            and total_rax >= 1
            and bot.time < 360.0
            and (total_fact >= 1 or intel.enemy_is_rushing)
            and bot.can_afford(UnitTypeId.BUNKER, check_supply_cost=False)
        ):
            pos = await placement.find_bunker_position(bot, base_mgr, intel)
            await self._try_build_structure(
                bot, economy, UnitTypeId.BUNKER, pos, scout_worker_tag
            )

        # 5. First Starport.
        if (
            total_sp == 0
            and bot.tech_requirement_progress(UnitTypeId.STARPORT) >= 1.0
            and bot.can_afford(UnitTypeId.STARPORT, check_supply_cost=False)
        ):
            pos = await placement.find_production_position(
                bot, UnitTypeId.STARPORT, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.STARPORT, pos, scout_worker_tag
            )

        # 6. 3rd, 4th, 5th+ expansions based on economy, time, or depleted bases.
        active_healthy_bases = sum(
            1
            for b in base_mgr.bases.values()
            if b.status != BaseStatus.OVERWHELMED and b.remaining_minerals >= 1500
        )
        want_3rd = (
            total_ccs == 2
            and total_rax >= 1
            and (
                bot.time >= 285.0
                or (len(bot.workers) >= 34 and bot.supply_army >= 16)
                or bot.minerals >= 700
            )
        )
        want_4th_plus = (
            3 <= total_ccs < 6
            and total_fact >= 1
            and (
                active_healthy_bases < total_ccs
                or bot.time >= 480.0 + (total_ccs - 3) * 150.0
                or (len(bot.workers) >= 50 and bot.supply_army >= 38)
                or bot.minerals >= 900
            )
        )
        if (want_3rd or want_4th_plus) and bot.can_afford(
            UnitTypeId.COMMANDCENTER, check_supply_cost=False
        ):
            cand_exp = base_mgr.choose_next_expansion(
                bot, intel, allow_natural_priority=True
            )
            exp_pos = (
                await self._resolve_cc_placement(bot, cand_exp) if cand_exp else None
            )
            if exp_pos is None and bot.minerals >= 750:
                exp_pos = await bot.find_placement(
                    UnitTypeId.COMMANDCENTER,
                    near=base_mgr.main_location or bot.start_location,
                    max_distance=20,
                    random_alternative=False,
                    placement_step=1,
                )
            if exp_pos is not None and await self._try_build_structure(
                bot, economy, UnitTypeId.COMMANDCENTER, exp_pos, scout_worker_tag
            ):
                base_mgr.reserve_expansion(exp_pos, float(bot.time) + 12.0)

    async def _manage_refineries(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        if bot.vespene >= 900 and bot.minerals < 350:
            return

        total_refs = self._count_planned(
            bot,
            {UnitTypeId.REFINERY, UnitTypeId.REFINERYRICH},
            AbilityId.TERRANBUILD_REFINERY,
        )
        total_rax = self._count_planned(
            bot, UnitTypeId.BARRACKS, AbilityId.TERRANBUILD_BARRACKS
        )
        total_fact = self._count_planned(
            bot, UnitTypeId.FACTORY, AbilityId.TERRANBUILD_FACTORY
        )
        ready_safe_halls = [
            th for th in base_mgr.get_safe_landed_townhalls(bot) if th.is_ready
        ]

        if total_refs == 0:
            desired_refs = 1 if bot.supply_used >= 15 else 0
        elif total_refs == 1:
            desired_refs = (
                2
                if (total_fact >= 1 or (total_rax >= 1 and bot.supply_used >= 22))
                else 1
            )
        else:
            saturated_halls = sum(
                1
                for th in ready_safe_halls
                if th.assigned_harvesters >= 10 or len(bot.townhalls) >= 3
            )
            desired_refs = min(
                6, max(2, min(len(ready_safe_halls), saturated_halls + 1) * 2)
            )

        if total_refs >= desired_refs:
            return
        if not bot.can_afford(UnitTypeId.REFINERY, check_supply_cost=False):
            return

        for th in ready_safe_halls:
            geysers = [
                g
                for g in bot.vespene_geyser
                if g.distance_to(th.position) <= 10.5
                and not any(r.distance_to(g.position) < 1.5 for r in bot.gas_buildings)
            ]
            if not geysers:
                continue
            target_geyser = min(geysers, key=lambda g: g.distance_to(th.position))
            if await self._try_build_structure(
                bot, economy, UnitTypeId.REFINERY, target_geyser, scout_worker_tag
            ):
                break

    async def _manage_urgent_turrets(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        placement: PlacementManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Prioritize Missile Turrets immediately if cloak or air threats are spotted."""
        if (
            bot.tech_requirement_progress(UnitTypeId.MISSILETURRET) < 1.0
            or not (intel.enemy_has_cloak or intel.enemy_has_air_threat)
            or not bot.can_afford(UnitTypeId.MISSILETURRET, check_supply_cost=False)
        ):
            return

        safe_bases = [
            b
            for b in base_mgr.bases.values()
            if b.status != BaseStatus.OVERWHELMED and b.remaining_minerals >= 300
        ]
        # Prefer building at threatened base or safe base with nearby SCVs first.
        safe_bases.sort(
            key=lambda b: (
                0
                if b.status in {BaseStatus.HEAVY_HARASS, BaseStatus.LIGHT_HARASS}
                else 1,
                0 if b.is_main else 1,
            )
        )
        existing_turrets = list(bot.structures(UnitTypeId.MISSILETURRET))
        for binfo in safe_bases:
            if any(t.distance_to(binfo.location) <= 13.0 for t in existing_turrets):
                continue
            pos = await placement.find_turret_position(bot, binfo.location)
            if await self._try_build_structure(
                bot,
                economy,
                UnitTypeId.MISSILETURRET,
                pos,
                scout_worker_tag,
                allow_evacuated=True,
            ):
                break

    async def _manage_production_and_tech(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        placement: PlacementManager,
        economy: EconomyManager,
        scout_worker_tag: int | None,
    ) -> None:
        safe_bases = [
            b
            for b in base_mgr.bases.values()
            if b.status != BaseStatus.OVERWHELMED and b.remaining_minerals >= 300
        ]
        base_count = max(1, len(safe_bases))
        ready_ccs = len(bot.townhalls.ready)

        total_rax = self._count_planned(
            bot, UnitTypeId.BARRACKS, AbilityId.TERRANBUILD_BARRACKS
        )
        total_fact = self._count_planned(
            bot, UnitTypeId.FACTORY, AbilityId.TERRANBUILD_FACTORY
        )
        total_sp = self._count_planned(
            bot, UnitTypeId.STARPORT, AbilityId.TERRANBUILD_STARPORT
        )
        total_ebay = self._count_planned(
            bot, UnitTypeId.ENGINEERINGBAY, AbilityId.TERRANBUILD_ENGINEERINGBAY
        )
        total_armory = self._count_planned(
            bot, UnitTypeId.ARMORY, AbilityId.TERRANBUILD_ARMORY
        )

        # Engineering Bays (for +1/+1 -> +3/+3 infantry & turrets).
        desired_ebays = 0
        if ready_ccs >= 2 or bot.time >= 220.0:
            desired_ebays = (
                2
                if (base_count >= 2 and bot.time >= 260.0) or bot.minerals >= 400
                else 1
            )
        if total_ebay < desired_ebays and bot.can_afford(
            UnitTypeId.ENGINEERINGBAY, check_supply_cost=False
        ):
            pos = await placement.find_tech_position(
                bot, UnitTypeId.ENGINEERINGBAY, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.ENGINEERINGBAY, pos, scout_worker_tag
            )

        # Missile Turrets against air/cloak harass or mid-game drop defense.
        if (
            bot.tech_requirement_progress(UnitTypeId.MISSILETURRET) >= 1.0
            and (
                intel.enemy_has_air_threat
                or intel.enemy_has_cloak
                or (bot.time >= 390.0 and bot.minerals >= 350)
            )
            and bot.can_afford(UnitTypeId.MISSILETURRET, check_supply_cost=False)
        ):
            existing_turrets = list(bot.structures(UnitTypeId.MISSILETURRET))
            for binfo in safe_bases:
                has_turret = any(
                    t.distance_to(binfo.location) <= 13.0 for t in existing_turrets
                )
                if has_turret:
                    continue
                pos = await placement.find_turret_position(bot, binfo.location)
                if await self._try_build_structure(
                    bot, economy, UnitTypeId.MISSILETURRET, pos, scout_worker_tag
                ):
                    break

        # Scale Barracks: 1 -> 3 (2 bases) -> 5 (3 bases) -> up to 8 (late-game float).
        desired_rax = 1
        if len(bot.townhalls) >= 2 and (total_sp >= 1 or bot.minerals >= 300):
            desired_rax = 3
        if base_count >= 3 or (bot.supply_used >= 95 and bot.minerals >= 450):
            desired_rax = max(desired_rax, 5)
        if base_count >= 4 or bot.minerals >= 800:
            desired_rax = min(8, desired_rax + 3)

        if (
            total_rax < desired_rax
            and bot.tech_requirement_progress(UnitTypeId.BARRACKS) >= 1.0
            and bot.can_afford(UnitTypeId.BARRACKS, check_supply_cost=False)
        ):
            pos = await placement.find_production_position(
                bot, UnitTypeId.BARRACKS, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.BARRACKS, pos, scout_worker_tag
            )

        # Scale Factories: 1 -> 2 on 3 bases (or heavy ground) -> 3 late game.
        desired_fact = 1 if (len(bot.townhalls) >= 2 or intel.enemy_is_rushing) else 0
        if base_count >= 3 or (
            bot.supply_used >= 110 and bot.minerals >= 500 and bot.vespene >= 250
        ):
            desired_fact = max(desired_fact, 2)
        if base_count >= 4 and bot.minerals >= 750 and bot.vespene >= 400:
            desired_fact = 3

        if (
            total_fact < desired_fact
            and total_fact >= 1
            and bot.tech_requirement_progress(UnitTypeId.FACTORY) >= 1.0
            and bot.can_afford(UnitTypeId.FACTORY, check_supply_cost=False)
        ):
            pos = await placement.find_production_position(
                bot, UnitTypeId.FACTORY, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.FACTORY, pos, scout_worker_tag
            )

        # Scale Starports: 2nd Starport (with Tech Lab) for Raven + Vikings vs air/late game.
        desired_sp = 1 if total_fact >= 1 else 0
        if total_sp >= 1 and (
            intel.enemy_air_count >= 4
            or intel.enemy_has_cloak
            or base_count >= 3
            or (bot.supply_used >= 100 and bot.vespene >= 250)
        ):
            desired_sp = 2

        if (
            total_sp < desired_sp
            and total_sp >= 1
            and bot.tech_requirement_progress(UnitTypeId.STARPORT) >= 1.0
            and bot.can_afford(UnitTypeId.STARPORT, check_supply_cost=False)
        ):
            pos = await placement.find_production_position(
                bot, UnitTypeId.STARPORT, base_mgr
            )
            await self._try_build_structure(
                bot, economy, UnitTypeId.STARPORT, pos, scout_worker_tag
            )

        # Armory for +2/+2 infantry & vehicle upgrades.
        inf_atk1_prog = bot.already_pending_upgrade(
            UpgradeId.TERRANINFANTRYWEAPONSLEVEL1
        )
        if (
            total_armory == 0
            and total_fact >= 1
            and (inf_atk1_prog >= 0.45 or bot.time >= 330.0)
            and bot.can_afford(UnitTypeId.ARMORY, check_supply_cost=False)
        ):
            pos = await placement.find_tech_position(bot, UnitTypeId.ARMORY, base_mgr)
            await self._try_build_structure(
                bot, economy, UnitTypeId.ARMORY, pos, scout_worker_tag
            )

        # Sensor Tower (100/50 in Patch 5.0.14+) on 3+ bases for early flank warning.
        total_sensor = self._count_planned(
            bot, UnitTypeId.SENSORTOWER, AbilityId.TERRANBUILD_SENSORTOWER
        )
        if (
            total_sensor == 0
            and base_count >= 3
            and total_ebay >= 1
            and bot.minerals >= 350
            and bot.vespene >= 150
            and bot.can_afford(UnitTypeId.SENSORTOWER, check_supply_cost=False)
        ):
            pos = await placement.find_sensor_tower_position(bot, base_mgr)
            await self._try_build_structure(
                bot, economy, UnitTypeId.SENSORTOWER, pos, scout_worker_tag
            )

    async def _manage_addons(self, bot: BotAI, intel: IntelTracker) -> None:
        """Attach Tech Labs and Reactors safely when addon footprint is clear."""
        # Barracks addons: 1st Tech Lab (Stim), then mostly Reactors (~1:2 TL:Reactor).
        barracks_list = list(bot.structures(UnitTypeId.BARRACKS).ready)
        tl_count = self._count_addons(
            bot,
            barracks_list,
            UnitTypeId.BARRACKSTECHLAB,
            {AbilityId.BUILD_TECHLAB_BARRACKS, AbilityId.BUILD_TECHLAB},
        )
        re_count = self._count_addons(
            bot,
            barracks_list,
            UnitTypeId.BARRACKSREACTOR,
            {AbilityId.BUILD_REACTOR_BARRACKS, AbilityId.BUILD_REACTOR},
        )

        # Wait to build 1st Barracks addon until after 1st Reaper or Marine has started/finished.
        rax_allow_addon = (
            self._count_planned(
                bot, TOWNHALL_ALL_TYPES, AbilityId.TERRANBUILD_COMMANDCENTER
            )
            >= 2
            or len(bot.townhalls) >= 2
            or bot.time >= 115.0
        )
        if rax_allow_addon:
            for rax in barracks_list:
                if (
                    rax.has_add_on
                    or not rax.is_idle
                    or rax.tag in bot.unit_tags_received_action
                ):
                    continue
                if not await bot.can_place_single(
                    UnitTypeId.SUPPLYDEPOT, rax.add_on_position
                ):
                    continue
                heavy_armor = intel.enemy_heavy_armor_ratio >= 0.40
                want_tl = (
                    (tl_count == 0)
                    or (heavy_armor and tl_count <= re_count)
                    or (not heavy_armor and re_count >= (tl_count * 2))
                )
                if want_tl:
                    if self._try_build_addon(
                        bot,
                        rax,
                        UnitTypeId.TECHLAB,
                        UnitTypeId.BARRACKSTECHLAB,
                        AbilityId.BUILD_TECHLAB_BARRACKS,
                    ):
                        tl_count += 1
                else:
                    if self._try_build_addon(
                        bot,
                        rax,
                        UnitTypeId.REACTOR,
                        UnitTypeId.BARRACKSREACTOR,
                        AbilityId.BUILD_REACTOR_BARRACKS,
                    ):
                        re_count += 1

        # Factory addons: Tech Labs for Siege Tanks.
        for fact in bot.structures(UnitTypeId.FACTORY).ready:
            if (
                fact.has_add_on
                or not fact.is_idle
                or fact.tag in bot.unit_tags_received_action
            ):
                continue
            if not await bot.can_place_single(
                UnitTypeId.SUPPLYDEPOT, fact.add_on_position
            ):
                continue
            self._try_build_addon(
                bot,
                fact,
                UnitTypeId.TECHLAB,
                UnitTypeId.FACTORYTECHLAB,
                AbilityId.BUILD_TECHLAB_FACTORY,
            )

        # Starport addons:
        # 1st Starport trains 1 Viking + 1 Medivac first (or Tech Lab immediately if cloak),
        # then gets Reactor; 2nd Starport gets Tech Lab for Ravens.
        starports = list(bot.structures(UnitTypeId.STARPORT).ready)
        sp_tl = self._count_addons(
            bot,
            starports,
            UnitTypeId.STARPORTTECHLAB,
            {AbilityId.BUILD_TECHLAB_STARPORT, AbilityId.BUILD_TECHLAB},
        )
        sp_re = self._count_addons(
            bot,
            starports,
            UnitTypeId.STARPORTREACTOR,
            {AbilityId.BUILD_REACTOR_STARPORT, AbilityId.BUILD_REACTOR},
        )
        air_units_made = len(bot.units(VIKING_TYPES)) + len(
            bot.units(UnitTypeId.MEDIVAC)
        )
        for sp in starports:
            if (
                sp.has_add_on
                or not sp.is_idle
                or sp.tag in bot.unit_tags_received_action
            ):
                continue
            if not await bot.can_place_single(
                UnitTypeId.SUPPLYDEPOT, sp.add_on_position
            ):
                continue
            if (
                intel.enemy_has_cloak
                and sp_tl == 0
                and len(bot.units(UnitTypeId.RAVEN)) == 0
            ):
                if self._try_build_addon(
                    bot,
                    sp,
                    UnitTypeId.TECHLAB,
                    UnitTypeId.STARPORTTECHLAB,
                    AbilityId.BUILD_TECHLAB_STARPORT,
                ):
                    sp_tl += 1
                continue
            if len(starports) == 1 and air_units_made < 2 and bot.time < 300.0:
                continue
            if sp_re == 0:
                if self._try_build_addon(
                    bot,
                    sp,
                    UnitTypeId.REACTOR,
                    UnitTypeId.STARPORTREACTOR,
                    AbilityId.BUILD_REACTOR_STARPORT,
                ):
                    sp_re += 1
            elif sp_tl == 0:
                if self._try_build_addon(
                    bot,
                    sp,
                    UnitTypeId.TECHLAB,
                    UnitTypeId.STARPORTTECHLAB,
                    AbilityId.BUILD_TECHLAB_STARPORT,
                ):
                    sp_tl += 1
            else:
                self._try_build_addon(
                    bot,
                    sp,
                    UnitTypeId.REACTOR,
                    UnitTypeId.STARPORTREACTOR,
                    AbilityId.BUILD_REACTOR_STARPORT,
                )

    def _manage_upgrades(self, bot: BotAI) -> None:
        """Research Bio upgrades, Engineering Bay +1/+1->+3/+3, and Armory vehicle upgrades."""
        # Barracks Tech Lab upgrades: Stimpack -> Combat Shield -> Concussive Shells.
        for upg in (
            UpgradeId.STIMPACK,
            UpgradeId.SHIELDWALL,
            UpgradeId.PUNISHERGRENADES,
        ):
            if (
                bot.already_pending_upgrade(upg) == 0
                and bot.can_afford(upg)
                and bot.structures(UnitTypeId.BARRACKSTECHLAB).ready
                and bot.research(upg)
            ):
                break

        # Engineering Bay upgrades.
        if bot.structures(UnitTypeId.ENGINEERINGBAY).ready:
            armory_ready = bool(bot.structures(UnitTypeId.ARMORY).ready)
            ebay_upgrades = [
                UpgradeId.TERRANINFANTRYWEAPONSLEVEL1,
                UpgradeId.TERRANINFANTRYARMORSLEVEL1,
            ]
            if armory_ready:
                ebay_upgrades = [
                    UpgradeId.TERRANINFANTRYWEAPONSLEVEL1,
                    UpgradeId.TERRANINFANTRYWEAPONSLEVEL2,
                    UpgradeId.TERRANINFANTRYARMORSLEVEL1,
                    UpgradeId.TERRANINFANTRYARMORSLEVEL2,
                    UpgradeId.TERRANINFANTRYWEAPONSLEVEL3,
                    UpgradeId.TERRANINFANTRYARMORSLEVEL3,
                ]
            if bot.supply_used >= 130:
                ebay_upgrades.extend(
                    [UpgradeId.HISECAUTOTRACKING, UpgradeId.TERRANBUILDINGARMOR]
                )

            for upg in ebay_upgrades:
                # Enforce level prerequisites cleanly.
                if (
                    upg == UpgradeId.TERRANINFANTRYWEAPONSLEVEL2
                    and UpgradeId.TERRANINFANTRYWEAPONSLEVEL1 not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANINFANTRYARMORSLEVEL2
                    and UpgradeId.TERRANINFANTRYARMORSLEVEL1 not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANINFANTRYWEAPONSLEVEL3
                    and UpgradeId.TERRANINFANTRYWEAPONSLEVEL2 not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANINFANTRYARMORSLEVEL3
                    and UpgradeId.TERRANINFANTRYARMORSLEVEL2 not in bot.state.upgrades
                ):
                    continue
                if bot.already_pending_upgrade(upg) == 0 and bot.can_afford(upg):
                    bot.research(upg)

        # Armory vehicle/ship upgrades.
        if bot.structures(UnitTypeId.ARMORY).ready:
            for upg in (
                UpgradeId.TERRANVEHICLEWEAPONSLEVEL1,
                UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL1,
                UpgradeId.TERRANVEHICLEWEAPONSLEVEL2,
                UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL2,
                UpgradeId.TERRANVEHICLEWEAPONSLEVEL3,
                UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL3,
            ):
                if (
                    upg == UpgradeId.TERRANVEHICLEWEAPONSLEVEL2
                    and UpgradeId.TERRANVEHICLEWEAPONSLEVEL1 not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL2
                    and UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL1
                    not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANVEHICLEWEAPONSLEVEL3
                    and UpgradeId.TERRANVEHICLEWEAPONSLEVEL2 not in bot.state.upgrades
                ):
                    continue
                if (
                    upg == UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL3
                    and UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL2
                    not in bot.state.upgrades
                ):
                    continue
                if bot.already_pending_upgrade(upg) == 0 and bot.can_afford(upg):
                    bot.research(upg)

    def _train_army(self, bot: BotAI, intel: IntelTracker) -> None:
        """Produce adaptive Bio/Tank/Medivac/Viking/Raven army respecting addon queues."""
        if bot.supply_left <= 0:
            return

        # 1. Starport units: Raven (Tech Lab), Vikings (Air threat), Medivacs (Bio sustain).
        bio_count = len(bot.units({UnitTypeId.MARINE, UnitTypeId.MARAUDER}))
        medivac_count = len(bot.units(UnitTypeId.MEDIVAC)) + int(
            bot.already_pending(UnitTypeId.MEDIVAC)
        )
        viking_count = len(bot.units(VIKING_TYPES)) + int(
            bot.already_pending(UnitTypeId.VIKINGFIGHTER)
        )
        raven_count = len(bot.units(UnitTypeId.RAVEN)) + int(
            bot.already_pending(UnitTypeId.RAVEN)
        )

        desired_ravens = 2 if intel.enemy_has_cloak else 1
        desired_medivacs = min(8, max(2, bio_count // 6))
        desired_vikings = min(
            14, max(2 if bot.time >= 240.0 else 1, intel.enemy_air_count + 2)
        )
        if intel.enemy_air_ratio >= 0.45:
            desired_vikings = max(desired_vikings, 8)

        for sp in bot.structures(UnitTypeId.STARPORT).ready:
            if sp.tag in bot.unit_tags_received_action:
                continue
            max_slots = 2 if sp.has_reactor else 1
            if len(sp.orders) >= max_slots:
                continue
            if (
                sp.has_techlab
                and raven_count < desired_ravens
                and self._try_train(bot, sp, UnitTypeId.RAVEN)
            ):
                raven_count += 1
                continue
            # First Starport opener: 1st Viking -> 1st Medivac -> balance.
            if viking_count == 0 and not intel.enemy_is_rushing:
                if self._try_train(bot, sp, UnitTypeId.VIKINGFIGHTER):
                    viking_count += 1
            elif medivac_count < desired_medivacs and (
                medivac_count < 2
                or viking_count >= desired_vikings
                or intel.enemy_air_count == 0
            ):
                if self._try_train(bot, sp, UnitTypeId.MEDIVAC):
                    medivac_count += 1
            elif viking_count < desired_vikings:
                if self._try_train(bot, sp, UnitTypeId.VIKINGFIGHTER):
                    viking_count += 1
            elif medivac_count < desired_medivacs + 2 and self._try_train(
                bot, sp, UnitTypeId.MEDIVAC
            ):
                medivac_count += 1

        # 2. Factory units: Siege Tanks (Tech Lab), fallback Hellion/Mine only if no TL & urgent.
        tank_count = len(bot.units(TANK_TYPES)) + int(
            bot.already_pending(UnitTypeId.SIEGETANK)
        )
        desired_tanks = min(12, max(4, int(bot.supply_army // 8) + 2))
        if intel.enemy_air_ratio >= 0.65:
            desired_tanks = min(desired_tanks, 4)

        for fact in bot.structures(UnitTypeId.FACTORY).ready:
            if fact.tag in bot.unit_tags_received_action:
                continue
            max_slots = 2 if fact.has_reactor else 1
            if len(fact.orders) >= max_slots:
                continue
            if fact.has_techlab and tank_count < desired_tanks:
                if self._try_train(bot, fact, UnitTypeId.SIEGETANK):
                    tank_count += 1
            elif (
                not fact.has_add_on
                and intel.enemy_is_rushing
                and len(bot.units(UnitTypeId.HELLION)) < 2
            ):
                self._try_train(bot, fact, UnitTypeId.HELLION)

        # 3. Barracks units: 1 opening Reaper -> Marauders on Tech Labs / Marines on Reactors.
        reapers_made = (
            len(bot.units(UnitTypeId.REAPER))
            + int(bot.already_pending(UnitTypeId.REAPER))
            + bot._units_created.get(UnitTypeId.REAPER, 0)
        )
        marauder_count = len(bot.units(UnitTypeId.MARAUDER)) + int(
            bot.already_pending(UnitTypeId.MARAUDER)
        )
        marine_count = len(bot.units(UnitTypeId.MARINE)) + int(
            bot.already_pending(UnitTypeId.MARINE)
        )

        for rax in bot.structures(UnitTypeId.BARRACKS).ready:
            if rax.tag in bot.unit_tags_received_action:
                continue
            max_slots = 2 if rax.has_reactor else 1
            if len(rax.orders) >= max_slots:
                continue

            if (
                not rax.has_add_on
                and reapers_made == 0
                and bot.time < 160.0
                and not intel.enemy_is_rushing
                and self._try_train(bot, rax, UnitTypeId.REAPER)
            ):
                reapers_made += 1
                continue

            if rax.has_techlab:
                prefer_marauder = intel.enemy_air_ratio < 0.55 and (
                    intel.enemy_heavy_armor_ratio >= 0.25
                    or marauder_count * 2 < marine_count
                    or bot.vespene >= 150
                )
                if prefer_marauder and self._try_train(bot, rax, UnitTypeId.MARAUDER):
                    marauder_count += 1
                elif self._try_train(bot, rax, UnitTypeId.MARINE):
                    marine_count += 1
            else:
                if self._try_train(bot, rax, UnitTypeId.MARINE):
                    marine_count += 1
