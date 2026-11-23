"""只记住自己真正看到过的敌情，并明确处理记忆过期。

这里的 ``power`` 是原创的粗略战力评分，不是战斗模拟器，也不是胜率。
它用于避免明显以少打多；真正接敌后的技能、射程和走位由 combat 处理。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from .model import Contact, IntelReport

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI
    from sc2.unit import Unit

    from .config import BotConfig


WORKERS = frozenset({U.SCV, U.PROBE, U.DRONE, U.DRONEBURROWED, U.MULE})
BASES = frozenset(
    {
        U.COMMANDCENTER,
        U.COMMANDCENTERFLYING,
        U.ORBITALCOMMAND,
        U.ORBITALCOMMANDFLYING,
        U.PLANETARYFORTRESS,
        U.NEXUS,
        U.HATCHERY,
        U.LAIR,
        U.HIVE,
    }
)
# 这些单位未必有普通武器字段，但明显不能作为“零战力”处理。
SUPPORT_COMBAT = frozenset(
    {
        U.CARRIER,
        U.MEDIVAC,
        U.RAVEN,
        U.VIPER,
        U.INFESTOR,
        U.INFESTORBURROWED,
        U.HIGHTEMPLAR,
        U.DISRUPTOR,
        U.SENTRY,
        U.WARPPRISM,
        U.WARPPRISMPHASING,
        U.QUEEN,
        U.QUEENBURROWED,
    }
)
CLOAK_EVIDENCE = frozenset(
    {
        U.DARKSHRINE,
        U.DARKTEMPLAR,
        U.BANSHEE,
        U.GHOSTACADEMY,
        U.GHOST,
        U.WIDOWMINE,
        U.WIDOWMINEBURROWED,
        U.LURKERDENMP,
        U.LURKERMP,
        U.LURKERMPBURROWED,
    }
)
PROXY_BUILDINGS = frozenset(
    {
        U.PYLON,
        U.PHOTONCANNON,
        U.SHIELDBATTERY,
        U.BARRACKS,
        U.BUNKER,
        U.GATEWAY,
        U.WARPGATE,
        U.NYDUSCANAL,
        U.SPINECRAWLER,
        U.SPINECRAWLERUPROOTED,
    }
)
AIR_THREATS_WITHOUT_WEAPONS = frozenset({U.CARRIER, U.RAVEN, U.VIPER})


def unit_supply(bot: BotAI, unit: Unit) -> float:
    """读取单位占用的总人口，不能用“变形增量人口”代替总人口。

    例如 Baneling 的生产增量为零，仍应计入敌军规模。少量未知或装饰型
    单位没有数据时返回零，不据未知单位凭空推测数值。
    """
    try:
        return max(0.0, float(bot.game_data.units[unit.type_id.value]._proto.food_required))
    except (AttributeError, KeyError):
        return 0.0


def unit_power(bot: BotAI, unit: Unit) -> float:
    """用正常观察到的单位数据给出可解释、低成本的战力粗估。"""
    if unit.type_id in WORKERS:
        return 0.3
    ground = bool(unit.can_attack_ground)
    air = bool(unit.can_attack_air)
    if unit.is_structure:
        if unit.type_id == U.BUNKER:
            # 观察不到地堡内装载情况：把它当作潜在威胁，而非断言有四个兵。
            return 4.5 * max(0.25, float(unit.build_progress))
        if not (ground or air):
            return 0.0
        dps = max(float(unit.ground_dps), float(unit.air_dps))
        return min(12.0, 2.0 + dps * 0.18) * max(0.25, float(unit.build_progress))
    supply = unit_supply(bot, unit)
    if not (ground or air or unit.type_id in SUPPORT_COMBAT):
        return 0.0
    dps = max(float(unit.ground_dps), float(unit.air_dps))
    # 带盾和残血都使用当时合法观察到的生命信息；新观察才会更新此值。
    health = max(0.0, min(1.0, float(unit.shield_health_percentage)))
    value = max(0.5, supply) * (0.75 + min(dps, 45.0) / 50.0)
    return value * (0.45 + 0.55 * health)


class Intelligence:
    """敌情记忆、基地威胁判断，以及没有已知目标时的搜索顺序。"""

    def __init__(self, config: BotConfig):
        self.config = config
        self.contacts: dict[int, Contact] = {}
        self.report = IntelReport()
        self._cloak_seen_at = -math.inf
        self._visited: dict[tuple[float, float], float] = {}
        self._search_points: tuple[Point2, ...] = ()

    def forget(self, tag: int) -> None:
        """由公开的 on_unit_destroyed 事件调用；不要推测雾中的死亡。"""
        self.contacts.pop(tag, None)

    def _confidence(self, contact: Contact, now: float) -> float:
        if contact.is_structure and not contact.is_flying:
            return 1.0
        age = max(0.0, now - contact.seen_at)
        # 近期记忆仍有价值，但不会把 40 秒前的敌军算成当前精确兵力。
        return max(0.0, 1.0 - age / self.config.mobile_memory_seconds)

    @staticmethod
    def _area_visible(bot: BotAI, point: Point2) -> bool:
        # 仅看到建筑中心所在格的边缘不足以确认整栋建筑已经消失。
        offsets = ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2))
        for x, y in offsets:
            sample = Point2((point.x + x, point.y + y))
            if hasattr(bot, "in_map_bounds") and not bot.in_map_bounds(sample):
                return False
            if not bot.is_visible(sample):
                return False
        return True

    def update(self, bot: BotAI) -> IntelReport:
        now = float(bot.time)
        observed = [
            unit
            for unit in (*bot.enemy_units, *bot.enemy_structures)
            if unit.is_visible and not unit.is_snapshot
        ]
        visible_tags = {unit.tag for unit in observed}
        for unit in observed:
            self.contacts[unit.tag] = Contact(
                tag=unit.tag,
                type_id=unit.type_id,
                position=unit.position,
                seen_at=now,
                is_structure=unit.is_structure,
                is_flying=unit.is_flying,
                supply=unit_supply(bot, unit),
                power=unit_power(bot, unit),
                is_armored=unit.is_armored,
                can_attack_ground=unit.can_attack_ground,
                can_attack_air=unit.can_attack_air,
            )
            if unit.type_id in CLOAK_EVIDENCE or unit.is_cloaked or unit.is_burrowed:
                self._cloak_seen_at = now

        for tag, contact in tuple(self.contacts.items()):
            stationary = contact.is_structure and not contact.is_flying
            lifetime = (
                self.config.structure_memory_seconds
                if stationary
                else self.config.mobile_memory_seconds
            )
            expired = now - contact.seen_at > lifetime
            confirmed_empty = (
                stationary and tag not in visible_tags and self._area_visible(bot, contact.position)
            )
            if expired or confirmed_empty:
                del self.contacts[tag]
            # 移动单位即使离开了已复查的旧坐标，也可能只是走进雾中；不凭空判死。

        air = ground = armored = enemy_power = 0.0
        colossi = bases = 0
        structures: list[Point2] = []
        cloak = now - self._cloak_seen_at < 180.0
        for contact in self.contacts.values():
            confidence = self._confidence(contact, now)
            if contact.type_id in CLOAK_EVIDENCE:
                cloak = True
            if contact.is_structure:
                structures.append(contact.position)
                bases += contact.type_id in BASES
                continue
            if contact.type_id in WORKERS or contact.power <= 0:
                continue
            enemy_power += contact.power * confidence
            if contact.is_flying:
                if (
                    contact.can_attack_air
                    or contact.can_attack_ground
                    or contact.type_id in AIR_THREATS_WITHOUT_WEAPONS
                ):
                    air += contact.supply * confidence
            else:
                ground += contact.supply * confidence
                if contact.is_armored:
                    armored += contact.supply * confidence
            if contact.type_id == U.COLOSSUS and confidence > 0.25:
                colossi += 1

        homes = [hall.position for hall in bot.townhalls if not hall.is_flying]
        if not homes:
            homes = [bot.start_location]
        home_contacts = [
            self.contacts[unit.tag]
            for unit in observed
            if any(unit.position.distance_to(home) < 26.0 for home in homes)
        ]
        armed = [c for c in home_contacts if c.power > 0 and c.type_id not in WORKERS]
        workers = [c for c in home_contacts if c.type_id in WORKERS]
        proxies = [c for c in home_contacts if c.type_id in PROXY_BUILDINGS]
        home_power = sum(c.power for c in armed)
        if len(workers) >= 4:
            home_power += len(workers) * 0.7
        # 一只探路农民不会把整支军队拉回家；施工中的炮台/地堡必须及时处理。
        significant = home_power >= 1.5 or bool(proxies)
        candidates = armed or proxies or workers
        home_threat = None
        if significant and candidates:
            threat = min(
                candidates,
                key=lambda c: (min(c.position.distance_to(p) for p in homes), c.tag),
            )
            home_threat = threat.position
        self.report = IntelReport(
            air_supply=air,
            ground_supply=ground,
            armored_ground_supply=armored,
            enemy_power=enemy_power,
            enemy_bases=bases,
            cloak_risk=cloak,
            rush=now < 300 and (home_power >= 4.0 or bool(proxies)),
            home_threat=home_threat,
            home_threat_power=home_power,
            known_structures=tuple(sorted(structures, key=lambda p: (p.x, p.y))),
            visible_enemies=len(observed),
            colossi=colossi,
        )
        return self.report

    def _initialize_search_points(self, bot: BotAI) -> None:
        starts = tuple(bot.enemy_start_locations)
        expansions = tuple(bot.expansion_locations_list)
        enemy_anchor = starts[0] if starts else bot.game_info.map_center
        points = [
            *starts,
            *sorted(expansions, key=lambda p: (p.distance_to(enemy_anchor), p.x, p.y)),
        ]
        unique: list[Point2] = []
        for point in points:
            if point.distance_to(bot.start_location) < 12.0:
                continue
            if all(point.distance_to(old) > 5.0 for old in unique):
                unique.append(point)
        self._search_points = tuple(unique) or (enemy_anchor,)

    def target(self, bot: BotAI) -> Point2:
        """返回合法观察的目标/最后坐标；无情报时轮流实地搜索公开扩张点。"""
        known = list(self.contacts.values())
        targets = [c for c in known if c.is_structure]
        if targets:
            # 外围建筑也可能挡路；从自己的前方基地向最近的已知建筑推进。
            homes = [hall.position for hall in bot.townhalls if not hall.is_flying]
            enemy_anchor = (
                bot.enemy_start_locations[0]
                if bot.enemy_start_locations
                else bot.game_info.map_center
            )
            origin = (
                min(homes, key=lambda p: (p.distance_to(enemy_anchor), p.x, p.y))
                if homes
                else bot.start_location
            )
            return min(
                targets,
                key=lambda c: (c.position.distance_to(origin), c.tag),
            ).position
        if not self._search_points:
            self._initialize_search_points(bot)
        now = float(bot.time)
        for point in self._search_points:
            if self._area_visible(bot, point):
                self._visited[(point.x, point.y)] = now
        # 未访问地点优先；相同访问时间保留事先排序，结果不依赖集合遍历顺序。
        return min(
            self._search_points,
            key=lambda p: self._visited.get((p.x, p.y), -math.inf),
        )
