"""Terran bio and tank pressure for the Starcarft arena."""

from collections import Counter
from math import hypot, inf
from statistics import median

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.buff_id import BuffId as B
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as G
from sc2.position import Point2

__all__ = ["CompetitiveBot"]

PRODUCTION = {U.BARRACKS, U.FACTORY, U.STARPORT}
HALLS = {U.COMMANDCENTER, U.ORBITALCOMMAND, U.PLANETARYFORTRESS}
TANKS = {U.SIEGETANK, U.SIEGETANKSIEGED}
BIO = {U.MARINE, U.MARAUDER}
ARMY = BIO | TANKS | {U.REAPER, U.HELLION, U.MEDIVAC, U.VIKINGFIGHTER, U.VIKINGASSAULT}
WORKERS = {U.SCV, U.PROBE, U.DRONE, U.DRONEBURROWED, U.MULE}
UNARMED_THREATS = {
    U.BUNKER,
    U.BANELING,
    U.BANELINGBURROWED,
    U.DISRUPTOR,
    U.HIGHTEMPLAR,
    U.INFESTOR,
    U.INFESTORBURROWED,
    U.WIDOWMINEBURROWED,
    U.RAVEN,
    U.VIPER,
}
CLOAK_TECH = {
    U.BANSHEE,
    U.DARKTEMPLAR,
    U.DARKSHRINE,
    U.GHOST,
    U.LURKERMP,
    U.LURKERMPBURROWED,
    U.WIDOWMINEBURROWED,
}
AIR_TRANSFORMERS = {U.VIKINGFIGHTER, U.LIBERATOR, U.BANSHEE, U.BROODLORD}
SIEGE_BUFFER = 14


def offset(point, x, y):
    return Point2((point.x + x, point.y + y))


def point_distance_squared(a, b):
    return (a.x - b.x) ** 2 + (a.y - b.y) ** 2


def footprint_overlap(a, size_a, b, size_b):
    half = (size_a + size_b) / 2
    return abs(a.x - b.x) < half and abs(a.y - b.y) < half


def weight(unit):
    """Rough force comparisons in marine equivalents, discounted by damage."""
    if unit.type_id in TANKS:
        base = 4.0
    elif unit.type_id in WORKERS:
        base = 0.45
    elif unit.type_id in UNARMED_THREATS:
        base = 3.0
    elif unit.type_id == U.MEDIVAC:
        base = 1.2
    else:
        base = max(1.0, min(8.0, (unit.health_max + unit.shield_max) / 60))
        if unit.is_structure:
            base *= 0.65
    return base * (0.45 + 0.55 * unit.shield_health_percentage)


class CompetitiveBot(BotAI):
    """A state-driven economy feeding a tank-led attack with a fallback rally."""

    def __init__(self):
        super().__init__()
        self.unit_command_uses_self_do = True
        self._reset_match()

    def _reset_match(self):
        self.mode = "hold"
        self.now = 0.0
        self.next_macro = 0.0
        self.next_labor = 0.0
        self.next_micro = 0.0
        self.next_scout = 0.0
        self.scout_tag = None
        self.scout_used = False
        self.regroup_until = 0.0
        self.push_peak = 0.0
        self.push_started = 0.0
        self.attack_destination = None
        self.sweep_goal = None
        self.sweep_distance = inf
        self.sweep_progress_at = 0.0
        self.sweep_retry = {}
        self.reinforce_point = None
        self.reinforce_updated_at = 0.0
        self.last_pressure = -inf
        self.last_air = -inf
        self.air_count = 0
        self.last_cloak = -inf
        self.last_scan = -inf
        self.scan_positions = {}
        self.enemy_sites = {}
        self.last_seen = {}
        self.site_retry = {}
        self.commands = {}
        self.siege_contact = {}
        self.foundation_retry = {}
        self.workers_busy = set()
        self.labor_targets = {}
        self.friendly_health = {}
        self.unseen_damage = None
        self.structure_ordered = False
        self.expansions = []
        self.enemy_start = None

    async def on_start(self):
        self._reset_match()
        candidates = [p for p in self.enemy_start_locations if p.distance_to(self.start_location) > 8]
        self.enemy_start = min(candidates, default=self.game_info.map_center)
        self.expansions = sorted(
            self.expansion_locations_list,
            key=lambda p: (p.distance_to(self.start_location), p),
        )
        self.last_seen = dict.fromkeys({*self.expansions, self.enemy_start}, -inf)

    async def on_step(self, iteration: int):
        self.now = self.time
        self.workers_busy.clear()
        self.structure_ordered = False
        self.visible = sorted(
            (u for u in self.enemy_units | self.enemy_structures if u.is_visible),
            key=lambda u: u.tag,
        )
        halls = [h.position for h in self.townhalls if not h.is_flying]
        self.zones = halls or [self._home()]
        self.hostiles = [u for u in self.visible if not u.is_hallucination and self._dangerous(u)]
        self.raiders = [u for u in self.hostiles if self._near_base(u, 18)]
        if self._pressured():
            self.last_pressure = self.now

        self._observe()
        self._scout()
        micro_due = self.now >= self.next_micro
        if micro_due:
            self._protect_workers()
        if self.now >= self.next_macro:
            self.next_macro = self.now + 0.7
            await self._macro()
        if self.now >= self.next_labor:
            self.next_labor = self.now + 2.6
            self._labor()
            self._repair()
        if micro_due:
            self.next_micro = self.now + 0.32
            for depot in self.structures(U.SUPPLYDEPOT).ready:
                self._issue(depot, A.MORPH_SUPPLYDEPOT_LOWER)
            self._combat()
        self._orbital_energy()

    def _home(self):
        if self.townhalls:
            return min(
                self.townhalls,
                key=lambda u: (not u.is_ready, u.distance_to(self.start_location), u.tag),
            ).position
        if self.workers:
            return self.workers.center
        return self.start_location

    def _near_base(self, unit, radius):
        return any(unit.distance_to_squared(p) <= radius**2 for p in self.zones)

    def _dangerous(self, unit):
        if unit.type_id in WORKERS:
            return any(unit.distance_to_squared(p) < 12**2 for p in self.zones)
        return unit.can_attack or unit.type_id in UNARMED_THREATS

    def _observe(self):
        visible_structures = {u.tag: u for u in self.visible if u.is_structure}
        for tag, point in tuple(self.enemy_sites.items()):
            seen = visible_structures.get(tag)
            stale = self.is_visible(point) if seen is None else seen.is_flying
            if stale:
                del self.enemy_sites[tag]
        for unit in visible_structures.values():
            if not unit.is_flying:
                self.enemy_sites[unit.tag] = unit.position

        if any(u.type_id in CLOAK_TECH or u.is_cloaked or u.is_burrowed for u in self.visible):
            self.last_cloak = self.now
        air = sum(
            u.is_flying
            and (
                u.is_structure
                or u.can_attack
                or u.type_id in AIR_TRANSFORMERS
                or u.type_id in UNARMED_THREATS
            )
            for u in self.visible
        )
        if air:
            self.air_count = max(air, self.air_count if self.now - self.last_air < 35 else 0)
            self.last_air = self.now

        if self.now < self.next_scout:
            return
        self.next_scout = self.now + 1.1
        self._notice_unseen_damage()
        for point in self.last_seen:
            if self.is_visible(point):
                self.last_seen[point] = self.now
        alive = self.units.tags | self.structures.tags
        self.commands = {tag: order for tag, order in self.commands.items() if tag in alive}
        self.siege_contact = {tag: t for tag, t in self.siege_contact.items() if tag in alive}
        self.scan_positions = {p: t for p, t in self.scan_positions.items() if self.now - t < 16}

    def _notice_unseen_damage(self):
        observed = self.workers | self.units(ARMY) | self.townhalls
        damage = [
            u
            for u in observed
            if u.tag in self.friendly_health
            and self.friendly_health[u.tag] - u.health >= 12
            and self._near_base(u, 18)
            and not (u.type_id in BIO and (u.has_buff(B.STIMPACK) or u.has_buff(B.STIMPACKMARAUDER)))
            and not any(e.distance_to_squared(u) < 15**2 for e in self.hostiles)
        ]
        self.friendly_health = {u.tag: u.health for u in observed}
        if damage:
            unit = min(damage, key=lambda u: (u.health_percentage, u.tag))
            self.unseen_damage = (unit.position, self.now)
            self.last_cloak = self.now

    def _pressured(self):
        return any(u.type_id not in WORKERS for u in self.raiders) or len(self.raiders) >= 4

    def _macro_count(self, kind):
        return self.structures(kind).amount + self.worker_en_route_to_build(kind)

    def _supply(self):
        if self.supply_cap >= 200:
            return False
        rate = len(self.structures(PRODUCTION).ready)
        buffer = min(25, max(3, 2 * rate + self.townhalls.ready.amount))
        pending = self.structures(U.SUPPLYDEPOT).not_ready.amount + self.worker_en_route_to_build(
            U.SUPPLYDEPOT
        )
        return self.supply_left <= buffer and self.supply_left + 8 * pending <= buffer

    def _build_worker(self, target, exclude=()):
        candidates = [
            w
            for w in self.workers
            if w.tag not in self.workers_busy
            and w.tag not in exclude
            and (w.is_idle or w.is_gathering or w.is_returning)
            and not w.is_carrying_vespene
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda w: (w.distance_to_squared(target) + (15 if w.is_carrying_resource else 0), w.tag),
        )

    def _send_build(self, kind, worker, target):
        if self._spend(worker.build(kind, target)):
            self.structure_ordered = True
            self.workers_busy.add(worker.tag)
            return True
        return False

    async def _build(self, kind, anchor):
        if (
            self.structure_ordered
            or self.now < self.site_retry.get(kind, 0)
            or not self.can_afford(kind)
            or self.tech_requirement_progress(kind) < 1
        ):
            return False
        if self._build_worker(anchor) is None:
            return False
        point = await self._building_site(kind, anchor)
        if point is None:
            self.site_retry[kind] = self.now + 3
            return False
        worker = self._build_worker(point)
        if (
            worker.distance_to_squared(point) > 3**2
            and await self.client.query_pathing(worker, point) is None
        ):
            self.site_retry[point] = self.now + 20
            return False
        return self._send_build(kind, worker, point)

    async def _building_site(self, kind, anchor):
        size = 2 if kind in {U.SUPPLYDEPOT, U.MISSILETURRET} else 3
        static_reservations = [(u.add_on_position, 2) for u in self.structures(PRODUCTION) if not u.is_flying]
        resources = [
            r.position
            for r in self.mineral_field | self.vespene_geyser
            if any(r.distance_to_squared(p) < 13**2 for p in self.zones)
        ]
        for p in self.expansions:
            if point_distance_squared(p, anchor) < 24**2:
                static_reservations.append((p, 5))

        def available(point, width):
            area = self.game_info.playable_area
            if not (area.x + 2 <= point.x < area.right - 2 and area.y + 2 <= point.y < area.top - 2):
                return False
            span = range(-(width // 2), (width + 1) // 2)
            if any(
                not self.in_placement_grid(offset(point, x, y)) or self.has_creep(offset(point, x, y))
                for x in span
                for y in span
            ):
                return False
            if any(footprint_overlap(point, width, p, s) for p, s in static_reservations):
                return False
            return all(point_distance_squared(point, r) >= (width / 2 + 2) ** 2 for r in resources)

        origin = Point2((int(anchor.x), int(anchor.y)))
        if size == 3:
            origin = offset(origin, 0.5, 0.5)
        for inner, outer in ((0, 7), (8, 13), (14, 19)):
            candidates = [
                offset(origin, x, y)
                for x in range(-outer, outer + 1)
                for y in range(-outer, outer + 1)
                if max(abs(x), abs(y)) >= inner
            ]
            candidates.sort(key=lambda p: (point_distance_squared(p, anchor), p))
            candidates = [
                p
                for p in candidates
                if self.now >= self.site_retry.get(p, 0)
                and available(p, size)
                and (kind not in PRODUCTION or available(offset(p, 2.5, -0.5), 2))
            ]
            for start in range(0, min(len(candidates), 96), 24):
                batch = candidates[start : start + 24]
                valid = await self.can_place(kind, batch)
                batch = [p for p, ok in zip(batch, valid) if ok]
                if batch and kind in PRODUCTION:
                    pads = [offset(p, 2.5, -0.5) for p in batch]
                    clear = await self.can_place(U.SUPPLYDEPOT, pads)
                    batch = [p for p, ok in zip(batch, clear) if ok]
                if batch:
                    return batch[0]
        return None

    async def _expand(self):
        if self.structure_ordered or not self.can_afford(U.COMMANDCENTER):
            return False
        home = self._home()
        for point in sorted(self.expansions, key=lambda p: (point_distance_squared(p, home), p)):
            if point_distance_squared(point, self.enemy_start) < 16**2:
                continue
            if any(point_distance_squared(point, site) < 9**2 for site in self.enemy_sites.values()):
                continue
            if self.now < self.site_retry.get(point, 0):
                continue
            if any(h.distance_to_squared(point) < 8**2 for h in self.townhalls):
                continue
            if any(u.distance_to_squared(point) < 11**2 for u in self.hostiles):
                continue
            worker = self._build_worker(point)
            if worker is None:
                return False
            if not (await self.can_place(U.COMMANDCENTER, [point]))[0]:
                self.site_retry[point] = self.now + 18
                continue
            path = await self.client.query_pathing(worker, point)
            if path is None:
                self.site_retry[point] = self.now + 35
                continue
            return self._send_build(U.COMMANDCENTER, worker, point)
        return False

    def _gas(self, limit):
        if self.structure_ordered or not self.can_afford(U.REFINERY):
            return
        existing = self.gas_buildings
        if existing.amount >= limit or self.already_pending(U.REFINERY) > existing.not_ready.amount:
            return
        near_halls = [
            (h.distance_to_squared(g), g.tag, g)
            for h in self.townhalls.ready
            for g in self.vespene_geyser
            if h.distance_to_squared(g) < 11**2
            and g.vespene_contents
            and not any(r.distance_to_squared(g) < 2**2 for r in existing)
        ]
        if not near_halls:
            return
        target = min(near_halls)[2]
        worker = self._build_worker(target)
        if worker is not None:
            self._send_build(U.REFINERY, worker, target)

    def _resume_construction(self):
        unfinished = self.structures.not_ready
        active_workers = [w for w in self.workers if w.is_constructing_scv]
        worker_targets = {w.order_target for w in self.workers}
        for building in sorted(unfinished, key=lambda b: (-b.build_progress, b.tag)):
            if building.type_id not in HALLS | PRODUCTION | {
                U.SUPPLYDEPOT,
                U.REFINERY,
                U.REFINERYRICH,
                U.ENGINEERINGBAY,
                U.ARMORY,
                U.MISSILETURRET,
                U.BUNKER,
            }:
                continue
            if building.tag in worker_targets or any(
                w.distance_to_squared(building) < 4**2 for w in active_workers
            ):
                continue
            if self.now < self.foundation_retry.get(building.tag, 0):
                continue
            if any(u.can_attack_ground and u.distance_to_squared(building) < 6**2 for u in self.raiders):
                continue
            worker = self._build_worker(building)
            if worker is not None:
                self.do(worker(A.SMART, building))
                self.workers_busy.add(worker.tag)
                self.foundation_retry[building.tag] = self.now + 5

    async def _macro(self):
        self._resume_construction()
        if not self.townhalls and not self.worker_en_route_to_build(U.COMMANDCENTER):
            await self._expand()

        stressed = self._pressured()
        home = self._home()
        rally = self._rally()
        barracks = self._macro_count(U.BARRACKS)
        factories = self._macro_count(U.FACTORY)
        starports = self._macro_count(U.STARPORT)
        tanks = self.units(TANKS).amount
        bio = self.units(BIO).amount
        halls = self.townhalls.ready.amount
        bases = self.townhalls.ready.filter(lambda h: not h.is_flying)
        mineral_capacity = sum(max(0, h.ideal_harvesters) for h in bases)
        workers = self.workers.amount
        air_alert = self.now - self.last_air <= 70
        cloak_alert = self.now - self.last_cloak <= 95
        if self._supply():
            await self._build(U.SUPPLYDEPOT, home.towards(self.game_info.map_center, -5))

        if barracks == 0:
            await self._build(U.BARRACKS, rally)
        if barracks:
            gas_limit = min(2 * halls, 1 if not factories else 2 + max(0, halls - 1))
            if self.vespene < 500:
                self._gas(gas_limit if factories > 1 or halls < 3 else min(gas_limit, 4))

        hold_minerals = self.supply_left <= 1 and self._supply() and not self.structure_ordered
        if hold_minerals:
            return

        cc_pending = self.townhalls.not_ready.amount + self.worker_en_route_to_build(U.COMMANDCENTER)
        natural_wanted = halls == 1 and not cc_pending and ((factories and bio >= 4) or self.now >= 210)
        later_base_wanted = (
            halls >= 2
            and not cc_pending
            and halls < 7
            and self.now >= (285 if halls == 2 else 390)
            and workers >= min(69, max(18, mineral_capacity - 3))
        )
        if (natural_wanted or later_base_wanted) and not stressed and self.now - self.last_pressure > 12:
            await self._expand()

        if self._macro_count(U.FACTORY) == 0 and (not stressed or bio >= 5):
            await self._build(U.FACTORY, rally)

        if stressed and self.now < 320 and not self._macro_count(U.BUNKER):
            await self._build(U.BUNKER, self._defense_point())
        # Start another infantry queue before air support when the ground opening is established.
        if barracks == 1 and factories and bio >= 4 and not air_alert:
            await self._build(U.BARRACKS, rally)
        if starports == 0 and factories and (halls >= 2 or cc_pending or air_alert or self.now >= 230):
            await self._build(U.STARPORT, home)
        barracks_goal = min(8, 1 + halls * 2 + int(self.minerals >= 850))
        if barracks < barracks_goal and (
            (stressed and bio < 6 and barracks > 0 and self.minerals >= 175)
            or (halls >= 2 and (starports or self.minerals >= 375))
            or self.minerals >= 500
        ):
            await self._build(U.BARRACKS, rally)
        if factories == 1 and halls >= 3 and tanks >= 3 and self.minerals >= 300:
            await self._build(U.FACTORY, home)
        if starports == 1 and halls >= 3 and air_alert and self.air_count >= 5:
            await self._build(U.STARPORT, home)

        if (air_alert or cloak_alert or bio >= 12) and not self._macro_count(U.ENGINEERINGBAY):
            await self._build(U.ENGINEERINGBAY, home)
        if air_alert or cloak_alert:
            for base in sorted(bases, key=lambda h: (h.distance_to_squared(self._defense_point()), h.tag)):
                turrets = self.structures(U.MISSILETURRET)
                if any(t.distance_to_squared(base) < 11**2 for t in turrets):
                    continue
                # Include workers travelling to build a turret in the pending count.
                if self.already_pending(U.MISSILETURRET) > turrets.not_ready.amount:
                    break
                minerals = self.mineral_field.closer_than(10, base)
                anchor = base.position.towards(minerals.center, 5) if minerals else base.position
                if await self._build(U.MISSILETURRET, anchor):
                    break
        if halls >= 3 and bio >= 20 and not self._macro_count(U.ARMORY):
            await self._build(U.ARMORY, home)

        # During a rush, keep reinforcements ahead of orbital upgrades and SCVs.
        if stressed:
            await self._army_production(air_alert, stressed)
        reserve = 0
        if (natural_wanted or later_base_wanted) and not stressed and not self.structure_ordered:
            reserve = 70 if natural_wanted else 125
        self._townhall_production(bases, mineral_capacity, stressed, reserve)
        if not stressed:
            await self._army_production(air_alert, stressed)
        if bio >= 9 and (not stressed or self.minerals >= 225):
            self._upgrades()

    def _spend(self, action, *, supply=False):
        return bool(action) and self.do(
            action, subtract_cost=True, subtract_supply=supply, can_afford_check=True
        )

    def _train(self, producer, kind):
        return self.can_afford(kind) and self._spend(
            producer.train(kind, queue=not producer.is_idle), supply=True
        )

    def _townhall_production(self, bases, mineral_capacity, stressed, reserve):
        desired = min(76, max(15, mineral_capacity + 3 * self.gas_buildings.ready.amount + 2))
        pending = self.already_pending(U.SCV)
        orbital_started = False
        for hall in sorted(bases.idle, key=lambda h: (h.distance_to_squared(self._home()), h.tag)):
            if (
                not stressed
                and not orbital_started
                and self.minerals >= 150 + reserve
                and (self.workers.amount >= 15 or self.units(BIO).amount >= 4)
                and hall.type_id == U.COMMANDCENTER
                and self.structures(U.BARRACKS).ready
            ):
                orbital_started = self._spend(hall(A.UPGRADETOORBITAL_ORBITALCOMMAND))
                if orbital_started:
                    continue
            if self.workers.amount + pending < desired and self.minerals >= 50 + reserve:
                pending += self._train(hall, U.SCV)

    async def _army_production(self, air_alert, stressed):
        buildings = sorted(self.structures(PRODUCTION).ready, key=lambda u: u.tag)
        labs = self.structures({U.TECHLAB, U.BARRACKSTECHLAB, U.FACTORYTECHLAB, U.STARPORTTECHLAB}).ready.tags
        reactors = self.structures({U.REACTOR, U.BARRACKSREACTOR}).ready.tags
        tank_count = self.units(TANKS).amount + self.already_pending(U.SIEGETANK)
        medivacs = self.units(U.MEDIVAC).amount + self.already_pending(U.MEDIVAC)
        vikings = self.units({U.VIKINGFIGHTER, U.VIKINGASSAULT}).amount + self.already_pending(
            U.VIKINGFIGHTER
        )
        marine = self.units(U.MARINE).amount + self.already_pending(U.MARINE)
        marauder = self.units(U.MARAUDER).amount + self.already_pending(U.MARAUDER)
        medivac_goal = min(8, max(2, (marine + marauder) // 7))
        viking_goal = min(16, max(3, self.air_count * 2)) if air_alert else 0

        factory_needs_lab = [
            u
            for u in buildings
            if u.type_id == U.FACTORY and u.is_idle and not u.add_on_tag and self.can_afford(U.FACTORYTECHLAB)
        ]
        infantry_queues = [u for u in buildings if u.type_id == U.BARRACKS]
        barracks_needs_addon = [
            u
            for u in infantry_queues
            if u.is_idle
            and not u.add_on_tag
            and marine >= 5
            and (len(infantry_queues) >= 2 or marine >= 10)
            and not stressed
            and self.vespene >= 50
        ]
        ask = factory_needs_lab + barracks_needs_addon
        abilities = await self.get_available_abilities(ask) if ask else []
        options = dict(zip((u.tag for u in ask), abilities))
        has_barracks_lab = (
            bool(self.structures(U.BARRACKSTECHLAB)) or self.already_pending(U.BARRACKSTECHLAB) > 0
        )
        reserved = set()
        for factory in factory_needs_lab:
            if (
                (A.BUILD_TECHLAB_FACTORY in options[factory.tag] or A.BUILD_TECHLAB in options[factory.tag])
                and self.can_afford(U.FACTORYTECHLAB)
                and self._spend(factory(A.BUILD_TECHLAB_FACTORY))
            ):
                reserved.add(factory.tag)
        for hall in barracks_needs_addon:
            if has_barracks_lab and marine < 10:
                break
            ability, kind = (
                (A.BUILD_REACTOR_BARRACKS, U.BARRACKSREACTOR)
                if has_barracks_lab
                else (A.BUILD_TECHLAB_BARRACKS, U.BARRACKSTECHLAB)
            )
            generic = A.BUILD_REACTOR if has_barracks_lab else A.BUILD_TECHLAB
            if (
                (ability in options[hall.tag] or generic in options[hall.tag])
                and self.can_afford(kind)
                and self._spend(hall(ability))
            ):
                reserved.add(hall.tag)
                has_barracks_lab = True

        ports = [u for u in buildings if u.type_id == U.STARPORT and u.is_idle]
        if air_alert and self.air_count >= 3 and vikings < viking_goal and ports:
            first_port = ports[0]
            if self._train(first_port, U.VIKINGFIGHTER):
                vikings += 1
                reserved.add(first_port.tag)

        for factory in buildings:
            if factory.type_id != U.FACTORY or not factory.is_idle or factory.tag in reserved:
                continue
            if factory.add_on_tag in labs and (
                tank_count <= max(5, (marine + marauder) // 3) or self.vespene >= 450
            ):
                tank_count += self._train(factory, U.SIEGETANK)
            elif factory.add_on_tag not in labs and self.minerals >= (100 if stressed else 300):
                self._train(factory, U.HELLION)

        for port in ports:
            if port.tag in reserved:
                continue
            if vikings < viking_goal and (medivacs >= 1 or self.air_count >= 3):
                vikings += self._train(port, U.VIKINGFIGHTER)
            elif medivacs < medivac_goal:
                medivacs += self._train(port, U.MEDIVAC)

        for barrack in infantry_queues:
            if barrack.tag in reserved:
                continue
            if len(barrack.orders) >= (2 if barrack.add_on_tag in reactors else 1):
                continue
            if (
                barrack.add_on_tag in labs
                and not air_alert
                and marine >= 8
                and marauder * 3 < marine
                and (tank_count >= 2 or self.vespene >= 150)
                and self._train(barrack, U.MARAUDER)
            ):
                marauder += 1
            elif (
                not self.scout_used
                and not stressed
                and self.now < 185
                and marine >= 2
                and self.vespene >= 75
                and not self.already_pending(U.REAPER)
                and self._train(barrack, U.REAPER)
            ):
                self.scout_used = True
            else:
                marine += self._train(barrack, U.MARINE)

    def _upgrades(self):
        for upgrade in (G.STIMPACK, G.SHIELDWALL, G.PUNISHERGRENADES):
            if self.already_pending_upgrade(upgrade) < 1:
                if self.already_pending_upgrade(upgrade) == 0:
                    self.research(upgrade)
                break
        for level in (
            (G.TERRANINFANTRYWEAPONSLEVEL1, G.TERRANINFANTRYARMORSLEVEL1),
            (G.TERRANINFANTRYWEAPONSLEVEL2, G.TERRANINFANTRYARMORSLEVEL2),
            (G.TERRANINFANTRYWEAPONSLEVEL3, G.TERRANINFANTRYARMORSLEVEL3),
        ):
            for upgrade in level:
                progress = self.already_pending_upgrade(upgrade)
                if progress < 1:
                    if progress == 0:
                        self.research(upgrade)
                    return

    def _labor(self):
        bases = self.townhalls.ready.filter(lambda h: not h.is_flying)
        if not bases:
            # Mining is still possible while a replacement hall is being built.
            home = self._home()
            minerals = sorted(self.mineral_field, key=lambda m: (m.distance_to_squared(home), m.tag))[:8]
        else:
            minerals = [
                m
                for m in self.mineral_field
                if m.mineral_contents
                and any(m.distance_to_squared(b) < 10**2 for b in bases)
                and not any(e.can_attack_ground and e.distance_to_squared(m) < 7**2 for e in self.raiders)
            ]
        gases = [
            r
            for r in self.gas_buildings.ready
            if r.vespene_contents
            and any(r.distance_to_squared(b) < 11**2 for b in bases)
            and not any(e.can_attack_ground and e.distance_to_squared(r) < 7**2 for e in self.raiders)
        ]
        gas_tags = {g.tag for g in gases}
        sites = {m.tag: m for m in minerals + gases}
        assignment = Counter()
        workers = sorted(self.workers, key=lambda w: w.tag)
        self.labor_targets = {
            tag: target for tag, target in self.labor_targets.items() if tag in self.workers.tags
        }
        for worker in workers:
            if worker.tag in self.workers_busy or not worker.is_collecting:
                self.labor_targets.pop(worker.tag, None)
                continue
            if worker.is_gathering and isinstance(worker.order_target, int):
                self.labor_targets[worker.tag] = worker.order_target
            target = self.labor_targets.get(worker.tag)
            if target in sites:
                assignment[target] += 1
        for worker in workers:
            if worker.tag in self.workers_busy or not (worker.is_collecting or worker.is_idle):
                continue
            current = self.labor_targets.get(worker.tag)
            if worker.is_carrying_resource and not worker.is_idle:
                continue
            gas_slots = [r for r in gases if assignment[r.tag] < 3]
            # Keep early mineral income if a rush has killed much of the workforce.
            if gas_slots and current not in gas_tags and len(workers) >= 13 and self.vespene < 650:
                target = min(gas_slots, key=lambda g: (worker.distance_to_squared(g), g.tag))
            elif current in sites and assignment[current] <= (3 if current in gas_tags else 2):
                continue
            elif minerals:
                target = min(
                    minerals,
                    key=lambda m: (max(0, assignment[m.tag] - 1), worker.distance_to_squared(m), m.tag),
                )
            else:
                continue
            if target.tag == current:
                continue
            if current in sites:
                assignment[current] -= 1
            self.do(worker.gather(target))
            self.labor_targets[worker.tag] = target.tag
            assignment[target.tag] += 1
            self.workers_busy.add(worker.tag)

        for mule in self.units(U.MULE).idle:
            if minerals:
                target = min(minerals, key=lambda m: (m.distance_to_squared(mule), m.tag))
                self.do(mule.gather(target))

    def _repair(self):
        if self.minerals < 40:
            return
        targets = self.townhalls.ready | self.structures(U.BUNKER).ready | self.units(TANKS)
        damaged = [
            u
            for u in targets
            if u.health_percentage < 0.68
            and self._near_base(u, 12)
            and not any(e.can_attack_ground and e.distance_to_squared(u) < 7**2 for e in self.raiders)
        ]
        if not damaged:
            return
        target = min(damaged, key=lambda u: (u.health_percentage, u.tag))
        if any(w.is_repairing and w.order_target == target.tag for w in self.workers):
            return
        worker = self._build_worker(target)
        if worker and worker.distance_to_squared(target) < 15**2:
            self.do(worker.repair(target))
            self.workers_busy.add(worker.tag)

    def _walkable(self, point, fallback):
        area = self.game_info.playable_area
        for dx, dy in ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2), (3, 3), (-3, -3)):
            p = offset(point, dx, dy)
            if area.x <= p.x < area.right and area.y <= p.y < area.top and self.in_pathing_grid(p):
                return p
        return fallback

    def _rally(self):
        home = self._home()
        center = self.game_info.map_center
        bases = self.townhalls.ready.filter(lambda h: not h.is_flying).closer_than(24, home)
        front = min(bases, key=lambda h: (h.distance_to_squared(center), h.tag)).position if bases else home
        return self._walkable(front.towards(center, 7), front)

    def _defense_point(self):
        if not self.raiders:
            return self._rally()

        # A cluster near a town hall takes precedence over a single passing unit.
        def urgency(point):
            return sum(weight(e) * max(0, 18 - e.distance_to(point)) for e in self.raiders)

        base = min(self.zones, key=lambda p: (-urgency(p), p))
        intruder = min(self.raiders, key=lambda e: (e.distance_to_squared(base), e.tag))
        return self._walkable(base.towards(intruder, min(7, base.distance_to(intruder))), base)

    def _issue(self, unit, ability, target=None, *, repeat=2.6):
        signature = (ability, getattr(target, "tag", target))
        previous = self.commands.get(unit.tag)
        if previous and previous[0] == signature and self.now - previous[1] < repeat:
            return
        if self.do(unit(ability, target)):
            self.commands[unit.tag] = (signature, self.now)

    def _scout(self):
        scouts = self.units(U.REAPER)
        if self.now >= 300 or not scouts:
            self.scout_tag = None
            return
        scout = min(scouts, key=lambda u: u.tag)
        self.scout_tag = scout.tag
        nearby = [u for u in self.hostiles if u.can_attack_ground and u.distance_to_squared(scout) < 8**2]
        if nearby:
            closest = min(nearby, key=lambda u: (u.distance_to_squared(scout), u.tag))
            safe = self._walkable(scout.position.towards(closest, -6), self._rally())
            self._issue(scout, A.MOVE, safe)
        elif scout.health_percentage < 0.55 or self._pressured():
            self._issue(scout, A.MOVE, self._defense_point())
        else:
            point = min(
                self.last_seen,
                key=lambda p: (
                    self.last_seen[p] > self.now - 40,
                    p != self.enemy_start,
                    scout.distance_to_squared(p),
                    self.last_seen[p],
                    p,
                ),
            )
            self._issue(scout, A.MOVE, point)

    def _attack_goal(self, center):
        seen = [e for e in self.visible if e.is_structure]
        if seen or self.enemy_sites:
            self.sweep_goal = None
            if seen:
                return min(
                    seen, key=lambda e: (e.distance_to_squared(center) + (90 if e.is_flying else 0), e.tag)
                ).position
            return min(self.enemy_sites.values(), key=lambda p: (point_distance_squared(p, center), p))

        goal = self.sweep_goal
        if goal is not None:
            distance = center.distance_to(goal)
            if distance <= self.sweep_distance - 3:
                self.sweep_distance = distance
                self.sweep_progress_at = self.now
            covered = self.is_visible(goal)
            if not covered and self.now - self.sweep_progress_at < 18:
                return goal
            self.sweep_retry[goal] = self.now + (90 if covered else 45)

        candidates = [
            p
            for p, t in self.last_seen.items()
            if t < self.now - 6
            and self.now >= self.sweep_retry.get(p, 0)
            and not self.is_visible(p)
            and not any(h.distance_to_squared(p) < 8**2 for h in self.townhalls)
        ]
        self.sweep_goal = min(
            candidates,
            key=lambda p: (
                p != self.enemy_start or self.last_seen[p] >= self.now - 75,
                point_distance_squared(p, center) + 6 * max(-180, self.last_seen[p] - self.now),
                p,
            ),
            default=None,
        )
        if self.sweep_goal is None:
            return center
        self.sweep_distance = center.distance_to(self.sweep_goal)
        self.sweep_progress_at = self.now
        return self.sweep_goal

    def _reinforcement_rally(self, center, ground):
        point = self.reinforce_point
        if point is None or (
            self.now - self.reinforce_updated_at >= 3 and point_distance_squared(center, point) >= 6**2
        ):
            anchor = min(ground, key=lambda u: (u.distance_to_squared(center), u.tag))
            self.reinforce_point = self._walkable(center, anchor.position)
            self.reinforce_updated_at = self.now
        return self.reinforce_point

    @staticmethod
    def _trailing(unit, center, goal):
        if unit.is_flying or unit.distance_to_squared(center) <= 18**2:
            return False
        x, y = goal.x - center.x, goal.y - center.y
        length = hypot(x, y)
        if length < 4:
            return unit.distance_to_squared(goal) > 18**2
        progress = (unit.position.x - center.x) * x + (unit.position.y - center.y) * y
        return progress < -6 * length

    def _regroup(self):
        self.mode = "regroup"
        self.regroup_until = self.now + 24
        self.attack_destination = None
        self.sweep_goal = None
        self.reinforce_point = None

    @staticmethod
    def _under_siege(point, siege):
        return any(tank.distance_to_squared(point) <= SIEGE_BUFFER**2 for tank in siege)

    def _siege_fallback(self, rally, siege):
        closest = min(siege, key=lambda tank: (tank.distance_to_squared(rally), tank.tag)).position
        home = self._home()
        area = self.game_info.playable_area
        away_x, away_y = rally.x - closest.x, rally.y - closest.y
        # Search on the friendly side of the line rather than walking through enemy splash.
        for distance in (0, 4, 8, 12, 16, 20):
            candidates = (
                offset(rally, dx * distance, dy * distance)
                for dx, dy in ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))
            )
            safe = [
                p
                for p in candidates
                if area.x <= p.x < area.right
                and area.y <= p.y < area.top
                and (p.x - rally.x) * away_x + (p.y - rally.y) * away_y >= 0
                and not self._under_siege(p, siege)
                and all(point_distance_squared(p, base) >= 3.5**2 for base in self.zones)
                and self.in_pathing_grid(p)
            ]
            if safe:
                return min(
                    safe, key=lambda p: (point_distance_squared(p, rally), point_distance_squared(p, home), p)
                )
        return self._rally()

    def _set_mode(self, force, local, opposition, pressured, cohesion, scaled):
        if self.mode == "push":
            self.push_peak = max(self.push_peak, force)
            if (
                pressured
                or force < self.push_peak * 0.58
                or (self.now - self.push_started > 8 and opposition >= 6 and local < opposition * 0.60)
            ):
                self._regroup()
        elif (
            not pressured
            and cohesion
            and (
                (self.mode == "hold" or self.now >= self.regroup_until)
                and (scaled or force >= (40 if self.now < 360 else 34) or self.supply_used >= 178)
            )
        ):
            self.mode = "push"
            self.push_started = self.now
            self.push_peak = force

    def _combat(self):
        army = sorted((u for u in self.units(ARMY) if u.tag != self.scout_tag), key=lambda u: u.tag)
        fighters = [u for u in army if u.type_id != U.MEDIVAC]
        ground = [u for u in fighters if not u.is_flying]
        if not fighters:
            if self.mode == "push":
                self._regroup()
            self._garrison([])
            return
        front = ground or fighters
        center = Point2((median(u.position.x for u in front), median(u.position.y for u in front)))
        local = [u for u in fighters if u.distance_to_squared(center) <= 17**2]
        opposition = [u for u in self.hostiles if u.distance_to_squared(center) <= 17**2]
        cohesion = (
            len(local) >= max(1, len(fighters) * 0.65)
            and point_distance_squared(center, self._rally()) < 23**2
        )
        scaled = (
            sum(u.type_id in TANKS for u in local) >= 3
            and sum(u.type_id in BIO for u in local) >= 14
            and sum(u.type_id == U.MEDIVAC for u in army) >= 2
        )
        pressed = self._pressured()
        self._set_mode(
            sum(map(weight, fighters)),
            sum(map(weight, local)),
            sum(map(weight, opposition)),
            pressed,
            cohesion,
            scaled,
        )

        rally = self._defense_point() if pressed else self._rally()
        pushing = self.mode == "push"
        self.attack_destination = self._attack_goal(center) if pushing else None
        goal = self.attack_destination if pushing else rally
        reinforce = self._reinforcement_rally(center, ground) if pushing and ground else center
        siege = [u for u in self.raiders if u.type_id == U.SIEGETANKSIEGED]
        bound = self._garrison(army, siege=siege)
        frontline = [u for u in ground if u.tag not in bound]
        fallback = self._siege_fallback(rally, siege) if siege else None
        support = [u for u in frontline if not self._under_siege(u.position, siege)] if siege else frontline
        intruder = min(self.raiders, key=lambda e: (e.distance_to_squared(rally), e.tag)) if pressed else None

        for unit in army:
            if unit.tag in bound:
                continue
            nearby = [e for e in self.visible if e.distance_to_squared(unit) < 15**2]
            if fallback is not None and not unit.is_flying and unit.type_id not in TANKS:
                if self._under_siege(unit.position, siege):
                    self._issue(unit, A.MOVE, fallback)
                else:
                    self._fighter(unit, nearby, fallback, center, hold_position=True)
                continue
            destination = reinforce if pushing and self._trailing(unit, center, goal) else goal
            if (
                intruder is not None
                and unit.type_id not in TANKS
                and intruder.can_be_attacked
                and (unit.can_attack_air if intruder.is_flying else unit.can_attack_ground)
            ):
                destination = intruder.position
            if unit.type_id in TANKS:
                self._tank(unit, nearby, destination, frontline)
            elif unit.type_id == U.MEDIVAC:
                self._medivac(unit, support, center, hold=fallback)
            else:
                self._fighter(unit, nearby, destination, center)

    def _tank(self, tank, nearby, destination, frontline):
        targets = [
            e
            for e in nearby
            if not e.is_flying and e.can_be_attacked and (e.type_id not in WORKERS or self._near_base(e, 12))
        ]
        firing_window = [e for e in targets if 3**2 <= tank.distance_to_squared(e) <= 13.5**2]
        target = min(firing_window, key=lambda e: (e.distance_to_squared(tank), e.tag), default=None)
        if target is not None:
            self.siege_contact[tank.tag] = self.now
        if tank.type_id == U.SIEGETANKSIEGED:
            expired = self.now - self.siege_contact.get(tank.tag, -inf) > 2.5
            withdrawing = self.mode == "regroup" and not self._pressured()
            cramped = expired and any(e.distance_to_squared(tank) < 3**2 for e in targets)
            if cramped or (tank.distance_to_squared(destination) > 5**2 and (expired or withdrawing)):
                self._issue(tank, A.UNSIEGE_UNSIEGE)
            return
        # A moving tank may anchor this group when infantry or another tank is near it.
        screened = sum(u.distance_to_squared(tank) < 9**2 for u in frontline) >= 3
        withdrawing = self.mode == "regroup" and not self._pressured()
        if (
            target is not None
            and 4.5**2 <= tank.distance_to_squared(target) <= 13**2
            and (screened or self._pressured())
            and not withdrawing
        ):
            self._issue(tank, A.SIEGEMODE_SIEGEMODE)
            return
        self._fighter(tank, nearby, destination, destination)

    def _fighter(self, unit, nearby, destination, center, *, hold_position=False):
        enemies = [
            e
            for e in nearby
            if e.can_be_attacked
            and not e.is_hallucination
            and (unit.can_attack_air if e.is_flying else unit.can_attack_ground)
        ]
        in_range = [e for e in enemies if unit.target_in_range(e, bonus_distance=0.35)]
        nearest = min(
            (
                e
                for e in nearby
                if self._dangerous(e) and (e.can_attack_air if unit.is_flying else e.can_attack_ground)
            ),
            key=lambda e: (e.distance_to_squared(unit), e.tag),
            default=None,
        )
        withdrawing = self.mode == "regroup" and not self._pressured()
        if (
            nearest is not None
            and nearest.distance_to_squared(unit) < 9**2
            and (withdrawing or unit.health_percentage < 0.34)
        ):
            point = destination if withdrawing or hold_position else self._rally()
            self._issue(unit, A.MOVE, point, repeat=0.65)
            return
        if in_range:
            target = min(
                in_range,
                key=lambda e: (
                    not self._dangerous(e),
                    e.health + e.shield,
                    e.distance_to_squared(unit),
                    e.tag,
                ),
            )
            if unit.type_id in BIO and unit.health_percentage > 0.62 and G.STIMPACK in self.state.upgrades:
                buff, stim = (
                    (B.STIMPACK, A.EFFECT_STIM_MARINE)
                    if unit.type_id == U.MARINE
                    else (B.STIMPACKMARAUDER, A.EFFECT_STIM_MARAUDER)
                )
                if self._dangerous(target) and not unit.has_buff(buff):
                    self._issue(unit, stim, repeat=1.5)
                    return
            if (
                nearest
                and not hold_position
                and not unit.is_flying
                and unit.type_id in BIO
                and unit.weapon_cooldown > 2
                and nearest.ground_range < 2.5
                and unit.distance_to_squared(nearest) < 3.2**2
            ):
                self._issue(
                    unit,
                    A.MOVE,
                    self._walkable(unit.position.towards(nearest, -2.7), self._rally()),
                    repeat=0.5,
                )
            else:
                self._issue(unit, A.ATTACK, target, repeat=0.75)
            return
        if unit.type_id == U.VIKINGFIGHTER:
            air = min(enemies, key=lambda e: (e.distance_to_squared(unit), e.tag), default=None)
            if air is not None:
                self._issue(unit, A.ATTACK, air)
            else:
                self._issue(unit, A.MOVE, center.towards(self._rally(), 4))
        elif hold_position:
            if unit.distance_to_squared(destination) > 2.5**2:
                self._issue(unit, A.MOVE, destination)
            else:
                self._issue(unit, A.HOLDPOSITION)
        elif unit.distance_to_squared(destination) > 2.5**2 or (enemies and self.mode == "push"):
            self._issue(unit, A.MOVE if withdrawing else A.ATTACK, destination)

    def _medivac(self, medic, frontline, center, *, hold=None):
        wounded = [
            u
            for u in frontline
            if u.type_id in BIO | {U.REAPER}
            and u.health_max - u.health >= 4
            and u.distance_to_squared(medic) <= 7**2
        ]
        danger = [e for e in self.hostiles if e.can_attack_air and e.distance_to_squared(medic) < 7**2]
        if danger and medic.health_percentage < 0.56:
            self._issue(medic, A.MOVE, hold if hold is not None else self._rally())
        elif wounded and medic.energy >= 3:
            target = min(wounded, key=lambda u: (u.health_percentage, u.distance_to_squared(medic), u.tag))
            self._issue(medic, A.MEDIVACHEAL_HEAL, target, repeat=0.9)
        else:
            anchor = center if frontline else self._rally()
            trail = (
                hold
                if hold is not None
                else anchor.towards(self._rally(), min(4, anchor.distance_to(self._rally())))
            )
            if medic.distance_to_squared(trail) > 3**2:
                self._issue(medic, A.MOVE, trail)

    def _garrison(self, army, *, siege=()):
        bound = set()
        for bunker in sorted(self.structures(U.BUNKER).ready, key=lambda u: u.tag):
            nearby = [e for e in self.raiders if e.distance_to_squared(bunker) < 14**2]
            if not nearby:
                if bunker.cargo_used and self.now - self.last_pressure > 8:
                    self._issue(bunker, A.UNLOADALL_BUNKER)
                continue
            if bunker.health_percentage < 0.18 and bunker.cargo_used:
                self._issue(bunker, A.UNLOADALL_BUNKER)
                continue
            if bunker.cargo_left > 0 and not self._under_siege(bunker.position, siege):
                candidates = [
                    u for u in army if u.type_id == U.MARINE and u.distance_to_squared(bunker) < 8**2
                ]
                if candidates:
                    marine = min(candidates, key=lambda u: (u.distance_to_squared(bunker), u.tag))
                    self._issue(bunker, A.LOAD_BUNKER, marine, repeat=1)
                    bound.add(marine.tag)
        return bound

    def _protect_workers(self):
        for worker in sorted(self.workers, key=lambda w: w.tag):
            if worker.tag in self.workers_busy:
                continue
            nearby = [e for e in self.raiders if e.can_attack_ground and e.distance_to_squared(worker) < 6**2]
            if not nearby:
                continue
            if worker.health_percentage > 0.6 and len(nearby) < 2 and worker.is_constructing_scv:
                continue
            safe = [
                m
                for m in self.mineral_field
                if m.mineral_contents
                and m.distance_to_squared(worker) < 26**2
                and any(m.distance_to_squared(h) < 10**2 for h in self.townhalls.ready)
                and not any(e.can_attack_ground and e.distance_to_squared(m) < 8**2 for e in self.raiders)
            ]
            if safe:
                target = min(safe, key=lambda m: (m.distance_to_squared(worker), m.tag))
                self._issue(worker, A.HARVEST_GATHER_SCV, target, repeat=1.2)
            else:
                nearest = min(nearby, key=lambda e: (e.distance_to_squared(worker), e.tag))
                point = self._walkable(worker.position.towards(nearest, -5), self._rally())
                self._issue(worker, A.MOVE, point, repeat=1)
            self.workers_busy.add(worker.tag)

    def _orbital_energy(self):
        orbitals = sorted(
            (u for u in self.structures(U.ORBITALCOMMAND).ready if u.energy >= 50),
            key=lambda u: (-u.energy, u.tag),
        )
        if not orbitals:
            return
        # Normal vision can reveal a cloaked or burrowed contact without making it targetable.
        suspect = min(
            (
                u
                for u in self.hostiles
                if not u.can_be_attacked
                and (self._near_base(u, 19) or self.mode == "push")
                and not any(u.distance_to_squared(p) < 10**2 for p in self.scan_positions)
            ),
            key=lambda u: (not self._near_base(u, 19), u.distance_to_squared(self._home()), u.tag),
            default=None,
        )
        suspected_point = self._unseen_scan_point()
        if suspect is not None and self.now - self.last_scan > 2:
            self._scan(orbitals.pop(0), suspect.position)
        elif suspected_point is not None:
            self._scan(orbitals.pop(0), suspected_point)
        elif (
            self.attack_destination is not None
            and self.now - self.last_scan > 65
            and orbitals[0].energy >= 100
            and not self.is_visible(self.attack_destination)
        ):
            self._scan(orbitals.pop(0), self.attack_destination)

        reserve = 50 if self.now >= 255 or self.now - self.last_cloak < 120 else 0
        if not orbitals or (len(orbitals) == 1 and orbitals[0].energy < 50 + reserve):
            return
        minerals = [
            m
            for m in self.mineral_field
            if m.mineral_contents >= 75
            and any(m.distance_to_squared(h) < 10**2 for h in self.townhalls.ready)
            and not any(e.can_attack_ground and e.distance_to_squared(m) < 8**2 for e in self.raiders)
        ]
        if not minerals:
            return
        keep = reserve
        for orbital in orbitals:
            if orbital.energy >= 50 + keep:
                patch = min(
                    minerals, key=lambda m: (m.distance_to_squared(orbital), -m.mineral_contents, m.tag)
                )
                self._issue(orbital, A.CALLDOWNMULE_CALLDOWNMULE, patch, repeat=4)
            keep = 0

    def _unseen_scan_point(self):
        if self.unseen_damage and self.now - self.last_scan > 8:
            point, time = self.unseen_damage
            if self.now - time < 4 and not any(
                point_distance_squared(p, point) < 10**2 for p in self.scan_positions
            ):
                return point
        return None

    def _scan(self, orbital, point):
        self._issue(orbital, A.SCANNERSWEEP_SCAN, point)
        self.last_scan = self.now
        self.scan_positions[point] = self.now
