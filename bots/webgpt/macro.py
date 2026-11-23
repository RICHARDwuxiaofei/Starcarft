"""人族运营：补给、扩张、科技、生产、升级与轨道支援。

这是一套按观察状态推进的规则，而非依靠精确时间点的一次性建造表。
建筑被摧毁、放置失败、工人阵亡后，下个周期仍会重新评估需求。
"""

from __future__ import annotations

from math import ceil

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as R
from sc2.position import Point2

from .model import Stance

PRODUCTION = {U.BARRACKS, U.FACTORY, U.STARPORT}
FLYING_PRODUCTION = {U.BARRACKSFLYING, U.FACTORYFLYING, U.STARPORTFLYING}
ADDONS = {
    U.BARRACKS: (U.BARRACKSTECHLAB, U.BARRACKSREACTOR),
    U.FACTORY: (U.FACTORYTECHLAB, U.FACTORYREACTOR),
    U.STARPORT: (U.STARPORTTECHLAB, U.STARPORTREACTOR),
}
LIFT = {U.BARRACKS: A.LIFT_BARRACKS, U.FACTORY: A.LIFT_FACTORY, U.STARPORT: A.LIFT_STARPORT}
LAND = {
    U.BARRACKSFLYING: (U.BARRACKS, A.LAND_BARRACKS),
    U.FACTORYFLYING: (U.FACTORY, A.LAND_FACTORY),
    U.STARPORTFLYING: (U.STARPORT, A.LAND_STARPORT),
}
TYPE_FAMILY = {
    U.SUPPLYDEPOT: {U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED},
    U.BARRACKS: {U.BARRACKS, U.BARRACKSFLYING},
    U.FACTORY: {U.FACTORY, U.FACTORYFLYING},
    U.STARPORT: {U.STARPORT, U.STARPORTFLYING},
    U.SIEGETANK: {U.SIEGETANK, U.SIEGETANKSIEGED},
    U.VIKINGFIGHTER: {U.VIKINGFIGHTER, U.VIKINGASSAULT},
}


def supply_buffer(production_slots: int, supply_used: float) -> int:
    """后期要覆盖多建筑同时出兵；不等卡人口才开工。"""
    return min(24, max(3, ceil(production_slots * 1.6), int(supply_used / 14)))


class MacroManager:
    def __init__(self, config, workers):
        self.config = config
        self.workers = workers
        self.placements: list[tuple[Point2, float]] = []
        self.placement_queries = 0
        self.blocked_addons: dict[int, float] = {}
        self.last_scan = -100.0
        self.last_threat_at = -100.0
        self.bad_expansions: dict[Point2, float] = {}
        self.expansion_cache: Point2 | None = None
        self.expansion_cache_at = -100.0
        self.wall_depots: list[Point2] = []

    def initialize(self, bot):
        # 坡口并非所有地图都能自动解析；无法使用时走普通建筑布局。
        ramps = [r for r in bot.game_info.map_ramps if len(r.upper) in {2, 5}]
        if ramps:
            ramp = min(ramps, key=lambda r: r.top_center.distance_to(bot.start_location))
            self.wall_depots = sorted(ramp.corner_depots, key=lambda p: (p.x, p.y))

    def count(self, bot, bus, kind):
        family = TYPE_FAMILY.get(kind, {kind})
        existing = bot.all_own_units.of_type(family)
        # already_pending 包含已开始的施工，所以只把尚未变成实体的订单加回来。
        unfinished = existing.not_ready.amount
        return (
            existing.amount
            + max(0, int(bot.already_pending(kind)) - unfinished)
            + bus.planned[kind]
        )

    def base_count(self, bot, bus):
        unfinished = bot.townhalls.not_ready.amount
        return (
            bot.townhalls.amount
            + max(0, int(bot.already_pending(U.COMMANDCENTER)) - unfinished)
            + bus.planned[U.COMMANDCENTER]
        )

    async def step(self, bot, bus, plan, intel):
        self.placements.clear()
        self.placement_queries = 0
        if intel.home_threat is not None:
            self.last_threat_at = bot.time
        if not bot.townhalls:
            # 最后400矿必须先重建指挥中心；先补Depot/兵营会切断恢复经济的机会。
            await self._expansion(bot, bus, plan, intel)
            return
        self._depots(bot, bus)
        self._orbitals(bot, bus, plan)
        self._bunkers(bot, bus, plan)
        await self._supply(bot, bus)
        await self._opening(bot, bus)
        self._upgrade_orbitals(bot, bus)
        self._train_workers(bot, bus, plan)
        # 先完成可支付的扩张；余额不足时为扩张预留，但不阻止农民和补给。
        await self._expansion(bot, bus, plan, intel)
        await self._infrastructure(bot, bus, plan, intel)
        await self._addons(bot, bus, plan)
        self._research(bot, bus)
        self._production(bot, bus, plan)

    def _depots(self, bot, bus):
        enemies = [e for e in bot.enemy_units if e.is_visible and not e.is_flying]
        for depot in bot.structures.of_type({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED}).ready:
            danger = any(e.distance_to(depot) < 8 for e in enemies)
            ability = A.MORPH_SUPPLYDEPOT_RAISE if danger else A.MORPH_SUPPLYDEPOT_LOWER
            if bus.has(depot, ability):
                bus.issue(depot(ability))

    def _orbitals(self, bot, bus, plan):
        orbitals = sorted(bot.townhalls(U.ORBITALCOMMAND).ready, key=lambda o: (-o.energy, o.tag))
        cloaked = [
            e
            for e in bot.enemy_units
            if e.is_visible and not e.can_be_attacked and (e.is_cloaked or e.is_burrowed)
        ]
        combat = bot.units.of_type({U.MARINE, U.MARAUDER, U.SIEGETANK, U.SIEGETANKSIEGED})
        for orbital in orbitals:
            if orbital.tag in bus.issued:
                continue
            scan_target = next(
                (
                    e.position
                    for e in cloaked
                    if (
                        (combat and combat.closest_distance_to(e) < 15)
                        or (bot.townhalls and bot.townhalls.closest_distance_to(e) < 20)
                    )
                ),
                None,
            )
            if (
                scan_target is None
                and plan.stance == Stance.ATTACK
                and combat
                and combat.closest_distance_to(plan.objective) < 14
                and not bot.is_visible(plan.objective)
                and orbital.energy >= 100
                and bot.time - self.last_scan > 25
            ):
                scan_target = plan.objective
            if (
                scan_target is not None
                and bot.time - self.last_scan > 10
                and bus.has(orbital, A.SCANNERSWEEP_SCAN)
            ):
                if bus.issue(orbital(A.SCANNERSWEEP_SCAN, scan_target)):
                    self.last_scan = bot.time
                continue
            reserve = 50 if plan.save_scan else 0
            if orbital.energy < 50 + reserve or not bus.has(orbital, A.CALLDOWNMULE_CALLDOWNMULE):
                continue
            # 本矿枯竭时支援其他已完成基地，避免将矿骡投到遥远敌方矿区。
            fields = [
                m
                for m in bot.mineral_field
                if m.mineral_contents >= 150
                and any(
                    m.distance_to(base) < 10 for base in bot.townhalls.ready if not base.is_flying
                )
                and not any(
                    e.is_visible and e.can_attack_ground and e.distance_to(m) < 8
                    for e in bot.enemy_units
                )
            ]
            if fields:
                mineral = min(
                    fields,
                    key=lambda m: (orbital.distance_to(m) / 3 - m.mineral_contents / 200, m.tag),
                )
                bus.issue(orbital(A.CALLDOWNMULE_CALLDOWNMULE, mineral))

    def _bunkers(self, bot, bus, plan):
        for bunker in bot.structures(U.BUNKER).ready:
            if plan.emergency:
                slots = int(bunker.cargo_left)
                marines = [
                    m
                    for m in bot.units(U.MARINE)
                    if m.distance_to(bunker) < 16 and m.tag not in bus.issued
                ]
                for marine in sorted(marines, key=lambda m: (m.distance_to(bunker), m.tag))[:slots]:
                    bus.issue(marine.smart(bunker))
            elif (
                bot.time - self.last_threat_at > 12
                and bunker.cargo_used
                and bus.has(bunker, A.UNLOADALL_BUNKER)
            ):
                bus.issue(bunker(A.UNLOADALL_BUNKER))

    async def _supply(self, bot, bus):
        if bot.supply_cap >= 200:
            return
        producers = bot.structures.of_type(PRODUCTION).ready
        slots = sum(2 if p.has_reactor else 1 for p in producers) + bot.townhalls.ready.amount
        incoming = 8 * (bot.already_pending(U.SUPPLYDEPOT) + bus.planned[U.SUPPLYDEPOT])
        # 指挥中心工期长，只有接近完成时才算进可用人口预测。
        incoming += 15 * sum(b.build_progress >= 0.8 for b in bot.townhalls.not_ready)
        # 开局12工人等到14人口再造；后期每次至多一座，后续周期再补。
        first_depot = self.count(bot, bus, U.SUPPLYDEPOT) == 0
        if first_depot and bot.supply_used < min(14, self.config.max_workers):
            return
        if not first_depot and bus.budget.supply + incoming >= supply_buffer(
            slots, bot.supply_used
        ):
            return
        near = next(
            (
                p
                for p in self.wall_depots
                if not bot.structures.of_type({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED}).closer_than(
                    2, p
                )
            ),
            None,
        )
        built = await self._build(
            bot,
            bus,
            U.SUPPLYDEPOT,
            near=near,
            urgent=True,
            max_distance=0 if near is not None else 14,
        )
        if near is not None and not built:
            # 墙位可能被地图地形、障碍或别的建筑占用，不能因此永久卡人口。
            await self._build(bot, bus, U.SUPPLYDEPOT, urgent=True, max_distance=14)

    async def _opening(self, bot, bus):
        if self.count(bot, bus, U.BARRACKS) == 0:
            await self._build(bot, bus, U.BARRACKS, urgent=True)
        if self.count(bot, bus, U.BARRACKS) and self.count(bot, bus, U.REFINERY) == 0:
            await self._gas(bot, bus, desired=1, urgent=True)

    def _upgrade_orbitals(self, bot, bus):
        for base in sorted(bot.townhalls(U.COMMANDCENTER).ready.idle, key=lambda b: b.tag):
            if bus.has(base, A.UPGRADETOORBITAL_ORBITALCOMMAND):
                bus.issue(
                    base(A.UPGRADETOORBITAL_ORBITALCOMMAND),
                    cost=U.ORBITALCOMMAND,
                    supply=0,
                    urgent=True,
                )

    def _train_workers(self, bot, bus, plan):
        bases = [b for b in bot.townhalls.ready if not b.is_flying]
        mineral_workers = sum(
            2
            for m in bot.mineral_field
            if m.mineral_contents and any(m.distance_to(b) < 10 for b in bases)
        )
        desired = min(
            self.config.max_workers,
            max(16, mineral_workers + 3 * bot.gas_buildings.ready.amount + 6),
        )
        if plan.emergency:
            desired = min(desired, max(18, len(bases) * 18))
        for base in sorted(bases, key=lambda b: (b.assigned_harvesters, b.tag)):
            if (
                base.is_idle
                and base.tag not in bus.issued
                and self.count(bot, bus, U.SCV) < desired
            ):
                self._train(bot, bus, base, U.SCV, urgent=True)

    async def _expansion(self, bot, bus, plan, intel):
        bases = self.base_count(bot, bus)
        required_workers = (
            1
            if bases == 0
            else 19
            if bases == 1
            else min(self.config.max_workers - 6, bases * 16 + 7)
        )
        can_expand = (
            plan.allow_expand
            and bases < min(plan.desired_bases, self.config.max_bases)
            and bot.workers.amount >= required_workers
            and (bases == 0 or bot.structures(U.BARRACKS).ready)
        )
        if not can_expand or bot.already_pending(U.COMMANDCENTER):
            return
        # 保住遭袭矿区，只有主基地全灭后的重建允许绕过经济扩张限制。
        if intel.home_threat is not None and bases:
            return
        target = await self._next_expansion(bot, intel)
        if target is None:
            return
        if await self._build(bot, bus, U.COMMANDCENTER, near=target, urgent=True, max_distance=0):
            self.expansion_cache = None
        elif self.workers.builder(bot, bus, target) is not None:
            price = bot.calculate_cost(U.COMMANDCENTER)
            bus.budget.held_minerals = min(bus.budget.minerals, price.minerals)

    async def _next_expansion(self, bot, intel):
        if (
            self.expansion_cache is not None
            and bot.time - self.expansion_cache_at < 15
            and not bot.townhalls.closer_than(10, self.expansion_cache)
        ):
            return self.expansion_cache
        origin = (
            bot.townhalls.closest_to(bot.start_location).position
            if bot.townhalls
            else bot.start_location
        )
        visible = [e for e in bot.enemy_units | bot.enemy_structures if e.is_visible]
        positions = [
            p
            for p in bot.expansion_locations_list
            if not bot.townhalls.closer_than(10, p)
            and self.bad_expansions.get(p, -100) < bot.time
            and not any(e.distance_to(p) < 15 for e in visible)
            and not any(p.distance_to(q) < 15 for q in intel.known_structures)
        ]
        positions.sort(key=lambda p: (p.distance_to(origin), p.x, p.y))
        best = None
        best_distance = float("inf")
        for position in positions[:6]:
            distance = await bot.client.query_pathing(origin, position)
            if distance is not None and distance < best_distance:
                best, best_distance = position, distance
        self.expansion_cache = best
        self.expansion_cache_at = bot.time
        return best

    async def _infrastructure(self, bot, bus, plan, intel):
        ready_bases = bot.townhalls.ready.amount
        gas_target = min(2 * ready_bases, max(1, (bot.workers.amount - 12) // 10 + 1))
        await self._gas(bot, bus, gas_target)
        if plan.emergency and not self.count(bot, bus, U.BUNKER):
            near = plan.rally.towards(plan.fallback, 2)
            await self._build(bot, bus, U.BUNKER, near=near, urgent=True)
        if self.count(bot, bus, U.FACTORY) < plan.desired_factories:
            await self._build(bot, bus, U.FACTORY)
        if self.count(bot, bus, U.STARPORT) < plan.desired_starports:
            await self._build(bot, bus, U.STARPORT)
        barracks_goal = min(plan.desired_barracks, max(1, bot.workers.amount // 8))
        if self.count(bot, bus, U.BARRACKS) < barracks_goal:
            await self._build(bot, bus, U.BARRACKS)
        if bot.workers.amount >= 35 or intel.cloak_risk or intel.air_supply >= 4:
            ebay_goal = 2 if bot.workers.amount >= 58 else 1
            if self.count(bot, bus, U.ENGINEERINGBAY) < ebay_goal:
                await self._build(bot, bus, U.ENGINEERINGBAY)
        if (
            bot.supply_used >= 100
            and bot.structures(U.ENGINEERINGBAY).ready
            and not self.count(bot, bus, U.ARMORY)
        ):
            await self._build(bot, bus, U.ARMORY)
        if intel.cloak_risk or intel.air_supply >= 5:
            for base in bot.townhalls.ready:
                if base.is_flying or bot.structures(U.MISSILETURRET).closer_than(11, base):
                    continue
                minerals = bot.mineral_field.closer_than(10, base)
                near = (
                    base.position.towards(minerals.center, 4)
                    if minerals
                    else base.position.towards(plan.rally, 5)
                )
                if await self._build(bot, bus, U.MISSILETURRET, near=near, urgent=intel.cloak_risk):
                    break

    async def _gas(self, bot, bus, desired, urgent=False):
        if self.count(bot, bus, U.REFINERY) >= desired or not bus.can_afford(
            U.REFINERY, urgent=urgent
        ):
            return False
        geysers = [
            g
            for g in bot.vespene_geyser
            if g.vespene_contents > 0
            and any(g.distance_to(b) < 12 for b in bot.townhalls.ready if not b.is_flying)
            and not bot.gas_buildings.closer_than(1, g)
        ]
        for geyser in sorted(geysers, key=lambda g: (g.distance_to(bot.start_location), g.tag)):
            if self._reserved(geyser.position, 2):
                continue
            worker = self.workers.builder(bot, bus, geyser.position)
            if worker is None:
                continue
            if any(
                e.is_visible and e.can_attack_ground and e.distance_to(geyser) < 8
                for e in bot.enemy_units
            ):
                continue
            if not await bot.can_place_single(U.REFINERY, geyser.position):
                continue
            if bus.issue(worker.build(U.REFINERY, geyser), cost=U.REFINERY, urgent=urgent):
                self.placements.append((geyser.position, 2))
                return True
        return False

    def _reserved(self, point, radius):
        return any(
            abs(point.x - other.x) < radius + size and abs(point.y - other.y) < radius + size
            for other, size in self.placements
        )

    async def _build(self, bot, bus, kind, near=None, urgent=False, max_distance=16):
        if (
            not bus.can_afford(kind, urgent=urgent)
            or bot.tech_requirement_progress(kind) < 1
            or self.placement_queries >= self.config.max_placement_queries
        ):
            return False
        if near is None:
            bases = bot.townhalls.ready
            base = bases.closest_to(bot.start_location) if bases else None
            anchor = base.position if base else bot.start_location
            toward = anchor.towards(bot.game_info.map_center, 6)
            # 固定网格偏移与标签排序保持可复现，不靠系统全局 random。
            index = bot.structures.of_type(PRODUCTION).amount + len(self.placements)
            near = toward.offset((((index % 4) - 1) * 5, (index // 4) * 5))
        radius = (
            2.5
            if kind == U.COMMANDCENTER
            else 1.0
            if kind in {U.SUPPLYDEPOT, U.MISSILETURRET}
            else 1.5
        )
        self.placement_queries += 1
        position = await bot.find_placement(
            kind,
            near=near,
            max_distance=max_distance,
            placement_step=3,
            random_alternative=False,
            addon_place=kind in PRODUCTION,
        )
        if position is None:
            if kind == U.COMMANDCENTER:
                self.bad_expansions[near] = bot.time + 20
                self.expansion_cache = None
            return False
        if self._reserved(position, radius + (1 if kind in PRODUCTION else 0)):
            return False
        worker = self.workers.builder(bot, bus, position)
        if worker is None:
            return False
        if bus.issue(worker.build(kind, position), cost=kind, urgent=urgent):
            self.placements.append((position, radius + (1 if kind in PRODUCTION else 0)))
            return True
        return False

    async def _addons(self, bot, bus, plan):
        for flying in sorted(bot.structures.of_type(FLYING_PRODUCTION), key=lambda u: u.tag):
            kind, ability = LAND[flying.type_id]
            if (
                flying.tag in bus.issued
                or not bus.has(flying, ability)
                or self.placement_queries >= self.config.max_placement_queries
            ):
                continue
            self.placement_queries += 1
            near = flying.position.towards(bot.start_location, 5)
            position = await bot.find_placement(
                kind,
                near=near,
                max_distance=18,
                placement_step=3,
                random_alternative=False,
                addon_place=True,
            )
            if (
                position is not None
                and not self._reserved(position, 3)
                and bus.issue(flying(ability, position))
            ):
                self.placements.append((position, 3))
                self.blocked_addons.pop(flying.tag, None)
        barracks = sorted(bot.structures(U.BARRACKS).ready, key=lambda b: b.tag)
        techlab_goal = min(3, max(1, ceil(len(barracks) * plan.composition.marauder_fraction)))
        for kind in sorted(PRODUCTION, key=lambda unit_type: unit_type.value):
            buildings = sorted(bot.structures(kind).ready, key=lambda b: b.tag)
            for index, building in enumerate(buildings):
                if building.add_on_tag or not building.is_idle or building.tag in bus.issued:
                    continue
                techlab = (
                    kind == U.FACTORY
                    or (kind == U.BARRACKS and index < techlab_goal)
                    or (kind == U.STARPORT and index == 0)
                )
                addon = ADDONS[kind][0 if techlab else 1]
                if not bus.can_afford(addon):
                    continue
                # 只有确认附件位置被堵时才搬迁，缺资源/科技绝不触发无谓起飞。
                addon_position = building.position.offset((2.5, -0.5))
                if not await bot.can_place_single(U.SUPPLYDEPOT, addon_position):
                    first_blocked = self.blocked_addons.setdefault(building.tag, bot.time)
                    if bot.time - first_blocked > 8 and bus.has(building, LIFT[kind]):
                        bus.issue(building(LIFT[kind]))
                    continue
                self.blocked_addons.pop(building.tag, None)
                creation = bot.game_data.units[addon.value].creation_ability
                if creation is not None and (
                    bus.has(building, creation.id) or bus.has(building, creation.exact_id)
                ):
                    bus.issue(building.build(addon), cost=addon, supply=0)

    def _train(self, bot, bus, building, kind, *, urgent=False, queued=False):
        creation = bot.game_data.units[kind.value].creation_ability
        if creation is None or not (
            bus.has(building, creation.id) or bus.has(building, creation.exact_id)
        ):
            return False
        return bus.issue(
            building.train(kind, queue=queued), cost=kind, urgent=urgent, allow_queue=queued
        )

    def _production(self, bot, bus, plan):
        composition = plan.composition
        # 母建筑有Tech Lab或Reactor才按对应科技/槽位出兵；不无限堆积队列。
        for building in sorted(bot.structures.of_type(PRODUCTION).ready, key=lambda b: b.tag):
            if building.tag in bus.issued:
                continue
            capacity = 2 if building.has_reactor else 1
            free = max(0, capacity - len(building.orders))
            if free == 0:
                continue
            for slot in range(free):
                kind = None
                if building.type_id == U.BARRACKS:
                    marines = self.count(bot, bus, U.MARINE)
                    marauders = self.count(bot, bus, U.MARAUDER)
                    want_marauder = (
                        marauders < (marines + marauders + 1) * composition.marauder_fraction
                    )
                    kind = (
                        U.MARAUDER
                        if building.has_techlab and want_marauder and bus.can_afford(U.MARAUDER)
                        else U.MARINE
                    )
                elif building.type_id == U.FACTORY:
                    if (
                        building.has_techlab
                        and self.count(bot, bus, U.SIEGETANK) < composition.tank_goal
                    ):
                        kind = U.SIEGETANK
                elif building.type_id == U.STARPORT:
                    if (
                        building.has_techlab
                        and self.count(bot, bus, U.RAVEN) < composition.raven_goal
                    ):
                        kind = U.RAVEN
                    elif self.count(bot, bus, U.VIKINGFIGHTER) < composition.viking_goal:
                        kind = U.VIKINGFIGHTER
                    elif self.count(bot, bus, U.MEDIVAC) < composition.medivac_goal:
                        kind = U.MEDIVAC
                if kind is None:
                    break
                # 反应堆已有一个订单时，新指令须排队，否则会取消原有生产。
                queued = bool(building.orders) or slot > 0
                if not self._train(bot, bus, building, kind, queued=queued):
                    break

    def _research(self, bot, bus):
        priorities = {
            U.BARRACKSTECHLAB: [R.STIMPACK, R.SHIELDWALL, R.PUNISHERGRENADES],
            U.ENGINEERINGBAY: [
                R.TERRANINFANTRYWEAPONSLEVEL1,
                R.TERRANINFANTRYARMORSLEVEL1,
                R.TERRANINFANTRYWEAPONSLEVEL2,
                R.TERRANINFANTRYARMORSLEVEL2,
                R.TERRANINFANTRYWEAPONSLEVEL3,
                R.TERRANINFANTRYARMORSLEVEL3,
            ],
            U.ARMORY: [
                R.TERRANVEHICLEWEAPONSLEVEL1,
                R.TERRANSHIPWEAPONSLEVEL1,
                R.TERRANVEHICLEANDSHIPARMORSLEVEL1,
                R.TERRANVEHICLEWEAPONSLEVEL2,
                R.TERRANSHIPWEAPONSLEVEL2,
                R.TERRANVEHICLEANDSHIPARMORSLEVEL2,
                R.TERRANVEHICLEWEAPONSLEVEL3,
                R.TERRANSHIPWEAPONSLEVEL3,
                R.TERRANVEHICLEANDSHIPARMORSLEVEL3,
            ],
        }
        for kind, upgrades in priorities.items():
            for structure in sorted(bot.structures(kind).ready.idle, key=lambda s: s.tag):
                for upgrade in upgrades:
                    if bot.already_pending_upgrade(upgrade) or bus.planned[upgrade]:
                        continue
                    data = bot.game_data.upgrades.get(upgrade.value)
                    if data is None:
                        continue
                    ability = data.research_ability
                    if ability is None or not (
                        bus.has(structure, ability.id) or bus.has(structure, ability.exact_id)
                    ):
                        continue
                    if bus.issue(structure.research(upgrade), cost=upgrade, supply=0):
                        break
