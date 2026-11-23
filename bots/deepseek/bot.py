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
import math
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

# 生产建筑 -> (科技实验室, 反应堆)。附件挂在建筑侧面，必须确认位置可用再下单。
ADDON_TYPES: dict[UnitTypeId, tuple[UnitTypeId, UnitTypeId]] = {
    UnitTypeId.BARRACKS: (UnitTypeId.BARRACKSTECHLAB, UnitTypeId.BARRACKSREACTOR),
    UnitTypeId.FACTORY: (UnitTypeId.FACTORYTECHLAB, UnitTypeId.FACTORYREACTOR),
    UnitTypeId.STARPORT: (UnitTypeId.STARPORTTECHLAB, UnitTypeId.STARPORTREACTOR),
}

# 附件建造技能的内置兜底表；正常运行优先用 game_data 里的 creation_ability。
ADDON_CREATION_ABILITY: dict[UnitTypeId, AbilityId] = {
    UnitTypeId.BARRACKSTECHLAB: AbilityId.BUILD_TECHLAB_BARRACKS,
    UnitTypeId.BARRACKSREACTOR: AbilityId.BUILD_REACTOR_BARRACKS,
    UnitTypeId.FACTORYTECHLAB: AbilityId.BUILD_TECHLAB_FACTORY,
    UnitTypeId.FACTORYREACTOR: AbilityId.BUILD_REACTOR_FACTORY,
    UnitTypeId.STARPORTTECHLAB: AbilityId.BUILD_TECHLAB_STARPORT,
    UnitTypeId.STARPORTREACTOR: AbilityId.BUILD_REACTOR_STARPORT,
}


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


def threat_blocks_attack(
    threat_supply: float,
    enemy_units_near: int,
    own_army_supply: float,
) -> bool:
    """这次的家门口威胁是否足以拦下主力。

    以前只要 `threat_level > 0`（任何一个能攻击的敌人在基地 30 格内）就全场回防
    并续期 `_defense_until`，等价于：对手一个死神绕后，就能把主力永久钉在家里。
    三场录像都出现 8–10 分钟的击杀空窗（deepseek-vs-antigravity 4:56–15:21、
    vs-codex 4:00–7:25 与 16:41–20:42）；击杀空窗只能定位区间，但结合
    “任何一个单位都触发全场回防”的实现，这是最可能的解释。

    只有“能在正面吃我分矿的兵力”才算拦阻；零星骚扰交给地堡、农民躲避和
    家里的残余守军处理，主力继续按计划推进。
    """
    if threat_supply >= 8:
        return True
    # 数量多但单兵价值低（如小狗群）时，只有家里没兵才需要回防。
    return enemy_units_near >= 5 and own_army_supply < 15


def attack_ready(
    army_supply: float,
    medivacs: int,
    time: float,
    enemy_army_supply: float,
    threat_blocking: bool,
) -> bool:
    """是否已经具备主动推进的条件。

    * 家里正在被正面推进时不出门（零星骚扰不再拦路）；
    * 300 秒前不主动进攻（保留运营窗口）；
    * 至少 26 人口部队，对手明显虚弱时放宽到 20；
    * 540 秒前至少带 1 架医疗运输机，之后可以先行；
    * 敌方兵力显著高于我方时不送。
    """
    if threat_blocking or time < 300:
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


def staging_point(base: Point2, enemy_start: Point2, distance: float = 10.0) -> Point2:
    """主力集结/防守锚点：从基地朝对手方向前出 `distance` 格。

    旧版直接把 `_home_point()` 当集结点，而它是自然矿指挥中心的**本体**
    （5x5 不可通行建筑）。两场 Sol 6.2 录像里，主力在 GATHER 状态下被
    `_rally` 收拢到分矿指挥中心旁边，首战死亡位置集中在建筑与矿脉之间的
    1–2 格：8:16 局 6:31.2–6:34.2 有 14 个陆战队员死在 (44,26)/(44,27)
    （分矿指挥中心 (42,24) 的边角），8:40 局 6:42.0–6:42.7 有 4 个死在
    同一格 (22,122)。架起坦克的溅射与生化集火正好覆盖这团兵：两局首战
    合计 42 个作战单位只换到 5 个陆战队员。

    把锚点挪到基地与对手之间、离基地 `distance` 格的开阔处，部队才有
    展开空间；距离在基地到对手一半以内，不会把主力送去对手家门口。
    """
    dx = enemy_start.x - base.x
    dy = enemy_start.y - base.y
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return Point2((base.x, base.y))
    step = min(max(0.0, distance), length * 0.5)
    return Point2((base.x + dx / length * step, base.y + dy / length * step))


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


def order_key(ability, target, queue: bool = False) -> tuple:
    """把一条指令压缩成可比较的键，用于本帧/短时间内的重复指令去重。"""
    ability_value = getattr(ability, "value", ability)
    if isinstance(target, Point2):
        target_key = (round(target.x, 1), round(target.y, 1))
    elif target is None:
        target_key = None
    else:
        target_key = getattr(target, "tag", target)
    return (ability_value, target_key, queue)


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
        self._last_structure_build: float = -999.0
        self._last_refinery_build: float = -999.0
        self._last_addon_attempt: float = -999.0
        self._last_open_build_try: float = -999.0
        self._last_supply_build: float = -999.0
        self._last_turret_build: float = -999.0
        # 附件位被占的建筑在冷却期内不再下单，避免每帧重复提交放置请求。
        self._addon_blocked_until: dict[int, float] = {}
        # 已下单的附件：building tag -> (附件类型, 下单时间)，用于避免重复挂同种附件。
        self._addon_orders: dict[int, tuple[UnitTypeId, float]] = {}
        # 短时间内的指令去重表：unit tag -> (指令键, 时间)。
        self._order_memo: dict[int, tuple[tuple, float]] = {}
        # 指令计数，用于发现“每帧刷指令”这类会拖垮客户端的行为。
        self._commands_total: int = 0
        self._commands_this_step: int = 0
        self._commands_peak: int = 0
        self._duplicates_skipped: int = 0
        self._steps_seen: int = 0
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
        self._threat_units: int = 0
        self._threat_blocking: bool = False
        self._last_harass_log: float = -999.0
        self._threat_position: Point2 | None = None
        self._economy_emergency_until: float = 0.0
        # 开局建造序列的完成标记（单兵营双开）。
        self._opening_done: bool = False
        self._open_depot1: bool = False
        self._open_rax: bool = False
        self._open_gas: bool = False
        self._open_depot2: bool = False
        self._open_cc: bool = False
        # 开局每一步的下单时刻：用来发现“下了单却没建成”的静默失败。
        self._open_depot1_at: float = -999.0
        self._open_rax_at: float = -999.0
        self._open_gas_at: float = -999.0
        # 上一次放置疑似落在不可达/被占位置时，改用随机候选点重试。
        self._open_alt_placement: bool = False
        # 推进统计：首次推进时间与累计处于进攻态的时间，作为下一场的量化指标。
        self._first_attack_at: float | None = None
        self._attack_seconds: float = 0.0
        self._retreat_at: float = -999.0
        self._prev_step_time: float = 0.0

    async def on_start(self) -> None:
        # 不覆盖对局脚本设置的推进步长：同一局两个客户端要用同一节奏推帧。
        # 旧版这里写死 game_step=2，而 burnysc2 的 Client 默认是 4，结果本机被推成
        # 对手两倍的帧数（诊断日志里同一局 1183 步 vs 592 步），请求量翻倍。
        step = getattr(self.client, "game_step", 4)
        if step != 4:
            logger.warning("[DeepSeek] 推进步长=%s（默认为 4），按对局脚本执行", step)
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
        self._commands_this_step = 0
        self._steps_seen += 1
        if not self.townhalls:
            # 基地全失：已无法运营，交由对局脚本判定胜负。
            return

        self._track_base_losses()
        self._update_intel()
        self._manage_orbital()
        await self._manage_economy()
        opening = await self._manage_opening()
        # 二人口完成后即便还在等 400 矿开分矿，也要允许补人口。
        if not opening or self._open_depot2:
            await self._manage_supply()
        await self._manage_addons()
        if not opening:
            await self._manage_structures()
        self._manage_tech()
        if not opening:
            await self._manage_expansion()
        self._manage_army_production()
        self._manage_scouting()
        await self._manage_defense()
        await self._manage_air_defense()
        self._manage_army()
        self._log_status()

    def train(self, unit_type: UnitTypeId, amount: int = 1) -> bool:
        """统计出兵指令，让日志里的“指令/帧”反映真实提交量。"""
        result = super().train(unit_type, amount)
        if result:
            issued = max(1, int(amount))
            self._commands_total += issued
            self._commands_this_step += issued
            self._commands_peak = max(self._commands_peak, self._commands_this_step)
        return result

    # ---- 指令下发 ---------------------------------------------------------

    def _issue(self, unit, command, min_interval: float = 0.6) -> bool:
        """下发一条指令，并抑制同一单位在短时间内的重复指令。

        每帧对同一单位重复提交同一条指令（尤其是建筑/附件放置）会让客户端的
        指令队列持续堆积，是本次排查的重点怀疑对象。这里统一做去重与计数，
        战术判断仍然由各 manager 决定。
        """
        if command is None or isinstance(command, bool):
            return False
        ability = getattr(command, "ability", None)
        key = order_key(
            ability, getattr(command, "target", None), getattr(command, "queue", False)
        )
        tag = getattr(unit, "tag", None)
        if tag is not None:
            previous = self._order_memo.get(tag)
            if (
                previous is not None
                and previous[0] == key
                and self.time - previous[1] < min_interval
            ):
                self._duplicates_skipped += 1
                return False
            if len(self._order_memo) > 512:
                self._order_memo.clear()
        if not self.do(command):
            return False
        if tag is not None:
            self._order_memo[tag] = (key, self.time)
        self._commands_this_step += 1
        self._commands_total += 1
        self._commands_peak = max(self._commands_peak, self._commands_this_step)
        return True

    # ---- 开局建造序列 -----------------------------------------------------

    def _verify_opening_builds(self) -> None:
        """校验开局每一步是否"真的建起来"，而不是只看下单成功。

        证据（deepseek-vs-codex 复盘资源样本）：1:23–3:45 人口上限一直是 31
        = 指挥中心 15 + 两座补给站 8+8，没有兵营的 15；同期矿从 1140 堆到 1695、
        兵力始终为 0，而 minerals_lost 一直为 0（说明不是被拆掉，是压根没建成）。
        按开局代码顺序（兵营 → 气矿 → 二补给 → 分矿），气矿与二补给都已完成，
        说明兵营这一步的标记早已置位。也就是说：`build()` 返回建筑只代表指令被
        接受，SCV 若被击杀或落点走不到，建筑会永远停在 0% 或根本不出现，而标记
        已经置位，整个开局静默瘫痪。这里用“是否存在 / 是否有施工进度”复核，
        超时就重置该步骤并换用随机候选位置重试。
        """
        if self._open_rax and self._open_rax_at > 0:
            elapsed = self.time - self._open_rax_at
            rax = self.structures(UnitTypeId.BARRACKS)
            if rax.amount == 0 and elapsed > 15:
                logger.warning(
                    "[DeepSeek] 兵营下单 %.0f 秒后仍不存在，换位置重新下单", elapsed
                )
                self._open_rax = False
                self._open_alt_placement = True
            elif rax.amount > 0 and rax.ready.amount == 0:
                progress = max(building.build_progress for building in rax)
                if progress <= 0.01 and elapsed > 25:
                    logger.warning(
                        "[DeepSeek] 兵营 %.0f 秒无施工进度（SCV 可能走不到），换位置重建",
                        elapsed,
                    )
                    self._open_rax = False
                    self._open_alt_placement = True

        if (
            self._open_gas
            and self._open_gas_at > 0
            and self.structures(UnitTypeId.REFINERY).amount == 0
            and self.time - self._open_gas_at > 20
        ):
            logger.warning("[DeepSeek] 气矿下单后未开工，重新尝试")
            self._open_gas = False

    async def _manage_opening(self) -> bool:
        """单兵营双开：14 人口 -> 兵营 -> 气矿 -> 二人口 -> 自然分矿。

        返回 True 表示开局序列仍在进行，此时暂缓通用补给/建筑/扩张逻辑，
        避免早期重复拍建筑或抢走分矿的 400 矿。
        """
        if self._opening_done:
            return False
        # 放置失败时每秒最多重试一次，避免每帧发起一次放置查询。
        if self.time - self._last_open_build_try < 1.0:
            return True

        self._verify_opening_builds()
        anchor = self._main_base().position
        # 开局补给站被拆会导致人口卡死，发现后重新走一次补给站步骤。
        if (
            self._open_depot1
            and not self.structures(UnitTypeId.SUPPLYDEPOT)
            and self.supply_left <= 4
        ):
            self._open_depot1 = False

        # 1) 14 人口补给站。
        if not self._open_depot1:
            if self.supply_used < 13 or self.minerals < 100:
                return True
            self._last_open_build_try = self.time
            self._open_depot1_at = self.time
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
            self._last_open_build_try = self.time
            self._open_rax_at = self.time
            if await self._build_with_addon_space(
                UnitTypeId.BARRACKS,
                anchor,
                max_distance=14 if self._open_alt_placement else 10,
                random_alternative=self._open_alt_placement,
            ):
                self._open_rax = True
                if self._open_alt_placement:
                    logger.info("[DeepSeek] 兵营改用随机候选位置重建")
                    self._open_alt_placement = False
            return True

        # 3) 气矿，为科技实验室与兴奋剂做准备。
        if not self._open_gas:
            if self.minerals >= 75:
                geyser = self._available_geyser()
                if geyser is not None:
                    self._last_open_build_try = self.time
                    self._open_gas_at = self.time
                if geyser is not None and await self.build(
                    UnitTypeId.REFINERY, near=geyser
                ):
                    self._open_gas = True
            return True

        # 4) 20 人口第二座补给站，保证分矿前不断人口。
        if not self._open_depot2:
            if self.supply_used < 20 and self.time < 180:
                return True
            self._last_open_build_try = self.time
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
            elif self.time >= 300:
                # 长期攒不到 400 矿（通常是被压制），先放行工厂/星港，分矿交给通用逻辑。
                self._open_cc = True
                logger.warning("[DeepSeek] 分矿迟迟无法开工，先恢复科技生产")
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

    def _staging_point(self) -> Point2:
        """主力等待/防守的落点：基地朝对手方向前出，不落在基地建筑格上。"""
        return staging_point(
            self._home_point(), self.enemy_start_locations[0], distance=10.0
        )

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
            and refineries < desired_refineries
            and self.minerals >= 100
            and self._can_start_building(75)
            and self.time - self._last_refinery_build > 6
        ):
            geyser = self._available_geyser()
            if geyser is not None:
                await self.build(UnitTypeId.REFINERY, near=geyser)
                self._last_refinery_build = self.time

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
                self._issue(worker, worker.gather(field), min_interval=2.0)

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
            self._issue(worker, worker.gather(field), min_interval=2.0)

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
            self._issue(best_worker, best_worker.gather(refinery), min_interval=2.0)
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
        # 产能越宽，人口越快被吃满：按“每座可出兵建筑预留 2 人口”提前补站。
        # 两场 Sol 录像在 6:25–6:26 都卡在 68–69/69 人口（快局还压着 1,140 矿），
        # 首战打起来时连增援都排不进去；对面同期上限是 78。
        production = (
            self.structures(UnitTypeId.BARRACKS).ready.amount
            + self.structures(UnitTypeId.FACTORY).ready.amount
            + self.structures(UnitTypeId.STARPORT).ready.amount
        )
        reserved_supply = min(12.0, 2.0 * production)
        # 存款多说明经济跑在产能前面，允许多排一座，避免下一次人口墙。
        max_pending = 3 if self.minerals >= 300 else 2
        if (
            supply_depot_needed(
                self.supply_used,
                self.supply_cap,
                pending,
                reserved_supply=reserved_supply,
                margin=4,
            )
            and pending < max_pending
            and self.minerals >= 100
            and self.time - self._last_supply_build > 1.0
        ):
            self._last_supply_build = self.time
            built = await self.build(
                UnitTypeId.SUPPLYDEPOT,
                near=self._main_base().position,
                max_distance=12,
                random_alternative=False,
            )
            if built is None:
                logger.info("[DeepSeek] 补给站放置失败，稍后重试")

    # ---- 指挥中心 / 矿骡 / 扫描 ------------------------------------------

    def _manage_orbital(self) -> None:
        # 主基地尽早升级轨道指挥中心，获得矿骡与扫描。
        for cc in self.townhalls(UnitTypeId.COMMANDCENTER).ready:
            if self.time < 240 or self.minerals < 150:
                continue
            if self._issue(cc, cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND), 2.0):
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
            self._issue(
                orbital,
                orbital(AbilityId.CALLDOWNMULE_CALLDOWNMULE, field),
                min_interval=3.0,
            )
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

    def _usable_structures(self, unit_type: UnitTypeId) -> int:
        """只统计“已完工或真的在施工”的建筑。

        进度停在 0 的建筑（SCV 被击杀、落点走不到）不算数，否则它会永久占住
        配额，让 Bot 以为兵营/工厂已经建好，从此不再补建。
        """
        count = 0
        for structure in self.structures(unit_type):
            if structure.is_ready or structure.build_progress > 0.01:
                count += 1
        return count

    async def _build_with_addon_space(
        self,
        unit_type: UnitTypeId,
        near: Point2,
        max_distance: int = 14,
        random_alternative: bool = False,
    ) -> bool:
        """优先在“附件位也空着”的落点造生产建筑。

        gen7 两场里第一座兵营都落在 (28,156)，附件位 (30.5,155.5) 正好在主基地
        (32,156) 的 5x5 占地里；第二座 (30,160) 的附件位压在补给站 (33,161) 上。
        结果科技实验室只能挂到 4:52–5:04 才完工的第三座兵营上。

        这里用 burnysc2 自带的 `find_placement(..., addon_place=True)`（它按
        `position.offset((2.5, -0.5))` 过滤附件位）先找带附件空位的位置，再在附近
        下单；找不到就退回原来的普通落点，不改变原有行为。
        """
        finder = getattr(self, "find_placement", None)
        spot = None
        if finder is not None:
            try:
                spot = await finder(
                    unit_type,
                    near=near,
                    max_distance=max_distance,
                    addon_place=True,
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                spot = None
        if spot is not None:
            built = await self.build(
                unit_type,
                near=spot,
                max_distance=4,
                random_alternative=random_alternative,
                placement_step=1,
            )
            if built:
                return True
        return await self.build(
            unit_type,
            near=near,
            max_distance=max_distance,
            random_alternative=random_alternative,
        )

    async def _manage_structures(self) -> None:
        """按时间目标补生产/科技建筑；资源紧张或挨打时降低优先级。"""
        if not self.townhalls:
            return
        if self._economy_emergency_until > self.time:
            return
        # 家里被正面推进时不抽农民出去盖楼；但一座能出兵的兵营都没有时必须补，
        # 否则越挨打越没兵，会直接锁死在中后期被打崩的循环里。
        if self._threat_blocking and self.structures(UnitTypeId.BARRACKS).ready.amount:
            return
        # 新建筑之间留 5 秒间隔，避免一次拉走太多农民。
        if self.time - self._last_structure_build < 5:
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
            # structures() 已包含在建建筑，不能再叠加 already_pending，否则会少建；
            # 但 0% 进度、长期不动的建筑要当作不存在，避免永久占配额。
            have = self._usable_structures(unit_type)
            if have >= target:
                continue
            if self.minerals < 200 and unit_type != UnitTypeId.BARRACKS:
                continue
            if not self.can_afford(unit_type):
                continue
            anchor = self._main_base().position
            if unit_type in ADDON_TYPES:
                # 兵营/工厂/星港要留附件位；否则科技实验室与反应堆永远挂不上。
                built = await self._build_with_addon_space(
                    unit_type, anchor, max_distance=14
                )
            else:
                built = await self.build(
                    unit_type,
                    near=anchor,
                    max_distance=14,
                    random_alternative=False,
                )
            self._last_structure_build = self.time
            if built is None:
                logger.info("[DeepSeek] %s 放置失败，附近没有空位", unit_type.name)
            return

    def _creation_ability(self, unit_type: UnitTypeId) -> AbilityId | None:
        """取建筑的真实建造技能：优先 game_data，取不到时退回内置表。"""
        try:
            creation = self.game_data.units[unit_type.value].creation_ability
        except (AttributeError, KeyError, IndexError):
            creation = None
        if creation is not None:
            return creation.id
        return ADDON_CREATION_ABILITY.get(unit_type)

    async def _try_addon(self, building, addon: UnitTypeId) -> bool:
        """尝试给一栋生产建筑挂附件；返回 True 表示本帧已处理过附件。"""
        if building.add_on_tag != 0:
            return False
        # 生产中的建筑也要能排队挂附件：gen7 的版本要求“空闲或队列 ≤1”，
        # 而 `train(MARINE, 2)` 让兵营队列常年是 2 —— 结果两场 4–5 座兵营
        # 全程 0 个反应堆（对手各 4 个），产能只有一半，掠夺者/兴奋剂也起不来。
        # 客户端若拒绝就由 `_issue` 失败退避 10 秒，不会退化成每帧提交。
        if self._addon_blocked_until.get(building.tag, 0) > self.time:
            return False
        ordered = self._addon_orders.get(building.tag)
        if ordered is not None and self.time - ordered[1] < 30:
            # 这栋建筑已经下过附件单，别在附件没出现前重复提交。
            return False
        if not self.can_afford(addon):
            return False
        ability = self._creation_ability(addon)
        if ability is None:
            return False
        # 附件只能贴在建筑侧面：位置被占时客户端会把放置请求挂住，
        # 必须先确认那一格空着（2x2，用补给站探位即可）。
        self._last_addon_attempt = self.time
        slot = building.position.offset((2.5, -0.5))
        if not await self.can_place_single(UnitTypeId.SUPPLYDEPOT, slot):
            self._addon_blocked_until[building.tag] = self.time + 10
            logger.info(
                "[DeepSeek] 附件位被占，暂缓挂载 %s tag=%s 位置=%s",
                addon.name,
                building.tag,
                slot,
            )
            return True
        command = building.build(addon)
        if isinstance(command, bool) or command is None:
            return False
        if not self._issue(building, command, min_interval=3.0):
            # 客户端拒绝（或刚发过同一条）时退避 10 秒，别回到“每帧重试”的老路。
            self._addon_blocked_until[building.tag] = self.time + 10
            return False
        self._addon_orders[building.tag] = (addon, self.time)
        logger.info(
            "[DeepSeek] 挂载附件 %s -> %s tag=%s 技能=%s",
            addon.name,
            building.type_id.name,
            building.tag,
            ability.name,
        )
        return True

    def _addon_planned(self, addon: UnitTypeId) -> bool:
        """该种附件是否已在建造或刚下过单（30 秒内生效）。"""
        for tag, (planned, ordered_at) in list(self._addon_orders.items()):
            if self.time - ordered_at > 30:
                del self._addon_orders[tag]
                continue
            if planned == addon:
                return True
        return False

    async def _manage_addons(self) -> None:
        """兵营/工厂/星港挂附件：科技实验室出掠夺者/坦克，反应堆爆生化。"""
        if not self.townhalls:
            return
        # 每 1 秒最多处理一次附件，避免每帧重复提交放置指令。
        if self.time - self._last_addon_attempt < 1.0:
            return

        # 工厂与星港：工厂科技实验室（坦克），星港反应堆（双倍医疗运输机）。
        for factory in sorted(
            self.structures(UnitTypeId.FACTORY).ready, key=lambda f: f.tag
        ):
            if await self._try_addon(factory, UnitTypeId.FACTORYTECHLAB):
                return

        for starport in sorted(
            self.structures(UnitTypeId.STARPORT).ready, key=lambda s: s.tag
        ):
            if await self._try_addon(starport, UnitTypeId.STARPORTREACTOR):
                return

        # 兵营：至少保留一个科技实验室（兴奋剂/掠夺者），其余反应堆。
        techlab, reactor = ADDON_TYPES[UnitTypeId.BARRACKS]
        bare = [
            rax
            for rax in sorted(
                self.structures(UnitTypeId.BARRACKS).ready, key=lambda b: b.tag
            )
            if rax.add_on_tag == 0
        ]
        if not bare:
            return
        # 科技实验室在建/已下单时也算已安排，避免给多个兵营都挂科技实验室。
        if (
            not self.techlab_tags
            and not self.structures(UnitTypeId.BARRACKSTECHLAB)
            and not self._addon_planned(UnitTypeId.BARRACKSTECHLAB)
        ):
            for rax in bare:
                if await self._try_addon(rax, techlab):
                    return
            return
        for rax in bare:
            if await self._try_addon(rax, reactor):
                return

    # ---- 科技 -------------------------------------------------------------

    def _manage_tech(self) -> None:
        """按优先级研究：兴奋剂 > 盾牌 > +1 攻 > +1 防 > +2 ..."""
        if not self.townhalls:
            return
        # 分矿未开工前先攒 400 矿，科技晚一点不影响运营节奏。
        if not self._open_cc and self.minerals < 500:
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
        # 分矿还没开工时，优先攒 400 矿，不提前把资源变成兵。
        if not self._threat_blocking and not self._open_cc:
            return
        # 还缺基地且钱不够时先攒钱；威胁当前时允许花钱防守。
        if (
            not self._threat_blocking
            and self._expansion_wanted()
            and self.minerals < 450
        ):
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
        # 但要有限度：只有“附件确实已经下单/在建”时才暂停产能。
        # gen7 对 Sol 与 Mizuki 两场里，首座兵营 51.7/50.2 秒就绪，可它的附件位
        # 被主基地占住，第一座能挂附件的兵营 4:52–5:04 才完工；旧门闸把兵营训练
        # 锁死 200+ 秒，同期存款 1005 矿/404 气（对 Sol）、675 矿/587 气（对 Mizuki），
        # 军队价值一直是 0。附件没能挂上时，出兵不能跟着一起停。
        techlab_in_progress = self.structures(
            UnitTypeId.BARRACKSTECHLAB
        ).amount > 0 or self._addon_planned(UnitTypeId.BARRACKSTECHLAB)
        if (
            not self.techlab_tags
            and self.time < 420
            and any(b.add_on_tag == 0 for b in barracks)
            and self.minerals >= 50
            and techlab_in_progress
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
        if self._threat_blocking:
            return

        desired = desired_base_count(
            self.time,
            self.workers.amount,
            self._army_supply(),
            len(self.townhalls),
            2 if self._threat_blocking else 0,
        )
        # townhalls 已包含在建的指挥中心。
        if len(self.townhalls) >= desired:
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
                self._issue(worker, worker.move(target), 5.0)
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
            self._issue(orbital, orbital(AbilityId.SCANNERSWEEP_SCAN, scan_target), 5.0)
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
        self._threat_units = len(enemy_near)
        self._threat_level = classify_threat(
            threat_supply, len(enemy_near), self._army_supply()
        )
        # 只有“打得动我分矿”的兵力才拦下主力；零星骚扰不拦。
        self._threat_blocking = self._threat_level > 0 and threat_blocks_attack(
            threat_supply, len(enemy_near), self._army_supply()
        )
        if self._threat_level > 0:
            closest = min(enemy_near, key=lambda u: u.distance_to(self._home_point()))
            if self._threat_blocking:
                self._defense_until = self.time + 8
                self._threat_position = closest.position
                if self.attack_state == "ATTACK":
                    logger.warning(
                        "[DeepSeek] 家门口被正面推进（敌 %.0f 人口/%d 单位），主力回防",
                        threat_supply,
                        len(enemy_near),
                    )
                    self.attack_state = "GATHER"
            elif self.time - self._last_harass_log > 10:
                # 骚扰级威胁：主力不回家，只记录，便于复盘“为什么没回防”。
                self._last_harass_log = self.time
                logger.info(
                    "[DeepSeek] 骚扰未拦阻推进：敌 %.0f 人口/%d 单位 位置=%s",
                    threat_supply,
                    len(enemy_near),
                    closest.position,
                )

        if self._threat_level == 0:
            return

        # 农民躲避：只在敌人贴脸时拉走，避免频繁打断采矿。
        if self.time - self._last_worker_flee > 1.0:
            self._last_worker_flee = self.time
            for worker in self.workers.filter(
                lambda w: any(w.distance_to(e) < 8 for e in enemy_near)
            ):
                self._issue(worker, worker.move(self._main_base().position), 2.0)

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
                self._issue(
                    bunker,
                    bunker(AbilityId.LOAD_BUNKER, marines.closest_to(bunker)),
                    1.0,
                )

        # 极端情况：兵力不足且敌人已进主矿，拉 10 个农民协防。
        if (
            self._threat_level == 2
            and self._army_supply() < 8
            and self._threat_position is not None
            and self._threat_position.distance_to(self._main_base()) < 15
        ):
            scvs = self.workers.sorted_by_distance_to(self._threat_position)[:10]
            for scv in scvs:
                self._issue(scv, scv.attack(self._threat_position), 2.0)

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
        if self.time - self._last_turret_build < 2.0:
            return
        for base in self.townhalls.ready:
            if any(turret.distance_to(base) < 12 for turret in turrets):
                continue
            self._last_turret_build = self.time
            await self.build(
                UnitTypeId.MISSILETURRET,
                near=base.position,
                max_distance=10,
                random_alternative=False,
            )
            return

    # ---- 部队指挥 ---------------------------------------------------------

    def _expansion_wanted(self) -> bool:
        """按当前时间/农民/兵力，是否还缺基地。"""
        if not self._expansions:
            return False
        desired = desired_base_count(
            self.time,
            self.workers.amount,
            self._army_supply(),
            len(self.townhalls),
            2 if self._threat_blocking else 0,
        )
        return len(self.townhalls) < desired

    def _army_units(self):
        return self.units(ARMY_TYPES)

    def _army_supply(self) -> float:
        return sum(self.calculate_supply_cost(u.type_id) for u in self._army_units())

    def _manage_army(self) -> None:
        army = self._army_units()
        medivacs = self.units(UnitTypeId.MEDIVAC)

        # 统计处于进攻态的时间，作为下一场复测的量化指标（推进占比）。
        step_dt = max(0.0, self.time - self._prev_step_time)
        self._prev_step_time = self.time
        if self.attack_state == "ATTACK":
            self._attack_seconds += step_dt

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
            staging = self._staging_point()
            self._manage_medivacs(medivacs, staging)
            self._manage_tanks(army, staging, defending=True)
            # 撤退后给 20 秒重整时间，避免在 ATTACK / RETREAT 之间来回横跳。
            if self.time - self._retreat_at >= 20 and attack_ready(
                self._army_supply(),
                medivacs.amount,
                self.time,
                self._enemy_army_supply,
                self._threat_blocking,
            ):
                self.attack_state = "ATTACK"
                self._attack_started_at = self.time
                if self._first_attack_at is None:
                    self._first_attack_at = self.time
                logger.info(
                    "[DeepSeek] 主动推进：兵力=%.1f 人口，运输机=%d，家门口威胁=%.0f",
                    self._army_supply(),
                    medivacs.amount,
                    self._threat_supply,
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
            self._retreat_at = self.time
            logger.warning(
                "[DeepSeek] 推进受阻，撤退重整：兵力=%.1f 敌兵=%.1f 敌近处=%d",
                self._army_supply(),
                self._enemy_army_supply,
                enemies_near.amount,
            )
            return
        self._push(army, target)
        self._prune_cleared_target(army, target)
        self._manage_medivacs(medivacs, center)
        self._manage_tanks(army, target, defending=False)

    def _rally(self, army) -> None:
        point = self._staging_point()
        for unit in army.idle:
            if unit.distance_to(point) > 6:
                self._issue(unit, unit.move(point), 1.0)

    def _defend(self, army, point: Point2) -> None:
        now = self.time
        if now - self._last_army_order < 0.5:
            return
        self._last_army_order = now
        # 生化打不到空中：把目标限定在地面，避免整队对着运输机/维京下攻击指令后
        # 原地不动。威胁点优先集火架起坦克等高价值目标——8:16 局首战 14 个阵亡
        # 陆战队员里 4 个是被架起坦克打掉的，其余是生化集火。
        enemies = self.enemy_units.filter(
            lambda u: u.distance_to(self._home_point()) < 40 and not u.is_flying
        )
        target = None
        if enemies:
            priority = enemies.filter(lambda u: u.type_id in HIGH_VALUE_TARGETS)
            candidates = priority if priority else enemies
            target = min(candidates, key=lambda u: u.distance_to(point))
        for unit in army:
            if unit.is_flying:
                continue
            if target is not None:
                if unit.type_id == UnitTypeId.SIEGETANKSIEGED:
                    continue
                self._issue(unit, unit.attack(target), 1.0)
            elif unit.distance_to(point) > 8:
                self._issue(unit, unit.move(point), 1.0)
        # 只在真正接敌时打兴奋剂，避免隔着半个屏幕先扣血。
        if target is not None and army and target.distance_to(army.center) < 16:
            self._maybe_stim(army)

    def _retreat(self, army) -> None:
        point = self._home_point()
        if self.time - self._last_army_order < 0.5:
            return
        self._last_army_order = self.time
        for unit in army:
            if unit.distance_to(point) > 6:
                self._issue(unit, unit.move(point), 1.0)

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
                self._issue(unit, unit.attack(focus), 1.0)
            self._maybe_stim(army)
        else:
            for unit in army:
                if unit.type_id == UnitTypeId.SIEGETANKSIEGED:
                    continue
                self._issue(unit, unit.move(target), 1.0)

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
                self._issue(unit, unit(AbilityId.EFFECT_STIM_MARINE), 5.0)
            else:
                self._issue(unit, unit(AbilityId.EFFECT_STIM_MARAUDER), 5.0)

    def _manage_medivacs(self, medivacs, point: Point2) -> None:
        """运输机跟随部队，靠自身治疗维持生化部队续航。"""
        if not medivacs:
            return
        for medivac in medivacs:
            if medivac.distance_to(point) > 10:
                self._issue(medivac, medivac.move(point), 1.5)

    def _manage_tanks(self, army, point: Point2, defending: bool) -> None:
        """坦克阵地逻辑：防守/接敌架起，长距离移动时收起。"""
        sieged = self.units(UnitTypeId.SIEGETANKSIEGED)
        unsieged = self.units(UnitTypeId.SIEGETANK)
        enemy_near = self.enemy_units.filter(lambda u: u.distance_to(point) < 22)
        for tank in unsieged:
            if (defending or enemy_near) and tank.distance_to(point) < 18:
                self._issue(tank, tank(AbilityId.SIEGEMODE_SIEGEMODE), 3.0)
        for tank in sieged:
            if not defending and not enemy_near and tank.distance_to(point) > 28:
                self._issue(tank, tank(AbilityId.UNSIEGE_UNSIEGE), 3.0)

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
        self._order_memo.clear()
        steps = max(1, self._steps_seen)
        if self._first_attack_at is None:
            first_attack = "无"
        else:
            seconds = int(self._first_attack_at)
            first_attack = f"{seconds // 60}:{seconds % 60:02d}"
        attack_ratio = 100.0 * self._attack_seconds / max(1.0, self.time)
        logger.info(
            "[DeepSeek] %s 农民=%d 基地=%d 人口=%.0f/%.0f 矿=%d 气=%d "
            "部队=%.1f 敌兵=%.1f 状态=%s 兵营=%d 威胁=%.0f/%d 拦阻=%s "
            "首推=%s 进攻占比=%.0f%% 指令=%.2f/帧(峰值%d, 去重%d) 步长=%s",
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
            self.structures(UnitTypeId.BARRACKS).ready.amount,
            self._threat_supply,
            self._threat_units,
            "是" if self._threat_blocking else "否",
            first_attack,
            attack_ratio,
            self._commands_total / steps,
            self._commands_peak,
            self._duplicates_skipped,
            getattr(self.client, "game_step", "?"),
        )


__all__ = ["CompetitiveBot"]
