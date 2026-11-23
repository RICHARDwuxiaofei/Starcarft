"""Deterministic Terran building placement respecting ramps, addons, and mineral lines."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from .config import DEPOT_TYPES

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI

    from .bases import BaseManager
    from .intel import IntelTracker


class PlacementManager:
    """Computes safe placement locations for depots, production with addons, and defenses."""

    def __init__(self) -> None:
        self.ramp_depot_positions: list[Point2] = []
        self.ramp_barracks_position: Point2 | None = None
        self.ramp_top_center: Point2 | None = None
        self.ramp_bottom_center: Point2 | None = None
        self._initialized: bool = False

    def initialize(self, bot: BotAI) -> None:
        """Extract main-base ramp wall coordinates defensively."""
        self._initialized = True
        try:
            ramp = bot.main_base_ramp
            self.ramp_top_center = ramp.top_center
            self.ramp_bottom_center = ramp.bottom_center
            depots = sorted(
                ramp.corner_depots,
                key=lambda p: (round(p.x, 2), round(p.y, 2)),
            )
            self.ramp_depot_positions = depots
            self.ramp_barracks_position = ramp.barracks_correct_placement
        except Exception:  # noqa: BLE001 — burnysc2 Ramp properties raise bare Exception on non-standard ramps
            self.ramp_depot_positions = []
            self.ramp_barracks_position = None
            self.ramp_top_center = None
            self.ramp_bottom_center = None

    def is_near_mineral_line(
        self, bot: BotAI, pos: Point2, min_dist: float = 4.2
    ) -> bool:
        """Return True if pos is too close to any mineral patch or geyser around our bases."""
        for th in bot.townhalls:
            if th.distance_to(pos) <= 4.5:
                return True
        for mf in bot.mineral_field:
            if mf.distance_to(pos) <= min_dist:
                return True
        for gey in bot.vespene_geyser:
            if gey.distance_to(pos) <= min_dist:
                return True
        return False

    def _overlaps_addon_zone(
        self, bot: BotAI, pos: Point2, radius: float = 2.6
    ) -> bool:
        """Prevent depots/tech buildings from blocking production building addon tiles."""
        for prod in bot.structures(
            {UnitTypeId.BARRACKS, UnitTypeId.FACTORY, UnitTypeId.STARPORT}
        ):
            if prod.add_on_position.distance_to(pos) <= radius:
                return True
        return False

    async def find_depot_position(
        self, bot: BotAI, base_mgr: BaseManager
    ) -> Point2 | None:
        """Place depots on the main ramp wall first, then cleanly around the main perimeter."""
        if not self._initialized:
            self.initialize(bot)

        existing_depots = bot.structures(DEPOT_TYPES)
        for ramp_pos in self.ramp_depot_positions:
            if any(d.distance_to(ramp_pos) < 1.5 for d in existing_depots):
                continue
            if await bot.can_place_single(UnitTypeId.SUPPLYDEPOT, ramp_pos):
                return ramp_pos

        anchor = base_mgr.main_location or bot.start_location
        away_dir = anchor.direction_vector(bot.game_info.map_center)
        # Try offset behind minerals or sides of main first, then expand outward.
        search_origins = [
            anchor - away_dir * 6.5,
            anchor + Point2((-7.5, 5.0)),
            anchor + Point2((7.5, 5.0)),
            anchor + Point2((-7.5, -5.0)),
            anchor + Point2((7.5, -5.0)),
            anchor + away_dir * 7.5,
        ]
        for origin in search_origins:
            pos = await bot.find_placement(
                UnitTypeId.SUPPLYDEPOT,
                near=origin,
                max_distance=14,
                random_alternative=False,
                placement_step=2,
            )
            if pos is None:
                continue
            if self.is_near_mineral_line(bot, pos, min_dist=3.6):
                continue
            if self._overlaps_addon_zone(bot, pos):
                continue
            return pos

        return await bot.find_placement(
            UnitTypeId.SUPPLYDEPOT,
            near=anchor,
            max_distance=24,
            random_alternative=False,
            placement_step=2,
        )

    async def find_production_position(
        self,
        bot: BotAI,
        building_type: UnitTypeId,
        base_mgr: BaseManager,
    ) -> Point2 | None:
        """Place Barracks/Factory/Starport with guaranteed 2x2 right-side addon clearance."""
        if not self._initialized:
            self.initialize(bot)

        anchor = base_mgr.main_location or bot.start_location
        if (
            building_type == UnitTypeId.BARRACKS
            and not bot.structures(UnitTypeId.BARRACKS)
            and self.ramp_barracks_position is not None
        ):
            rp = self.ramp_barracks_position
            if await bot.can_place_single(
                UnitTypeId.BARRACKS, rp
            ) and await bot.can_place_single(
                UnitTypeId.SUPPLYDEPOT, rp.offset(Point2((2.5, -0.5)))
            ):
                return rp

        toward_center = anchor.direction_vector(bot.game_info.map_center)
        anchors = [
            anchor + toward_center * 7.0,
            anchor + Point2((0.0, 8.0)),
            anchor + Point2((0.0, -8.0)),
            anchor - toward_center * 6.0,
        ]
        if base_mgr.natural_location is not None and len(bot.townhalls) >= 2:
            anchors.append(base_mgr.natural_location + toward_center * 6.0)

        for origin in anchors:
            pos = await bot.find_placement(
                building_type,
                near=origin,
                max_distance=20,
                random_alternative=False,
                placement_step=2,
                addon_place=True,
            )
            if pos is not None and not self.is_near_mineral_line(
                bot, pos, min_dist=3.8
            ):
                return pos

        return await bot.find_placement(
            building_type,
            near=anchor,
            max_distance=28,
            random_alternative=False,
            placement_step=2,
            addon_place=True,
        )

    async def find_tech_position(
        self,
        bot: BotAI,
        building_type: UnitTypeId,
        base_mgr: BaseManager,
    ) -> Point2 | None:
        """Place Engineering Bay, Armory, or Ghost Academy safely inside main."""
        anchor = base_mgr.main_location or bot.start_location
        away_dir = anchor.direction_vector(bot.game_info.map_center)
        for origin in (anchor - away_dir * 5.0, anchor + away_dir * 6.0, anchor):
            pos = await bot.find_placement(
                building_type,
                near=origin,
                max_distance=18,
                random_alternative=False,
                placement_step=2,
            )
            if (
                pos is not None
                and not self.is_near_mineral_line(bot, pos, min_dist=3.5)
                and not self._overlaps_addon_zone(bot, pos)
            ):
                return pos
        return await bot.find_placement(
            building_type,
            near=anchor,
            max_distance=24,
            random_alternative=False,
            placement_step=2,
        )

    async def find_bunker_position(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
        intel: IntelTracker,
    ) -> Point2 | None:
        """Place a defensive Bunker at the front of the natural expansion."""
        nat = base_mgr.natural_location
        anchor = (
            nat if nat is not None else (base_mgr.main_location or bot.start_location)
        )
        enemy_dir = anchor.direction_vector(
            intel.confirmed_enemy_main or bot.game_info.map_center
        )
        ideal = anchor + enemy_dir * 6.5
        return await bot.find_placement(
            UnitTypeId.BUNKER,
            near=ideal,
            max_distance=10,
            random_alternative=False,
            placement_step=1,
        )

    async def find_turret_position(
        self, bot: BotAI, townhall_pos: Point2
    ) -> Point2 | None:
        """Place a Missile Turret near the mineral line of an active base."""
        patches = [m for m in bot.mineral_field if m.distance_to(townhall_pos) <= 10.0]
        if patches:
            mx = sum(m.position.x for m in patches) / len(patches)
            my = sum(m.position.y for m in patches) / len(patches)
            mineral_center = Point2((mx, my))
            dir_vec = townhall_pos.direction_vector(mineral_center)
            ideal = townhall_pos + dir_vec * 3.8
        else:
            ideal = townhall_pos + Point2((3.5, 3.5))
        return await bot.find_placement(
            UnitTypeId.MISSILETURRET,
            near=ideal,
            max_distance=8,
            random_alternative=False,
            placement_step=1,
        )

    async def find_sensor_tower_position(
        self,
        bot: BotAI,
        base_mgr: BaseManager,
    ) -> Point2 | None:
        """Place a Sensor Tower on the forward edge of our natural or main."""
        anchor = (
            base_mgr.natural_location or base_mgr.main_location or bot.start_location
        )
        toward_center = anchor.direction_vector(bot.game_info.map_center)
        ideal = anchor + toward_center * 7.5
        return await bot.find_placement(
            UnitTypeId.SENSORTOWER,
            near=ideal,
            max_distance=14,
            random_alternative=False,
            placement_step=2,
        )
