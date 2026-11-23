"""Worker economy, post-harass recovery, SCV defense/repair, and Orbital energy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sc2.constants import IS_CONSTRUCTING_SCV, TERRAN_STRUCTURES_REQUIRE_SCV
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from .bases import BaseManager, BaseStatus
from .config import (
    DEPOT_TYPES,
    OWN_NON_COMBAT_TYPES,
    TOWNHALL_LANDED_TYPES,
    WORKER_TYPES,
    BotConfig,
)
from .intel import IntelTracker, safe_is_visible

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


class EconomyManager:
    """Manages mineral/gas saturation, evacuation, recovery, repairs, and Orbital spells."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.builder_reservations: dict[int, float] = {}
        self.repair_assignments: dict[int, int] = {}
        self.defender_tags: set[int] = set()
        self.evacuated_tags: set[int] = set()
        self.last_scan_positions: list[tuple[Point2, float]] = []

    def on_unit_destroyed(self, unit_tag: int) -> None:
        """Clean up state associated with destroyed SCVs or repair targets."""
        self.builder_reservations.pop(unit_tag, None)
        self.repair_assignments.pop(unit_tag, None)
        self.defender_tags.discard(unit_tag)
        self.evacuated_tags.discard(unit_tag)
        stale_repairs = [
            scv_tag
            for scv_tag, target_tag in self.repair_assignments.items()
            if target_tag == unit_tag
        ]
        for scv_tag in stale_repairs:
            self.repair_assignments.pop(scv_tag, None)

    def _is_in_overwhelmed_zone(self, pos: Point2, base_mgr: BaseManager) -> bool:
        for info in base_mgr.bases.values():
            if (
                info.status == BaseStatus.OVERWHELMED
                and info.location.distance_to(pos) <= 16.0
            ):
                return True
        return False

    def _is_worker_busy_building(self, worker: Unit) -> bool:
        return bool(worker.is_constructing_scv)

    def select_builder(
        self,
        bot: BotAI,
        near: Point2,
        scout_worker_tag: int | None = None,
        allow_evacuated: bool = False,
    ) -> Unit | None:
        """Select a healthy non-occupied mineral SCV closest to the build site."""
        now = float(bot.time)
        gather_abilities = {AbilityId.HARVEST_GATHER, AbilityId.HARVEST_RETURN}
        actions_by_tag = {a.unit.tag: a.ability for a in bot.actions}
        candidates: list[Unit] = []
        fallback_carrying: list[Unit] = []
        fallback_gather: list[Unit] = []
        fallback_evacuated: list[Unit] = []

        for w in bot.workers:
            if w.tag == scout_worker_tag:
                continue
            if self._is_worker_busy_building(w):
                continue
            if (
                w.tag in self.builder_reservations
                and self.builder_reservations[w.tag] > now
            ):
                continue
            if w.tag in self.repair_assignments or w.tag in self.defender_tags:
                continue
            if w.health_percentage < 0.35:
                continue
            if w.tag in bot.unit_tags_received_action:
                prior_ab = actions_by_tag.get(w.tag)
                if prior_ab not in gather_abilities:
                    continue
                if w.tag in self.evacuated_tags:
                    if allow_evacuated:
                        fallback_evacuated.append(w)
                    continue
                fallback_gather.append(w)
                continue
            if w.tag in self.evacuated_tags:
                if allow_evacuated:
                    fallback_evacuated.append(w)
                continue
            if w.is_carrying_minerals or w.is_carrying_vespene:
                fallback_carrying.append(w)
                continue
            candidates.append(w)

        pool = (
            candidates
            or fallback_carrying
            or fallback_gather
            or (fallback_evacuated if allow_evacuated else [])
        )
        if not pool:
            return None
        chosen = min(pool, key=lambda w: w.distance_to(near))
        self.builder_reservations[chosen.tag] = now + 1.5
        return chosen

    def update(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Run evacuation, self-defense, building resumption, repair, distribution, and OCs."""
        now = float(bot.time)
        live_worker_tags = {w.tag for w in bot.workers}

        self.builder_reservations = {
            tag: exp
            for tag, exp in self.builder_reservations.items()
            if tag in live_worker_tags and exp > now
        }
        self.defender_tags &= live_worker_tags
        self.evacuated_tags &= live_worker_tags
        self.last_scan_positions = [
            (pos, ts) for pos, ts in self.last_scan_positions if (now - ts) < 10.0
        ]

        self._handle_evacuation(bot, base_mgr, scout_worker_tag)
        self._handle_light_harass_defense(bot, base_mgr, scout_worker_tag)
        self._resume_halted_buildings(bot, base_mgr, scout_worker_tag)
        self._handle_repairs(bot, base_mgr, scout_worker_tag)
        self._balance_workers(bot, base_mgr, scout_worker_tag)
        self._manage_orbitals(bot, intel, base_mgr, now)

    def _handle_evacuation(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Evacuate workers from heavy-harass or overwhelmed bases to safe bases."""
        safe_halls = [
            th
            for th in base_mgr.get_safe_landed_townhalls(bot)
            if any(m.distance_to(th.position) <= 10.5 for m in bot.mineral_field)
        ]
        if not safe_halls:
            safe_halls = base_mgr.get_safe_landed_townhalls(bot)

        fallback_point = base_mgr.main_location or bot.start_location

        evac_bases = [b for b in base_mgr.bases.values() if b.evacuate_workers]
        for binfo in evac_bases:
            for w in bot.workers:
                if w.tag in bot.unit_tags_received_action or w.tag == scout_worker_tag:
                    continue
                if self._is_worker_busy_building(w):
                    continue
                if w.distance_to(binfo.location) > 14.0:
                    continue
                self.defender_tags.discard(w.tag)
                self.repair_assignments.pop(w.tag, None)
                self.evacuated_tags.add(w.tag)
                self._send_worker_to_safe_base(bot, w, safe_halls, fallback_point)

        # Pull badly injured workers near enemy combat units to safety.
        for w in bot.workers:
            if w.tag in bot.unit_tags_received_action or w.tag == scout_worker_tag:
                continue
            if self._is_worker_busy_building(w) or w.health_percentage >= 0.45:
                continue
            near_enemy = any(
                e.type_id not in WORKER_TYPES and e.distance_to(w.position) <= 7.5
                for e in bot.enemy_units
            )
            if near_enemy:
                self.defender_tags.discard(w.tag)
                self.evacuated_tags.add(w.tag)
                self._send_worker_to_safe_base(bot, w, safe_halls, fallback_point)

    def _send_worker_to_safe_base(
        self,
        bot: BotAI,
        worker: Unit,
        safe_halls: list[Unit],
        fallback_point: Point2,
    ) -> None:
        if safe_halls:
            dest_hall = min(safe_halls, key=lambda th: th.distance_to(worker.position))
            patches = [
                m
                for m in bot.mineral_field
                if m.distance_to(dest_hall.position) <= 10.5
            ]
            if patches:
                patch = min(patches, key=lambda m: m.distance_to(dest_hall.position))
                worker.gather(patch)
                return
            if worker.distance_to(dest_hall.position) > 5.5:
                worker.move(dest_hall.position)
                return
        if worker.distance_to(fallback_point) > 6.0:
            worker.move(fallback_point)

    def _handle_light_harass_defense(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Use a small SCV squad against light early harass when no combat units are near."""
        active_light_bases = []
        for info in base_mgr.bases.values():
            if info.status != BaseStatus.LIGHT_HARASS:
                continue
            combat_near = sum(
                1
                for u in bot.units
                if u.type_id not in OWN_NON_COMBAT_TYPES
                and u.distance_to(info.location) <= 13.0
            )
            if combat_near < 2:
                active_light_bases.append(info)

        # Release defenders that are hurt or whose base is now clear/guarded.
        workers_by_tag = {w.tag: w for w in bot.workers}
        to_release: list[int] = []
        for tag in list(self.defender_tags):
            w = workers_by_tag.get(tag)
            if w is None or w.health_percentage < 0.45:
                to_release.append(tag)
                continue
            matching_base = next(
                (b for b in active_light_bases if w.distance_to(b.location) <= 18.0),
                None,
            )
            if matching_base is None:
                to_release.append(tag)

        safe_halls = base_mgr.get_safe_landed_townhalls(bot)
        fallback_point = base_mgr.main_location or bot.start_location
        for tag in to_release:
            self.defender_tags.discard(tag)
            w = workers_by_tag.get(tag)
            if w is not None and w.tag not in bot.unit_tags_received_action:
                self._send_worker_to_safe_base(bot, w, safe_halls, fallback_point)

        for info in active_light_bases:
            enemies = [
                e
                for e in bot.enemy_units
                if e.distance_to(info.location) <= 16.0 and e.can_be_attacked
            ]
            if not enemies:
                continue
            desired_defenders = min(4, len(enemies) * 2 + 1)
            current_defenders = [
                workers_by_tag[t]
                for t in self.defender_tags
                if t in workers_by_tag
                and workers_by_tag[t].distance_to(info.location) <= 18.0
            ]
            if len(current_defenders) < desired_defenders:
                pool = [
                    w
                    for w in bot.workers
                    if w.tag not in self.defender_tags
                    and w.tag != scout_worker_tag
                    and w.tag not in bot.unit_tags_received_action
                    and not self._is_worker_busy_building(w)
                    and w.tag not in self.repair_assignments
                    and w.health_percentage > 0.55
                    and w.distance_to(info.location) <= 13.0
                ]
                pool.sort(key=lambda w: w.distance_to(enemies[0].position))
                for w in pool[: desired_defenders - len(current_defenders)]:
                    self.defender_tags.add(w.tag)
                    current_defenders.append(w)

            for w in current_defenders:
                if w.tag in bot.unit_tags_received_action:
                    continue
                target = min(enemies, key=lambda e: e.distance_to(w.position))
                if w.order_target != target.tag:
                    w.attack(target)

    def _resume_halted_buildings(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Resume unfinished Terran buildings whose SCV was killed or pulled away."""
        unfinished = [
            s
            for s in bot.structures.not_ready
            if s.type_id in TERRAN_STRUCTURES_REQUIRE_SCV
            and not self._is_in_overwhelmed_zone(s.position, base_mgr)
        ]
        if not unfinished:
            return

        for struct in unfinished:
            has_builder = False
            for w in bot.workers:
                if any(
                    o.ability.id in IS_CONSTRUCTING_SCV
                    and (
                        o.target == struct.tag
                        or (
                            isinstance(o.target, Point2)
                            and o.target.distance_to(struct.position) <= 1.5
                        )
                    )
                    for o in w.orders
                ):
                    has_builder = True
                    break
                if any(
                    o.ability.id == AbilityId.SMART and o.target == struct.tag
                    for o in w.orders
                ):
                    has_builder = True
                    break
                if w.is_constructing_scv and w.distance_to(struct.position) <= 3.8:
                    has_builder = True
                    break
            if has_builder:
                continue

            builder = self.select_builder(bot, struct.position, scout_worker_tag)
            if builder is not None:
                builder.smart(struct)

    def _handle_repairs(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Assign repair SCVs to damaged PFs, Bunkers, CCs, Depot walls, and nearby mech."""
        if bot.minerals < 20:
            self.repair_assignments.clear()
            return

        all_targets_by_tag: dict[int, Unit] = {}
        desired_repairers: dict[int, tuple[int, int]] = {}

        for s in bot.structures.ready:
            if self._is_in_overwhelmed_zone(s.position, base_mgr):
                continue
            all_targets_by_tag[s.tag] = s
            hp = s.health_percentage
            if s.type_id == UnitTypeId.PLANETARYFORTRESS and hp < 0.92:
                desired_repairers[s.tag] = (0, 5 if hp < 0.70 else 2)
            elif s.type_id == UnitTypeId.BUNKER and hp < 0.90:
                desired_repairers[s.tag] = (1, 3)
            elif s.type_id in TOWNHALL_LANDED_TYPES and hp < 0.88:
                desired_repairers[s.tag] = (2, 2)
            elif s.type_id in DEPOT_TYPES and hp < 0.85:
                desired_repairers[s.tag] = (3, 2)
            elif s.type_id == UnitTypeId.MISSILETURRET and hp < 0.80:
                desired_repairers[s.tag] = (4, 1)

        safe_bases = [
            b.location
            for b in base_mgr.bases.values()
            if b.status != BaseStatus.OVERWHELMED
        ]
        mech_repair_types = {
            UnitTypeId.SIEGETANK,
            UnitTypeId.SIEGETANKSIEGED,
            UnitTypeId.MEDIVAC,
            UnitTypeId.RAVEN,
            UnitTypeId.THOR,
            UnitTypeId.VIKINGFIGHTER,
        }
        for u in bot.units:
            if u.type_id not in mech_repair_types or u.health_percentage >= 0.78:
                continue
            if not any(u.distance_to(loc) <= 18.0 for loc in safe_bases):
                continue
            all_targets_by_tag[u.tag] = u
            desired_repairers[u.tag] = (5, 1)

        # Validate existing repair assignments.
        workers_by_tag = {w.tag: w for w in bot.workers}
        active_counts: dict[int, int] = {}
        for scv_tag, target_tag in list(self.repair_assignments.items()):
            scv = workers_by_tag.get(scv_tag)
            target = all_targets_by_tag.get(target_tag)
            if (
                scv is None
                or target is None
                or target.health_percentage >= 0.99
                or scv.health_percentage < 0.35
                or scv_tag in self.defender_tags
            ):
                self.repair_assignments.pop(scv_tag, None)
                continue
            active_counts[target_tag] = active_counts.get(target_tag, 0) + 1
            if (
                scv.tag not in bot.unit_tags_received_action
                and scv.order_target != target.tag
            ):
                scv.repair(target)

        # Add needed repair SCVs in priority order.
        sorted_targets = sorted(
            desired_repairers.items(),
            key=lambda kv: (kv[1][0], all_targets_by_tag[kv[0]].health_percentage),
        )
        for target_tag, (_, max_scvs) in sorted_targets:
            current = active_counts.get(target_tag, 0)
            if current >= max_scvs:
                continue
            target = all_targets_by_tag[target_tag]
            pool = [
                w
                for w in bot.workers
                if w.tag not in self.repair_assignments
                and w.tag not in self.defender_tags
                and w.tag != scout_worker_tag
                and w.tag not in bot.unit_tags_received_action
                and not self._is_worker_busy_building(w)
                and w.health_percentage > 0.50
                and w.distance_to(target.position) <= 22.0
            ]
            pool.sort(key=lambda w: w.distance_to(target.position))
            for w in pool[: max_scvs - current]:
                self.repair_assignments[w.tag] = target_tag
                w.repair(target)

    def _balance_workers(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        scout_worker_tag: int | None,
    ) -> None:
        """Saturate safe refineries and mineral lines, recovering evacuated/idle workers."""
        safe_halls = [
            th
            for th in base_mgr.get_safe_landed_townhalls(bot)
            if th.is_ready
            and any(m.distance_to(th.position) <= 10.5 for m in bot.mineral_field)
        ]
        if not safe_halls:
            return

        safe_hall_tags = {th.tag for th in safe_halls}
        safe_mineral_tags_by_hall: dict[int, set[int]] = {}
        patches_by_hall: dict[int, list[Unit]] = {}
        all_safe_mineral_tags: set[int] = set()
        for th in safe_halls:
            patches = [
                m for m in bot.mineral_field if m.distance_to(th.position) <= 10.5
            ]
            patches.sort(key=lambda m: m.distance_to(th.position))
            patches_by_hall[th.tag] = patches
            tags = {m.tag for m in patches}
            safe_mineral_tags_by_hall[th.tag] = tags
            all_safe_mineral_tags |= tags

        safe_refineries: list[Unit] = []
        for ref in bot.gas_buildings.ready:
            if getattr(ref, "vespene_contents", 1) <= 0:
                continue
            if self._is_in_overwhelmed_zone(ref.position, base_mgr):
                continue
            if any(ref.distance_to(th.position) <= 11.5 for th in safe_halls):
                safe_refineries.append(ref)
        safe_ref_tags = {ref.tag for ref in safe_refineries}

        # Dynamic gas throttle only when mineral-starved with banked vespene.
        if bot.vespene >= 900 and bot.minerals < 120:
            desired_gas = 0
        elif bot.vespene >= 650 and bot.minerals < 160:
            desired_gas = 1
        elif bot.vespene >= 450 and bot.minerals < 180:
            desired_gas = 2
        else:
            desired_gas = 3

        all_landed_halls = [
            th for th in bot.townhalls if th.type_id in TOWNHALL_LANDED_TYPES
        ]

        busy_tags = (
            set(self.builder_reservations.keys())
            | set(self.repair_assignments.keys())
            | self.defender_tags
        )
        if scout_worker_tag is not None:
            busy_tags.add(scout_worker_tag)

        # Classify every non-busy worker's current mining site.
        idle_workers: list[Unit] = []
        relocate_workers: list[Unit] = []
        hall_workers: dict[int, list[Unit]] = {th.tag: [] for th in safe_halls}
        ref_workers: dict[int, list[Unit]] = {ref.tag: [] for ref in safe_refineries}

        for w in bot.workers:
            if w.tag in busy_tags or w.tag in bot.unit_tags_received_action:
                continue
            if self._is_worker_busy_building(w):
                continue

            if w.is_idle:
                self.evacuated_tags.discard(w.tag)
                idle_workers.append(w)
                continue

            target = w.order_target
            assigned_to_safe_ref = False
            if target in safe_ref_tags:
                ref_workers[target].append(w)
                assigned_to_safe_ref = True
            elif w.is_carrying_vespene:
                nearest_hall = min(
                    all_landed_halls,
                    key=lambda th: th.distance_to(w.position),
                    default=None,
                )
                if target in safe_hall_tags or (
                    w.is_returning
                    and nearest_hall is not None
                    and nearest_hall.tag in safe_hall_tags
                    and w.distance_to(nearest_hall.position) <= 14.0
                    and w.tag not in self.evacuated_tags
                ):
                    nearest_ref = min(
                        safe_refineries,
                        key=lambda r: r.distance_to(w.position),
                        default=None,
                    )
                    if (
                        nearest_ref is not None
                        and nearest_ref.distance_to(w.position) <= 14.0
                    ):
                        ref_workers[nearest_ref.tag].append(w)
                        assigned_to_safe_ref = True

            if assigned_to_safe_ref:
                self.evacuated_tags.discard(w.tag)
                continue

            assigned_hall_tag: int | None = None
            if target in all_safe_mineral_tags:
                for htag, mtags in safe_mineral_tags_by_hall.items():
                    if target in mtags:
                        assigned_hall_tag = htag
                        break
            elif w.is_carrying_minerals:
                if target in safe_hall_tags:
                    assigned_hall_tag = target
                elif w.is_returning and w.tag not in self.evacuated_tags:
                    nearest_hall = min(
                        all_landed_halls,
                        key=lambda th: th.distance_to(w.position),
                        default=None,
                    )
                    if (
                        nearest_hall is not None
                        and nearest_hall.tag in safe_hall_tags
                        and w.distance_to(nearest_hall.position) <= 14.0
                    ):
                        assigned_hall_tag = nearest_hall.tag

            if assigned_hall_tag is not None:
                self.evacuated_tags.discard(w.tag)
                hall_workers[assigned_hall_tag].append(w)
                continue

            # Worker is mining an evacuated, overwhelmed, or depleted site -> reassign.
            if w.is_gathering or w.is_returning or w.tag in self.evacuated_tags:
                self.evacuated_tags.discard(w.tag)
                relocate_workers.append(w)

        hall_ideals = {
            th.tag: min(24, max(1, len(patches_by_hall[th.tag]) * 2))
            for th in safe_halls
        }
        hall_max_useful = {
            th.tag: min(24, max(1, len(patches_by_hall[th.tag]) * 3))
            for th in safe_halls
        }
        must_assign: list[Unit] = idle_workers + relocate_workers

        # Pull excess gas workers if strictly over-saturated or throttled (only if a mineral line has room).
        any_hall_can_take_gas_excess = any(
            len(hall_workers[th.tag]) < hall_max_useful[th.tag] for th in safe_halls
        )
        for ref in safe_refineries:
            ref_max = ref.ideal_harvesters or 3
            cap = min(desired_gas, ref_max)
            effective_cap = cap if any_hall_can_take_gas_excess else ref_max
            assigned = ref_workers[ref.tag]
            if len(assigned) > effective_cap:
                keep = list(assigned[:effective_cap])
                kept_carrying: list[Unit] = []
                for w in assigned[effective_cap:]:
                    if w.is_carrying_vespene or w.is_carrying_minerals:
                        kept_carrying.append(w)
                    else:
                        must_assign.append(w)
                ref_workers[ref.tag] = keep + kept_carrying

        ref_deficits = {
            ref.tag: max(
                0,
                min(desired_gas, ref.ideal_harvesters or 3) - len(ref_workers[ref.tag]),
            )
            for ref in safe_refineries
        }
        hall_deficits = {
            th.tag: max(0, hall_ideals[th.tag] - len(hall_workers[th.tag]))
            for th in safe_halls
        }
        total_deficit = sum(ref_deficits.values()) + sum(hall_deficits.values())

        # Only pull excess mineral workers when another safe refinery/base actually has a deficit,
        # or gently rebalance grossly uneven saturated bases without ever re-gathering onto the same base.
        transfer_workers: list[Unit] = []
        source_hall_by_tag: dict[int, int] = {}
        if total_deficit > len(must_assign):
            need_from_excess = total_deficit - len(must_assign)
            for th in safe_halls:
                if need_from_excess <= 0:
                    break
                ideal = hall_ideals[th.tag]
                assigned = hall_workers[th.tag]
                if len(assigned) <= ideal:
                    continue
                surplus = len(assigned) - ideal
                take_limit = min(surplus, need_from_excess)
                kept: list[Unit] = []
                taken = 0
                for w in assigned:
                    if (
                        taken < take_limit
                        and not w.is_carrying_minerals
                        and not w.is_carrying_vespene
                    ):
                        transfer_workers.append(w)
                        source_hall_by_tag[w.tag] = th.tag
                        taken += 1
                        need_from_excess -= 1
                    else:
                        kept.append(w)
                hall_workers[th.tag] = kept
        elif total_deficit == 0 and len(safe_halls) >= 2:
            most_sat = max(
                safe_halls,
                key=lambda th: len(hall_workers[th.tag]) - hall_ideals[th.tag],
            )
            least_sat = min(
                safe_halls,
                key=lambda th: len(hall_workers[th.tag]) - hall_ideals[th.tag],
            )
            most_count = len(hall_workers[most_sat.tag])
            least_count = len(hall_workers[least_sat.tag])
            if (
                most_sat.tag != least_sat.tag
                and most_count > hall_ideals[most_sat.tag]
                and (most_count - least_count) >= 3
            ):
                transfer_count = (most_count - least_count) // 2
                patches_dst = patches_by_hall[least_sat.tag]
                kept_src: list[Unit] = []
                moved = 0
                for w in hall_workers[most_sat.tag]:
                    if (
                        moved < transfer_count
                        and not w.is_carrying_minerals
                        and not w.is_carrying_vespene
                    ):
                        w.gather(patches_dst[moved % len(patches_dst)])
                        hall_workers[least_sat.tag].append(w)
                        moved += 1
                    else:
                        kept_src.append(w)
                hall_workers[most_sat.tag] = kept_src

        must_assign_tags = {w.tag for w in must_assign}
        assignable: list[Unit] = must_assign + transfer_workers
        if not assignable:
            return

        # 1. Fill under-saturated safe refineries (when not gas-throttled).
        for ref in safe_refineries:
            cap = min(desired_gas, ref.ideal_harvesters or 3)
            needed = cap - len(ref_workers[ref.tag])
            if needed <= 0 or not assignable:
                continue
            # Prefer non-carrying workers and must-assign workers closest to the refinery.
            assignable.sort(
                key=lambda w: (
                    1 if (w.is_carrying_minerals or w.is_carrying_vespene) else 0,
                    0 if w.tag in must_assign_tags else 1,
                    w.distance_to(ref.position),
                )
            )
            for _ in range(min(needed, len(assignable))):
                w = assignable.pop(0)
                w.gather(ref)
                ref_workers[ref.tag].append(w)

        # 2. Fill under-saturated safe mineral bases.
        safe_halls_sorted = sorted(
            safe_halls,
            key=lambda th: len(hall_workers[th.tag]) - hall_ideals[th.tag],
        )
        for th in safe_halls_sorted:
            if not assignable:
                break
            needed = hall_ideals[th.tag] - len(hall_workers[th.tag])
            if needed <= 0:
                continue
            eligible = [
                w for w in assignable if source_hall_by_tag.get(w.tag) != th.tag
            ]
            if not eligible:
                continue
            patches = patches_by_hall[th.tag]
            eligible.sort(key=lambda w: w.distance_to(th.position))
            for idx, w in enumerate(eligible[:needed]):
                assignable.remove(w)
                if w.is_carrying_minerals or w.is_carrying_vespene:
                    nearest_hall = min(
                        all_landed_halls,
                        key=lambda h: h.distance_to(w.position),
                        default=None,
                    )
                    if nearest_hall is not None and nearest_hall.tag in safe_hall_tags:
                        if not w.is_returning:
                            w.return_resource()
                    else:
                        w.gather(patches[idx % len(patches)])
                else:
                    w.gather(patches[idx % len(patches)])
                hall_workers[th.tag].append(w)

        # 3. Remaining truly free/idle/relocated workers go to the least-saturated safe base;
        #    never disturb any leftover transfer_workers.
        remaining_must = [w for w in assignable if w.tag in must_assign_tags]
        if remaining_must:
            for idx, w in enumerate(remaining_must):
                best_th = min(
                    safe_halls,
                    key=lambda th: (
                        len(hall_workers[th.tag]) / max(1, len(patches_by_hall[th.tag]))
                    ),
                )
                patches = patches_by_hall[best_th.tag]
                hall_workers[best_th.tag].append(w)
                if w.is_carrying_minerals or w.is_carrying_vespene:
                    nearest_hall = min(
                        all_landed_halls,
                        key=lambda h: h.distance_to(w.position),
                        default=None,
                    )
                    if nearest_hall is not None and nearest_hall.tag in safe_hall_tags:
                        if not w.is_returning:
                            w.return_resource()
                    else:
                        w.gather(patches[idx % len(patches)])
                elif w.order_target not in safe_mineral_tags_by_hall[best_th.tag]:
                    w.gather(patches[idx % len(patches)])

    def _manage_orbitals(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        now: float,
    ) -> None:
        """Spend Orbital Command energy on cloak scans, siege vision, emergency drops, or MULEs."""
        orbitals = [
            th
            for th in bot.townhalls
            if th.type_id == UnitTypeId.ORBITALCOMMAND
            and th.is_ready
            and not th.is_flying
            and th.energy >= 50
            and th.tag not in bot.unit_tags_received_action
        ]
        if not orbitals:
            return

        # 1. Scan hidden cloaked/burrowed enemies threatening our units or bases.
        cloaked_threats = [
            e
            for e in bot.enemy_units
            if (
                not e.can_be_attacked
                or (e.is_cloaked and not e.is_revealed)
                or e.is_burrowed
            )
            and (
                any(u.distance_to(e.position) <= 13.5 for u in bot.units)
                or any(s.distance_to(e.position) <= 13.5 for s in bot.structures)
            )
        ]
        for threat in cloaked_threats:
            if not orbitals:
                break
            covered_by_raven = any(
                r.distance_to(threat.position) <= 10.5
                for r in bot.units(UnitTypeId.RAVEN)
            )
            covered_by_turret = any(
                t.distance_to(threat.position) <= 10.5
                for t in bot.structures(UnitTypeId.MISSILETURRET).ready
            )
            covered_by_scan = any(
                pos.distance_to(threat.position) <= 10.0
                for pos, _ in self.last_scan_positions
            )
            if covered_by_raven or covered_by_turret or covered_by_scan:
                continue
            oc = max(orbitals, key=lambda o: o.energy)
            orbitals.remove(oc)
            oc(AbilityId.SCANNERSWEEP_SCAN, target=threat.position)
            self.last_scan_positions.append((threat.position, now))

        if not orbitals:
            return

        # 2. High-ground siege tank vision scan when pushing into remembered structures/tanks.
        sieged_tanks = bot.units(UnitTypeId.SIEGETANKSIEGED)
        if sieged_tanks:
            for rec in intel.tracked_enemies.values():
                if not rec.is_structure and rec.type_id not in {
                    UnitTypeId.SIEGETANKSIEGED,
                    UnitTypeId.LURKERMPBURROWED,
                }:
                    continue
                if safe_is_visible(bot, rec.position):
                    continue
                if any(
                    pos.distance_to(rec.position) <= 10.0
                    for pos, _ in self.last_scan_positions
                ):
                    continue
                if any(
                    7.0 <= t.distance_to(rec.position) <= 13.0 for t in sieged_tanks
                ):
                    eligible = [
                        oc
                        for oc in orbitals
                        if not intel.enemy_has_cloak
                        or oc.energy >= 100
                        or len(orbitals) >= 2
                    ]
                    if eligible:
                        oc = max(eligible, key=lambda o: o.energy)
                        orbitals.remove(oc)
                        oc(AbilityId.SCANNERSWEEP_SCAN, target=rec.position)
                        self.last_scan_positions.append((rec.position, now))
                        break

        if not orbitals:
            return

        # 3. Emergency Extra Supplies (heals & raises depot HP to 500 in 5.0.14+).
        if (
            bot.supply_left <= 0
            and bot.supply_cap < 200
            and bot.minerals >= 150
            and not any(
                d.build_progress >= 0.70 for d in bot.structures(DEPOT_TYPES).not_ready
            )
        ):
            eligible_depots = [
                d
                for d in bot.structures(DEPOT_TYPES).ready
                if not d.has_buff(BuffId.SUPPLYDROP)
            ]
            if eligible_depots:
                oc = max(orbitals, key=lambda o: o.energy)
                orbitals.remove(oc)
                depot = max(eligible_depots, key=lambda d: d.health_percentage)
                oc(AbilityId.SUPPLYDROP_SUPPLYDROP, target=depot)

        # 4. Drop MULEs on richest safe mineral patches.
        safe_halls = [
            th for th in base_mgr.get_safe_landed_townhalls(bot) if th.is_ready
        ]
        safe_patches: list[Unit] = []
        for th in safe_halls:
            for m in bot.mineral_field:
                if m.distance_to(th.position) <= 9.5:
                    safe_patches.append(m)

        if not safe_patches:
            return

        best_patch = max(
            safe_patches,
            key=lambda m: (
                getattr(m, "mineral_contents", 1000),
                -m.distance_to(safe_halls[0]),
            ),
        )
        # Keep 50 energy scan reserve on 1 OC when cloak threat exists.
        reserve_one = intel.enemy_has_cloak
        orbitals.sort(key=lambda o: o.energy, reverse=True)
        for idx, oc in enumerate(orbitals):
            if reserve_one and idx == 0 and oc.energy < 100:
                continue
            oc(AbilityId.CALLDOWNMULE_CALLDOWNMULE, target=best_patch)
