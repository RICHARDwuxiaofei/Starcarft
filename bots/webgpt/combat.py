"""原创的人族战斗控制：每个单位每帧只领取一个主要动作。

这里的阈值是可解释的启发式，并非训练所得，也不是实战胜率承诺。
所有目标来自本帧正常视野；战略层的历史记忆只能提供移动目的地。
本模块不操作工人、不分配资源，也不发送调试指令。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.buff_id import BuffId
from sc2.ids.effect_id import EffectId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from .config import BotConfig
from .model import BattlePlan, IntelReport, Stance

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI
    from sc2.unit import Unit

    from .runtime import CommandBus


WORKERS = {U.SCV, U.MULE, U.DRONE, U.PROBE}
BIO = {U.MARINE, U.MARAUDER}
TANKS = {U.SIEGETANK, U.SIEGETANKSIEGED}
SUPPORT = {U.MEDIVAC, U.RAVEN}

# 只是决策权重，不替代单位的实际攻击范围/合法目标校验。
PRIORITY = {
    U.BANELING: 12.0,
    U.BANELINGBURROWED: 12.0,
    U.HIGHTEMPLAR: 11.0,
    U.DISRUPTOR: 11.0,
    U.INFESTOR: 10.0,
    U.SIEGETANKSIEGED: 9.0,
    U.LURKERMPBURROWED: 9.0,
    U.COLOSSUS: 9.0,
    U.MEDIVAC: 7.0,
    U.RAVEN: 7.0,
    U.WARPPRISM: 6.0,
    U.WARPPRISMPHASING: 6.0,
}


def distance_sq(a: Point2, b: Point2) -> float:
    return (a.x - b.x) ** 2 + (a.y - b.y) ** 2


def can_hit(attacker: Unit, target: Unit) -> bool:
    """巨像是特殊目标：防空武器也能攻击它。"""
    if target.is_flying:
        return attacker.can_attack_air
    return attacker.can_attack_ground or (target.type_id == U.COLOSSUS and attacker.can_attack_air)


def weapon_range(attacker: Unit, target: Unit) -> float:
    if target.is_flying or (target.type_id == U.COLOSSUS and not attacker.can_attack_ground):
        return attacker.air_range
    return attacker.ground_range


def in_weapon_range(attacker: Unit, target: Unit, padding: float = 0.0) -> bool:
    """按当前单位数据和碰撞半径计算；坦克最小射程在坦克分支另检查。"""
    if not can_hit(attacker, target):
        return False
    reach = weapon_range(attacker, target) + attacker.radius + target.radius + padding
    return distance_sq(attacker.position, target.position) <= max(0.0, reach) ** 2


class SpatialIndex:
    """局部查询桶，避免给每一个海军陆战队员遍历整张图的敌人。"""

    def __init__(self, units: Iterable[Unit], cell_size: float = 10.0):
        self.cell_size = cell_size
        self.buckets: dict[tuple[int, int], list[Unit]] = defaultdict(list)
        for unit in units:
            self.buckets[self._key(unit.position)].append(unit)

    def _key(self, point: Point2) -> tuple[int, int]:
        return math.floor(point.x / self.cell_size), math.floor(point.y / self.cell_size)

    def near(self, point: Point2, radius: float) -> list[Unit]:
        low = self._key(Point2((point.x - radius, point.y - radius)))
        high = self._key(Point2((point.x + radius, point.y + radius)))
        result = []
        radius_sq = radius * radius
        for x in range(low[0], high[0] + 1):
            for y in range(low[1], high[1] + 1):
                result.extend(
                    unit
                    for unit in self.buckets.get((x, y), ())
                    if distance_sq(unit.position, point) <= radius_sq
                )
        return result


@dataclass(frozen=True, slots=True)
class Hazard:
    position: Point2
    radius: float
    ground: bool = True
    air: bool = True


class CombatController:
    def __init__(self, config: BotConfig):
        self.config = config
        self._last_morph: dict[int, float] = {}
        self._tank_contact: dict[int, float] = {}
        self._last_air_seen: float | None = None

    async def step(self, bot: BotAI, bus: CommandBus, plan: BattlePlan, intel: IntelReport) -> None:
        # Unit 对象不能跨帧缓存：保存 tag 和时间即可。
        army = [
            u for u in bot.units if u.is_ready and not u.is_structure and u.type_id not in WORKERS
        ]
        live = {u.tag for u in army}
        self._last_morph = {k: v for k, v in self._last_morph.items() if k in live}
        self._tank_contact = {k: v for k, v in self._tank_contact.items() if k in live}
        if not army:
            return

        observed = [
            u
            for u in (*bot.enemy_units, *bot.enemy_structures)
            if u.is_visible and u.type_id != U.DISRUPTORPHASED
        ]
        # 对不可攻击的隐形目标仍可做风险判断，但不会发出精确攻击命令。
        enemies = SpatialIndex(observed)
        targets = SpatialIndex(u for u in observed if u.can_be_attacked)
        hazards = self._hazards(bot)
        if self._last_air_seen is None:
            self._last_air_seen = bot.time
        if any(u.is_flying and not u.is_structure for u in observed):
            self._last_air_seen = bot.time

        ground = [u for u in army if not u.is_flying]
        anchor = self._center(ground, plan.rally)
        # 只剩维京时也要能搜索和收尾；支援机则留在集结点等待新部队。
        fighter_anchor = (
            plan.objective
            if not ground and plan.stance in {Stance.ATTACK, Stance.SEARCH}
            else anchor
        )
        wounded = [
            u
            for u in bot.units
            if u.is_ready and u.is_biological and not u.is_structure and u.health < u.health_max - 1
        ]
        heal_claims: Counter[int] = Counter()
        for unit in sorted(army, key=lambda u: u.tag):
            if unit.tag in bus.issued:
                continue
            local = enemies.near(unit.position, 16.0)
            attackable = targets.near(unit.position, 16.0)
            if self._evade(bot, bus, unit, hazards, local):
                continue
            if unit.type_id in TANKS:
                self._tank(bot, bus, unit, attackable, plan)
            elif unit.type_id == U.MEDIVAC:
                self._medivac(bot, bus, unit, local, wounded, heal_claims, anchor, plan)
            elif unit.type_id == U.RAVEN:
                self._raven(bot, bus, unit, local, anchor, plan)
            elif unit.type_id in {U.VIKINGFIGHTER, U.VIKINGASSAULT}:
                self._viking(bot, bus, unit, local, attackable, fighter_anchor, plan, intel)
            else:
                self._infantry(bot, bus, unit, local, attackable, plan)

    @staticmethod
    def _center(units: list[Unit], default: Point2) -> Point2:
        if not units:
            return default
        # 中位数使刚走出兵营的增援和少数离群单位不会把全军锚点拖回家。
        x = sorted(u.position.x for u in units)
        y = sorted(u.position.y for u in units)
        return Point2((x[len(x) // 2], y[len(y) // 2]))

    @staticmethod
    def _hazards(bot: BotAI) -> list[Hazard]:
        hazards = []
        all_damage = {
            EffectId.PSISTORMPERSISTENT,
            EffectId.RAVAGERCORROSIVEBILECP,
            EffectId.NUKEPERSISTENT,
        }
        ground_enemy = {
            EffectId.LIBERATORTARGETMORPHPERSISTENT,
            EffectId.LIBERATORTARGETMORPHDELAYPERSISTENT,
            EffectId.BLINDINGCLOUDCP,
        }
        for effect in bot.state.effects:
            effect_id = effect.id
            if effect_id in all_damage:
                for pos in effect.positions:
                    hazards.append(Hazard(pos, max(effect.radius, 1.5)))
            elif effect_id in ground_enemy and effect.is_enemy:
                for pos in effect.positions:
                    hazards.append(Hazard(pos, max(effect.radius, 1.5), air=False))
            elif effect_id == "KD8CHARGE":
                for pos in effect.positions:
                    hazards.append(Hazard(pos, effect.radius, air=False))
            elif effect_id == "PARASITICBOMB":
                for pos in effect.positions:
                    hazards.append(Hazard(pos, effect.radius, ground=False))
        # 净化新星是可见单位而非普通 Effect；只避开本帧真实观察到的球。
        for unit in (*bot.units, *bot.enemy_units):
            if unit.type_id == U.DISRUPTORPHASED and unit.is_visible:
                hazards.append(Hazard(unit.position, 3.0, air=False))
        return hazards

    @staticmethod
    def _safe_point(bot: BotAI, unit: Unit, point: Point2) -> Point2 | None:
        if bot.in_map_bounds(point) and (unit.is_flying or bot.in_pathing_grid(point)):
            return point
        for radius in (1.5, 3.0, 5.0):
            for index in range(8):
                angle = index * math.pi / 4
                candidate = Point2(
                    (
                        point.x + radius * math.cos(angle),
                        point.y + radius * math.sin(angle),
                    )
                )
                if bot.in_map_bounds(candidate) and (
                    unit.is_flying or bot.in_pathing_grid(candidate)
                ):
                    return candidate
        return None

    @staticmethod
    def _away(unit: Unit, danger: Point2, amount: float) -> Point2:
        dx, dy = unit.position.x - danger.x, unit.position.y - danger.y
        length = math.hypot(dx, dy)
        if length < 0.01:
            angle = (unit.tag % 24) * math.tau / 24
            dx, dy, length = math.cos(angle), math.sin(angle), 1.0
        return Point2(
            (
                unit.position.x + amount * dx / length,
                unit.position.y + amount * dy / length,
            )
        )

    def _evade(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        hazards: list[Hazard],
        enemies: list[Unit],
    ) -> bool:
        relevant = (
            [h for h in hazards if h.air] if unit.is_flying else [h for h in hazards if h.ground]
        )
        near = [
            h
            for h in relevant
            if distance_sq(unit.position, h.position) < (h.radius + unit.radius + 0.6) ** 2
        ]
        if not near:
            return False
        if unit.type_id == U.SIEGETANKSIEGED:
            self._morph(bot, bus, unit, A.UNSIEGE_UNSIEGE)
            return True
        # 从多个危险区的加权反方向逃离，再检查八个候选终点。
        dx = dy = 0.0
        for hazard in near:
            away = self._away(unit, hazard.position, 1.0)
            weight = 1 / max(0.5, math.sqrt(distance_sq(unit.position, hazard.position)))
            dx += (away.x - unit.position.x) * weight
            dy += (away.y - unit.position.y) * weight
        angle = math.atan2(dy, dx)
        distance = max(4.0, max(h.radius for h in near) + unit.radius + 1.5)
        candidates = []
        for offset in (0, 1, -1, 2, -2, 3, -3, 4):
            theta = angle + offset * math.pi / 4
            point = Point2(
                (
                    unit.position.x + distance * math.cos(theta),
                    unit.position.y + distance * math.sin(theta),
                )
            )
            if not bot.in_map_bounds(point) or (
                not unit.is_flying and not bot.in_pathing_grid(point)
            ):
                continue
            overlap = sum(
                max(
                    0.0,
                    h.radius + unit.radius + 0.5 - math.sqrt(distance_sq(point, h.position)),
                )
                for h in relevant
            )
            threat = sum(
                1.0
                for e in enemies
                if can_hit(e, unit)
                and distance_sq(point, e.position)
                < (weapon_range(e, unit) + e.radius + unit.radius) ** 2
            )
            candidates.append((overlap * 100 + threat * 2 + abs(offset) * 0.1, point))
        if candidates:
            point = min(candidates, key=lambda pair: pair[0])[1]
            bus.issue(unit.move(point), force=True)
        return True

    @staticmethod
    def _select(unit: Unit, targets: list[Unit]) -> Unit | None:
        candidates = [enemy for enemy in targets if can_hit(unit, enemy)]
        if not candidates:
            return None

        def score(enemy: Unit) -> tuple[float, int]:
            priority = PRIORITY.get(enemy.type_id, 2.0 if enemy.can_attack else 0.0)
            if unit.type_id == U.MARAUDER and enemy.is_armored:
                priority += 3.0
            if unit.type_id == U.VIKINGFIGHTER and enemy.type_id == U.COLOSSUS:
                priority += 5.0
            missing_health = 1.0 - enemy.health_percentage
            distance = math.sqrt(distance_sq(unit.position, enemy.position))
            return priority + 3.0 * missing_health - 0.7 * distance, -enemy.tag

        return max(candidates, key=score)

    def _move(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        point: Point2,
        attack: bool = False,
    ) -> None:
        safe = self._safe_point(bot, unit, point)
        if safe is None or distance_sq(unit.position, safe) < 1.0:
            return
        bus.issue(unit.attack(safe) if attack and unit.can_attack else unit.move(safe))

    def _morph(self, bot: BotAI, bus: CommandBus, unit: Unit, ability: A) -> bool:
        if (
            bus.has(unit, ability)
            and bot.time - self._last_morph.get(unit.tag, -100.0) >= 2.5
            and bus.issue(unit(ability))
        ):
            self._last_morph[unit.tag] = bot.time
            return True
        return False

    def _tank(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        targets: list[Unit],
        plan: BattlePlan,
    ) -> None:
        ground = [e for e in targets if not e.is_flying]
        if unit.type_id == U.SIEGETANKSIEGED:
            firing = [
                e
                for e in ground
                if in_weapon_range(unit, e)
                and distance_sq(unit.position, e.position) > (2.0 + unit.radius + e.radius) ** 2
            ]
            if firing:
                self._tank_contact[unit.tag] = bot.time
            if plan.stance == Stance.RETREAT:
                self._morph(bot, bus, unit, A.UNSIEGE_UNSIEGE)
                return
            if firing:
                target = self._select(unit, firing)
                if target is not None and unit.weapon_cooldown <= 0:
                    bus.issue(unit.attack(target))
                return
            # 贴身敌人和没有射击目标是不同情形；不能永远在最小射程内发呆。
            trapped = any(
                e.can_attack_ground and distance_sq(unit.position, e.position) < 4.0**2
                for e in ground
            )
            stale = bot.time - self._tank_contact.get(unit.tag, -100.0) >= 4.0
            distant = distance_sq(unit.position, plan.objective) > 8.0**2
            if trapped or (stale and distant):
                self._morph(bot, bus, unit, A.UNSIEGE_UNSIEGE)
            return
        if plan.stance == Stance.RETREAT:
            self._move(bot, bus, unit, plan.fallback)
            return
        deploy = [e for e in ground if 4.5**2 < distance_sq(unit.position, e.position) < 11.5**2]
        if deploy and self._morph(bot, bus, unit, A.SIEGEMODE_SIEGEMODE):
            self._tank_contact[unit.tag] = bot.time
            return
        # 极近的近战敌人面前先后退；其它时候移动形态也能正常开火。
        close = [
            e
            for e in ground
            if e.can_attack_ground
            and e.ground_range < 2
            and distance_sq(unit.position, e.position) < 4.5**2
        ]
        if close and unit.weapon_cooldown > 0:
            danger = min(close, key=lambda e: distance_sq(unit.position, e.position))
            self._move(bot, bus, unit, self._away(unit, danger.position, 3.0))
        else:
            self._infantry(bot, bus, unit, ground, targets, plan, stim=False)

    def _infantry(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        enemies: list[Unit],
        targets: list[Unit],
        plan: BattlePlan,
        stim: bool = True,
    ) -> None:
        firing = [e for e in targets if in_weapon_range(unit, e)]
        target = self._select(unit, firing)
        threats = [e for e in enemies if can_hit(e, unit)]
        if plan.stance == Stance.RETREAT:
            if (
                target is not None
                and unit.weapon_cooldown <= 0
                and distance_sq(unit.position, plan.fallback) < 8**2
            ):
                bus.issue(unit.attack(target))
            else:
                self._move(bot, bus, unit, plan.fallback)
            return
        if (
            unit.health_percentage < 0.23
            and threats
            and distance_sq(unit.position, plan.fallback) > 7**2
        ):
            self._move(bot, bus, unit, plan.fallback)
            return
        if (
            stim
            and unit.type_id in BIO
            and target is not None
            and any(e.can_attack and e.type_id not in WORKERS for e in firing)
        ):
            ability = A.EFFECT_STIM_MARINE if unit.type_id == U.MARINE else A.EFFECT_STIM_MARAUDER
            buff = BuffId.STIMPACK if unit.type_id == U.MARINE else BuffId.STIMPACKMARAUDER
            if (
                unit.health_percentage > 0.6
                and not unit.has_buff(buff)
                and bus.has(unit, ability)
                and bus.issue(unit(ability))
            ):
                return
        if target is not None and unit.weapon_cooldown <= 0:
            bus.issue(unit.attack(target))
            return
        if unit.weapon_cooldown > max(1.0, self.config.game_step * 0.6) and threats:
            close = min(threats, key=lambda e: distance_sq(unit.position, e.position))
            outranges = (
                can_hit(unit, close) and weapon_range(unit, close) > weapon_range(close, unit) + 0.7
            )
            dangerous = close.type_id in {U.BANELING, U.BANELINGBURROWED}
            if (outranges or dangerous) and in_weapon_range(unit, close, padding=0.8):
                self._move(bot, bus, unit, self._away(unit, close.position, 2.5))
                return
        destination = plan.rally if plan.stance == Stance.HOLD else plan.objective
        self._move(bot, bus, unit, destination, attack=True)

    def _support_escape(self, bot: BotAI, bus: CommandBus, unit: Unit, enemies: list[Unit]) -> bool:
        threats = [e for e in enemies if e.can_attack_air and in_weapon_range(e, unit, padding=2.5)]
        if not threats:
            return False
        if (
            unit.type_id == U.MEDIVAC
            and bus.has(unit, A.EFFECT_MEDIVACIGNITEAFTERBURNERS)
            and bus.issue(unit(A.EFFECT_MEDIVACIGNITEAFTERBURNERS))
        ):
            return True
        threat = min(threats, key=lambda e: distance_sq(unit.position, e.position))
        self._move(bot, bus, unit, self._away(unit, threat.position, 6.0))
        return True

    def _medivac(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        enemies: list[Unit],
        wounded: list[Unit],
        claims: Counter[int],
        anchor: Point2,
        plan: BattlePlan,
    ) -> None:
        if self._support_escape(bot, bus, unit, enemies):
            return
        patients = [
            u
            for u in wounded
            if distance_sq(unit.position, u.position) <= 12**2 and claims[u.tag] < 2
        ]
        if patients and unit.energy >= 5 and bus.has(unit, A.MEDIVACHEAL_HEAL):
            patient = min(
                patients,
                key=lambda u: (
                    claims[u.tag] * 4
                    + u.health_percentage * 3
                    + math.sqrt(distance_sq(unit.position, u.position)) * 0.2,
                    u.tag,
                ),
            )
            claims[patient.tag] += 1
            # Heal 自带走入施法距离，指令不会被后面的统一跟随覆盖。
            bus.issue(unit(A.MEDIVACHEAL_HEAL, patient))
            return
        destination = plan.fallback if plan.stance == Stance.RETREAT else anchor
        self._move(bot, bus, unit, destination)

    def _raven(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        enemies: list[Unit],
        anchor: Point2,
        plan: BattlePlan,
    ) -> None:
        if self._support_escape(bot, bus, unit, enemies):
            return
        if plan.stance == Stance.RETREAT:
            self._move(bot, bus, unit, plan.fallback)
            return
        hidden = [
            e for e in enemies if not e.can_be_attacked and distance_sq(e.position, anchor) < 16**2
        ]
        if hidden:
            target = min(hidden, key=lambda e: distance_sq(unit.position, e.position))
            # 在探测半径内停在己方部队这一侧，不直接飞到隐形单位头顶。
            distance = max(3.0, unit.detect_range - 2.0)
            destination = (
                target.position.towards(anchor, min(distance, target.position.distance_to(anchor)))
                if target.position != anchor
                else anchor
            )
            self._move(bot, bus, unit, destination)
        else:
            self._move(
                bot,
                bus,
                unit,
                anchor.towards(plan.fallback, 3.0) if anchor != plan.fallback else anchor,
            )

    def _viking(
        self,
        bot: BotAI,
        bus: CommandBus,
        unit: Unit,
        enemies: list[Unit],
        targets: list[Unit],
        anchor: Point2,
        plan: BattlePlan,
        intel: IntelReport,
    ) -> None:
        air_targets = [e for e in targets if e.is_flying or e.type_id == U.COLOSSUS]
        if unit.type_id == U.VIKINGASSAULT:
            if (air_targets or intel.air_supply >= 2) and self._morph(
                bot, bus, unit, A.MORPH_VIKINGFIGHTERMODE
            ):
                return
            self._infantry(bot, bus, unit, enemies, targets, plan, stim=False)
            return
        if plan.stance == Stance.RETREAT:
            self._move(bot, bus, unit, plan.fallback)
            return
        firing = [e for e in air_targets if in_weapon_range(unit, e)]
        target = self._select(unit, firing)
        if target is not None and unit.weapon_cooldown <= 0:
            bus.issue(unit.attack(target))
            return
        threats = [e for e in enemies if e.can_attack_air and in_weapon_range(e, unit, padding=1.0)]
        if threats:
            threat = min(threats, key=lambda e: distance_sq(unit.position, e.position))
            self._move(bot, bus, unit, self._away(unit, threat.position, 3.0))
            return
        if air_targets:
            target = self._select(unit, air_targets)
            if target is not None:
                self._move(bot, bus, unit, target.position, attack=True)
                return
        quiet = self._last_air_seen is not None and bot.time - self._last_air_seen > 30.0
        exposed_ground = any(
            not e.is_flying and distance_sq(unit.position, e.position) < 7**2 for e in targets
        )
        if (
            quiet
            and exposed_ground
            and intel.air_supply < 0.5
            and intel.colossi == 0
            and not any(e.can_attack_air for e in enemies)
            and self._morph(bot, bus, unit, A.MORPH_VIKINGASSAULTMODE)
        ):
            return
        self._move(bot, bus, unit, anchor)
