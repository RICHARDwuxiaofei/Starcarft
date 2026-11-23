"""Base status tracking, outer-base loss triage, flying CC recovery, and safe expansion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from .config import (
    LETHAL_HARASS_TYPES,
    OWN_NON_COMBAT_TYPES,
    STATIC_DEFENSE_TYPES,
    TOWNHALL_FLYING_TYPES,
    TOWNHALL_LANDED_TYPES,
    WORKER_TYPES,
    BotConfig,
    unit_combat_value,
)
from .intel import IntelTracker, safe_is_visible

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


class BaseStatus(Enum):
    """Operational safety classification for a landed Terran base."""

    SAFE = "safe"
    LIGHT_HARASS = "light_harass"
    HEAVY_HARASS = "heavy_harass"
    OVERWHELMED = "overwhelmed"


@dataclass
class BaseInfo:
    """Live telemetry and triage decisions for an owned landed townhall."""

    tag: int
    location: Point2
    is_main: bool
    is_natural: bool
    status: BaseStatus
    enemy_power: float
    enemy_count: int
    friendly_power: float
    has_lethal_harass: bool
    last_threat_time: float
    evacuate_workers: bool
    remaining_minerals: int
    mineral_patch_count: int


@dataclass
class AbandonedBaseRecord:
    """Memory of an overwhelmed outer base for smart avoidance and later reclaim."""

    location: Point2
    abandoned_at: float
    enemy_power: float
    cleared_since: float | None = None


class BaseManager:
    """Monitors base safety, executes outer-base triage, and selects safe expansions."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.main_location: Point2 | None = None
        self.natural_location: Point2 | None = None
        self.bases: dict[int, BaseInfo] = {}
        self.abandoned_expansions: dict[tuple[int, int], AbandonedBaseRecord] = {}
        self._last_threat_times: dict[int, float] = {}
        self.reserved_expansion_targets: dict[Point2, float] = {}

    def initialize(self, bot: BotAI) -> None:
        """Identify main and natural expansion coordinates."""
        self.main_location = bot.start_location
        candidates = [
            p
            for p in bot.expansion_locations_list
            if p.distance_to(bot.start_location) > 6.0
        ]
        if candidates:
            candidates.sort(
                key=lambda p: (
                    round(p.distance_to(bot.start_location), 2),
                    round(p.x, 1),
                    round(p.y, 1),
                )
            )
            self.natural_location = candidates[0]

    def reserve_expansion(self, pos: Point2, expires_at: float) -> None:
        """Reserve an expansion site briefly so duplicate CCs are not queued."""
        self.reserved_expansion_targets[pos] = expires_at

    def update(self, bot: BotAI, intel: IntelTracker) -> None:
        """Evaluate all bases, handle outer-base triage, and land flying CCs safely."""
        now = float(bot.time)
        if self.main_location is None:
            self.initialize(bot)

        # Expire old expansion reservations.
        self.reserved_expansion_targets = {
            pos: exp
            for pos, exp in self.reserved_expansion_targets.items()
            if exp > now
        }

        landed_townhalls = [
            th for th in bot.townhalls if th.type_id in TOWNHALL_LANDED_TYPES
        ]
        flying_townhalls = [
            th for th in bot.townhalls if th.type_id in TOWNHALL_FLYING_TYPES
        ]

        main_th_tag: int | None = None
        if landed_townhalls:
            anchor = self.main_location or bot.start_location
            main_th_tag = min(
                landed_townhalls, key=lambda th: th.distance_to(anchor)
            ).tag

        new_bases: dict[int, BaseInfo] = {}
        for th in landed_townhalls:
            info = self._evaluate_base(bot, th, main_th_tag, len(landed_townhalls), now)
            new_bases[th.tag] = info
            self._handle_base_triage(bot, th, info, now)

        self.bases = new_bases
        self._update_abandoned_records(bot, intel, now)
        self._lift_depleted_or_inbase_ccs(bot, intel, landed_townhalls)
        self._handle_flying_townhalls(bot, intel, flying_townhalls)

    def _evaluate_base(
        self,
        bot: BotAI,
        th: Unit,
        main_th_tag: int | None,
        landed_count: int,
        now: float,
    ) -> BaseInfo:
        pos = th.position
        is_main = th.tag == main_th_tag
        is_natural = (
            self.natural_location is not None
            and pos.distance_to(self.natural_location) <= 8.0
        )

        # Visible hostile units near this base.
        nearby_enemies = [
            e
            for e in bot.enemy_units
            if e.distance_to(pos) <= 16.5
            and (e.type_id not in WORKER_TYPES or e.distance_to(pos) <= 10.0)
            and (
                e.can_attack_ground
                or e.can_attack_air
                or e.type_id in LETHAL_HARASS_TYPES
                or e.type_id in WORKER_TYPES
            )
        ]
        # Exclude pure harmless scouting worker far from mineral patches unless attacking.
        combat_enemies = [
            e
            for e in nearby_enemies
            if e.type_id not in WORKER_TYPES or e.distance_to(pos) <= 8.5
        ]
        enemy_power = sum(
            unit_combat_value(e, include_workers=True) for e in combat_enemies
        )
        enemy_count = len(combat_enemies)
        has_lethal_harass = any(
            e.type_id in LETHAL_HARASS_TYPES or e.is_cloaked or e.is_burrowed
            for e in combat_enemies
        )

        # Nearby friendly defense power (combat units + ready static defenses).
        friendly_units = [
            u
            for u in bot.units
            if u.type_id not in OWN_NON_COMBAT_TYPES and u.distance_to(pos) <= 22.0
        ]
        friendly_static = [
            s
            for s in bot.structures
            if s.is_ready
            and s.type_id in STATIC_DEFENSE_TYPES
            and s.distance_to(pos) <= 14.0
        ]
        friendly_power = sum(unit_combat_value(u) for u in friendly_units) + sum(
            unit_combat_value(s) for s in friendly_static
        )
        if th.is_ready and th.type_id == UnitTypeId.PLANETARYFORTRESS:
            friendly_power += unit_combat_value(th)

        patches = [m for m in bot.mineral_field if m.distance_to(pos) <= 10.5]
        remaining_minerals = sum(
            int(getattr(m, "mineral_contents", 0)) for m in patches
        )

        prev_info = self.bases.get(th.tag)
        last_threat = self._last_threat_times.get(th.tag, -999.0)
        if enemy_power > 0.01:
            last_threat = now
            self._last_threat_times[th.tag] = now

        can_abandon = (not is_main) and landed_count >= 2
        evacuate_workers = False
        if enemy_power <= 0.01:
            # Hysteresis before returning evacuated workers so they don't ping-pong.
            if (
                prev_info
                and prev_info.evacuate_workers
                and (now - last_threat) < self.config.harass_clear_delay
            ):
                status = prev_info.status
                evacuate_workers = True
            else:
                status = BaseStatus.SAFE
                evacuate_workers = False
        elif can_abandon and (
            enemy_power > max(6.0, friendly_power * self.config.base_overwhelm_ratio)
            or (th.health_percentage < 0.45 and enemy_power > friendly_power * 1.1)
        ):
            status = BaseStatus.OVERWHELMED
            evacuate_workers = True
        elif enemy_power <= 3.5 and enemy_count <= 3 and not has_lethal_harass:
            status = BaseStatus.LIGHT_HARASS
            evacuate_workers = False
        else:
            status = BaseStatus.HEAVY_HARASS
            close_to_mineral_line = any(
                e.distance_to(pos) <= 11.5 for e in combat_enemies
            )
            evacuate_workers = bool(
                has_lethal_harass
                or close_to_mineral_line
                or enemy_power > friendly_power * 0.9
            )

        return BaseInfo(
            tag=th.tag,
            location=pos,
            is_main=is_main,
            is_natural=is_natural,
            status=status,
            enemy_power=enemy_power,
            enemy_count=enemy_count,
            friendly_power=friendly_power,
            has_lethal_harass=has_lethal_harass,
            last_threat_time=last_threat,
            evacuate_workers=evacuate_workers,
            remaining_minerals=remaining_minerals,
            mineral_patch_count=len(patches),
        )

    def _handle_base_triage(
        self,
        bot: BotAI,
        th: Unit,
        info: BaseInfo,
        now: float,
    ) -> None:
        """Lift or cancel overwhelmed outer townhalls to preserve resources."""
        if info.status != BaseStatus.OVERWHELMED:
            return

        key = (round(info.location.x), round(info.location.y))
        existing = self.abandoned_expansions.get(key)
        self.abandoned_expansions[key] = AbandonedBaseRecord(
            location=info.location,
            abandoned_at=existing.abandoned_at if existing else now,
            enemy_power=max(
                info.enemy_power, existing.enemy_power if existing else 0.0
            ),
            cleared_since=None,
        )

        if th.tag in bot.unit_tags_received_action:
            return

        # Cancel an unfinished outer CC if it's about to die.
        if not th.is_ready:
            if th.health_percentage < 0.45 and info.enemy_power >= 3.0:
                th(AbilityId.CANCEL_BUILDINPROGRESS)
            return

        # Planetary Fortress cannot lift; it fights while SCVs evacuate.
        if th.type_id == UnitTypeId.PLANETARYFORTRESS:
            return

        should_lift = th.health_percentage < 0.72 or info.enemy_power > max(
            6.5, info.friendly_power * 1.45
        )
        if should_lift:
            lift_ability = (
                AbilityId.LIFT_ORBITALCOMMAND
                if th.type_id == UnitTypeId.ORBITALCOMMAND
                else AbilityId.LIFT_COMMANDCENTER
            )
            th(lift_ability)

    def _update_abandoned_records(
        self,
        bot: BotAI,
        intel: IntelTracker,
        now: float,
    ) -> None:
        """Clear abandoned base flags once confirmed safe or expired."""
        to_delete: list[tuple[int, int]] = []
        for key, rec in self.abandoned_expansions.items():
            if safe_is_visible(bot, rec.location):
                enemy_here = intel.estimate_enemy_power_near(
                    rec.location, 15.0, max_age=5.0, current_time=now
                )
                if enemy_here <= 0.1:
                    if rec.cleared_since is None:
                        rec.cleared_since = now
                    elif (now - rec.cleared_since) >= 4.0:
                        to_delete.append(key)
                else:
                    rec.cleared_since = None
                    rec.enemy_power = max(rec.enemy_power, enemy_here)
            elif (now - rec.abandoned_at) >= self.config.abandoned_recheck_seconds:
                to_delete.append(key)

        for key in to_delete:
            self.abandoned_expansions.pop(key, None)

    def _lift_depleted_or_inbase_ccs(
        self,
        bot: BotAI,
        intel: IntelTracker,
        landed_townhalls: list[Unit],
    ) -> None:
        """Float idle in-base safety CCs or mined-out outer CCs/OCs to open expansions."""
        for th in landed_townhalls:
            info = self.bases.get(th.tag)
            if (
                info is None
                or info.is_main
                or info.status == BaseStatus.OVERWHELMED
                or info.remaining_minerals > 0
                or not th.is_ready
                or not th.is_idle
                or th.tag in bot.unit_tags_received_action
                or th.type_id
                not in {UnitTypeId.COMMANDCENTER, UnitTypeId.ORBITALCOMMAND}
            ):
                continue
            target = self.choose_next_expansion(bot, intel, allow_natural_priority=True)
            if target is not None:
                lift_ability = (
                    AbilityId.LIFT_ORBITALCOMMAND
                    if th.type_id == UnitTypeId.ORBITALCOMMAND
                    else AbilityId.LIFT_COMMANDCENTER
                )
                th(lift_ability)
                break

    def _handle_flying_townhalls(
        self,
        bot: BotAI,
        intel: IntelTracker,
        flying_townhalls: list[Unit],
    ) -> None:
        """Relocate and land flying CCs/OCs at safe unoccupied expansions."""
        if not flying_townhalls:
            return

        reserved_targets: list[Point2] = []
        for th in flying_townhalls:
            if th.tag in bot.unit_tags_received_action:
                continue

            # If already ordered to land somewhere safe, keep it if clear.
            existing_target = th.order_target
            if isinstance(existing_target, Point2) and any(
                o.ability.id
                in {AbilityId.LAND_COMMANDCENTER, AbilityId.LAND_ORBITALCOMMAND}
                for o in th.orders
            ):
                enemy_blocking = intel.estimate_enemy_power_near(
                    existing_target, 14.0, max_age=6.0, current_time=float(bot.time)
                )
                if enemy_blocking <= 0.5:
                    reserved_targets.append(existing_target)
                    continue

            target = self.choose_next_expansion(
                bot,
                intel,
                exclude_positions=reserved_targets,
                allow_natural_priority=True,
            )
            if target is not None:
                reserved_targets.append(target)
                land_ability = (
                    AbilityId.LAND_ORBITALCOMMAND
                    if th.type_id == UnitTypeId.ORBITALCOMMANDFLYING
                    else AbilityId.LAND_COMMANDCENTER
                )
                th(land_ability, target=target)
            else:
                # Hover safely behind main until a safe expansion opens.
                anchor = self.main_location or bot.start_location
                away_dir = anchor.direction_vector(bot.game_info.map_center)
                safe_hover = anchor - away_dir * 4.0
                if th.distance_to(safe_hover) > 4.0:
                    th.move(safe_hover)

    def choose_next_expansion(
        self,
        bot: BotAI,
        intel: IntelTracker,
        exclude_positions: list[Point2] | None = None,
        allow_natural_priority: bool = True,
    ) -> Point2 | None:
        """Select the safest profitable expansion away from enemy pressure."""
        excludes = list(exclude_positions or [])
        excludes.extend(self.reserved_expansion_targets.keys())

        occupied_points = [th.position for th in bot.townhalls]
        for th in bot.townhalls:
            if th.type_id in TOWNHALL_FLYING_TYPES and isinstance(
                th.order_target, Point2
            ):
                occupied_points.append(th.order_target)

        now = float(bot.time)
        candidates: list[tuple[Point2, int]] = []
        fallback_low_mineral: list[tuple[Point2, int]] = []

        for pos in bot.expansion_locations_list:
            if any(pos.distance_to(occ) < 7.5 for occ in occupied_points):
                continue
            if any(pos.distance_to(ex) < 7.5 for ex in excludes):
                continue

            key = (round(pos.x), round(pos.y))
            if key in self.abandoned_expansions:
                continue

            enemy_near = intel.estimate_enemy_power_near(
                pos, 16.0, max_age=20.0, current_time=now
            )
            if enemy_near > 0.5:
                continue
            if any(e.distance_to(pos) <= 16.0 for e in bot.enemy_units) or any(
                s.distance_to(pos) <= 16.0 for s in bot.enemy_structures
            ):
                continue

            minerals_here = sum(
                int(getattr(m, "mineral_contents", 0))
                for m in bot.mineral_field
                if m.distance_to(pos) <= 10.5
            )
            if minerals_here >= self.config.min_expansion_minerals:
                candidates.append((pos, minerals_here))
            elif minerals_here > 0:
                fallback_low_mineral.append((pos, minerals_here))

        pool = candidates or fallback_low_mineral
        if not pool:
            return None

        if allow_natural_priority and self.natural_location is not None:
            for pos, _ in pool:
                if pos.distance_to(self.natural_location) <= 4.0:
                    return pos

        home = self.main_location or bot.start_location
        enemy_ref = intel.confirmed_enemy_main or bot.game_info.map_center

        def score(entry: tuple[Point2, int]) -> tuple[float, float, float]:
            pos, minerals_here = entry
            dist_home = pos.distance_to(home)
            dist_enemy = pos.distance_to(enemy_ref)
            mineral_penalty = 20.0 if minerals_here < 2500 else 0.0
            # Favor closer expansions that also maximize distance from enemy main.
            val = dist_home - 0.65 * dist_enemy + mineral_penalty
            return (round(val, 2), round(pos.x, 1), round(pos.y, 1))

        return min(pool, key=score)[0]

    def get_safe_landed_townhalls(self, bot: BotAI) -> list[Unit]:
        """Return landed townhalls whose workers do not need evacuation."""
        result: list[Unit] = []
        for th in bot.townhalls:
            if th.type_id not in TOWNHALL_LANDED_TYPES:
                continue
            info = self.bases.get(th.tag)
            if info is None or not info.evacuate_workers:
                result.append(th)
        return result

    def get_defendable_threatened_bases(self) -> list[BaseInfo]:
        """Return bases currently under attack that are not abandoned."""
        threatened = [
            info
            for info in self.bases.values()
            if info.status in {BaseStatus.LIGHT_HARASS, BaseStatus.HEAVY_HARASS}
            and info.enemy_power > 0.01
        ]
        threatened.sort(
            key=lambda b: (
                0 if b.is_main else (1 if b.is_natural else 2),
                -b.enemy_power,
            )
        )
        return threatened

    def get_reclaim_candidate(
        self, own_army_power: float
    ) -> AbandonedBaseRecord | None:
        """Return an abandoned outer base if our army is strong enough to retake it."""
        if not self.abandoned_expansions:
            return None
        candidates = [
            rec
            for rec in self.abandoned_expansions.values()
            if own_army_power >= max(18.0, rec.enemy_power * 1.25)
        ]
        if not candidates:
            return None
        anchor = self.main_location or Point2((0.0, 0.0))
        return min(candidates, key=lambda r: r.location.distance_to(anchor))

    def get_rally_point(self, bot: BotAI, intel: IntelTracker) -> Point2:
        """Choose a cohesive staging/rally point at our natural or forward-most safe base."""
        home = self.main_location or bot.start_location
        enemy_ref = intel.confirmed_enemy_main or bot.game_info.map_center
        safe_ready = [
            th
            for th in bot.townhalls
            if th.type_id in TOWNHALL_LANDED_TYPES
            and self.bases.get(th.tag, None) is not None
            and self.bases[th.tag].status != BaseStatus.OVERWHELMED
        ]
        if safe_ready:
            # Rally near our forward-most safe base toward the enemy/map center.
            anchor_th = min(
                safe_ready,
                key=lambda th: th.distance_to(enemy_ref),
            )
            direction = anchor_th.position.direction_vector(enemy_ref)
            return anchor_th.position + direction * 6.5

        direction = home.direction_vector(enemy_ref)
        return home + direction * 8.0
