"""Legal-vision enemy tracking, 4-player spawn discovery, and threat intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from .config import (
    AIR_PRODUCTION_PRIORITY_TYPES,
    CLOAK_OR_BURROW_ENEMY_TYPES,
    HEAVY_ARMOR_TYPES,
    LETHAL_HARASS_TYPES,
    STATIC_DEFENSE_TYPES,
    WORKER_TYPES,
    BotConfig,
    unit_combat_value,
)

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


@dataclass
class TrackedEnemy:
    """Snapshot of a legally observed enemy unit or structure."""

    tag: int
    type_id: UnitTypeId
    position: Point2
    is_structure: bool
    is_flying: bool
    is_combat: bool
    combat_value: float
    last_seen: float
    is_cloaked_or_burrowed: bool


def safe_is_visible(bot: BotAI, pos: Point2) -> bool:
    """Safely query bot visibility grid at a map position with bounds guard."""
    rounded = pos.rounded
    try:
        vis = bot.state.visibility
        if 0 <= rounded.x < vis.width and 0 <= rounded.y < vis.height:
            return int(vis[rounded]) == 2
    except (AttributeError, IndexError, AssertionError, TypeError):
        pass
    # Fallback using nearby own unit sight radius if visibility grid is unavailable.
    return any(u.distance_to(pos) <= 8.5 for u in bot.units) or any(
        s.distance_to(pos) <= 9.0 for s in bot.structures
    )


def _pos_key(pos: Point2) -> tuple[int, int]:
    return (round(pos.x), round(pos.y))


class IntelTracker:
    """Tracks legal vision of enemy units, structures, tech, and spawn locations."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.candidate_enemy_spawns: list[Point2] = []
        self.confirmed_enemy_main: Point2 | None = None
        self.cleared_spawns: set[tuple[int, int]] = set()
        self.tracked_enemies: dict[int, TrackedEnemy] = {}

        self.enemy_has_cloak: bool = False
        self.enemy_has_air_threat: bool = False
        self.enemy_is_rushing: bool = False
        self.enemy_air_count: int = 0
        self.enemy_air_ratio: float = 0.0
        self.enemy_heavy_armor_ratio: float = 0.0
        self.enemy_main_cleared: bool = False
        self.enemy_main_ever_cleared: bool = False

        self.visited_points: dict[tuple[int, int], float] = {}
        self.cleanup_targets: list[Point2] = []

    def initialize(self, bot: BotAI) -> None:
        """Initialize deterministic candidate enemy spawn order for 2-4 player maps."""
        spawns = list(bot.enemy_start_locations)
        spawns.sort(
            key=lambda p: (
                round(bot.start_location.distance_to(p), 2),
                round(p.x, 1),
                round(p.y, 1),
            )
        )
        self.candidate_enemy_spawns = spawns
        if len(spawns) == 1:
            self.confirmed_enemy_main = spawns[0]

    def on_unit_destroyed(self, unit_tag: int) -> None:
        """Remove destroyed units from memory immediately."""
        self.tracked_enemies.pop(unit_tag, None)

    def on_enemy_unit_entered_vision(self, bot: BotAI, unit: Unit) -> None:
        """Record an enemy unit immediately on entering legal vision."""
        self._record_enemy(bot, unit, is_structure=unit.is_structure)

    def _record_enemy(self, bot: BotAI, unit: Unit, is_structure: bool) -> None:
        is_cloaked_or_burrowed = bool(
            unit.is_cloaked
            or unit.is_burrowed
            or unit.type_id in CLOAK_OR_BURROW_ENEMY_TYPES
        )
        val = unit_combat_value(unit, include_workers=False)
        is_combat = unit.type_id not in WORKER_TYPES and (
            val > 0.0
            or unit.can_attack_ground
            or unit.can_attack_air
            or unit.type_id in LETHAL_HARASS_TYPES
        )
        self.tracked_enemies[unit.tag] = TrackedEnemy(
            tag=unit.tag,
            type_id=unit.type_id,
            position=unit.position,
            is_structure=is_structure,
            is_flying=bool(unit.is_flying),
            is_combat=is_combat,
            combat_value=val,
            last_seen=float(bot.time),
            is_cloaked_or_burrowed=is_cloaked_or_burrowed,
        )

    def update(self, bot: BotAI) -> None:
        """Refresh visible enemy state, expire stale memories, and resolve spawn."""
        now = float(bot.time)
        live_tags: set[int] = set()

        for enemy in bot.enemy_units:
            live_tags.add(enemy.tag)
            self._record_enemy(bot, enemy, is_structure=bool(enemy.is_structure))

        for enemy_struct in bot.enemy_structures:
            live_tags.add(enemy_struct.tag)
            self._record_enemy(bot, enemy_struct, is_structure=True)

        # Purge enemies whose remembered location is currently visible and empty,
        # or mobile units that have grown too stale.
        to_remove: list[int] = []
        for tag, rec in self.tracked_enemies.items():
            if tag in live_tags:
                continue
            if safe_is_visible(bot, rec.position):
                to_remove.append(tag)
                continue
            if (
                not rec.is_structure
                and (now - rec.last_seen) > self.config.max_enemy_memory_seconds
            ):
                to_remove.append(tag)
        for tag in to_remove:
            self.tracked_enemies.pop(tag, None)

        self._update_spawn_discovery(bot, now)
        self._update_enemy_composition_flags(bot)
        self._update_cleanup_targets(bot, now)

    def _update_spawn_discovery(self, bot: BotAI, now: float) -> None:
        """Confirm enemy main by structure sighting or elimination on 4-player maps."""
        if not self.candidate_enemy_spawns:
            self.initialize(bot)

        known_structures = [r for r in self.tracked_enemies.values() if r.is_structure]
        if self.confirmed_enemy_main is None:
            for spawn in self.candidate_enemy_spawns:
                if any(s.position.distance_to(spawn) <= 22.0 for s in known_structures):
                    self.confirmed_enemy_main = spawn
                    break

        if self.confirmed_enemy_main is None:
            for spawn in self.candidate_enemy_spawns:
                key = _pos_key(spawn)
                if key in self.cleared_spawns:
                    continue
                if safe_is_visible(bot, spawn):
                    has_struct = any(
                        s.position.distance_to(spawn) <= 16.0 for s in known_structures
                    )
                    if not has_struct:
                        self.cleared_spawns.add(key)

            remaining = [
                s
                for s in self.candidate_enemy_spawns
                if _pos_key(s) not in self.cleared_spawns
            ]
            if len(remaining) == 1:
                self.confirmed_enemy_main = remaining[0]
            elif len(remaining) == 0 and self.candidate_enemy_spawns:
                # Re-open candidates if every spawn appeared empty (e.g. proxy-only start).
                self.cleared_spawns.clear()

        if self.confirmed_enemy_main is not None:
            if safe_is_visible(bot, self.confirmed_enemy_main):
                has_struct_at_main = any(
                    s.position.distance_to(self.confirmed_enemy_main) <= 18.0
                    for s in known_structures
                )
                self.enemy_main_cleared = not has_struct_at_main
                if self.enemy_main_cleared:
                    self.enemy_main_ever_cleared = True
            elif self.enemy_main_cleared and any(
                s.position.distance_to(self.confirmed_enemy_main) <= 18.0
                for s in known_structures
            ):
                # Stay cleared unless a new structure appears at enemy main.
                self.enemy_main_cleared = False

    def _update_enemy_composition_flags(self, bot: BotAI) -> None:
        """Analyze seen enemy composition to guide turrets, scans, and production."""
        combat_recs = [
            r
            for r in self.tracked_enemies.values()
            if r.is_combat and not r.is_structure
        ]
        struct_recs = [r for r in self.tracked_enemies.values() if r.is_structure]

        cloak_structs = {
            UnitTypeId.DARKSHRINE,
            UnitTypeId.LURKERDENMP,
            UnitTypeId.GHOSTACADEMY,
        }
        if any(r.is_cloaked_or_burrowed for r in self.tracked_enemies.values()) or any(
            r.type_id in cloak_structs for r in struct_recs
        ):
            self.enemy_has_cloak = True

        air_structs = {
            UnitTypeId.STARGATE,
            UnitTypeId.FLEETBEACON,
            UnitTypeId.SPIRE,
            UnitTypeId.GREATERSPIRE,
            UnitTypeId.FUSIONCORE,
        }
        air_threats = [
            r
            for r in combat_recs
            if r.is_flying or r.type_id in AIR_PRODUCTION_PRIORITY_TYPES
        ]
        if (
            air_threats
            or any(r.type_id in air_structs for r in struct_recs)
            or (
                bot.time >= 170.0
                and any(r.type_id == UnitTypeId.STARPORT for r in struct_recs)
            )
        ):
            self.enemy_has_air_threat = True

        self.enemy_air_count = len(air_threats)
        total_combat = len(combat_recs)
        self.enemy_air_ratio = (
            (len(air_threats) / total_combat) if total_combat > 0 else 0.0
        )
        heavy_count = sum(1 for r in combat_recs if r.type_id in HEAVY_ARMOR_TYPES)
        self.enemy_heavy_armor_ratio = (
            (heavy_count / total_combat) if total_combat > 0 else 0.0
        )

        # Early rush indicator: combat units near our side or proxy buildings early.
        if bot.time <= 240.0:
            near_home_combat = sum(
                1
                for e in bot.enemy_units
                if e.type_id not in WORKER_TYPES
                and e.distance_to(bot.start_location) <= 50.0
                and (e.can_attack_ground or e.type_id in LETHAL_HARASS_TYPES)
            )
            proxy_structs = sum(
                1
                for s in bot.enemy_structures
                if s.distance_to(bot.start_location) <= 55.0
            )
            if near_home_combat >= 2 or proxy_structs >= 1:
                self.enemy_is_rushing = True
        else:
            self.enemy_is_rushing = False

    def _update_cleanup_targets(self, bot: BotAI, now: float) -> None:
        """Track least-recently-visited expansions & map corners for base cleanup."""
        for unit in bot.units:
            key = _pos_key(unit.position)
            self.visited_points[key] = now
        for pos in list(bot.expansion_locations_list):
            if safe_is_visible(bot, pos):
                self.visited_points[_pos_key(pos)] = now

        w = float(getattr(bot.game_info.map_size, "x", 160))
        h = float(getattr(bot.game_info.map_size, "y", 160))
        corners = [
            Point2((10.0, 10.0)),
            Point2((max(12.0, w - 10.0), 10.0)),
            Point2((max(12.0, w - 10.0), max(12.0, h - 10.0))),
            Point2((10.0, max(12.0, h - 10.0))),
        ]
        all_spots = list(bot.expansion_locations_list) + corners

        def last_visit(pt: Point2) -> float:
            best = -1.0
            for vkey, ts in self.visited_points.items():
                if ((vkey[0] - pt.x) ** 2 + (vkey[1] - pt.y) ** 2) <= 100.0:
                    best = max(best, ts)
            return best

        all_spots.sort(key=lambda pt: (last_visit(pt), round(pt.x, 1), round(pt.y, 1)))
        self.cleanup_targets = all_spots

    def get_unvisited_candidate_spawns(self) -> list[Point2]:
        """Return candidate spawns not yet cleared (excluding confirmed if already found)."""
        if self.confirmed_enemy_main is not None:
            return [self.confirmed_enemy_main]
        return [
            s
            for s in self.candidate_enemy_spawns
            if _pos_key(s) not in self.cleared_spawns
        ]

    def get_known_enemy_structures(self) -> list[TrackedEnemy]:
        """Return currently live or remembered enemy structures."""
        return [r for r in self.tracked_enemies.values() if r.is_structure]

    def estimate_enemy_army_power(
        self, max_age: float = 45.0, current_time: float | None = None
    ) -> float:
        """Estimate total known enemy combat power from recent legal observations."""
        total = 0.0
        for rec in self.tracked_enemies.values():
            if not rec.is_combat and rec.type_id not in STATIC_DEFENSE_TYPES:
                continue
            if (
                current_time is not None
                and not rec.is_structure
                and (current_time - rec.last_seen) > max_age
            ):
                continue
            total += rec.combat_value
        return total

    def estimate_enemy_power_near(
        self,
        pos: Point2,
        radius: float,
        max_age: float = 12.0,
        current_time: float | None = None,
    ) -> float:
        """Estimate enemy combat value near a point using live + recent memory."""
        total = 0.0
        for rec in self.tracked_enemies.values():
            if rec.combat_value <= 0.0:
                continue
            if (
                current_time is not None
                and not rec.is_structure
                and (current_time - rec.last_seen) > max_age
            ):
                continue
            if rec.position.distance_to(pos) <= radius:
                total += rec.combat_value
        return total

    def get_primary_attack_target(self, bot: BotAI) -> Point2:
        """Choose the highest-priority known enemy structure or spawn/cleanup location."""
        # Prefer visible enemy structures, then remembered enemy structures closest to army.
        if bot.enemy_structures:
            townhall_structs = [
                s
                for s in bot.enemy_structures
                if s.type_id
                in {
                    UnitTypeId.COMMANDCENTER,
                    UnitTypeId.ORBITALCOMMAND,
                    UnitTypeId.PLANETARYFORTRESS,
                    UnitTypeId.NEXUS,
                    UnitTypeId.HATCHERY,
                    UnitTypeId.LAIR,
                    UnitTypeId.HIVE,
                }
            ]
            pool = townhall_structs or list(bot.enemy_structures)
            anchor = bot.start_location
            if self.confirmed_enemy_main is not None:
                return min(pool, key=lambda s: s.distance_to(anchor)).position
            return min(pool, key=lambda s: s.distance_to(anchor)).position

        remembered_structs = self.get_known_enemy_structures()
        if remembered_structs:
            return min(
                remembered_structs,
                key=lambda r: r.position.distance_to(bot.start_location),
            ).position

        if self.confirmed_enemy_main is not None and not self.enemy_main_cleared:
            return self.confirmed_enemy_main

        if self.cleanup_targets:
            return self.cleanup_targets[0]

        if self.candidate_enemy_spawns:
            return self.candidate_enemy_spawns[0]
        return bot.game_info.map_center
