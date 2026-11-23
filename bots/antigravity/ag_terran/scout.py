"""4-player spawn scouting, Reaper active scouting, and late-game air cleanup scouts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from .bases import BaseManager
from .config import WORKER_TYPES, BotConfig
from .economy import EconomyManager
from .intel import IntelTracker

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


class ScoutManager:
    """Coordinates early SCV spawn scouting, Reaper harassment/scouting, and cleanup."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.scout_worker_tag: int | None = None
        self.reaper_scout_tag: int | None = None
        self.cleanup_scout_tags: set[int] = set()
        self._scv_scout_done: bool = False
        self._scv_target: Point2 | None = None
        self._reaper_target: Point2 | None = None

    def initialize(self, bot: BotAI) -> None:
        """Reset scout state at game start."""
        self.scout_worker_tag = None
        self.reaper_scout_tag = None
        self.cleanup_scout_tags.clear()
        self._scv_scout_done = False
        self._scv_target = None
        self._reaper_target = None

    def on_unit_destroyed(self, unit_tag: int) -> None:
        """Clear destroyed scout references."""
        if unit_tag == self.scout_worker_tag:
            self.scout_worker_tag = None
            self._scv_scout_done = True
        if unit_tag == self.reaper_scout_tag:
            self.reaper_scout_tag = None
        self.cleanup_scout_tags.discard(unit_tag)

    def get_reserved_unit_tags(self) -> set[int]:
        """Return combat/cleanup scout tags that should not be pulled by main army micro."""
        tags = set(self.cleanup_scout_tags)
        if self.reaper_scout_tag is not None:
            tags.add(self.reaper_scout_tag)
        return tags

    def update(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        economy: EconomyManager,
    ) -> None:
        """Update SCV scout, Reaper scout, and late-game Viking cleanup scouts."""
        self._update_scv_scout(bot, intel, base_mgr, economy)
        self._update_reaper_scout(bot, intel, base_mgr)
        self._update_cleanup_scouts(bot, intel)

    def _saw_enemy_natural_or_tech(self, bot: BotAI, intel: IntelTracker) -> bool:
        if intel.confirmed_enemy_main is None:
            return False
        structs = intel.get_known_enemy_structures()
        tech_or_nat = sum(
            1
            for s in structs
            if s.position.distance_to(intel.confirmed_enemy_main) > 10.0
            or s.type_id
            in {
                UnitTypeId.BARRACKS,
                UnitTypeId.FACTORY,
                UnitTypeId.GATEWAY,
                UnitTypeId.CYBERNETICSCORE,
                UnitTypeId.SPAWNINGPOOL,
                UnitTypeId.ROACHWARREN,
            }
        )
        return tech_or_nat >= 1

    def _update_scv_scout(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        economy: EconomyManager,
    ) -> None:
        if not self._scv_scout_done and self.scout_worker_tag is None:
            should_launch = intel.confirmed_enemy_main is None and (
                bot.supply_used >= 16 or bool(bot.structures(UnitTypeId.BARRACKS))
            )
            if should_launch:
                pool = [
                    w
                    for w in bot.workers
                    if w.tag not in economy.builder_reservations
                    and w.tag not in economy.repair_assignments
                    and w.tag not in economy.defender_tags
                    and not w.is_constructing_scv
                    and w.health_percentage >= 0.80
                ]
                if pool:
                    # Prefer mineral worker not carrying cargo.
                    pool.sort(
                        key=lambda w: (
                            1
                            if (w.is_carrying_minerals or w.is_carrying_vespene)
                            else 0,
                            w.distance_to(bot.start_location),
                        )
                    )
                    self.scout_worker_tag = pool[0].tag

        if self.scout_worker_tag is None:
            return

        scv = bot.workers.find_by_tag(self.scout_worker_tag)
        if scv is None:
            self.scout_worker_tag = None
            self._scv_scout_done = True
            return

        should_retire = (
            bot.time >= 175.0
            or scv.health_percentage < 0.65
            or (
                intel.confirmed_enemy_main is not None
                and self._saw_enemy_natural_or_tech(bot, intel)
                and bot.time >= 115.0
            )
        )
        if should_retire:
            self._scv_scout_done = True
            self.scout_worker_tag = None
            safe_halls = base_mgr.get_safe_landed_townhalls(bot)
            if safe_halls and bot.mineral_field:
                dest_hall = min(safe_halls, key=lambda th: th.distance_to(scv.position))
                patches = [
                    m
                    for m in bot.mineral_field
                    if m.distance_to(dest_hall.position) <= 10.5
                ]
                if patches:
                    scv.gather(
                        min(patches, key=lambda m: m.distance_to(dest_hall.position))
                    )
                    return
            scv.move(base_mgr.main_location or bot.start_location)
            return

        unvisited = intel.get_unvisited_candidate_spawns()
        if intel.confirmed_enemy_main is None and unvisited:
            # Visit nearest unvisited candidate spawn.
            target = min(unvisited, key=lambda p: p.distance_to(scv.position))
            self._scv_target = target
            if scv.distance_to(target) > 4.0:
                scv.move(target)
        elif intel.confirmed_enemy_main is not None:
            # Orbit between enemy main and enemy natural to verify expansion & tech.
            enemy_main = intel.confirmed_enemy_main
            exp_candidates = [
                p
                for p in bot.expansion_locations_list
                if p.distance_to(enemy_main) > 6.0
            ]
            enemy_nat = (
                min(exp_candidates, key=lambda p: p.distance_to(enemy_main))
                if exp_candidates
                else enemy_main.towards(bot.game_info.map_center, 18.0)
            )
            orbit_pt = enemy_nat.towards(enemy_main, 6.0)
            if scv.distance_to(orbit_pt) > 5.0:
                scv.move(orbit_pt)
            else:
                scv.move(enemy_nat)

    def _update_reaper_scout(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
    ) -> None:
        reapers = list(bot.units(UnitTypeId.REAPER))
        if self.reaper_scout_tag is not None:
            reaper = next((r for r in reapers if r.tag == self.reaper_scout_tag), None)
            if reaper is None:
                self.reaper_scout_tag = None
        else:
            reaper = None

        if reaper is None and reapers and bot.time < 300.0:
            reaper = reapers[0]
            self.reaper_scout_tag = reaper.tag

        if reaper is None or reaper.tag in bot.unit_tags_received_action:
            return

        # Heal / retreat if hurt or outgunned.
        nearby_enemies = [
            e for e in bot.enemy_units if e.distance_to(reaper.position) <= 8.5
        ]
        combat_threats = [
            e
            for e in nearby_enemies
            if e.type_id not in WORKER_TYPES
            and (e.can_attack_ground or e.can_attack_air)
        ]
        if reaper.health_percentage < 0.62 or (
            reaper.health_percentage < 0.80 and len(combat_threats) >= 2
        ):
            safe_pt = base_mgr.get_rally_point(bot, intel)
            if combat_threats and reaper.weapon_cooldown == 0:
                closest = min(
                    combat_threats, key=lambda e: e.distance_to(reaper.position)
                )
                if (
                    reaper.distance_to(closest) <= 5.5
                    and reaper.health_percentage > 0.45
                ):
                    reaper.move(reaper.position.towards(safe_pt, 5.0))
                    return
            reaper.move(safe_pt)
            return

        # Active 4-player spawn discovery: search opposite spawn from SCV scout.
        unvisited = intel.get_unvisited_candidate_spawns()
        if intel.confirmed_enemy_main is None and unvisited:
            if len(unvisited) >= 2 and self._scv_target is not None:
                others = [p for p in unvisited if p.distance_to(self._scv_target) > 5.0]
                pool = others or unvisited
            else:
                pool = unvisited
            target = max(pool, key=lambda p: p.distance_to(bot.start_location))
            self._reaper_target = target
            if nearby_enemies:
                self._micro_reaper_skirmish(reaper, nearby_enemies, target)
            else:
                reaper.move(target)
            return

        # Once enemy main is known: probe enemy natural/third or hold center watchtower.
        if intel.confirmed_enemy_main is not None and bot.time < 320.0:
            enemy_main = intel.confirmed_enemy_main
            exp_candidates = [
                p
                for p in bot.expansion_locations_list
                if p.distance_to(enemy_main) > 6.0
            ]
            if exp_candidates:
                exp_candidates.sort(key=lambda p: p.distance_to(enemy_main))
                enemy_nat = exp_candidates[0]
            else:
                enemy_nat = enemy_main.towards(bot.game_info.map_center, 16.0)

            if nearby_enemies:
                self._micro_reaper_skirmish(reaper, nearby_enemies, enemy_nat)
            elif reaper.distance_to(enemy_nat) > 6.0:
                reaper.move(enemy_nat)
            return

        # Mid/late game: watchtower map control.
        if bot.watchtowers:
            wt = min(
                bot.watchtowers,
                key=lambda w: w.distance_to(bot.game_info.map_center),
            )
            if nearby_enemies:
                self._micro_reaper_skirmish(reaper, nearby_enemies, wt.position)
            elif reaper.distance_to(wt.position) > 2.5:
                reaper.move(wt.position)

    def _micro_reaper_skirmish(
        self,
        reaper: Unit,
        enemies: list[Unit],
        goal: Point2,
    ) -> None:
        combat = [
            e
            for e in enemies
            if e.type_id not in WORKER_TYPES
            and e.can_attack_ground
            and e.can_be_attacked
        ]
        if combat:
            closest = min(combat, key=lambda e: e.distance_to(reaper.position))
            dist = reaper.distance_to(closest)
            if dist <= 5.5:
                # Kite away and toss KD8 if close.
                if dist <= 4.5:
                    reaper(AbilityId.KD8CHARGE_KD8CHARGE, target=closest.position)
                    return
                reaper.move(reaper.position.towards(closest.position, -3.5))
                return
        workers = [
            e for e in enemies if e.type_id in WORKER_TYPES and e.can_be_attacked
        ]
        if workers and reaper.weapon_cooldown == 0:
            reaper.attack(min(workers, key=lambda w: w.health))
            return
        reaper.move(goal)

    def _update_cleanup_scouts(self, bot: BotAI, intel: IntelTracker) -> None:
        """Assign spare Vikings to uncover floated/hidden enemy buildings late game."""
        need_cleanup = (
            intel.enemy_main_cleared
            and not intel.get_known_enemy_structures()
            and bool(intel.cleanup_targets)
        )
        if not need_cleanup:
            self.cleanup_scout_tags.clear()
            return

        fighters = list(bot.units(UnitTypeId.VIKINGFIGHTER))
        if not fighters:
            self.cleanup_scout_tags.clear()
            return

        live_fighter_tags = {v.tag for v in fighters}
        self.cleanup_scout_tags &= live_fighter_tags
        desired = min(2, len(fighters))
        if len(self.cleanup_scout_tags) < desired:
            for v in fighters:
                if v.tag not in self.cleanup_scout_tags:
                    self.cleanup_scout_tags.add(v.tag)
                    if len(self.cleanup_scout_tags) >= desired:
                        break

        scouts = [v for v in fighters if v.tag in self.cleanup_scout_tags]
        for idx, viking in enumerate(scouts):
            if viking.tag in bot.unit_tags_received_action:
                continue
            target = intel.cleanup_targets[idx % len(intel.cleanup_targets)]
            if viking.distance_to(target) > 5.0:
                viking.attack(target)
