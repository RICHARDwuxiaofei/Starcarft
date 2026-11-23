"""Mizuki: original Terran bio-mech for private SC2 API matches."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as Up
from sc2.position import Point2
from sc2.unit import Unit

from .combat import control_combat
from .policy import (
    Signals,
    air_targets,
    desired_workers,
    expansion_allowed,
    supply_order_count,
)

WORKER_TYPES = {U.SCV, U.PROBE, U.DRONE, U.MULE}
CLOAK_HINTS = {
    U.DARKSHRINE,
    U.DARKTEMPLAR,
    U.BANSHEE,
    U.LURKERMP,
    U.LURKERMPBURROWED,
    U.LURKERDENMP,
    U.WIDOWMINE,
    U.WIDOWMINEBURROWED,
    U.GHOST,
}
RANGED_AIR = {
    U.VIKINGFIGHTER,
    U.BANSHEE,
    U.BATTLECRUISER,
    U.LIBERATOR,
    U.VOIDRAY,
    U.CARRIER,
    U.TEMPEST,
    U.PHOENIX,
    U.ORACLE,
    U.MUTALISK,
    U.CORRUPTOR,
    U.BROODLORD,
}
WEAPONS = (
    Up.TERRANINFANTRYWEAPONSLEVEL1,
    Up.TERRANINFANTRYWEAPONSLEVEL2,
    Up.TERRANINFANTRYWEAPONSLEVEL3,
)
ARMOR = (
    Up.TERRANINFANTRYARMORSLEVEL1,
    Up.TERRANINFANTRYARMORSLEVEL2,
    Up.TERRANINFANTRYARMORSLEVEL3,
)
ADDON_OFFSET = Point2((2.5, -0.5))


@dataclass
class Contact:
    position: Point2
    stamp: float


def visible(units: Iterable[Unit]) -> list[Unit]:
    return [u for u in units if u.is_visible and not u.is_snapshot]


class CompetitiveBot(BotAI):
    """SC2 Arena entry point. Only API-visible observations inform enemy adaptation."""

    def __init__(self) -> None:
        super().__init__()
        self._scout_tag: int | None = None
        self._scout_finished = False
        self._scout_route: list[Point2] = []
        self._scout_ordered = -99.0
        self._next_build: dict[U, float] = {}
        self._blocked_expansions: dict[Point2, float] = {}
        self._queued_upgrades: set[Up] = set()
        self._seen_air: dict[int, tuple[float, float]] = {}
        self._armor_until = 0.0
        self._cloak_until = 0.0
        self._scan_last = -99.0
        self._damaged_workers: dict[int, float] = {}
        self._scan_contact: Contact | None = None

    async def on_start(self) -> None:
        self.client.game_step = 4
        starts = sorted(self.enemy_start_locations, key=lambda p: (p.x, p.y))
        self._scout_route = starts.copy()
        for p in starts:
            candidates = [
                e
                for e in self.expansion_locations_list
                if 8 < e.distance_to(p) < 32 and e.distance_to(self.start_location) > 20
            ]
            self._scout_route.extend(
                sorted(candidates, key=lambda e: (e.distance_to(p), e.x, e.y))[:2]
            )

    async def on_step(self, iteration: int) -> None:
        contacts = visible(self.enemy_units)
        structures = visible(self.enemy_structures)
        state = self._signals(contacts, structures)

        if iteration % 12 == 0:
            await self.distribute_workers(
                resource_ratio=2.4 if self.time < 230 else 1.8
            )
        if iteration % 5 == 0:
            self._scout(contacts)
            self._orbitals(contacts, state)
            self._depot_control(contacts)
        if iteration % 3 == 0:
            await self._macro(state)
        await control_combat(self, iteration)

    def _signals(self, contacts: list[Unit], structures: list[Unit]) -> Signals:
        now = self.time
        for u in contacts:
            if u.type_id in RANGED_AIR or (
                u.is_flying and u.can_attack and u.type_id not in WORKER_TYPES
            ):
                self._seen_air[u.tag] = (
                    now,
                    2.3 if u.type_id in {U.CARRIER, U.BATTLECRUISER, U.TEMPEST} else 1,
                )
        self._seen_air = {
            tag: val for tag, val in self._seen_air.items() if now - val[0] <= 55
        }
        if sum(u.is_armored for u in contacts if u.can_attack and not u.is_flying) >= 3:
            self._armor_until = now + 50
        if any(
            u.type_id in CLOAK_HINTS or u.is_cloaked or u.is_burrowed
            for u in contacts + structures
        ):
            self._cloak_until = now + 100
        bases = list(self.townhalls)
        positions = [b.position for b in bases] or [self.start_location]
        nearby = [u for u in contacts if any(u.distance_to(p) < 21 for p in positions)]
        pressure = sum(
            max(1.0, self.calculate_supply_cost(u.type_id))
            for u in nearby
            if u.can_attack and u.type_id not in WORKER_TYPES
        )
        pressure += max(0, sum(u.type_id in WORKER_TYPES for u in nearby) - 2)
        pressure += 7 * sum(
            u.can_attack_ground and any(u.distance_to(p) < 20 for p in positions)
            for u in structures
        )
        return Signals(
            now,
            len(bases),
            self.supply_army,
            pressure,
            sum(weight for _, weight in self._seen_air.values()),
            3 if now < self._armor_until else 0,
            now < self._cloak_until,
        )

    def _scout(self, contacts: list[Unit]) -> None:
        reapers = sorted(self.units(U.REAPER), key=lambda u: u.tag)
        if not reapers:
            self._scout_tag = None
            return
        scout = next((u for u in reapers if u.tag == self._scout_tag), reapers[0])
        self._scout_tag = scout.tag
        threats = [
            u
            for u in contacts
            if not u.is_flying and u.can_attack_ground and u.distance_to(scout) < 11
        ]
        if threats and (scout.health_percentage < 0.6 or len(threats) >= 2):
            scout.move(self.start_location)
            self._scout_ordered = self.time
            return
        if self.time < 490 and self._scout_route:
            target = self._scout_route[0]
            if scout.distance_to(target) < 6:
                self._scout_route.pop(0)
                self._scout_ordered = -99.0
            if self._scout_route and (
                scout.is_idle or self.time - self._scout_ordered >= 5
            ):
                scout.move(self._scout_route[0])
                self._scout_ordered = self.time
        elif scout.is_idle and scout.distance_to(self.start_location) > 9:
            scout.move(self.start_location)

    def _orbitals(self, contacts: list[Unit], s: Signals) -> None:
        orbitals = sorted(
            self.structures(U.ORBITALCOMMAND).ready, key=lambda h: (-h.energy, h.tag)
        )
        if not orbitals:
            return
        bases = list(self.townhalls)
        hidden = [
            e
            for e in contacts
            if (e.is_cloaked or e.is_burrowed)
            and any(e.distance_to(h) < 20 for h in bases)
        ]
        if hidden:
            self._scan_contact = Contact(hidden[0].position, self.time)
        # A local burst of damage with no visible attacker is Scan evidence, not omniscience.
        current = {w.tag: w.health for w in self.workers}
        wounded = [
            w
            for w in self.workers
            if self._damaged_workers.get(w.tag, w.health) - w.health >= 12
            and not any(e.can_attack_ground and e.distance_to(w) < 13 for e in contacts)
        ]
        if wounded and (s.detection_risk or len(wounded) >= 2):
            self._scan_contact = Contact(wounded[0].position, self.time)
            self._cloak_until = max(self._cloak_until, self.time + 35)
        self._damaged_workers = current
        scan = self._scan_contact
        if scan and self.time - scan.stamp <= 8 and self.time - self._scan_last > 12:
            for orbital in orbitals:
                if orbital.energy >= 50 and orbital(A.SCANNERSWEEP_SCAN, scan.position):
                    self._scan_last = self.time
                    self._scan_contact = None
                    break
        for orbital in orbitals:
            if orbital.tag in self.unit_tags_received_action or orbital.energy < (
                100 if s.detection_risk else 50
            ):
                continue
            patches = [
                m
                for m in self.mineral_field.closer_than(10, orbital)
                if m.mineral_contents > 100
            ]
            if patches:
                mineral = max(
                    patches,
                    key=lambda m: (m.mineral_contents, -m.distance_to(orbital), -m.tag),
                )
                orbital(A.CALLDOWNMULE_CALLDOWNMULE, mineral)

    def _depot_control(self, contacts: list[Unit]) -> None:
        ramp = self.main_base_ramp.top_center
        for depot in self.structures({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED}).ready:
            threat = depot.distance_to(ramp) < 12 and any(
                e.can_attack_ground
                and not e.is_flying
                and e.type_id not in WORKER_TYPES
                and e.distance_to(depot) < 8
                for e in contacts
            )
            if threat and depot.type_id == U.SUPPLYDEPOTLOWERED:
                depot(A.MORPH_SUPPLYDEPOT_RAISE)
            elif not threat and depot.type_id == U.SUPPLYDEPOT:
                depot(A.MORPH_SUPPLYDEPOT_LOWER)

    def _pending(self, item: U) -> int:
        amount = int(self.already_pending(item))
        # Terran worker orders can disappear when the constructor is killed.
        # Count the unfinished building independently so it can be resumed.
        return max(amount, self.structures(item).not_ready.amount)

    def _plan_total(self, item: U) -> int:
        # already_pending counts structures under construction as well as en route orders.
        return self.structures(item).ready.amount + self._pending(item)

    def _train(
        self,
        building: Unit,
        kind: U,
        supply_paid: dict[str, float],
        *,
        queue: bool = False,
    ) -> bool:
        supply = self.calculate_supply_cost(kind)
        if supply > supply_paid["left"] or not self.can_afford(kind):
            return False
        if building.train(kind, queue=queue, can_afford_check=True):
            supply_paid["left"] -= supply
            return True
        return False

    async def _build(
        self, kind: U, near: Point2, *, radius: int = 9, addon: bool = False
    ) -> bool:
        if (
            self.time < self._next_build.get(kind, -1)
            or not self.can_afford(kind)
            or not self.workers
        ):
            return False
        # Bound placement RPCs and avoid spending another worker on a rejected attempt every observation.
        self._next_build[kind] = self.time + 2.0
        bases = [near]
        if addon:
            bases += [
                near.offset(Point2(offset))
                for offset in ((0, 5), (0, -5), (-5, 0), (5, 0))
            ]
        used_positions: set[Point2] = set()
        for anchor in bases:
            pos = await self.find_placement(
                kind, near=anchor, max_distance=radius, random_alternative=False
            )
            if pos is None or pos in used_positions:
                continue
            used_positions.add(pos)
            if addon and not await self.can_place_single(
                U.SUPPLYDEPOT, pos.offset(ADDON_OFFSET)
            ):
                continue
            worker = self.select_build_worker(pos)
            if worker is None or worker.tag in self.unit_tags_received_action:
                continue
            return bool(worker.build(kind, pos, can_afford_check=True))
        return False

    async def _gas(self, limit: int) -> bool:
        if (
            self._plan_total(U.REFINERY) >= limit
            or not self.can_afford(U.REFINERY)
            or self._pending(U.REFINERY)
        ):
            return False
        for hall in sorted(self.townhalls.ready, key=lambda h: h.tag):
            for gas in sorted(
                self.vespene_geyser.closer_than(10, hall), key=lambda g: g.tag
            ):
                if gas.vespene_contents <= 0 or self.gas_buildings.closer_than(1, gas):
                    continue
                worker = self.select_build_worker(gas.position)
                if (
                    worker is not None
                    and worker.tag not in self.unit_tags_received_action
                ):
                    return bool(worker.build_gas(gas, can_afford_check=True))
        return False

    async def _macro(self, s: Signals) -> None:
        self._queued_upgrades.clear()
        if not self.workers and not self.townhalls:
            return
        supply = {"left": self.supply_left}
        townhalls = sorted(self.townhalls.ready, key=lambda u: u.tag)
        ramp = self.main_base_ramp.top_center
        interior = self.start_location.towards(self.game_info.map_center, -7)
        depot_spots = sorted(
            self.main_base_ramp.corner_depots, key=lambda p: (p.x, p.y)
        )
        depots = self.structures({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED})
        rax = self.structures(U.BARRACKS)
        producers = (
            rax.ready.amount
            + self.structures(U.FACTORY).ready.amount
            + self.structures(U.STARPORT).ready.amount
        )
        build_depot = supply_order_count(
            self.supply_used,
            self.supply_left,
            self.supply_cap,
            self._pending(U.SUPPLYDEPOT),
            producers,
        )
        if build_depot:
            location = depot_spots[0] if not depots and depot_spots else interior
            await self._build(U.SUPPLYDEPOT, location, radius=10)
        if depots.ready and not self._plan_total(U.BARRACKS):
            await self._build(
                U.BARRACKS,
                self.main_base_ramp.barracks_correct_placement or ramp,
                addon=True,
            )
        if rax and self.time >= 38:
            gas_target = (
                1
                if not self.structures(U.FACTORY)
                else min(6, max(2, 2 * len(townhalls) - 1))
            )
            await self._gas(gas_target)

        for hall in townhalls:
            if not hall.is_idle or hall.tag in self.unit_tags_received_action:
                continue
            if (
                rax.ready
                and hall.type_id == U.COMMANDCENTER
                and self.workers.amount >= 15
                and self.can_afford(U.ORBITALCOMMAND)
            ):
                hall(
                    A.UPGRADETOORBITAL_ORBITALCOMMAND,
                    subtract_cost=True,
                    can_afford_check=True,
                )
            elif self.workers.amount + self._pending(U.SCV) < desired_workers(
                len(townhalls), self.gas_buildings.ready.amount
            ):
                self._train(hall, U.SCV, supply)

        expanding = expansion_allowed(s) and not self._pending(U.COMMANDCENTER)
        if (
            expanding
            and self.can_afford(U.COMMANDCENTER)
            and self.time >= self._next_build.get(U.COMMANDCENTER, 0)
        ):
            self._next_build[U.COMMANDCENTER] = self.time + 3.0
            await self._expand(townhalls)
        # Saving keeps the first command center from being starved by repeated 50-mineral orders.
        # Break the reserve under pressure or while expansion tiles are failing.
        bank_natural = (
            s.bases == 1
            and expanding
            and 110 <= self.time < 170
            and 5 <= self.supply_army < 16
            and s.local_pressure < 3
            and 200 <= self.minerals < 400
        )
        if not bank_natural:
            await self._tech(s, interior)
        await self._addons_and_army(s, supply, bank_natural)
        if s.local_pressure < 4:
            self._resume_construction()
        if s.local_pressure < 3 and self.time > 125:
            self._repair(townhalls)

    async def _expand(self, halls: list[Unit]) -> None:
        occupied = [h.position for h in self.townhalls]
        origin = halls[0].position if halls else self.start_location
        sites = sorted(
            self.expansion_locations_list,
            key=lambda p: (p.distance_to(origin), p.x, p.y),
        )
        for location in sites:
            if any(p.distance_to(location) < 12 for p in occupied):
                continue
            if self._blocked_expansions.get(location, 0) > self.time:
                continue
            # Query placement before moving an SCV; a blocked site is not a reason to stall macro.
            if not await self.can_place_single(U.COMMANDCENTER, location):
                self._blocked_expansions[location] = self.time + 22
                continue
            worker = self.select_build_worker(location)
            if worker is None or worker.tag in self.unit_tags_received_action:
                continue
            if not await self.client.query_pathing(worker.position, location):
                self._blocked_expansions[location] = self.time + 22
                continue
            if worker.build(U.COMMANDCENTER, location, can_afford_check=True):
                return

    async def _tech(self, s: Signals, near: Point2) -> None:
        rax = self.structures(U.BARRACKS)
        fac = self.structures(U.FACTORY)
        sp = self.structures(U.STARPORT)
        eb = self.structures(U.ENGINEERINGBAY)
        rax_target = min(
            10, 1 if self.time < 115 else (3 if s.bases <= 2 else s.bases * 2)
        )
        if s.local_pressure >= 5 and self.time < 190:
            rax_target = max(2, rax_target)
        fac_target = 2 if s.bases >= 3 and s.army_supply >= 35 else 1
        sp_target = 2 if s.bases >= 3 and (s.enemy_air >= 5 or self.time > 430) else 1

        readiness = (
            (U.FACTORY, rax.ready and s.time >= 99, 1, True),
            (U.STARPORT, fac.ready, 1, True),
            (U.BARRACKS, rax.ready, rax_target, True),
            (U.FACTORY, fac.ready, fac_target, True),
            (U.STARPORT, sp.ready, sp_target, True),
            (
                U.ENGINEERINGBAY,
                s.time >= 200 or s.detection_risk,
                2 if s.time >= 325 and s.bases >= 3 else 1,
                False,
            ),
            (U.ARMORY, eb.ready and s.time >= 320, 1, False),
        )
        for kind, ready, count, addon in readiness:
            if (
                ready
                and self._plan_total(kind) < count
                and await self._build(kind, near, addon=addon)
            ):
                return
        if (
            eb.ready
            and (s.detection_risk or s.enemy_air >= 4)
            and not self._pending(U.MISSILETURRET)
        ):
            for hall in sorted(self.townhalls.ready, key=lambda u: u.tag):
                if not self.structures(U.MISSILETURRET).closer_than(
                    10, hall
                ) and await self._build(
                    U.MISSILETURRET,
                    hall.position.towards(self.game_info.map_center, 6),
                    radius=6,
                ):
                    return

    def _research(self, lab: Unit, options: Iterable[Up]) -> bool:
        for up in options:
            if (
                up in self.state.upgrades
                or up in self._queued_upgrades
                or self.already_pending_upgrade(up)
            ):
                continue
            if self.can_afford(up) and lab.research(up, can_afford_check=True):
                self._queued_upgrades.add(up)
                return True
            break
        return False

    async def _addons_and_army(
        self, s: Signals, supply: dict[str, float], save: bool
    ) -> None:
        can_upgrade = self.structures(U.ARMORY).ready.exists
        for bay in sorted(
            self.structures(U.ENGINEERINGBAY).ready.idle, key=lambda u: u.tag
        ):
            for level in range(3 if can_upgrade else 1):
                chosen = False
                for up in (WEAPONS[level], ARMOR[level]):
                    chain = WEAPONS if up in WEAPONS else ARMOR
                    if level > 0 and chain[level - 1] not in self.state.upgrades:
                        continue
                    if self._research(bay, (up,)):
                        chosen = True
                        break
                if chosen:
                    break
        if not save:
            self._stim_and_support()
        tech_count = self._plan_total(U.BARRACKSTECHLAB)
        tech_wanted = 2 if s.enemy_armor or s.bases >= 3 else 1
        if s.time > 510 and s.enemy_armor:
            tech_wanted = 3
        med, vik = air_targets(s)
        kinds = (U.FACTORY, U.STARPORT, U.BARRACKS)
        viking_count = self.units(
            {U.VIKINGFIGHTER, U.VIKINGASSAULT}
        ).amount + self._pending(U.VIKINGFIGHTER)
        med_count = self.units(U.MEDIVAC).amount + self._pending(U.MEDIVAC)
        marauder_count = self.units(U.MARAUDER).amount + self._pending(U.MARAUDER)
        marine_count = self.units(U.MARINE).amount + self._pending(U.MARINE)
        for kind in kinds:
            for building in sorted(self.structures(kind).ready, key=lambda u: u.tag):
                if building.tag in self.unit_tags_received_action:
                    continue
                # An unfinished addon doesn't enable its production commands yet.
                if building.add_on_tag and not self.structures.ready.find_by_tag(
                    building.add_on_tag
                ):
                    continue
                queue_count = len(building.orders)
                if queue_count >= (2 if building.has_reactor else 1):
                    continue
                train: list[U] = []
                if kind == U.BARRACKS:
                    if (
                        not self._scout_finished
                        and not save
                        and s.local_pressure < 4
                        and s.time < 175
                        and self._train(building, U.REAPER, supply)
                    ):
                        self._scout_finished = True
                        continue
                    if (
                        not building.add_on_tag
                        and not save
                        and s.time > 95
                        and s.local_pressure < 5
                    ):
                        ability = (
                            A.BUILD_TECHLAB_BARRACKS
                            if tech_count < tech_wanted
                            else A.BUILD_REACTOR_BARRACKS
                        )
                        if await self._addon(building, ability):
                            tech_count += ability == A.BUILD_TECHLAB_BARRACKS
                            continue
                    if not save:
                        if (
                            building.has_techlab
                            and (s.enemy_armor or s.time >= 250)
                            and marauder_count
                            <= max(2, marine_count * (0.75 if s.enemy_armor else 0.40))
                        ):
                            train.append(U.MARAUDER)
                        train.append(U.MARINE)
                elif kind == U.FACTORY and not save:
                    if not building.add_on_tag:
                        await self._addon(building, A.BUILD_TECHLAB_FACTORY)
                    elif building.has_techlab:
                        train.append(U.SIEGETANK)
                elif kind == U.STARPORT and not save:
                    if (
                        not building.add_on_tag
                        and s.time >= 255
                        and med_count >= 1
                        and await self._addon(building, A.BUILD_REACTOR_STARPORT)
                    ):
                        continue
                    if (
                        vik
                        and viking_count < vik
                        and (med_count >= 1 or s.enemy_air >= 4)
                    ):
                        train.append(U.VIKINGFIGHTER)
                    if med_count < med:
                        train.append(U.MEDIVAC)
                for unit in train:
                    if self._train(building, unit, supply, queue=queue_count > 0):
                        viking_count += unit == U.VIKINGFIGHTER
                        med_count += unit == U.MEDIVAC
                        marauder_count += unit == U.MARAUDER
                        marine_count += unit == U.MARINE
                        break

    async def _addon(self, building: Unit, ability: A) -> bool:
        reactors = {A.BUILD_REACTOR_BARRACKS, A.BUILD_REACTOR_STARPORT}
        # The library explicitly corrects generic Tech Lab / Reactor costs; raw
        # ability lookup sometimes returns zero for the producer-specific ability.
        cost = self.calculate_cost(U.REACTOR if ability in reactors else U.TECHLAB)
        if self.minerals < cost.minerals or self.vespene < cost.vespene:
            return False
        if not await self.can_place_single(
            U.SUPPLYDEPOT, building.position.offset(ADDON_OFFSET)
        ):
            return False
        if not building(ability):
            return False
        self.minerals -= cost.minerals
        self.vespene -= cost.vespene
        return True

    def _stim_and_support(self) -> None:
        for lab in sorted(
            self.structures(U.BARRACKSTECHLAB).ready.idle, key=lambda u: u.tag
        ):
            options = (Up.STIMPACK, Up.COMBATSHIELD, Up.PUNISHERGRENADES)
            if self._research(lab, options):
                break

    def _resume_construction(self) -> None:
        abandoned = self.structures_without_construction_SCVs
        if not abandoned:
            return
        target = max(abandoned, key=lambda u: (u.build_progress, -u.tag))
        workers = [
            w
            for w in self.workers.closer_than(22, target)
            if (w.is_collecting or w.is_idle)
            and w.tag not in self.unit_tags_received_action
        ]
        if workers:
            min(workers, key=lambda w: (w.distance_to(target), w.tag)).smart(target)

    def _repair(self, halls: list[Unit]) -> None:
        if self.minerals < 25:
            return
        assets = halls + list(self.units({U.SIEGETANK, U.SIEGETANKSIEGED}))
        for asset in sorted(assets, key=lambda u: (u.health_percentage, u.tag))[:2]:
            if asset.health_percentage >= 0.7:
                continue
            near = [
                w
                for w in self.workers.closer_than(8, asset)
                if w.is_collecting and w.tag not in self.unit_tags_received_action
            ]
            if near and not any(
                w.is_repairing for w in self.workers.closer_than(7, asset)
            ):
                min(near, key=lambda w: (w.distance_to(asset), w.tag)).repair(asset)
