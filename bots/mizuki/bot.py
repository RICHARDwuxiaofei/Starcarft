"""Mizuki: original Terran bio-mech for private SC2 API matches."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sc2.bot_ai import BotAI
from sc2.game_info import Ramp
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as Up
from sc2.position import Point2
from sc2.unit import Unit

from .combat import control_combat
from .policy import (
    Signals,
    air_targets,
    desired_refineries,
    desired_workers,
    expansion_allowed,
    protect_natural,
    supply_order_count,
    surplus_gas,
)
from .trace import DecisionTrace

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

    def __init__(self, *, trace: DecisionTrace | None = None) -> None:
        super().__init__()
        self._trace = trace
        self._wall_anchors: (
            tuple[Point2 | None, Point2 | None, Point2 | None] | None
        ) = None
        self._reserved_addons: dict[Point2, float] = {}
        self._next_refinery_staff = 0.0
        self._natural_retry_at = 0.0
        self._expansion_intent: Contact | None = None
        self._support_retry_at = 0.0
        self._last_macro_mode: str | None = None
        self._scout_retreat_until = 0.0
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
        self._worker_sightings: dict[int, tuple[float, Point2]] = {}
        self._unexplained_hits: list[Contact] = []
        self._gas_worker_sightings: set[int] = set()
        self._scan_contact: Contact | None = None

    async def on_start(self) -> None:
        self.client.game_step = 4
        home = self.start_location
        starts = sorted(
            (p for p in self.enemy_start_locations if p.distance_to(home) > 8),
            key=lambda p: (p.distance_to(home), p.x, p.y),
        )
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

        if self._trace is not None:
            self._trace.start_completed = True

    async def on_step(self, iteration: int) -> None:
        trace = getattr(self, "_trace", None)
        try:
            await CompetitiveBot._play_frame(self, iteration)
        except Exception as error:
            if trace is not None:
                trace.record(
                    self.time,
                    "step_error",
                    iteration=iteration,
                    exception=type(error).__name__,
                )
                trace.last_error = {
                    "t": round(self.time, 2),
                    "iteration": iteration,
                    "type": type(error).__name__,
                }
            raise
        if trace is not None:
            trace.steps_completed += 1

    async def _play_frame(self, iteration: int) -> None:
        contacts = visible(self.enemy_units)
        structures = visible(self.enemy_structures)
        state = self._signals(contacts, structures)

        if iteration % 12 == 0:
            await self._distribute_economy()
        if iteration % 5 == 0:
            self._scout(contacts)
            self._orbitals(contacts, state)
            self._depot_control(contacts)
        if iteration % 3 == 0:
            await self._macro(state)
        await control_combat(self, iteration)
        trace = getattr(self, "_trace", None)
        if trace is not None and iteration % 2 == 0:
            self._sample_trace(trace, state)

    def _sample_trace(self, trace: DecisionTrace, state: Signals) -> None:
        combat = getattr(self, "_mizuki_combat_state", None)
        phase = combat.phase.name if combat is not None else "GATHER"
        trace.sample(
            self.time,
            phase,
            minerals=self.minerals,
            gas=self.vespene,
            workers=self.supply_workers,
            army_supply=self.supply_army,
            supply_used=self.supply_used,
            supply_cap=self.supply_cap,
            ready_bases=self.townhalls.ready.amount,
            visible_pressure_supply=round(state.local_pressure, 2),
            scouted_air_weight=round(state.enemy_air, 2),
            detection_risk=state.detection_risk,
            macro_mode=self._last_macro_mode,
        )

    def _trace_intent(self, event: str, **details) -> None:
        if self._trace is not None:
            self._trace.record(self.time, event, **details)

    def _anchors(self) -> tuple[Point2 | None, Point2 | None, Point2 | None]:
        if self._wall_anchors is None:
            top = None
            try:
                ramp: Ramp = self.main_base_ramp
                top = ramp.top_center
                if len(ramp.upper2_for_ramp_wall) != 2:
                    self._wall_anchors = (top, None, None)
                    return self._wall_anchors
                corners = sorted(ramp.corner_depots, key=lambda p: (p.x, p.y))
                self._wall_anchors = (
                    top,
                    corners[0] if corners else None,
                    ramp.barracks_correct_placement,
                )
            except (ValueError, ArithmeticError, AssertionError):
                # No ladder-shaped ramp or geometry too irregular for wall hints.
                self._wall_anchors = (top, None, None)
        return self._wall_anchors

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
            if self.time >= self._scout_retreat_until:
                self._trace_intent("reaper_retreat", threats=len(threats))
            self._scout_retreat_until = self.time + 8
            if scout.is_idle or self.time - self._scout_ordered >= 2:
                scout.move(self.start_location)
                self._scout_ordered = self.time
            return
        if self.time < self._scout_retreat_until:
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

    def _missing_worker_contact(
        self, contacts: list[Unit], bases: list[Unit], detection: bool
    ) -> Contact | None:
        now = self.time
        gas = tuple(getattr(self, "gas_buildings", ()))
        gas_tags = {g.tag for g in gas}
        prior_gassers = getattr(self, "_gas_worker_sightings", set())
        current = {w.tag: (w.health, w.position) for w in self.workers}
        self._gas_worker_sightings = {
            w.tag
            for w in self.workers
            if getattr(w, "order_target", None) in gas_tags
            or getattr(w, "is_carrying_vespene", False)
        }
        unexplained = [e for e in self._unexplained_hits if now - e.stamp <= 5]
        for tag, (health, old_position) in self._worker_sightings.items():
            latest = current.get(tag)
            if latest is not None and health - latest[0] < 12:
                continue
            if latest is None and (
                tag in prior_gassers
                or any(old_position.distance_to(g.position) <= 5 for g in gas)
            ):
                # Workers can disappear from raw observations while inside a refinery.
                continue
            p = latest[1] if latest is not None else old_position
            if any(p.distance_to(b.position) < 13 for b in bases) and not any(
                e.can_attack_ground and e.distance_to(p) < 13 for e in contacts
            ):
                unexplained.append(Contact(p, now))
        self._worker_sightings = current
        self._unexplained_hits = unexplained
        for hit in reversed(unexplained):
            if (
                detection
                or sum(
                    hit.position.distance_to(other.position) <= 9
                    for other in unexplained
                )
                >= 2
            ):
                self._unexplained_hits.clear()
                return hit
        return None

    def _orbitals(self, contacts: list[Unit], s: Signals) -> None:
        orbitals = sorted(
            self.structures(U.ORBITALCOMMAND).ready, key=lambda h: (-h.energy, h.tag)
        )
        bases = list(self.townhalls)
        damage = self._missing_worker_contact(contacts, bases, s.detection_risk)
        hidden = [
            e
            for e in contacts
            if (e.is_cloaked or e.is_burrowed)
            and any(e.distance_to(h) < 20 for h in bases)
        ]
        if hidden:
            self._scan_contact = Contact(hidden[0].position, self.time)
        elif damage and (self.time >= 120 or s.detection_risk):
            self._scan_contact = damage
            self._cloak_until = max(self._cloak_until, self.time + 35)
        if not orbitals:
            return
        scan = self._scan_contact
        if scan and self.time - scan.stamp <= 8 and self.time - self._scan_last > 12:
            for orbital in orbitals:
                if orbital.energy >= 50 and orbital(A.SCANNERSWEEP_SCAN, scan.position):
                    self._trace_intent(
                        "defensive_scan_queued",
                        x=round(scan.position.x, 1),
                        y=round(scan.position.y, 1),
                        evidence="visible_cloak"
                        if hidden
                        else "unseen_worker_damage_or_previous_contact",
                    )
                    self._scan_last = self.time
                    self._scan_contact = None
                    break
        for orbital in orbitals:
            if orbital.tag in self.unit_tags_received_action or orbital.energy < (
                100 if s.detection_risk or self.time < self._cloak_until else 50
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
        ramp, _, _ = self._anchors()
        for depot in self.structures({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED}).ready:
            threat = (
                ramp is not None
                and depot.distance_to(ramp) < 12
                and any(
                    e.can_attack_ground
                    and not e.is_flying
                    and e.type_id not in WORKER_TYPES
                    and e.distance_to(depot) < 8
                    for e in contacts
                )
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

    def _candidate_builders(self, position: Point2) -> list[Unit]:
        candidates = [
            w
            for w in self.workers
            if w.tag not in self.unit_tags_received_action
            and (w.is_idle or w.is_collecting)
        ]
        return sorted(
            candidates,
            key=lambda w: (w.distance_to(position) + (0 if w.is_idle else 2), w.tag),
        )

    def _available_builder(self, position: Point2) -> Unit | None:
        return next(iter(CompetitiveBot._candidate_builders(self, position)), None)

    async def _distribute_economy(self) -> None:
        if not surplus_gas(self.minerals, self.vespene):
            await self.distribute_workers(
                resource_ratio=2.4 if self.time < 230 else 1.8
            )
            return
        # The stock allocator can fill gas deficits despite its resource_ratio
        # argument. Keep that allocator out of the high-gas branch entirely.
        self._distribute_minerals_only()

    def _distribute_minerals_only(self) -> None:
        fields = []
        for hall in sorted(self.townhalls.ready, key=lambda h: h.tag):
            if hall.is_flying:
                continue
            patches = [
                m
                for m in self.mineral_field.closer_than(9, hall)
                if m.mineral_contents > 0
            ]
            if patches:
                fields.append((hall, patches))
        if not fields:
            return

        idle = [
            w
            for w in self.workers
            if w.is_idle
            and not w.is_carrying_resource
            and w.tag not in self.unit_tags_received_action
        ]
        overflow = []
        for hall, patches in fields:
            if hall.surplus_harvesters <= 0:
                continue
            patch_tags = {m.tag for m in patches}
            excess = [
                w
                for w in self.workers
                if not w.is_idle
                and w.order_target in patch_tags
                and not w.is_carrying_resource
                and w.tag not in self.unit_tags_received_action
            ]
            excess.sort(key=lambda w: w.tag)
            overflow.extend(excess[: hall.surplus_harvesters])
        vacancies = {h.tag: max(0, -h.surplus_harvesters) for h, _ in fields}
        targets: dict[int, int] = {}
        for w in self.workers:
            target = w.order_target
            if isinstance(target, int):
                targets[target] = targets.get(target, 0) + 1
        for w in sorted(idle, key=lambda u: u.tag) + overflow:
            if w.tag in self.unit_tags_received_action:
                continue
            free_fields = [(h, p) for h, p in fields if vacancies[h.tag] > 0]
            if not free_fields and not w.is_idle:
                break
            hall, patches = min(
                free_fields or fields, key=lambda hp: (hp[0].distance_to(w), hp[0].tag)
            )
            patch = min(
                patches, key=lambda m: (targets.get(m.tag, 0), m.distance_to(w), m.tag)
            )
            if w.gather(patch):
                vacancies[hall.tag] -= 1
                targets[patch.tag] = targets.get(patch.tag, 0) + 1

    def _staff_refineries(self) -> None:
        if self.time < self._next_refinery_staff:
            return
        self._next_refinery_staff = self.time + 5
        gas_tags = self.gas_buildings.tags
        if surplus_gas(self.minerals, self.vespene):
            gassers = [
                w
                for w in self.workers
                if w.order_target in gas_tags
                and not w.is_carrying_resource
                and w.tag not in self.unit_tags_received_action
            ]
            for w in sorted(gassers, key=lambda u: u.tag)[:2]:
                patches = self.mineral_field.closer_than(14, w)
                if patches:
                    w.gather(min(patches, key=lambda m: (m.distance_to(w), m.tag)))
            return
        for gas in sorted(self.gas_buildings.ready, key=lambda u: u.tag):
            if gas.vespene_contents <= 0 or gas.surplus_harvesters >= 0:
                continue
            workers = [
                w
                for w in self.workers.closer_than(12, gas)
                if w.tag not in self.unit_tags_received_action
                and not w.is_carrying_resource
                and (w.is_idle or w.is_gathering)
                and w.order_target not in gas_tags
            ]
            workers.sort(key=lambda w: (not w.is_idle, w.distance_to(gas), w.tag))
            for w in workers[: min(2, -gas.surplus_harvesters)]:
                w.gather(gas)

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
        positions = [
            building.position.offset(ADDON_OFFSET)
            for building in self.structures({U.BARRACKS, U.FACTORY, U.STARPORT})
            if not building.add_on_tag
        ]
        self._reserved_addons = {
            p: expiry
            for p, expiry in self._reserved_addons.items()
            if expiry > self.time
        }
        positions.extend(p.offset(ADDON_OFFSET) for p in self._reserved_addons)
        # Start with the preferred anchor, then choose alternate tiles only when needed.
        # The native helper checks all candidates in a ring for addon clearance.
        anchors = (
            near,
            near.offset((0, 6)),
            near.offset((0, -6)),
            near.offset((-6, 0)),
        )
        footprint = 1 if kind in {U.SUPPLYDEPOT, U.MISSILETURRET} else 1.5
        for anchor in anchors:
            pos = await self.find_placement(
                kind,
                near=anchor,
                max_distance=radius,
                random_alternative=False,
                addon_place=addon,
            )
            if pos is None:
                continue
            if any(
                abs(pos.x - p.x) < footprint + 1 and abs(pos.y - p.y) < footprint + 1
                for p in positions
            ):
                continue
            if addon and any(
                abs(pos.x + ADDON_OFFSET.x - p.x) < 2
                and abs(pos.y + ADDON_OFFSET.y - p.y) < 2
                for p in positions
            ):
                continue
            worker = self._available_builder(pos)
            if worker is None or worker.tag in self.unit_tags_received_action:
                continue
            if worker.build(kind, pos, can_afford_check=True):
                if addon:
                    self._reserved_addons[pos] = self.time + 25
                self._trace_intent(
                    "build_queued", unit=kind.name, x=round(pos.x, 1), y=round(pos.y, 1)
                )
                return True
        self._trace_intent(
            "build_site_unavailable", unit=kind.name, workers=self.workers.amount
        )
        return False

    def _working_refineries(self) -> int:
        return sum(
            refinery.vespene_contents > 0 for refinery in self.gas_buildings.ready
        )

    async def _gas(self, limit: int) -> bool:
        if (
            self._working_refineries() + self._pending(U.REFINERY) >= limit
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
                worker = self._available_builder(gas.position)
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
        ramp, depot_anchor, rax_anchor = self._anchors()
        home = townhalls[0].position if townhalls else self.start_location
        interior = home.towards(self.game_info.map_center, -6)
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
        pending_cc = CompetitiveBot._expansion_pending(
            self, bool(self._pending(U.COMMANDCENTER))
        )
        # A lost main can be rebuilt even if its replacement has no supporting barracks.
        recovering = s.bases == 0 and not pending_cc
        bank_natural = protect_natural(
            s,
            barracks_ready=bool(rax.ready),
            pending=pending_cc,
            opening_cover=min(2, self._pending(U.MARINE)),
            retry_ok=self.time >= self._natural_retry_at,
        )
        expanding = (
            recovering
            or bank_natural
            or (s.bases >= 2 and expansion_allowed(s) and not pending_cc)
        )
        if (
            expanding
            and self.can_afford(U.COMMANDCENTER)
            and self.time >= self._next_build.get(U.COMMANDCENTER, 0)
        ):
            self._next_build[U.COMMANDCENTER] = self.time + 3.0
            started = await self._expand(townhalls)
            if started:
                bank_natural = False
            elif s.bases <= 1:
                # Bad terrain / occupied sites may delay a retry, not freeze production indefinitely.
                self._natural_retry_at = self.time + 8.0
                bank_natural = False
                self._trace_intent(
                    "expansion_wait_no_site",
                    next_retry=round(self._natural_retry_at, 2),
                )
        emergency = s.local_pressure >= 5
        if build_depot and (
            not bank_natural or self.supply_left < 4 or not depots.ready
        ):
            location = (
                depot_anchor if not depots and depot_anchor is not None else interior
            )
            await self._build(U.SUPPLYDEPOT, location, radius=10)
        if depots.ready and not self._plan_total(U.BARRACKS):
            await self._build(U.BARRACKS, rax_anchor or ramp or interior, addon=True)

        support = (
            None
            if bank_natural or emergency
            else self._protected_support(s, supply["left"])
        )
        bank_support = False
        if support:
            item, producer = support
            if self.can_afford(item):
                sent = (
                    (await self._build(item, interior, addon=True))
                    if producer is None
                    else self._train(producer, item, supply)
                )
                if sent:
                    self._trace_intent("support_queued", item=item.name)
                else:
                    self._support_retry_at = self.time + 4
                    self._trace_intent("support_attempt_failed", item=item.name)
            else:
                bank_support = True

        factory_ready = bool(self.structures(U.FACTORY).ready)
        if (
            rax
            and self.time >= 38
            and not bank_natural
            and not bank_support
            and not emergency
        ):
            gas_target = desired_refineries(
                len(townhalls), self.vespene, factory_ready=factory_ready
            )
            await self._gas(gas_target)

        # Maintain worker production; Orbital conversions and discretionary spending
        # cannot shave the last minerals off the funded one-base or first-support plan.
        for hall in townhalls:
            if not hall.is_idle or hall.tag in self.unit_tags_received_action:
                continue
            if (
                not bank_natural
                and not bank_support
                and not emergency
                and rax.ready
                and hall.type_id == U.COMMANDCENTER
                and self.supply_workers >= 15
                and self.can_afford(U.ORBITALCOMMAND)
            ):
                hall(
                    A.UPGRADETOORBITAL_ORBITALCOMMAND,
                    subtract_cost=True,
                    can_afford_check=True,
                )
            elif self.supply_workers + self._pending(U.SCV) < desired_workers(
                len(townhalls), self._working_refineries()
            ):
                self._train(hall, U.SCV, supply)

        mode = (
            "emergency"
            if emergency
            else "bank_natural"
            if bank_natural
            else f"bank_{support[0].name.lower()}"
            if bank_support and support
            else "normal"
        )
        if mode != self._last_macro_mode:
            self._trace_intent(
                "macro_mode",
                previous=self._last_macro_mode,
                current=mode,
                pressure=round(s.local_pressure, 2),
                bases=s.bases,
            )
            self._last_macro_mode = mode
        if emergency:
            await self._addons_and_army(s, supply, False)
            await self._tech(s, interior)
        elif not bank_natural and not bank_support:
            await self._tech(s, interior)
            await self._addons_and_army(s, supply, False)
        if s.local_pressure < 4:
            self._resume_construction()
            self._staff_refineries()
        if s.local_pressure < 3 and self.time > 125:
            self._repair(townhalls)

    def _expansion_pending(self, observed_order: bool) -> bool:
        intent = getattr(self, "_expansion_intent", None)
        if intent is None:
            return observed_order
        if observed_order or any(
            h.distance_to(intent.position) < 3 for h in self.townhalls
        ):
            self._expansion_intent = None
            return observed_order
        if self.time - intent.stamp < 3:
            return True
        # Queue acceptance is local; an absent order/foundation across several
        # observations must not make us retry the same invalid tile forever.
        self._blocked_expansions[intent.position] = self.time + 18
        self._expansion_intent = None
        self._trace_intent(
            "expand_unconfirmed", x=intent.position.x, y=intent.position.y
        )
        return False

    async def _expand(self, halls: list[Unit]) -> bool:
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
            candidates = self._candidate_builders(location)[:3]
            if not candidates:
                return False
            if not await self.can_place_single(U.COMMANDCENTER, location):
                self._blocked_expansions[location] = self.time + 22
                if getattr(self, "_trace", None) is not None:
                    self._trace_intent(
                        "expand_site_rejected",
                        reason="placement",
                        x=location.x,
                        y=location.y,
                    )
                continue
            for worker in candidates:
                distance = await self.client.query_pathing(worker, location)
                if distance is None and worker.distance_to(location) > 2.5:
                    continue
                if worker.build(U.COMMANDCENTER, location, can_afford_check=True):
                    self._expansion_intent = Contact(location, self.time)
                    if getattr(self, "_trace", None) is not None:
                        self._trace_intent(
                            "expand_queued",
                            x=round(location.x, 1),
                            y=round(location.y, 1),
                        )
                    return True
            self._blocked_expansions[location] = self.time + 8
            if getattr(self, "_trace", None) is not None:
                self._trace_intent(
                    "expand_site_rejected",
                    reason="no_builder_path_or_queue",
                    x=location.x,
                    y=location.y,
                )
        return False

    def _protected_support(
        self, s: Signals, supply: float
    ) -> tuple[U, Unit | None] | None:
        if self.time < self._support_retry_at or s.bases < 2 or s.army_supply < 4:
            return None
        factory = self.structures(U.FACTORY).ready
        starport = self.structures(U.STARPORT).ready
        existing_tank = bool(
            self.units({U.SIEGETANK, U.SIEGETANKSIEGED})
        ) or self._pending(U.SIEGETANK)
        existing_med = bool(self.units(U.MEDIVAC)) or self._pending(U.MEDIVAC)

        def gas_ready(kind: U) -> bool:
            return self.vespene >= self.calculate_cost(kind).vespene

        if not existing_tank and supply >= 3 and gas_ready(U.SIEGETANK):
            for building in sorted(factory, key=lambda u: u.tag):
                addon = self.structures(U.FACTORYTECHLAB).ready.find_by_tag(
                    building.add_on_tag
                )
                if (
                    building.is_idle
                    and addon
                    and building.tag not in self.unit_tags_received_action
                ):
                    return U.SIEGETANK, building
        if factory and not self._plan_total(U.STARPORT) and gas_ready(U.STARPORT):
            return U.STARPORT, None
        if not existing_med and supply >= 2 and gas_ready(U.MEDIVAC):
            for building in sorted(starport, key=lambda u: u.tag):
                if (
                    building.is_idle
                    and building.tag not in self.unit_tags_received_action
                ):
                    return U.MEDIVAC, building
        return None

    async def _tech(self, s: Signals, near: Point2) -> None:
        rax = self.structures(U.BARRACKS)
        fac = self.structures(U.FACTORY)
        sp = self.structures(U.STARPORT)
        eb = self.structures(U.ENGINEERINGBAY)
        rax_target = min(
            10, 1 if self.time < 115 else (3 if s.bases <= 2 else s.bases * 2)
        )
        if s.bases < 2 and self.time < 185:
            rax_target = 2 if s.local_pressure >= 5 else 1
        fac_target = 2 if s.bases >= 3 and s.army_supply >= 35 else 1
        sp_target = 2 if s.bases >= 3 and (s.enemy_air >= 5 or self.time > 430) else 1

        readiness = (
            (
                U.FACTORY,
                rax.ready
                and s.time >= 103
                and (
                    s.bases >= 2
                    or s.time >= 184
                    or (s.local_pressure >= 5 and s.army_supply >= 5)
                ),
                1,
                True,
            ),
            (U.STARPORT, fac.ready, 1, True),
            (U.BARRACKS, rax.ready, rax_target, True),
            (U.FACTORY, fac.ready, fac_target, True),
            (U.STARPORT, sp.ready, sp_target, True),
            (
                U.ENGINEERINGBAY,
                s.time >= 220 or (s.time >= 150 and s.detection_risk),
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
            and s.time >= 150
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
        upgrades = not save and s.local_pressure < 5
        bays = self.structures(U.ENGINEERINGBAY).ready.idle if upgrades else []
        for bay in sorted(bays, key=lambda u: u.tag):
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
        if upgrades:
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
                        and s.time > 105
                        and s.army_supply + self._pending(U.MARINE) >= 3
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
