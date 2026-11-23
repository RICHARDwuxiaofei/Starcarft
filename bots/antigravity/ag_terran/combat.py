"""Unit-level combat micro: Bio stutter-step/Stim, Tank leapfrog, Medivac, Viking, Raven."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit

from .bases import BaseManager
from .config import (
    AIR_PRODUCTION_PRIORITY_TYPES,
    ANTI_AIR_THREAT_TYPES,
    DEPOT_TYPES,
    MATRIX_TARGETS,
    OWN_NON_COMBAT_TYPES,
    STATIC_DEFENSE_TYPES,
    TANK_TYPES,
    WORKER_TYPES,
    BotConfig,
)
from .intel import IntelTracker
from .placement import PlacementManager
from .tactics import ArmyPosture, TacticsManager

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


HIGH_VALUE_SIEGE_TARGETS: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SIEGETANKSIEGED,
        UnitTypeId.SIEGETANK,
        UnitTypeId.LURKERMPBURROWED,
        UnitTypeId.LURKERMP,
        UnitTypeId.COLOSSUS,
        UnitTypeId.DISRUPTOR,
        UnitTypeId.HIGHTEMPLAR,
        UnitTypeId.INFESTOR,
        UnitTypeId.BANELING,
        UnitTypeId.PLANETARYFORTRESS,
        UnitTypeId.BUNKER,
        UnitTypeId.PHOTONCANNON,
        UnitTypeId.SPINECRAWLER,
    }
)


class CombatMicro:
    """Executes tactical micro for Bio, Tanks, Medivacs, Vikings, Ravens, Bunkers, Depots."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.tank_last_enemy_seen: dict[int, float] = {}

    def on_unit_destroyed(self, unit_tag: int) -> None:
        """Remove destroyed tanks from siege timer tracking."""
        self.tank_last_enemy_seen.pop(unit_tag, None)

    def update(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        placement: PlacementManager,
        tactics: TacticsManager,
        reserved_scout_tags: set[int],
    ) -> None:
        """Run all structure and combat unit micro for the current step."""
        self._manage_ramp_depots(bot, placement)
        self._manage_bunkers(bot, tactics)

        target = tactics.target_position or base_mgr.get_rally_point(bot, intel)
        posture = tactics.posture

        combat_units = [
            u
            for u in bot.units
            if u.type_id not in OWN_NON_COMBAT_TYPES
            and u.tag not in reserved_scout_tags
        ]
        if not combat_units:
            return

        # Local harass defense detachment when main army stays pushing.
        local_squad_tags: set[int] = set()
        if tactics.local_defense_target is not None and tactics.local_defense_count > 0:
            bio_candidates = [
                u
                for u in combat_units
                if u.type_id in {UnitTypeId.MARINE, UnitTypeId.MARAUDER}
            ]
            bio_candidates.sort(
                key=lambda u: u.distance_to(tactics.local_defense_target)
            )
            for u in bio_candidates[: tactics.local_defense_count]:
                local_squad_tags.add(u.tag)

        # Compute Tank anchor and Bio centroid for cohesion & Medivac positioning.
        tanks = [u for u in combat_units if u.type_id in TANK_TYPES]
        bio_main = [
            u
            for u in combat_units
            if u.type_id in {UnitTypeId.MARINE, UnitTypeId.MARAUDER, UnitTypeId.GHOST}
            and u.tag not in local_squad_tags
        ]
        tank_anchor: Point2 | None = None
        if tanks:
            tx = sum(t.position.x for t in tanks) / len(tanks)
            ty = sum(t.position.y for t in tanks) / len(tanks)
            tank_anchor = Point2((tx, ty))

        bio_center: Point2 | None = None
        if bio_main:
            bx = sum(b.position.x for b in bio_main) / len(bio_main)
            by = sum(b.position.y for b in bio_main) / len(bio_main)
            bio_center = Point2((bx, by))

        army_center = bio_center or tank_anchor or target

        # All visible attackable enemies (units + structures).
        live_enemies = [e for e in bot.enemy_units if e.can_be_attacked]
        live_enemy_structs = [s for s in bot.enemy_structures if s.can_be_attacked]

        self._micro_tanks(
            bot, intel, tanks, live_enemies, live_enemy_structs, posture, target
        )
        self._micro_bio(
            bot,
            bio_main,
            [u for u in combat_units if u.tag in local_squad_tags],
            tactics.local_defense_target,
            live_enemies,
            live_enemy_structs,
            posture,
            target,
            tank_anchor,
            bio_center,
        )
        self._micro_medivacs(
            bot,
            [u for u in combat_units if u.type_id == UnitTypeId.MEDIVAC],
            bio_main,
            live_enemies,
            army_center,
            target,
        )
        self._micro_vikings(
            bot,
            intel,
            [
                u
                for u in combat_units
                if u.type_id in {UnitTypeId.VIKINGFIGHTER, UnitTypeId.VIKINGASSAULT}
            ],
            live_enemies,
            live_enemy_structs,
            posture,
            target,
            army_center,
        )
        self._micro_ravens(
            bot,
            [u for u in combat_units if u.type_id == UnitTypeId.RAVEN],
            live_enemies,
            army_center,
            base_mgr,
        )
        self._micro_extra_ground(
            bot,
            [
                u
                for u in combat_units
                if u.type_id
                in {
                    UnitTypeId.HELLION,
                    UnitTypeId.HELLIONTANK,
                    UnitTypeId.CYCLONE,
                    UnitTypeId.THOR,
                    UnitTypeId.REAPER,
                }
            ],
            live_enemies,
            live_enemy_structs,
            posture,
            target,
        )

    def _manage_ramp_depots(self, bot: BotAI, placement: PlacementManager) -> None:
        """Raise wall Supply Depots when ground enemies approach; keep lowered otherwise."""
        wall_points = list(placement.ramp_depot_positions)
        if not wall_points:
            return
        ground_enemies = [e for e in bot.enemy_units if not e.is_flying]
        for depot in bot.structures(DEPOT_TYPES).ready:
            if depot.tag in bot.unit_tags_received_action:
                continue
            is_wall = any(depot.distance_to(rp) <= 2.2 for rp in wall_points)
            if not is_wall:
                continue
            enemy_close = any(
                e.distance_to(depot.position) <= 7.5 for e in ground_enemies
            )
            enemy_far = not any(
                e.distance_to(depot.position) <= 9.5 for e in ground_enemies
            )
            if depot.type_id == UnitTypeId.SUPPLYDEPOTLOWERED and enemy_close:
                # Avoid trapping our own unit directly on top of the depot footprint.
                own_on_top = any(
                    u.distance_to(depot.position) <= 1.2 for u in bot.units
                )
                if not own_on_top:
                    depot(AbilityId.MORPH_SUPPLYDEPOT_RAISE)
            elif depot.type_id == UnitTypeId.SUPPLYDEPOT and enemy_far:
                depot(AbilityId.MORPH_SUPPLYDEPOT_LOWER)

    def _manage_bunkers(self, bot: BotAI, tactics: TacticsManager) -> None:
        """Load nearby Marines during defense and unload them when pushing out."""
        bunkers = list(bot.structures(UnitTypeId.BUNKER).ready)
        if not bunkers:
            return
        marines = list(bot.units(UnitTypeId.MARINE))
        for bunker in bunkers:
            if bunker.tag in bot.unit_tags_received_action:
                continue
            enemy_near = any(
                e.distance_to(bunker.position) <= 14.5 for e in bot.enemy_units
            )
            if (
                enemy_near
                and bunker.cargo_left > 0
                and tactics.posture != ArmyPosture.ATTACK
            ):
                nearby_marines = [
                    m
                    for m in marines
                    if m.distance_to(bunker.position) <= 10.0
                    and m.tag not in bot.unit_tags_received_action
                ]
                nearby_marines.sort(key=lambda m: m.distance_to(bunker.position))
                for m in nearby_marines[: bunker.cargo_left]:
                    bunker(AbilityId.LOAD_BUNKER, target=m)
                    break
            elif bunker.cargo_used > 0 and (
                not enemy_near
                and tactics.posture in {ArmyPosture.ATTACK, ArmyPosture.RECLAIM}
            ):
                bunker(AbilityId.UNLOADALL_BUNKER)

    def _micro_tanks(
        self,
        bot: BotAI,
        intel: IntelTracker,
        tanks: list[Unit],
        live_enemies: list[Unit],
        live_enemy_structs: list[Unit],
        posture: ArmyPosture,
        target: Point2,
    ) -> None:
        """Siege in combat range, focus high-value targets, and unsiege promptly when clear."""
        if not tanks:
            return

        now = float(bot.time)
        ground_enemies = [e for e in live_enemies if not e.is_flying]
        ground_all = ground_enemies + [s for s in live_enemy_structs if not s.is_flying]

        remembered_ground = [
            r
            for r in intel.tracked_enemies.values()
            if not r.is_flying
            and (r.is_combat or r.type_id in STATIC_DEFENSE_TYPES or r.is_structure)
            and (r.is_structure or (now - r.last_seen) <= 12.0)
        ]

        sieged_tanks = [t for t in tanks if t.type_id == UnitTypeId.SIEGETANKSIEGED]
        unsiege_budget = (
            max(1, math.ceil(len(sieged_tanks) / 2)) if len(sieged_tanks) > 1 else 1
        )
        unsieges_issued = 0

        for tank in tanks:
            if tank.tag in bot.unit_tags_received_action:
                continue

            # Check live + remembered ground threats around this tank.
            near_live = [
                e
                for e in ground_all
                if e.distance_to(tank.position) <= self.config.unsiege_clear_range
            ]
            combat_in_siege_range = [
                e
                for e in ground_enemies
                if e.distance_to(tank.position) <= self.config.siege_engage_range
                and (
                    e.type_id not in WORKER_TYPES
                    or e.distance_to(tank.position) <= 10.0
                )
            ] + [
                s
                for s in live_enemy_structs
                if s.distance_to(tank.position) <= self.config.siege_engage_range
            ]
            remembered_near = [
                r
                for r in remembered_ground
                if r.position.distance_to(tank.position)
                <= self.config.unsiege_clear_range
            ]
            remembered_siege_threat = any(
                (r.type_id in HIGH_VALUE_SIEGE_TARGETS or r.is_structure)
                and r.position.distance_to(tank.position) <= 13.0
                for r in remembered_near
            )

            if near_live or remembered_near:
                self.tank_last_enemy_seen[tank.tag] = now
            last_seen = self.tank_last_enemy_seen.get(tank.tag, now)

            if tank.type_id == UnitTypeId.SIEGETANK:
                should_siege = posture != ArmyPosture.RETREAT and (
                    bool(combat_in_siege_range)
                    or (posture == ArmyPosture.ATTACK and remembered_siege_threat)
                )
                if should_siege:
                    tank(AbilityId.SIEGEMODE_SIEGEMODE)
                    continue

                if posture == ArmyPosture.RETREAT:
                    tank.move(target)
                elif tank.distance_to(target) > 4.0:
                    tank.attack(target)
                continue

            # Tank is SIEGED:
            in_range_targets = [
                e for e in ground_all if 2.0 <= e.distance_to(tank.position) <= 13.2
            ]
            if in_range_targets and tank.weapon_cooldown == 0:
                best = max(
                    in_range_targets,
                    key=lambda e: (
                        2
                        if e.type_id in HIGH_VALUE_SIEGE_TARGETS
                        else (1 if not e.is_structure else 0),
                        -e.health_percentage,
                        -e.distance_to(tank.position),
                    ),
                )
                tank.attack(best)
                continue

            # Unsiege if retreating (and not surrounded at point-blank) or area is clear.
            min_live_dist = min(
                (e.distance_to(tank.position) for e in ground_enemies),
                default=999.0,
            )
            if posture == ArmyPosture.RETREAT and min_live_dist > 5.0:
                tank(AbilityId.UNSIEGE_UNSIEGE)
                continue

            if not near_live:
                clear_elapsed = now - last_seen
                far_from_goal = tank.distance_to(target) > 13.5
                remembered_further = any(
                    r.position.distance_to(tank.position) <= 22.0
                    for r in remembered_ground
                )
                if (
                    clear_elapsed >= self.config.unsiege_clear_delay
                    and far_from_goal
                    and (not remembered_further or unsieges_issued < unsiege_budget)
                ):
                    tank(AbilityId.UNSIEGE_UNSIEGE)
                    unsieges_issued += 1

    def _score_bio_target(self, unit: Unit, enemy: Unit) -> float:
        """Prioritize dangerous attackable targets in weapon range, weighted by low HP."""
        dist = unit.distance_to(enemy.position)
        in_range = 1.0 if unit.target_in_range(enemy) else 0.0
        if enemy.type_id in {
            UnitTypeId.BANELING,
            UnitTypeId.HIGHTEMPLAR,
            UnitTypeId.DISRUPTOR,
            UnitTypeId.SIEGETANK,
            UnitTypeId.SIEGETANKSIEGED,
            UnitTypeId.MEDIVAC,
            UnitTypeId.LURKERMP,
            UnitTypeId.LURKERMPBURROWED,
            UnitTypeId.COLOSSUS,
        }:
            tier = 4.0
        elif not enemy.is_structure and enemy.type_id not in WORKER_TYPES:
            tier = 3.0
        elif enemy.type_id in STATIC_DEFENSE_TYPES:
            tier = 2.5
        elif enemy.type_id in WORKER_TYPES:
            tier = 1.8
        else:
            tier = 0.5
        return (
            in_range * 10.0
            + tier * 2.0
            + (1.0 - enemy.health_percentage) * 1.5
            - dist * 0.1
        )

    def _micro_bio(
        self,
        bot: BotAI,
        bio_main: list[Unit],
        local_squad: list[Unit],
        local_target: Point2 | None,
        live_enemies: list[Unit],
        live_enemy_structs: list[Unit],
        posture: ArmyPosture,
        target: Point2,
        tank_anchor: Point2 | None,
        bio_center: Point2 | None,
    ) -> None:
        """Stutter-step, Stimpack, focus-fire, and keep combined-arms cohesion with Tanks."""
        stim_researched = UpgradeId.STIMPACK in bot.state.upgrades
        all_targets = live_enemies + live_enemy_structs

        # Local harass defense squad moves directly to clear minor harass.
        if local_squad and local_target is not None:
            for u in local_squad:
                self._micro_single_bio(
                    u,
                    all_targets,
                    live_enemies,
                    ArmyPosture.DEFEND,
                    local_target,
                    stim_researched,
                    None,
                )

        if not bio_main:
            return

        # Keep bio from outrunning Siege Tanks when pushing unopposed.
        effective_target = target
        if (
            posture == ArmyPosture.ATTACK
            and tank_anchor is not None
            and bio_center is not None
            and bio_center.distance_to(tank_anchor) > 13.5
            and bio_center.distance_to(target) < tank_anchor.distance_to(target)
            and not any(e.distance_to(bio_center) <= 13.0 for e in live_enemies)
        ):
            effective_target = tank_anchor.towards(target, 6.0)

        for u in bio_main:
            if u.tag in bot.unit_tags_received_action:
                continue
            self._micro_single_bio(
                u,
                all_targets,
                live_enemies,
                posture,
                effective_target,
                stim_researched,
                bio_center,
            )

    def _micro_single_bio(
        self,
        unit: Unit,
        all_targets: list[Unit],
        live_enemies: list[Unit],
        posture: ArmyPosture,
        dest: Point2,
        stim_researched: bool,
        bio_center: Point2 | None,
    ) -> None:
        if posture == ArmyPosture.RETREAT:
            unit.move(dest)
            return

        valid_targets = [
            e
            for e in all_targets
            if e.distance_to(unit.position) <= 11.5
            and (
                (not e.is_flying and unit.can_attack_ground)
                or (
                    (e.is_flying or e.type_id == UnitTypeId.COLOSSUS)
                    and unit.can_attack_air
                )
            )
        ]

        if not valid_targets:
            if unit.distance_to(dest) > 3.5:
                unit.attack(dest)
            elif bio_center is not None and unit.distance_to(bio_center) > 7.5:
                unit.attack(bio_center)
            return

        # Stimpack check when engaging real enemy combat units.
        if stim_researched and unit.health_percentage > 0.55:
            stim_buff = (
                BuffId.STIMPACKMARAUDER
                if unit.type_id == UnitTypeId.MARAUDER
                else BuffId.STIMPACK
            )
            if not unit.has_buff(stim_buff):
                combat_close = any(
                    e.type_id not in WORKER_TYPES
                    and e.distance_to(unit.position) <= 9.5
                    for e in live_enemies
                )
                if combat_close:
                    ability = (
                        AbilityId.EFFECT_STIM_MARAUDER
                        if unit.type_id == UnitTypeId.MARAUDER
                        else AbilityId.EFFECT_STIM_MARINE
                    )
                    unit(ability)
                    return

        best = max(valid_targets, key=lambda e: self._score_bio_target(unit, e))
        if unit.weapon_cooldown == 0:
            unit.attack(best)
            return

        # Cooldown active -> kite back from melee/close threats or stutter forward.
        dangerous_close = [
            e
            for e in live_enemies
            if e.type_id not in WORKER_TYPES
            and not e.is_flying
            and e.distance_to(unit.position) <= 4.6
            and e.ground_range <= 3.0
        ]
        if dangerous_close:
            threat = min(dangerous_close, key=lambda e: e.distance_to(unit.position))
            unit.move(unit.position.towards(threat.position, -2.5))
        elif unit.health_percentage < 0.45 and unit.distance_to(best.position) <= 6.5:
            unit.move(unit.position.towards(best.position, -2.2))
        elif not unit.target_in_range(best) and posture in {
            ArmyPosture.ATTACK,
            ArmyPosture.RECLAIM,
        }:
            unit.move(unit.position.towards(best.position, 2.0))

    def _micro_medivacs(
        self,
        bot: BotAI,
        medivacs: list[Unit],
        bio_main: list[Unit],
        live_enemies: list[Unit],
        army_center: Point2,
        target: Point2,
    ) -> None:
        """Keep Medivacs behind Bio, trigger boost under fire, and heal injured infantry."""
        if not medivacs:
            return

        injured_bio = [b for b in bio_main if b.health_percentage < 0.92]
        if injured_bio:
            follow_target = min(injured_bio, key=lambda b: b.health_percentage).position
        elif bio_main:
            fx = sum(b.position.x for b in bio_main) / len(bio_main)
            fy = sum(b.position.y for b in bio_main) / len(bio_main)
            follow_target = Point2((fx, fy))
        else:
            follow_target = army_center

        # Slightly trail behind Bio away from target.
        staging = follow_target.towards(target, -2.5)
        aa_threats = [
            e
            for e in live_enemies
            if e.can_attack_air or e.type_id in ANTI_AIR_THREAT_TYPES
        ]

        for med in medivacs:
            if med.tag in bot.unit_tags_received_action:
                continue

            close_aa = [e for e in aa_threats if e.distance_to(med.position) <= 8.0]
            if not med.has_buff(BuffId.MEDIVACSPEEDBOOST) and (
                med.health_percentage < 0.65
                or bool(close_aa)
                or med.distance_to(follow_target) > 13.0
            ):
                med(AbilityId.EFFECT_MEDIVACIGNITEAFTERBURNERS)
                continue

            if close_aa and med.health_percentage < 0.70:
                nearest_aa = min(close_aa, key=lambda e: e.distance_to(med.position))
                med.move(med.position.towards(nearest_aa.position, -4.0))
                continue

            if injured_bio:
                hurt = min(injured_bio, key=lambda b: b.distance_to(med.position))
                if med.distance_to(hurt.position) > 5.5:
                    med.move(hurt.position.towards(target, -2.0))
            elif med.distance_to(staging) > 3.5:
                med.move(staging)

    def _micro_vikings(
        self,
        bot: BotAI,
        intel: IntelTracker,
        vikings: list[Unit],
        live_enemies: list[Unit],
        live_enemy_structs: list[Unit],
        posture: ArmyPosture,
        target: Point2,
        army_center: Point2,
    ) -> None:
        """Prioritize air superiority, high-ground tank vision, and safe ground cleanup."""
        if not vikings:
            return

        air_targets = [
            e for e in live_enemies if e.is_flying or e.type_id == UnitTypeId.COLOSSUS
        ]
        ground_enemies = [e for e in live_enemies if not e.is_flying]
        ground_all = ground_enemies + [s for s in live_enemy_structs if not s.is_flying]
        any_known_air = bool(air_targets) or intel.enemy_air_count > 0

        for v in vikings:
            if v.tag in bot.unit_tags_received_action:
                continue

            if v.type_id == UnitTypeId.VIKINGASSAULT:
                # Immediately lift back to Fighter mode if any air target appears.
                if air_targets or (
                    intel.enemy_air_count > 0 and not intel.enemy_main_cleared
                ):
                    v(AbilityId.MORPH_VIKINGFIGHTERMODE)
                    continue
                if ground_all:
                    best_g = min(ground_all, key=lambda g: g.distance_to(v.position))
                    if v.distance_to(best_g.position) <= 14.0:
                        v.attack(best_g)
                        continue
                # Lift to move efficiently if far from action.
                if v.distance_to(target) > 12.0:
                    v(AbilityId.MORPH_VIKINGFIGHTERMODE)
                continue

            # Fighter mode:
            if air_targets:
                best_air = max(
                    air_targets,
                    key=lambda e: (
                        2 if e.type_id in AIR_PRODUCTION_PRIORITY_TYPES else 1,
                        -e.health_percentage,
                        -e.distance_to(v.position),
                    ),
                )
                if v.weapon_cooldown == 0 or not v.target_in_range(best_air):
                    v.attack(best_air)
                else:
                    # Kite short-range air threats slightly.
                    if (
                        best_air.air_range < 7.0
                        and v.distance_to(best_air.position) < 6.5
                    ):
                        v.move(v.position.towards(best_air.position, -2.5))
                continue

            # Land for mop-up when sky is clear, or emergency ground defense when short on ground.
            ground_defenders = sum(
                1
                for u in bot.units
                if u.type_id
                in {
                    UnitTypeId.MARINE,
                    UnitTypeId.MARAUDER,
                    UnitTypeId.SIEGETANK,
                    UnitTypeId.SIEGETANKSIEGED,
                }
            )
            emergency_ground_defend = (
                posture == ArmyPosture.DEFEND and ground_defenders < 4
            )
            if (
                not any_known_air
                and (intel.enemy_main_cleared or emergency_ground_defend)
                and ground_all
                and any(g.distance_to(v.position) <= 10.0 for g in ground_all)
            ):
                v(AbilityId.MORPH_VIKINGASSAULTMODE)
                continue

            # Otherwise hover safely over army to provide high-ground vision.
            hover = army_center if posture != ArmyPosture.RETREAT else target
            if v.distance_to(hover) > 6.5:
                v.move(hover)

    def _micro_ravens(
        self,
        bot: BotAI,
        ravens: list[Unit],
        live_enemies: list[Unit],
        army_center: Point2,
        base_mgr: BaseManager,
    ) -> None:
        """Use Interference Matrix, Anti-Armor Missile, Auto-Turret, and mobile detection."""
        if not ravens:
            return

        # Check if any own base has cloaked enemies needing Raven detector coverage.
        cloaked_home_threat = next(
            (
                e
                for e in bot.enemy_units
                if (e.is_cloaked or e.is_burrowed)
                and any(
                    e.distance_to(b.location) <= 16.0 for b in base_mgr.bases.values()
                )
            ),
            None,
        )

        for idx, raven in enumerate(ravens):
            if raven.tag in bot.unit_tags_received_action:
                continue

            # 1. Interference Matrix on high-value mechanical/psionic threat.
            if raven.energy >= 75:
                matrix_cands = [
                    e
                    for e in live_enemies
                    if e.type_id in MATRIX_TARGETS
                    and not e.has_buff(BuffId.RAVENSCRAMBLERMISSILE)
                    and e.distance_to(raven.position) <= 11.0
                ]
                if matrix_cands:
                    target = max(matrix_cands, key=lambda e: e.health + e.shield)
                    raven(AbilityId.EFFECT_INTERFERENCEMATRIX, target=target)
                    continue

                # 2. Anti-Armor Missile on dense enemy combat clumps.
                combat_near = [
                    e
                    for e in live_enemies
                    if e.type_id not in WORKER_TYPES
                    and e.distance_to(raven.position) <= 11.0
                ]
                if len(combat_near) >= 5:
                    best_cluster = max(
                        combat_near,
                        key=lambda c: sum(
                            1 for o in combat_near if o.distance_to(c.position) <= 3.5
                        ),
                    )
                    cluster_count = sum(
                        1
                        for o in combat_near
                        if o.distance_to(best_cluster.position) <= 3.5
                    )
                    if cluster_count >= 5:
                        raven(AbilityId.EFFECT_ANTIARMORMISSILE, target=best_cluster)
                        continue

            # 3. Auto-Turret when at high energy or repelling close ground harass.
            if raven.energy >= 135 or (
                cloaked_home_threat is not None and raven.energy >= 50
            ):
                turret_targets = [
                    e
                    for e in live_enemies
                    if not e.is_flying and e.distance_to(raven.position) <= 7.0
                ]
                if turret_targets:
                    near_e = min(
                        turret_targets, key=lambda e: e.distance_to(raven.position)
                    )
                    drop_pt = raven.position.towards(near_e.position, 2.0)
                    raven(AbilityId.BUILDAUTOTURRET_AUTOTURRET, target=drop_pt)
                    continue

            # Keep 1 Raven near cloaked home threat if needed; otherwise shadow army safely.
            if idx == 0 and cloaked_home_threat is not None:
                if raven.distance_to(cloaked_home_threat.position) > 7.0:
                    raven.move(cloaked_home_threat.position)
                continue

            if raven.distance_to(army_center) > 6.5:
                raven.move(army_center)

    def _micro_extra_ground(
        self,
        bot: BotAI,
        units: list[Unit],
        live_enemies: list[Unit],
        live_enemy_structs: list[Unit],
        posture: ArmyPosture,
        target: Point2,
    ) -> None:
        """Basic attack-move / retreat support for any supplementary Factory/Barracks units."""
        for u in units:
            if u.tag in bot.unit_tags_received_action:
                continue
            if posture == ArmyPosture.RETREAT:
                u.move(target)
                continue
            if u.distance_to(target) > 4.0:
                u.attack(target)
