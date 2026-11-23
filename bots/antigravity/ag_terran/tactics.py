"""Army posture state machine: defend, rally, reclaim lost bases, attack, or retreat."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from .bases import BaseManager
from .config import (
    OWN_NON_COMBAT_TYPES,
    STATIC_DEFENSE_TYPES,
    TANK_TYPES,
    BotConfig,
    unit_combat_value,
)
from .intel import IntelTracker

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


UNIT_SUPPLY_FALLBACK: dict[UnitTypeId, float] = {
    UnitTypeId.MARINE: 1.0,
    UnitTypeId.MARAUDER: 2.0,
    UnitTypeId.REAPER: 1.0,
    UnitTypeId.GHOST: 2.0,
    UnitTypeId.HELLION: 2.0,
    UnitTypeId.HELLIONTANK: 2.0,
    UnitTypeId.WIDOWMINE: 2.0,
    UnitTypeId.WIDOWMINEBURROWED: 2.0,
    UnitTypeId.CYCLONE: 2.0,
    UnitTypeId.SIEGETANK: 3.0,
    UnitTypeId.SIEGETANKSIEGED: 3.0,
    UnitTypeId.THOR: 6.0,
    UnitTypeId.VIKINGFIGHTER: 2.0,
    UnitTypeId.VIKINGASSAULT: 2.0,
    UnitTypeId.MEDIVAC: 2.0,
    UnitTypeId.LIBERATOR: 3.0,
    UnitTypeId.LIBERATORAG: 3.0,
    UnitTypeId.RAVEN: 2.0,
    UnitTypeId.BANSHEE: 3.0,
    UnitTypeId.BATTLECRUISER: 6.0,
}


class ArmyPosture(Enum):
    """High-level strategic state of the main combat army."""

    DEFEND = "defend"
    RALLY = "rally"
    RECLAIM = "reclaim"
    ATTACK = "attack"
    RETREAT = "retreat"


class TacticsManager:
    """Decides when to stage, defend home bases, push proactively, reclaim, or pull back."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.posture: ArmyPosture = ArmyPosture.RALLY
        self.target_position: Point2 | None = None
        self.local_defense_target: Point2 | None = None
        self.local_defense_count: int = 0
        self.own_army_supply: float = 0.0
        self.own_army_power: float = 0.0
        self.known_enemy_power: float = 0.0

    def _unit_supply(self, bot: BotAI, unit_type: UnitTypeId) -> float:
        if unit_type in UNIT_SUPPLY_FALLBACK:
            return UNIT_SUPPLY_FALLBACK[unit_type]
        try:
            return float(bot.calculate_supply_cost(unit_type))
        except (KeyError, AttributeError, TypeError):
            return 1.0

    def update(
        self,
        bot: BotAI,
        intel: IntelTracker,
        base_mgr: BaseManager,
        reserved_scout_tags: set[int],
    ) -> None:
        """Update army posture and strategic targets with anti-oscillation hysteresis."""
        now = float(bot.time)
        combat_units = [
            u
            for u in bot.units
            if u.type_id not in OWN_NON_COMBAT_TYPES
            and u.tag not in reserved_scout_tags
        ]
        self.own_army_supply = sum(
            self._unit_supply(bot, u.type_id) for u in combat_units
        )
        self.own_army_power = sum(unit_combat_value(u) for u in combat_units)
        self.known_enemy_power = intel.estimate_enemy_army_power(
            max_age=45.0, current_time=now
        )
        self.local_defense_target = None
        self.local_defense_count = 0

        if not combat_units:
            self.posture = ArmyPosture.RALLY
            self.target_position = base_mgr.get_rally_point(bot, intel)
            return

        # 1. Defend threatened non-overwhelmed bases.
        defendable = base_mgr.get_defendable_threatened_bases()
        if defendable:
            threat_base = defendable[0]
            nearby_enemies = [
                e
                for e in bot.enemy_units
                if e.distance_to(threat_base.location) <= 17.5
            ]
            if nearby_enemies:
                ex = sum(e.position.x for e in nearby_enemies) / len(nearby_enemies)
                ey = sum(e.position.y for e in nearby_enemies) / len(nearby_enemies)
                threat_pos = Point2((ex, ey))
            else:
                threat_pos = threat_base.location

            # If already pushing with a strong army and only a tiny harass hits an outer base,
            # peel off a small detachment rather than aborting the whole push.
            if (
                self.posture == ArmyPosture.ATTACK
                and self.own_army_supply >= self.config.advantage_push_supply
                and threat_base.enemy_power <= 5.0
                and not threat_base.is_main
            ):
                self.local_defense_target = threat_pos
                self.local_defense_count = min(
                    6, max(3, int(threat_base.enemy_power) + 2)
                )
            else:
                self.posture = ArmyPosture.DEFEND
                self.target_position = threat_pos
                return

        # 2. Check local combat disadvantage while pushing -> orderly retreat.
        if self.posture in {ArmyPosture.ATTACK, ArmyPosture.RECLAIM}:
            ground_core = [u for u in combat_units if not u.is_flying] or combat_units
            cx = sum(u.position.x for u in ground_core) / len(ground_core)
            cy = sum(u.position.y for u in ground_core) / len(ground_core)
            army_center = Point2((cx, cy))

            local_friendly = sum(
                unit_combat_value(u)
                for u in combat_units
                if u.distance_to(army_center) <= 17.0
            )
            local_enemy = sum(
                unit_combat_value(e)
                for e in bot.enemy_units
                if e.distance_to(army_center) <= 16.0
            ) + sum(
                unit_combat_value(s)
                for s in bot.enemy_structures
                if s.type_id in STATIC_DEFENSE_TYPES
                and s.distance_to(army_center) <= 16.0
            )
            if (
                local_enemy >= 6.0
                and local_friendly < local_enemy * self.config.retreat_power_ratio
            ):
                self.posture = ArmyPosture.RETREAT
                self.target_position = base_mgr.get_rally_point(bot, intel)
                return

        # 3. Proactive push & reclaim evaluation.
        stim_ready_or_soon = (
            UpgradeId.STIMPACK in bot.state.upgrades
            or bot.already_pending_upgrade(UpgradeId.STIMPACK) >= 0.70
        )
        tank_count = len(bot.units(TANK_TYPES))
        medivac_count = len(bot.units(UnitTypeId.MEDIVAC))

        timing_push = (
            stim_ready_or_soon
            and self.own_army_supply >= self.config.timing_push_supply
            and tank_count >= 2
            and medivac_count >= 2
            and self.own_army_power >= max(12.0, self.known_enemy_power * 1.05)
        )
        advantage_push = (
            self.own_army_supply >= self.config.advantage_push_supply
            and self.own_army_power >= max(16.0, self.known_enemy_power * 1.20)
        )
        force_push = (
            self.own_army_supply >= self.config.force_push_army_supply
            or bot.supply_used >= self.config.force_push_total_supply
        )
        cleanup_push = intel.enemy_main_cleared and self.own_army_supply >= 16.0
        keep_pushing = (
            self.posture in {ArmyPosture.ATTACK, ArmyPosture.RECLAIM}
            and self.own_army_supply >= 22.0
            and self.own_army_power >= max(10.0, self.known_enemy_power * 0.90)
        )

        # If an outer base was previously abandoned and we now outmatch that threat, reclaim it.
        reclaim_rec = base_mgr.get_reclaim_candidate(self.own_army_power)
        if reclaim_rec is not None and self.own_army_supply >= 26.0:
            # Prefer attacking directly if already fighting inside enemy structures.
            already_deep = any(
                s.distance_to(combat_units[0].position) <= 18.0
                for s in bot.enemy_structures
            )
            if not already_deep:
                self.posture = ArmyPosture.RECLAIM
                self.target_position = reclaim_rec.location
                return

        if timing_push or advantage_push or force_push or cleanup_push or keep_pushing:
            self.posture = ArmyPosture.ATTACK
            self.target_position = intel.get_primary_attack_target(bot)
            return

        # 4. Default staging/rallying.
        self.posture = ArmyPosture.RALLY
        self.target_position = base_mgr.get_rally_point(bot, intel)
