"""DeepSeek 人族（Terran）竞技 Bot。

设计目标
--------
* 从 12 农民开局持续运营到中后期：双基地起步、三基地滚雪球、持续补农民与补给。
* 主力为陆战队员（Marine）+ 掠夺者（Marauder）+ 医疗运输机（Medivac），
  中期加入少量攻城坦克（Siege Tank）用于阵地战与防守。
* 具备侦察、防守、撤退、主动推进与骚扰后恢复能力。
* 不读取任何对手实现，不使用外部网络接口；所有决策只依赖本地游戏 API 与
  己方/正常视野内的信息。

对外契约
--------
`CompetitiveBot` 可无参数实例化，继承 `sc2.bot_ai.BotAI`，供统一对局脚本加载。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

logger = logging.getLogger(__name__)

# 被视为“基地”的敌方建筑，用于选择进攻目标。
ENEMY_TOWNHALL_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.COMMANDCENTER,
        UnitTypeId.ORBITALCOMMAND,
        UnitTypeId.PLANETARYFORTRESS,
        UnitTypeId.NEXUS,
        UnitTypeId.HATCHERY,
        UnitTypeId.LAIR,
        UnitTypeId.HIVE,
    }
)

# 优先集火的高价值目标。
HIGH_VALUE_TARGETS: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.SIEGETANK,
        UnitTypeId.SIEGETANKSIEGED,
        UnitTypeId.COLOSSUS,
        UnitTypeId.IMMORTAL,
        UnitTypeId.LURKER,
        UnitTypeId.LURKERBURROWED,
        UnitTypeId.BANELING,
        UnitTypeId.ULTRALISK,
        UnitTypeId.THOR,
        UnitTypeId.BROODLORD,
        UnitTypeId.DISRUPTOR,
        UnitTypeId.HIGHTEMPLAR,
        UnitTypeId.RAVEN,
        UnitTypeId.WIDOWMINEBURROWED,
    }
)

# 己方“作战单位”集合（医疗运输机单独处理）。
ARMY_TYPES: frozenset[UnitTypeId] = frozenset(
    {
        UnitTypeId.MARINE,
        UnitTypeId.MARAUDER,
        UnitTypeId.SIEGETANK,
        UnitTypeId.SIEGETANKSIEGED,
    }
)


# ---------------------------------------------------------------------------
# 纯决策函数：不依赖游戏客户端，便于离线单元测试与场景回归。
# ---------------------------------------------------------------------------


def supply_depot_needed(
    supply_used: float,
    supply_cap: float,
    pending_depots: int,
    reserved_supply: float = 0,
    margin: int = 4,
) -> bool:
    """是否需要补人口。

    已开工但未完成的人口不会立刻提供上限，因此按“每个待建人口 +8”估算，
    避免同一时间过量拍人口导致经济被拖慢。
    """
    if supply_cap >= 200:
        return False
    projected = supply_cap + 8 * pending_depots - supply_used - reserved_supply
    return projected <= margin


def worker_target(mineral_fields: int, refineries: int) -> int:
    """农民目标数：每块矿 2 人、每座气矿 3 人，最多 66 人。"""
    return min(66, max(0, mineral_fields) * 2 + max(0, refineries) * 3)


def desired_base_count(
    time: float,
    workers: int,
    army_supply: float,
    current_bases: int,
    threat_level: int,
) -> int:
    """根据时间、农民数与兵力决定“想要几个基地”。

    遭受攻击时不扩张；农民不足时优先补经济而不是开矿。
    """
    if threat_level > 0:
        return current_bases
    target = 1
    if time >= 150 and workers >= 16:
        target = 2
    if time >= 420 and workers >= 30 and army_supply >= 12:
        target = 3
    if time >= 660 and workers >= 46 and army_supply >= 20:
        target = 4
    if time >= 900 and workers >= 58:
        target = 5
    return max(target, current_bases)


def choose_expansion_index(
    distances: Sequence[float],
    taken: Sequence[bool],
    blocked: Sequence[bool],
    cooldown: Sequence[bool],
) -> int | None:
    """选择下一个扩张点；跳过已占据、被敌人压制的点与刚失守的冷却点。"""
    best_index: int | None = None
    best_distance = float("inf")
    for index, distance in enumerate(distances):
        if taken[index] or blocked[index] or cooldown[index]:
            continue
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def attack_ready(
    army_supply: float,
    medivacs: int,
    time: float,
    enemy_army_supply: float,
    threat_level: int,
) -> bool:
    """是否已经具备主动推进的条件。

    * 家里被打时不出门；
    * 300 秒前不主动进攻（保留运营窗口）；
    * 至少 26 人口部队，对手明显虚弱时放宽到 20；
    * 540 秒前至少带 1 架医疗运输机，之后可以先行；
    * 敌方兵力显著高于我方时不送。
    """
    if threat_level > 0 or time < 300:
        return False
    needed = 20 if enemy_army_supply < 10 else 26
    if army_supply < needed:
        return False
    if medivacs < 1 and time < 540:
        return False
    return not enemy_army_supply > army_supply * 1.3


def should_retreat(
    own_army_supply: float,
    own_units: int,
    enemy_army_supply: float,
    enemy_units: int,
    time_in_attack: float,
) -> bool:
    """推进受挫时是否撤退，避免主力被吃光。"""
    if own_units <= 6:
        return True
    if own_army_supply < 12:
        return True
    if enemy_units >= 8 and own_army_supply < enemy_army_supply * 0.75:
        return True
    # 久攻不下且兵力已经亏空，先回家重整。
    return time_in_attack > 150 and own_army_supply < 18


def classify_threat(
    enemy_supply_near_base: float,
    enemy_units_near_base: int,
    own_army_supply: float,
) -> int:
    """0=安全，1=骚扰，2=需要全力防守。"""
    if enemy_units_near_base <= 0:
        return 0
    if enemy_supply_near_base >= 10:
        return 2
    if enemy_units_near_base >= 4 and own_army_supply < 15:
        return 2
    return 1


def needs_marauder(marines: int, marauders: int) -> bool:
    """掠夺者约占生化部队的 1/4，用于抗重甲与吸收伤害。"""
    return marauders * 3 < marines


def needs_medivac(bio_count: int, medivacs: int) -> bool:
    """每 8 个生化单位配 1 架医疗运输机，最多 6 架。"""
    return medivacs < 6 and medivacs * 8 < bio_count


def needs_tank(time: float, tanks: int, factories: int) -> bool:
    """有工厂后补 3 辆坦克，优先保证生化产能。"""
    return factories > 0 and tanks < 3 and time >= 300


def needs_viking(enemy_air_supply: float, vikings: int) -> bool:
    """对手空军成型时转少量维京。"""
    return enemy_air_supply >= 6 and vikings < 6


def needs_turret(enemy_air_units: int, turrets: int, bases: int) -> bool:
    """对手空军单位达到 3 个后，每个基地补 1 座导弹塔。"""
    return enemy_air_units >= 3 and turrets < max(1, bases)


class CompetitiveBot(BotAI):
    """人族生化运营推进 Bot。

    开局：单兵营双开 -> 工厂/星港 -> 三兵营生化 + 医疗运输机，
    中期靠 +1/+2 攻防与坦克压制，兵力成型后主动推进敌方基地。
    """

    # ---- 生命周期 ---------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self.attack_state: str = "GATHER"
        self._attack_started_at: float = 0.0
        self._defense_until: float = 0.0
        self._last_rebalance: float = 0.0
        self._last_transfer: float = 0.0
        self._last_gas_fill: float = 0.0
        self._last_mule: float = 0.0
        self._last_scan: float = -999.0
        self._last_expand: float = -999.0
        self._last_log: float = 0.0
        self._last_army_order: float = 0.0
        self._last_stim: float = 0.0
        self._last_worker_flee: float = 0.0
        self._last_bunker_try: float = -999.0
        self._scout_tag: int | None = None
        self._scout_sent: bool = False
        self._natural: Point2 | None = None
        self._expansions: list[Point2] = []
        self._lost_bases: dict[tuple[int, int], float] = {}
        self._known_base_positions: dict[int, Point2] = {}
        # 仅记录“亲眼看到过”的敌方建筑，用于制定进攻目标。
        self._seen_enemy_structures: dict[int, tuple[UnitTypeId, Point2, float]] = {}
        self._enemy_air_supply: float = 0.0
        self._enemy_army_supply: float = 0.0
        self._threat_level: int = 0
        self._threat_supply: float = 0.0
        self._threat_position: Point2 | None = None
        self._economy_emergency_until: float = 0.0
        # 开局建造序列的完成标记（单兵营双开）。
        self._opening_done: bool = False
        self._open_depot1: bool = False
        self._open_rax: bool = False
        self._open_gas: bool = False
        self._open_depot2: bool = False
        self._open_cc: bool = False

    async def on_start(self) -> None:
        self.client.game_step = 2
        self._expansions = sorted(
            self.expansion_locations_list,
            key=lambda pos: pos.distance_to(self.start_location),
        )
        if len(self._expansions) > 1:
            self._natural = self._expansions[1]
        self._known_base_positions = {th.tag: th.position for th in self.townhalls}
        logger.info(
            "[DeepSeek] 开局：种族=%s 对手=%s 地图=%s 起始点=%s",
            self.race,
            self.enemy_race,
            self.game_info.map_name,
            self.start_location,
        )

    async def on_step(self, iteration: int) -> None:
        if not self.townhalls:
            # 基地全失：已无法运营，交由对局脚本判定胜负。
            return

        self._track_base_losses()
        self._update_intel()
        self._manage_orbital()
        await self._manage_economy()
        opening = await self._manage_opening()
        # 二人口完成后即便还在等 400 矿开分矿，也要允许补人口，避免卡死。
        if not opening or self._open_depot2:
            await self._manage_supply()
        self._manage_addons()
        if not opening:
            await self._manage_structures()
        self._manage_tech()
        self._manage_army_production()
        if not opening:
            await self._manage_expansion()
        self._manage_scouting()
        await self._manage_defense()
        await self._manage_air_defense()
        self._manage_army()
        self._log_status()

    # ---- 开局建造序列 -----------------------------------------------------

    async def _manage_opening(self) -> bool:
        """单兵营双开：14 人口 -> 兵营 -> 气矿 -> 二人口 -> 自然分矿。

        返回 True 表示开局序列仍在进行，此时暂缓通用补给/建筑/扩张逻辑，
        避免早期重复拍建筑或抢走分矿的 400 矿。
        """
        if self._opening_done:
            return False

        anchor = self._main_base().position
        # 1) 14 人口补给站。
        if not self._open_depot1:
            if self.supply_used < 13 or self.minerals < 100:
                return True
            if await self.build(
                UnitTypeId.SUPPLYDEPOT,
                near=anchor,
                max_distance=10,
                random_alternative=False,
            ):
                self._open_depot1 = True
            return True

        # 2) 15 人口兵营。
        if not self._open_rax:
            if self.supply_used < 15 or self.minerals < 150:
                return True
            if await self.build(
                UnitTypeId.BARRACKS,
                near=anchor,
                max_distance=10,
                random_alternative=False,
            ):
                self._open_rax = True
            return True

        # 3) 气矿，为科技实验室与兴奋剂做准备。
        if not self._open_gas:
            if self.minerals >= 75:
                geyser = self._available_geyser()
                if geyser is not None and await self.build(
                    UnitTypeId.REFINERY, near=geyser
                ):
                    self._open_gas = True
            return True

        # 4) 20 人口第二座补给站，保证分矿前不断人口。
        if not self._open_depot2:
            if self.supply_used < 20 and self.time < 180:
                return True
            if await self.build(
                UnitTypeId.SUPPLYDEPOT,
                near=anchor,
                max_distance=12,
                random_alternative=False,
            ):
                self._open_depot2 = True
            return True

        # 5) 自然分矿。若长期攒不到 400 矿，超时后放行通用运营逻辑。
        if not self._open_cc:
            natural_blocked = self._natural is not None and any(
                unit.distance_to(self._natural) < 10 for unit in self.enemy_units
            )
            if natural_blocked:
                return True
            if self.time >= 150 and self.minerals >= 400:
                if self._natural is not None:
                    await self.expand_now(location=self._natural)
                else:
                    await self.expand_now()
                self._open_cc = True
                self._last_expand = self.time
                logger.info("[DeepSeek] 单兵营双开：自然分矿开工")
            elif self.time >= 420:
                self._open_cc = True
            return True

        self._opening_done = True
        logger.info("[DeepSeek] 开局序列完成，进入通用运营")
        return False

    # ---- 敌方情报 ---------------------------------------------------------

    def _update_intel(self) -> None:
        """更新可见敌军规模与“见过的建筑”记忆。

        只使用当前视野内的 `enemy_units` 与 `enemy_structures`，不读取战争迷雾
        之外的信息；记忆仅用于攻击已侦察到的建筑坐标。
        """
        now = self.time
        enemy_air_supply = 0.0
        enemy_army_supply = 0.0
        for unit in self.enemy_units:
            supply = self.calculate_supply_cost(unit.type_id)
            if unit.is_flying and unit.can_attack:
                enemy_air_supply += supply
            if unit.can_attack or unit.type_id in HIGH_VALUE_TARGETS:
                enemy_army_supply += supply
        self._enemy_air_supply = enemy_air_supply
        self._enemy_army_supply = enemy_army_supply

        for structure in self.enemy_structures:
            self._seen_enemy_structures[structure.tag] = (
                structure.type_id,
                structure.position,
                now,
            )
        # 只保留 10 分钟内的建筑记忆，避免字典无限增长。
        for tag in [
            tag
            for tag, (_, _, seen_at) in self._seen_enemy_structures.items()
            if now - seen_at > 600
        ]:
            del self._seen_enemy_structures[tag]

    # ---- 基地与农民 -------------------------------------------------------

    def _track_base_losses(self) -> None:
        """记录基地失守的位置，用于扩张点冷却，避免立刻回去送死。"""
        current = {th.tag: th.position for th in self.townhalls}
        for tag, position in self._known_base_positions.items():
            if tag not in current:
                key = (int(position.x), int(position.y))
                self._lost_bases[key] = self.time
                logger.warning("[DeepSeek] 基地失守，位置=%s", position)
        self._known_base_positions = current

    def _main_base(self):
        return self.townhalls.first

    def _home_point(self) -> Point2:
        """防守/集结位置：有自然分矿则守自然，否则守主基地。"""
        if self._natural and self._owns_position(self._natural):
            return self._natural
        return self.start_location

    def _owns_position(self, position: Point2) -> bool:
        return any(th.distance_to(position) < 15 for th in self.townhalls)

    def _owned_mineral_fields(self) -> int:
        if not self.townhalls:
            return 0
        count = 0
        for field in self.mineral_field:
            if any(field.distance_to(th.position) < 12 for th in self.townhalls):
                count += 1
        return count

    async def _manage_economy(self) -> None:
        """补农民、分配空闲农民、补气矿、投放矿骡与战后恢复。"""
        if not self.townhalls:
            return

        mineral_fields = self._owned_mineral_fields()
        refineries = self.structures(UnitTypeId.REFINERY).amount
        target_workers = worker_target(mineral_fields, refineries)

        # 被骚扰后 20 秒内进入经济紧急状态：优先补农民，暂缓扩张与科技建筑。
        if self.workers.amount < target_workers * 0.55:
            self._economy_emergency_until = self.time + 20

        # 持续补农民；只在有补给余量时训练，避免卡人口。
        if (
            self.workers.amount < target_workers
            and self.supply_left > 0
            and self.can_afford(UnitTypeId.SCV)
        ):
            self.train(UnitTypeId.SCV, 1)

        # 补气矿：单矿期只开一座，双矿后每矿两座，气矿总数不超过基地数 * 2。
        if len(self.townhalls) == 1 and self.time < 240:
            desired_refineries = 1
        elif self.time < 360:
            desired_refineries = 2
        elif self.time < 480:
            desired_refineries = 3
        else:
            desired_refineries = min(6, len(self.townhalls) * 2)
        if (
            self._opening_done
            and refineries + self.already_pending(UnitTypeId.REFINERY)
            < desired_refineries
            and self.minerals >= 100
            and self._can_start_building(75)
        ):
            geyser = self._available_geyser()
            if geyser is not None:
                await self.build(UnitTypeId.REFINERY, near=geyser)

        if self.time - self._last_rebalance >= 0.4:
            self._last_rebalance = self.time
            self._rebalance_workers()
        if self.time - self._last_transfer >= 1.5:
            self._last_transfer = self.time
            self._transfer_surplus_workers()
        if self.time - self._last_gas_fill >= 0.6:
            self._last_gas_fill = self.time
            self._fill_refineries()

    def _can_start_building(self, cost: int) -> bool:
        """常规建造留出 100 矿余量；经济紧急时不额外留。"""
        if self._economy_emergency_until > self.time:
            return self.minerals >= cost
        return self.minerals >= cost + 100

    def _best_mineral_field(self, worker, prefer_near: Point2 | None = None):
        """为农民挑一块“人最少”的矿，优先当前基地。"""
        fields = self.mineral_field
        if not fields:
            return None
        if prefer_near is not None:
            nearby = fields.filter(lambda f: f.distance_to(prefer_near) < 12)
            if nearby:
                fields = nearby
        return min(fields, key=lambda f: (f.assigned_harvesters, f.distance_to(worker)))

    def _rebalance_workers(self) -> None:
        """空闲农民立刻回去采矿，这是被骚扰后恢复经济的关键。"""
        for worker in self.workers.idle:
            # 侦察农民在完成首轮侦察前不要召回。
            if worker.tag == self._scout_tag and self.time < 240:
                continue
            base = self.townhalls.closest_to(worker)
            field = self._best_mineral_field(worker, prefer_near=base.position)
            if field is not None:
                self.do(worker.gather(field))

    def _transfer_surplus_workers(self) -> None:
        """把主矿多余农民调往未饱和的分矿，避免一边拥挤一边缺人。"""
        bases = [th for th in self.townhalls if th.is_ready]
        if len(bases) < 2:
            return
        under = [b for b in bases if b.assigned_harvesters < 16]
        if not under:
            return
        target_base = min(under, key=lambda b: b.assigned_harvesters)
        donors = [b for b in bases if b.assigned_harvesters > 18]
        if not donors:
            return
        donor = max(donors, key=lambda b: b.assigned_harvesters)
        field = self._best_mineral_field(donor, prefer_near=target_base.position)
        if field is None:
            return
        candidates = self.workers.filter(
            lambda w: w.distance_to(donor) < 12 and w.order_target != field.tag
        )
        if candidates:
            worker = candidates.closest_to(target_base)
            self.do(worker.gather(field))

    def _fill_refineries(self) -> None:
        """每座气矿保持 3 名农民。"""
        for refinery in self.structures(UnitTypeId.REFINERY).ready:
            if refinery.assigned_harvesters >= 3:
                continue
            base = self.townhalls.closest_to(refinery)
            if base.assigned_harvesters <= 4:
                continue
            best_worker = None
            best_distance = 999.0
            for worker in self.workers:
                if worker.order_target == refinery.tag:
                    continue
                distance = worker.distance_to(refinery)
                if distance < 16 and distance < best_distance:
                    best_worker = worker
                    best_distance = distance
            if best_worker is None:
                continue
            self.do(best_worker.gather(refinery))
            return

    def _available_geyser(self):
        """找一座还没被气矿覆盖、且靠近己方基地的瓦斯泉。"""
        refineries = self.structures(UnitTypeId.REFINERY)
        candidates = [
            geyser
            for geyser in self.vespene_geyser
            if not any(geyser.distance_to(refinery) < 3 for refinery in refineries)
            and any(geyser.distance_to(th.position) < 14 for th in self.townhalls)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda g: g.distance_to(self._main_base()))

    # ---- 补给 -------------------------------------------------------------

    async def _manage_supply(self) -> None:
        if not self.townhalls:
            return
        pending = int(self.already_pending(UnitTypeId.SUPPLYDEPOT))
        if (
            supply_depot_needed(
                self.supply_used,
                self.supply_cap,
                pending,
                margin=4,
            )
            # 一次最多两座，减少“人口墙”。
            and pending < 2
            and self.minerals >= 100
        ):
            await self.build(
                UnitTypeId.SUPPLYDEPOT,
                near=self._main_base().position,
                max_distance=12,
                random_alternative=False,
            )

    # ---- 指挥中心 / 矿骡 / 扫描 ------------------------------------------

    def _manage_orbital(self) -> None:
        # 主基地尽早升级轨道指挥中心，获得矿骡与扫描。
        for cc in self.townhalls(UnitTypeId.COMMANDCENTER).ready:
            if self.time < 240 or self.minerals < 150:
                continue
            self.do(cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND))
            logger.info("[DeepSeek] 升级轨道指挥中心")
            return

        # 矿骡：每个轨道中心能量满 50 就投放，优先保证经济。
        if self.time - self._last_mule < 1.5:
            return
        if not self.mineral_field:
            return
        for orbital in self.townhalls(UnitTypeId.ORBITALCOMMAND).ready:
            if orbital.energy < 50:
                continue
            # 情报空白且该扫描时，至少留 50 能量给扫描。
            if self._scan_due() and orbital.energy < 100:
                continue
            field = self.mineral_field.closest_to(orbital)
            self.do(orbital(AbilityId.CALLDOWNMULE_CALLDOWNMULE, field))
            self._last_mule = self.time

    # ---- 生产建筑与附件 ---------------------------------------------------

    def _structure_targets(self) -> dict[UnitTypeId, int]:
        t = self.time
        barracks = 1 if t < 240 else 3 if t < 420 else 5 if t < 660 else 7
        factories = 0 if t < 210 else 1 if t < 480 else 2
        starports = 0 if t < 300 else 1 if t < 600 else 2
        engineering_bays = 1 if t >= 330 else 0
        armories = 1 if t >= 540 else 0
        return {
            UnitTypeId.BARRACKS: min(barracks, 7),
            UnitTypeId.FACTORY: factories,
            UnitTypeId.STARPORT: starports,
            UnitTypeId.ENGINEERINGBAY: engineering_bays,
            UnitTypeId.ARMORY: armories,
        }

    async def _manage_structures(self) -> None:
        """按时间目标补生产/科技建筑；资源紧张或挨打时降低优先级。"""
        if not self.townhalls:
            return
        if self._economy_emergency_until > self.time:
            return
        if self._threat_level > 0:
            return

        targets = self._structure_targets()
        # 先补兵营，再补科技；每步最多开工一个建筑，避免农民全部停工。
        for unit_type in (
            UnitTypeId.BARRACKS,
            UnitTypeId.FACTORY,
            UnitTypeId.STARPORT,
            UnitTypeId.ENGINEERINGBAY,
            UnitTypeId.ARMORY,
        ):
            target = targets[unit_type]
            if target <= 0:
                continue
            have = self.structures(unit_type).amount + int(
                self.already_pending(unit_type)
            )
            if have >= target:
                continue
            if self.minerals < 200 and unit_type != UnitTypeId.BARRACKS:
                continue
            if not self.can_afford(unit_type):
                continue
            anchor = self._main_base().position
            await self.build(
                unit_type,
                near=anchor,
                max_distance=14,
                random_alternative=False,
            )
            return

    def _manage_addons(self) -> None:
        """兵营/工厂/星港挂附件：科技实验室出掠夺者/坦克，反应堆爆生化。"""
        if not self.townhalls:
            return

        # 工厂与星港：工厂科技实验室（坦克），星港反应堆（双倍医疗运输机）。
        for factory in self.structures(UnitTypeId.FACTORY).ready.filter(
            lambda f: f.add_on_tag == 0 and f.is_idle
        ):
            if self.minerals >= 50 and self.vespene >= 25:
                self.do(factory(AbilityId.BUILD_TECHLAB_FACTORY))
                return

        for starport in self.structures(UnitTypeId.STARPORT).ready.filter(
            lambda s: s.add_on_tag == 0 and s.is_idle
        ):
            if self.minerals >= 50:
                self.do(starport(AbilityId.BUILD_REACTOR_STARPORT))
                return

        # 兵营：至少保留一个科技实验室（兴奋剂/掠夺者），其余反应堆。
        barracks = sorted(
            self.structures(UnitTypeId.BARRACKS).ready.filter(
                lambda b: b.add_on_tag == 0 and b.is_idle
            ),
            key=lambda b: b.tag,
        )
        if not barracks:
            return
        if not self.techlab_tags and self.vespene >= 25 and self.minerals >= 50:
            self.do(barracks[0](AbilityId.BUILD_TECHLAB_BARRACKS))
            return
        if self.minerals >= 50:
            self.do(barracks[0](AbilityId.BUILD_REACTOR_BARRACKS))

    # ---- 科技 -------------------------------------------------------------

    def _manage_tech(self) -> None:
        """按优先级研究：兴奋剂 > 盾牌 > +1 攻 > +1 防 > +2 ..."""
        if not self.townhalls:
            return
        priorities = (
            UpgradeId.STIMPACK,
            UpgradeId.SHIELDWALL,
            UpgradeId.TERRANINFANTRYWEAPONSLEVEL1,
            UpgradeId.TERRANINFANTRYARMORSLEVEL1,
            UpgradeId.TERRANINFANTRYWEAPONSLEVEL2,
            UpgradeId.TERRANINFANTRYARMORSLEVEL2,
            UpgradeId.TERRANINFANTRYWEAPONSLEVEL3,
            UpgradeId.TERRANINFANTRYARMORSLEVEL3,
        )
        for upgrade in priorities:
            if upgrade in self.state.upgrades:
                continue
            if self.already_pending_upgrade(upgrade) > 0:
                continue
            if self.research(upgrade):
                logger.info("[DeepSeek] 开始研究 %s", upgrade.name)
                return

    # ---- 军队生产 ---------------------------------------------------------

    def _manage_army_production(self) -> None:
        if not self.townhalls:
            return
        if self.supply_left <= 0:
            return
        # 分矿开工前只保留少量守家兵力，优先攒 400 矿。
        if not self._open_cc and self._threat_level == 0 and self.minerals < 250:
            return

        marines = self.units(UnitTypeId.MARINE).amount
        marauders = self.units(UnitTypeId.MARAUDER).amount
        medivacs = self.units(UnitTypeId.MEDIVAC).amount
        vikings = (
            self.units(UnitTypeId.VIKINGFIGHTER).amount
            + self.units(UnitTypeId.VIKINGASSAULT).amount
        )
        tanks = (
            self.units(UnitTypeId.SIEGETANK).amount
            + self.units(UnitTypeId.SIEGETANKSIEGED).amount
        )
        factories = self.structures(UnitTypeId.FACTORY).ready.amount
        starports = self.structures(UnitTypeId.STARPORT).ready.amount
        barracks = self.structures(UnitTypeId.BARRACKS).ready
        bio = marines + marauders

        # 星港：默认医疗运输机；对手空军成型则补维京。
        if starports > 0:
            if needs_viking(self._enemy_air_supply, vikings):
                self.train(UnitTypeId.VIKINGFIGHTER, 1)
            elif needs_medivac(bio, medivacs):
                self.train(UnitTypeId.MEDIVAC, 1)

        # 工厂：有科技实验室才排队坦克；否则留资源给生化。
        if needs_tank(self.time, tanks, factories):
            techlab_factory = self.structures(UnitTypeId.FACTORY).ready.filter(
                lambda f: f.add_on_tag in self.techlab_tags
            )
            if techlab_factory:
                self.train(UnitTypeId.SIEGETANK, 1)

        # 兵营：掠夺者占 1/4，其余机枪兵。
        if not barracks:
            return
        # 还没有科技实验室时，先让附件开工，否则兴奋剂/掠夺者永远补不上。
        # 420 秒后不再为此阻塞产能，避免附件位被挤占时兵营空转。
        if (
            not self.techlab_tags
            and self.time < 420
            and any(b.add_on_tag == 0 for b in barracks)
            and self.minerals >= 50
        ):
            return
        if self.minerals >= 100:
            techlab_barracks = barracks.filter(
                lambda b: b.add_on_tag in self.techlab_tags
            )
            if (
                techlab_barracks
                and needs_marauder(marines, marauders)
                and self.train(UnitTypeId.MARAUDER, 1)
            ):
                return
            self.train(UnitTypeId.MARINE, 2)

    # ---- 扩张 -------------------------------------------------------------

    async def _manage_expansion(self) -> None:
        if not self._expansions or not self.townhalls:
            return
        if self.time - self._last_expand < 25:
            return
        if self._threat_level > 0:
            return

        desired = desired_base_count(
            self.time,
            self.workers.amount,
            self._army_supply(),
            len(self.townhalls),
            self._threat_level,
        )
        pending_cc = int(self.already_pending(UnitTypeId.COMMANDCENTER))
        if len(self.townhalls) + pending_cc >= desired:
            return
        if self.minerals < 400 or not self.can_afford(UnitTypeId.COMMANDCENTER):
            return
        # 经济被打崩时先补农民，不急着开矿。
        if self._economy_emergency_until > self.time:
            return

        distances = [pos.distance_to(self.start_location) for pos in self._expansions]
        taken = [self._owns_position(pos) for pos in self._expansions]
        blocked = [
            any(
                unit.distance_to(pos) < 12
                for unit in self.enemy_units
                if not unit.is_flying
            )
            for pos in self._expansions
        ]
        cooldown = [self._expansion_on_cooldown(pos) for pos in self._expansions]
        index = choose_expansion_index(distances, taken, blocked, cooldown)
        if index is None:
            return
        target = self._expansions[index]
        await self.expand_now(location=target)
        self._last_expand = self.time
        logger.info(
            "[DeepSeek] 扩张到 %s（第 %d 基地）", target, len(self.townhalls) + 1
        )

    def _expansion_on_cooldown(self, position: Point2) -> bool:
        """刚失守的矿点 90 秒内不再去建，避免连续送基地。"""
        for (x, y), lost_at in self._lost_bases.items():
            if self.time - lost_at > 90:
                continue
            if position.distance_to(Point2((x, y))) < 6:
                return True
        return False

    # ---- 侦察 -------------------------------------------------------------

    def _manage_scouting(self) -> None:
        """早期农民侦察 + 中后期用轨道扫描补情报。"""
        if not self._scout_sent and self.time >= 95 and self.workers.amount >= 15:
            worker = self.workers.closest_to(self.start_location)
            if worker is not None and worker.tag != self._scout_tag:
                target = self.enemy_start_locations[0]
                self.do(worker.move(target))
                self._scout_tag = worker.tag
                self._scout_sent = True
                logger.info("[DeepSeek] 派出农民侦察 %s", target)

        # 情报空白且经济允许时扫一次敌方分矿。
        if not self._scan_due():
            return
        for orbital in self.townhalls(UnitTypeId.ORBITALCOMMAND).ready:
            if orbital.energy < 50:
                continue
            scan_target = self._enemy_natural() or self.enemy_start_locations[0]
            self.do(orbital(AbilityId.SCANNERSWEEP_SCAN, scan_target))
            self._last_scan = self.time
            logger.info("[DeepSeek] 扫描敌方分矿 %s", scan_target)
            return

    def _scan_due(self) -> bool:
        """是否需要扫描补情报：5 分钟后、90 秒没扫、建筑记忆很少。"""
        return (
            self.time >= 300
            and self.time - self._last_scan >= 90
            and len(self._seen_enemy_structures) <= 3
            and self.workers.amount >= 35
        )

    def _enemy_natural(self) -> Point2 | None:
        enemy_start = self.enemy_start_locations[0]
        candidates = sorted(
            self.expansion_locations_list,
            key=lambda pos: pos.distance_to(enemy_start),
        )
        for pos in candidates:
            if pos.distance_to(enemy_start) > 5:
                return pos
        return None

    # ---- 防守 -------------------------------------------------------------

    async def _manage_defense(self) -> None:
        """统计威胁、拉农民、修地堡/防空，并把部队召回。"""
        if not self.townhalls:
            return
        enemy_near: list = []
        threat_supply = 0.0
        for unit in self.enemy_units:
            if not (unit.can_attack or unit.type_id in HIGH_VALUE_TARGETS):
                continue
            nearest_base = self.townhalls.closest_to(unit)
            if unit.distance_to(nearest_base) < 30:
                enemy_near.append(unit)
                threat_supply += self.calculate_supply_cost(unit.type_id)

        self._threat_supply = threat_supply
        self._threat_level = classify_threat(
            threat_supply, len(enemy_near), self._army_supply()
        )
        if self._threat_level > 0:
            self._defense_until = self.time + 8
            closest = min(enemy_near, key=lambda u: u.distance_to(self._home_point()))
            self._threat_position = closest.position
            if self.attack_state == "ATTACK":
                logger.warning("[DeepSeek] 家中遇袭，主力回防")
                self.attack_state = "GATHER"

        if self._threat_level == 0:
            return

        # 农民躲避：只在敌人贴脸时拉走，避免频繁打断采矿。
        if self.time - self._last_worker_flee > 1.0:
            self._last_worker_flee = self.time
            for worker in self.workers.filter(
                lambda w: any(w.distance_to(e) < 8 for e in enemy_near)
            ):
                self.do(worker.move(self._main_base().position))

        # 地堡：自然矿防守支点，敌方压制时最多一座。
        if (
            self.time >= 120
            and self._natural is not None
            and self._owns_position(self._natural)
            and not self.structures(UnitTypeId.BUNKER)
            and self.minerals >= 100
            and self.time - self._last_bunker_try > 5
        ):
            self._last_bunker_try = self.time
            await self.build(
                UnitTypeId.BUNKER,
                near=self._natural,
                max_distance=8,
                random_alternative=False,
            )
            return

        # 地堡装 4 个机枪兵。
        for bunker in self.structures(UnitTypeId.BUNKER).ready:
            if bunker.cargo_used >= 4:
                continue
            marines = self.units(UnitTypeId.MARINE)
            if marines:
                self.do(bunker(AbilityId.LOAD_BUNKER, marines.closest_to(bunker)))

        # 极端情况：兵力不足且敌人已进主矿，拉 10 个农民协防。
        if (
            self._threat_level == 2
            and self._army_supply() < 8
            and self._threat_position is not None
            and self._threat_position.distance_to(self._main_base()) < 15
        ):
            scvs = self.workers.sorted_by_distance_to(self._threat_position)[:10]
            for scv in scvs:
                self.do(scv.attack(self._threat_position))

    async def _manage_air_defense(self) -> None:
        """看到成规模空军后，在每个基地附近补一座导弹塔。"""
        if not self.structures(UnitTypeId.ENGINEERINGBAY).ready:
            return
        air_units = len([u for u in self.enemy_units if u.is_flying and u.can_attack])
        turrets = self.structures(UnitTypeId.MISSILETURRET)
        if not needs_turret(air_units, turrets.amount, len(self.townhalls)):
            return
        if self.minerals < 125 or self.already_pending(UnitTypeId.MISSILETURRET) > 0:
            return
        for base in self.townhalls.ready:
            if any(turret.distance_to(base) < 12 for turret in turrets):
                continue
            await self.build(
                UnitTypeId.MISSILETURRET,
                near=base.position,
                max_distance=10,
                random_alternative=False,
            )
            return

    # ---- 部队指挥 ---------------------------------------------------------

    def _army_units(self):
        return self.units(ARMY_TYPES)

    def _army_supply(self) -> float:
        return sum(self.calculate_supply_cost(u.type_id) for u in self._army_units())

    def _manage_army(self) -> None:
        army = self._army_units()
        medivacs = self.units(UnitTypeId.MEDIVAC)

        # 防守优先：把部队拉向威胁点。
        if self.time < self._defense_until:
            point = self._threat_position or self._home_point()
            self._defend(army, point)
            self._manage_medivacs(medivacs, point)
            self._manage_tanks(army, point, defending=True)
            return

        if self.attack_state == "RETREAT":
            self._retreat(army)
            self._manage_medivacs(medivacs, self._home_point())
            self._manage_tanks(army, self._home_point(), defending=False)
            if self._army_supply() >= 20:
                self.attack_state = "GATHER"
            return

        if self.attack_state == "GATHER":
            self._rally(army)
            self._manage_medivacs(medivacs, self._home_point())
            self._manage_tanks(army, self._home_point(), defending=True)
            if attack_ready(
                self._army_supply(),
                medivacs.amount,
                self.time,
                self._enemy_army_supply,
                self._threat_level,
            ):
                self.attack_state = "ATTACK"
                self._attack_started_at = self.time
                logger.info(
                    "[DeepSeek] 主动推进：兵力=%.1f 人口，运输机=%d",
                    self._army_supply(),
                    medivacs.amount,
                )
            return

        # ATTACK
        target = self._attack_target()
        center = army.center if army.amount else target
        enemies_near = self.enemy_units.filter(lambda u: u.distance_to(center) < 25)
        if should_retreat(
            self._army_supply(),
            army.amount,
            self._enemy_army_supply,
            enemies_near.amount,
            self.time - self._attack_started_at,
        ):
            self.attack_state = "RETREAT"
            logger.warning("[DeepSeek] 推进受阻，撤退重整")
            return
        self._push(army, target)
        self._prune_cleared_target(army, target)
        self._manage_medivacs(medivacs, center)
        self._manage_tanks(army, target, defending=False)

    def _rally(self, army) -> None:
        point = self._home_point()
        for unit in army.idle:
            if unit.distance_to(point) > 6:
                self.do(unit.move(point))

    def _defend(self, army, point: Point2) -> None:
        now = self.time
        if now - self._last_army_order < 0.5:
            return
        self._last_army_order = now
        enemies = self.enemy_units.filter(
            lambda u: u.distance_to(self._home_point()) < 40
        )
        target = None
        if enemies:
            target = min(enemies, key=lambda u: u.distance_to(point))
        for unit in army:
            if unit.is_flying:
                continue
            if target is not None:
                if unit.type_id == UnitTypeId.SIEGETANKSIEGED:
                    continue
                self.do(unit.attack(target))
            elif unit.distance_to(point) > 8:
                self.do(unit.move(point))

    def _retreat(self, army) -> None:
        point = self._home_point()
        if self.time - self._last_army_order < 0.5:
            return
        self._last_army_order = self.time
        for unit in army:
            if unit.distance_to(point) > 6:
                self.do(unit.move(point))

    def _push(self, army, target: Point2) -> None:
        if self.time - self._last_army_order < 0.5:
            return
        self._last_army_order = self.time
        center = army.center if army else target
        enemies = self.enemy_units.filter(lambda u: u.distance_to(center) < 16)
        if enemies:
            priority = enemies.filter(lambda u: u.type_id in HIGH_VALUE_TARGETS)
            focus = (
                priority.closest_to(center) if priority else enemies.closest_to(center)
            )
            for unit in army:
                if unit.type_id == UnitTypeId.SIEGETANKSIEGED:
                    # 架起的坦克交给自动攻击，不打断炮击。
                    continue
                self.do(unit.attack(focus))
            self._maybe_stim(army)
        else:
            for unit in army:
                if unit.type_id == UnitTypeId.SIEGETANKSIEGED:
                    continue
                self.do(unit.move(target))

    def _prune_cleared_target(self, army, target: Point2) -> None:
        """部队已站到目标点且视野内没有建筑时，移除失效的建筑记忆。

        只有部队贴近目标（因此有正常视野）才清理，避免凭记忆误删远端建筑。
        """
        if not army.amount or army.center.distance_to(target) > 8:
            return
        if self.enemy_structures.filter(lambda s: s.distance_to(target) < 6):
            return
        removed = False
        for tag, (_, position, seen_at) in list(self._seen_enemy_structures.items()):
            if position.distance_to(target) < 6 and self.time - seen_at > 20:
                del self._seen_enemy_structures[tag]
                removed = True
        if removed:
            logger.info("[DeepSeek] 目标点已清空，重新选择进攻目标")

    def _maybe_stim(self, army) -> None:
        """接敌时给血量健康的生化单位打兴奋剂。"""
        if UpgradeId.STIMPACK not in self.state.upgrades:
            return
        if self.time - self._last_stim < 1.0:
            return
        self._last_stim = self.time
        for unit in army:
            if unit.type_id not in {UnitTypeId.MARINE, UnitTypeId.MARAUDER}:
                continue
            if unit.health + unit.shield <= 45:
                continue
            if unit.has_buff(BuffId.STIMPACK):
                continue
            if unit.type_id == UnitTypeId.MARINE:
                self.do(unit(AbilityId.EFFECT_STIM_MARINE))
            else:
                self.do(unit(AbilityId.EFFECT_STIM_MARAUDER))

    def _manage_medivacs(self, medivacs, point: Point2) -> None:
        """运输机跟随部队，靠自身治疗维持生化部队续航。"""
        if not medivacs:
            return
        for medivac in medivacs:
            if medivac.distance_to(point) > 10:
                self.do(medivac.move(point))

    def _manage_tanks(self, army, point: Point2, defending: bool) -> None:
        """坦克阵地逻辑：防守/接敌架起，长距离移动时收起。"""
        sieged = self.units(UnitTypeId.SIEGETANKSIEGED)
        unsieged = self.units(UnitTypeId.SIEGETANK)
        enemy_near = self.enemy_units.filter(lambda u: u.distance_to(point) < 22)
        for tank in unsieged:
            if (defending or enemy_near) and tank.distance_to(point) < 18:
                self.do(tank(AbilityId.SIEGEMODE_SIEGEMODE))
        for tank in sieged:
            if not defending and not enemy_near and tank.distance_to(point) > 28:
                self.do(tank(AbilityId.UNSIEGE_UNSIEGE))

    def _attack_target(self) -> Point2:
        """优先打最近见过的敌方基地，其次任意建筑，最后打出生点。"""
        valid = [
            (unit_type, position, seen_at)
            for unit_type, position, seen_at in self._seen_enemy_structures.values()
            if self.time - seen_at < 240
        ]
        townhalls = [item for item in valid if item[0] in ENEMY_TOWNHALL_TYPES]
        if townhalls:
            return min(
                townhalls, key=lambda item: item[1].distance_to(self.start_location)
            )[1]
        if valid:
            return min(
                valid, key=lambda item: item[1].distance_to(self.start_location)
            )[1]
        if self.enemy_structures:
            return self.enemy_structures.closest_to(self.start_location).position
        return self.enemy_start_locations[0]

    # ---- 日志 -------------------------------------------------------------

    def _log_status(self) -> None:
        if self.time - self._last_log < 30:
            return
        self._last_log = self.time
        logger.info(
            "[DeepSeek] %s 农民=%d 基地=%d 人口=%.0f/%.0f 矿=%d 气=%d "
            "部队=%.1f 敌兵=%.1f 状态=%s",
            self.time_formatted,
            self.workers.amount,
            len(self.townhalls),
            self.supply_used,
            self.supply_cap,
            self.minerals,
            self.vespene,
            self._army_supply(),
            self._enemy_army_supply,
            self.attack_state,
        )


__all__ = ["CompetitiveBot"]
