"""原创建议型战略状态机：经济成长、兵种应对和带迟滞的进退决策。

所有阈值都是尚待实战调参的启发式。模块不读取对手源码，不推断雾中真值。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from sc2.position import Point2

from .intelligence import WORKERS, unit_power
from .model import BattlePlan, Composition, Stance

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI

    from .config import BotConfig
    from .intelligence import Intelligence
    from .model import IntelReport


def composition_for(
    report: IntelReport, army_supply: float, workers: int, now: float
) -> Composition:
    """把“看到的敌军构成”转为有限生产目标，而非假定知道对手整支军队。"""
    total = report.ground_supply + report.air_supply
    armor_fraction = report.armored_ground_supply / max(1.0, report.ground_supply)
    air_fraction = report.air_supply / max(1.0, total)
    marauders = min(0.5, 0.20 + armor_fraction * 0.30)
    if air_fraction > 0.55:
        marauders = 0.12
    tanks = min(8, max(2, workers // 10))
    if report.armored_ground_supply >= 14:
        tanks = min(10, tanks + 2)
    if air_fraction > 0.65:
        tanks = min(3, tanks)
    vikings = min(16, math.ceil(report.air_supply * 0.65 + report.colossi * 2))
    if report.air_supply > 0:
        vikings = max(2, vikings)
    return Composition(
        marauder_fraction=marauders,
        tank_goal=tanks,
        medivac_goal=min(8, max(2, math.ceil(army_supply / 18.0))),
        viking_goal=vikings,
        raven_goal=int(report.cloak_risk or (now >= 480 and army_supply >= 45)),
    )


class Strategist:
    def __init__(self, config: BotConfig):
        self.config = config
        self.stance = Stance.HOLD
        self._entered_at = 0.0
        self._attack_entry_supply = 0.0
        self._last_home_threat_at = -math.inf
        self._last_defense_point: Point2 | None = None

    def _set_stance(self, stance: Stance, now: float) -> None:
        if stance != self.stance:
            self.stance = stance
            self._entered_at = now

    @staticmethod
    def _ground_point(bot: BotAI, candidates: tuple[Point2, ...]) -> Point2:
        for candidate in candidates:
            if bot.in_map_bounds(candidate) and bot.in_pathing_grid(candidate):
                return candidate
        return candidates[-1]

    @staticmethod
    def _toward(start: Point2, end: Point2, distance: float) -> Point2:
        if start.distance_to(end) < 0.01:
            return start
        return start.towards(end, min(distance, start.distance_to(end)))

    def update(self, bot: BotAI, intel: Intelligence) -> BattlePlan:
        now = float(bot.time)
        report = intel.report
        workers = len(bot.workers)
        halls = sorted(
            (hall for hall in bot.townhalls if not hall.is_flying),
            key=lambda hall: hall.tag,
        )
        completed_halls = [hall for hall in halls if getattr(hall, "is_ready", True)]
        # 尚未造好的前线矿不宜成为整支军队的默认集结/撤退基地。
        homes = [hall.position for hall in (completed_halls or halls)] or [bot.start_location]
        target = intel.target(bot)
        forward = min(homes, key=lambda p: p.distance_to(target))
        safest = max(homes, key=lambda p: p.distance_to(report.home_threat or target))
        rally = self._ground_point(
            bot,
            (
                self._toward(forward, target, 7.0),
                self._toward(forward, bot.game_info.map_center, 5.0),
                self._toward(forward, bot.start_location, 5.0),
                forward,
            ),
        )
        fallback = self._ground_point(
            bot,
            (
                self._toward(safest, bot.start_location, 5.0),
                self._toward(safest, bot.game_info.map_center, 4.0),
                safest,
            ),
        )
        army = [unit for unit in bot.units if unit.type_id not in WORKERS]
        combat = [unit for unit in army if unit.can_attack_ground or unit.can_attack_air]
        army_supply = max(0.0, float(bot.supply_army))
        power = sum(unit_power(bot, unit) for unit in army)
        ratio = power / max(1.0, report.enemy_power)
        # 中位数比单个冲到远处的侦察单位更能代表主力位置。
        if combat:
            xs = sorted(unit.position.x for unit in combat)
            ys = sorted(unit.position.y for unit in combat)
            center = Point2((xs[len(xs) // 2], ys[len(ys) // 2]))
        else:
            center = rally
        local_friendly = sum(
            unit_power(bot, unit) for unit in army if unit.position.distance_to(center) < 18.0
        )
        local_enemy = sum(
            contact.power
            for contact in intel.contacts.values()
            if now - contact.seen_at <= 8.0
            and contact.type_id not in WORKERS
            and contact.position.distance_to(center) < 18.0
        )
        local_ratio = local_friendly / max(1.0, local_enemy)
        reason = "集结与经济建设"

        if report.home_threat is not None:
            self._last_home_threat_at = now
            self._last_defense_point = report.home_threat
            self._set_stance(Stance.DEFEND, now)
            objective = report.home_threat
            reason = "已观察到基地附近的实际威胁"
        elif self.stance == Stance.DEFEND and now - self._last_home_threat_at < 6.0:
            objective = self._last_defense_point or rally
            reason = "威胁消失后的短暂复查"
        else:
            if self.stance == Stance.DEFEND:
                self._set_stance(Stance.HOLD, now)
            large_losses = army_supply < max(10.0, self._attack_entry_supply * 0.5)
            locally_outmatched = local_enemy >= 4.0 and local_ratio < self.config.retreat_ratio
            if self.stance in {Stance.ATTACK, Stance.SEARCH} and (
                locally_outmatched or large_losses
            ):
                self._set_stance(Stance.RETREAT, now)
                reason = "局部兵力劣势或推进损失过重，保存剩余兵力"
            if self.stance == Stance.RETREAT:
                elapsed = now - self._entered_at
                rallied = (
                    not combat
                    or sum(u.position.distance_to(rally) < 15 for u in combat) >= len(combat) * 0.65
                )
                if elapsed >= 10.0 and (rallied or elapsed >= 24.0):
                    self._set_stance(Stance.HOLD, now)
                else:
                    reason = "撤退期间先重新集结，不逐帧反复改变方向"
            if self.stance == Stance.HOLD:
                timing_ready = army_supply >= self.config.attack_supply
                # 未观察到大量敌军时，仍主动出门，不能等敌人先送到门口。
                late_probe = now >= 480 and army_supply >= 28 and report.enemy_power < power
                capped = bot.supply_used >= 185 and army_supply >= 65
                ready = bool(combat) and (
                    (timing_ready and ratio >= self.config.attack_ratio) or late_probe or capped
                )
                if ready:
                    self._attack_entry_supply = army_supply
                    self._set_stance(
                        Stance.ATTACK if report.known_structures else Stance.SEARCH, now
                    )
                    reason = "形成推进兵力，主动检查敌军基地与扩张"
            elif self.stance in {Stance.ATTACK, Stance.SEARCH}:
                # 战略继续推进；遇到较小优势波动不会立即全军后退。
                self.stance = Stance.ATTACK if report.known_structures else Stance.SEARCH
                reason = "保持推进；局部交火由微操处理"
            objective = (
                target
                if self.stance in {Stance.ATTACK, Stance.SEARCH}
                else (fallback if self.stance == Stance.RETREAT else rally)
            )

        emergency = report.home_threat is not None and (
            report.rush or report.home_threat_power >= max(4.0, power * 0.7)
        )
        desired_bases = 2
        if workers >= 42 or (now >= 420 and workers >= 32):
            desired_bases = 3
        if workers >= 60 or (now >= 660 and workers >= 48):
            desired_bases = 4
        if now >= 900 and workers >= 60:
            desired_bases = 5
        if now >= 1200 and workers >= 60:
            desired_bases = 6
        desired_bases = min(self.config.max_bases, desired_bases)
        if report.rush:
            desired_bases = max(1, min(desired_bases, len(halls)))
        barracks = 1 if workers < 24 and len(halls) < 2 else 3
        if workers >= 42:
            barracks = 5
        if workers >= 58:
            barracks = 7
        if bot.minerals > 900 and workers >= 44:
            barracks = min(10, barracks + 2)
        comp = composition_for(report, army_supply, workers, now)
        factories = 2 if workers >= 48 and comp.tank_goal >= 5 else 1
        starports = 2 if workers >= 48 and (comp.viking_goal >= 6 or army_supply >= 100) else 1
        return BattlePlan(
            stance=self.stance,
            objective=objective,
            rally=rally,
            fallback=fallback,
            composition=comp,
            desired_bases=desired_bases,
            desired_barracks=barracks,
            desired_factories=factories,
            desired_starports=starports,
            allow_expand=not emergency or not halls,
            save_scan=report.cloak_risk,
            emergency=emergency,
            reason=reason,
        )
