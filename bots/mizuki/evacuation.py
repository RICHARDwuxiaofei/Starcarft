"""Mineral-line evacuation driven only by ordinary visible observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, pi, sin

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2


@dataclass(frozen=True)
class SiegeZone:
    position: Point2
    radius: float
    observed: float

    def margin(self, point: Point2) -> float:
        return self.position.distance_to(point) - self.radius


_INFANTRY_WEIGHT = {U.MARINE: 1.0, U.MARAUDER: 1.5}
_DEFENDER_WEIGHT = {**_INFANTRY_WEIGHT, U.SIEGETANK: 1.5, U.SIEGETANKSIEGED: 3.5}


def infantry_breach_zones(bot, infantry, now: float) -> dict[int, SiegeZone]:
    """Cover the actual approach to a mineral line overwhelmed by visible bio."""
    if len(infantry) < 4:
        return {}
    own = [u for u in bot.units if u.type_id in _DEFENDER_WEIGHT and u.is_ready]
    zones = {}
    for hall in bot.townhalls.ready:
        if hall.is_flying:
            continue
        patches = [
            m.position
            for m in bot.mineral_field.closer_than(9, hall)
            if m.mineral_contents > 0
        ]
        if not patches:
            continue
        targets = [hall.position, *patches]

        def to_line(unit, targets=targets):
            return min(unit.distance_to(p) for p in targets)

        attackers = [u for u in infantry if u.distance_to(hall) <= 20]
        attackers.sort(key=lambda u: (to_line(u), u.tag))
        if len(attackers) < 4 or to_line(attackers[0]) > 11:
            continue
        attack = sum(_INFANTRY_WEIGHT[u.type_id] for u in attackers)
        defense = sum(
            _DEFENDER_WEIGHT[u.type_id] * u.health_percentage
            for u in own
            if u.distance_to(hall) <= 13
        )
        # Healthy, covered bases should not abandon mining on first contact.
        breached = to_line(attackers[0]) <= 7 and hall.health_percentage < 0.82
        if not breached and attack < max(5, 1.2 * defense + 1):
            continue
        vanguard = attackers[: min(5, len(attackers))]
        center = Point2(
            (
                sum(u.position.x for u in vanguard) / len(vanguard),
                sum(u.position.y for u in vanguard) / len(vanguard),
            )
        )
        # The nearest units to the mineral line determine the evacuation arc;
        # a sprawling rear formation does not quarantine the rest of the map.
        radius = min(13, max(10, center.distance_to(vanguard[0].position) + 8))
        # SC2 unit tags are positive; a negative key marks a short bio alert
        # without colliding with actual siege-tank tags or death events.
        zones[-hall.tag] = SiegeZone(center, radius, now)
    return zones


@dataclass(frozen=True)
class EvacOrder:
    mode: str
    destination: Point2
    issued: float


@dataclass
class MineralLineEvacuation:
    zones: dict[int, SiegeZone] = field(default_factory=dict)
    sheltered: set[int] = field(default_factory=set)
    orders: dict[int, EvacOrder] = field(default_factory=dict)
    last_exposed: dict[int, float] = field(default_factory=dict)

    def update(
        self,
        contacts,
        dead: set[int],
        now: float,
        infantry: dict[int, SiegeZone] | None = None,
    ) -> None:
        self.zones = {
            tag: zone
            for tag, zone in self.zones.items()
            if (tag < 0 or tag not in dead)
            and now - zone.observed <= (5 if tag < 0 else 9)
        }
        if infantry:
            self.zones.update(infantry)
        for enemy in contacts:
            if enemy.type_id == U.SIEGETANKSIEGED:
                self.zones[enemy.tag] = SiegeZone(
                    enemy.position,
                    max(13, enemy.ground_range) + enemy.radius + 1.7,
                    now,
                )
            elif enemy.type_id == U.SIEGETANK:
                self.zones.pop(enemy.tag, None)
        if not self.zones:
            self.sheltered.clear()
            self.orders.clear()
            self.last_exposed.clear()

    def margin(self, point: Point2) -> float:
        return min(
            (zone.margin(point) for zone in self.zones.values()), default=float("inf")
        )

    def avoids_fire(self, start: Point2, end: Point2, clearance: float = 0) -> bool:
        dx, dy = end.x - start.x, end.y - start.y
        length_sq = dx * dx + dy * dy
        if not length_sq:
            return self.margin(start) >= clearance
        for zone in self.zones.values():
            relative_x = zone.position.x - start.x
            relative_y = zone.position.y - start.y
            t = min(1.0, max(0.0, (relative_x * dx + relative_y * dy) / length_sq))
            if (relative_x - t * dx) ** 2 + (relative_y - t * dy) ** 2 < (
                zone.radius + clearance
            ) ** 2:
                return False
        return True

    def _escape_keeps_clear(self, origin: Point2, samples: list[Point2]) -> bool:
        return all(
            zone.margin(p) >= min(zone.margin(origin) - 0.1, 0)
            for zone in self.zones.values()
            for p in samples
        )

    def _finish_escape(self, worker, previous: EvacOrder, now: float) -> bool:
        """Commit to an observed escape leg unless new fire makes it worse."""
        if previous.mode != "move":
            return False
        goal = previous.destination
        distance = worker.distance_to(goal)
        age = now - previous.issued
        if distance <= 1.3 or age >= 4.0:
            return False
        target = worker.order_target
        acknowledged = (
            isinstance(target, Point2)
            and target.distance_to(goal) <= 0.8
            and not worker.is_idle
        )
        if not acknowledged and age >= 0.7:
            return False
        samples = [
            worker.position.towards(goal, distance * f) for f in (0.25, 0.5, 0.75, 1)
        ]
        return self._escape_keeps_clear(worker.position, samples)

    def _escape(self, bot, worker, refuge) -> Point2 | None:
        origin = worker.position
        initial_margin = self.margin(origin)
        options = []
        area = bot.game_info.playable_area

        def pathable(point: Point2) -> bool:
            tile = point.rounded
            return (
                area.x <= tile.x < area.x + area.width
                and area.y <= tile.y < area.y + area.height
                and bot.in_pathing_grid(tile)
            )

        # Local waypoints first: never issue a cross-map move through the guns.
        for radius in (4, 7, 10):
            for lane in range(12):
                angle = lane * pi / 6
                p = origin.offset((radius * cos(angle), radius * sin(angle)))
                samples = [origin.towards(p, radius * f) for f in (0.25, 0.5, 0.75, 1)]
                if not all(pathable(sample) for sample in samples):
                    continue
                # Every leg must make progress out of danger; do not cross a
                # second tank's line (or initially move deeper into the first).
                if not self._escape_keeps_clear(origin, samples):
                    continue
                margin = self.margin(p)
                if margin <= initial_margin + 0.75:
                    continue
                near_refuge = min((h.distance_to(p) for h in refuge), default=0)
                fan = ((worker.tag % 5) - 2) * 0.025 * angle
                options.append(
                    (
                        (
                            min(margin, 3) - 0.18 * near_refuge - 0.08 * radius + fan,
                            margin,
                        ),
                        p,
                    )
                )
        return max(options, key=lambda pair: pair[0])[1] if options else None

    def react(self, bot, now: float) -> int:
        if not self.zones:
            return 0
        minerals = {
            patch.tag: patch
            for patch in bot.mineral_field
            if patch.mineral_contents > 0
        }
        halls = [h for h in bot.townhalls.ready if not h.is_flying]
        safe_halls = [h for h in halls if self.margin(h.position) > 3]
        refuge_patches = []
        for patch in minerals.values():
            dropoff = min(
                halls, key=lambda h: (h.distance_to(patch), h.tag), default=None
            )
            if (
                dropoff in safe_halls
                and dropoff.distance_to(patch) < 9
                and self.avoids_fire(patch.position, dropoff.position)
            ):
                refuge_patches.append(patch)
        # Gas miners can temporarily be absent or flagged dead entering the
        # refinery. Keep their tags protected until the alert fully clears.
        targets = {**minerals, **{h.tag: h for h in bot.townhalls}}
        targets.update({gas.tag: gas for gas in bot.gas_buildings})
        assignments: dict[int, int] = {}
        for worker in bot.workers:
            target = worker.order_target
            if isinstance(target, int):
                assignments[target] = assignments.get(target, 0) + 1
        new_orders = 0
        for worker in bot.workers:
            tag, p = worker.tag, worker.position
            target = worker.order_target
            assigned = targets.get(target) if isinstance(target, int) else None
            previous = self.orders.get(tag)
            assigned_point = (
                target
                if isinstance(target, Point2)
                else (assigned.position if assigned is not None else None)
            )
            threatened = self.margin(p) < 0 or (
                assigned_point is not None and not self.avoids_fire(p, assigned_point)
            )
            if threatened:
                self.sheltered.add(tag)
                self.last_exposed[tag] = now
            if tag not in self.sheltered or tag in bot.unit_tags_received_action:
                continue
            # Leave a worker mining safely after a successful transfer. Still
            # retain its shelter lock so another helper can't reassign it.
            if not threatened and assigned is not None and worker.is_collecting:
                if now - self.last_exposed.get(tag, now) >= 12:
                    self.sheltered.discard(tag)
                    self.last_exposed.pop(tag, None)
                    self.orders.pop(tag, None)
                continue
            if previous and self._finish_escape(worker, previous, now):
                continue
            if (
                previous
                and now - previous.issued < 0.7
                and not threatened
                and not worker.is_idle
            ):
                continue
            order = None
            if self.margin(p) >= 1.5:
                patches = [
                    patch
                    for patch in refuge_patches
                    if self.avoids_fire(p, patch.position, clearance=1.5)
                ]
                if patches:
                    patch = min(
                        patches,
                        key=lambda m: (
                            worker.distance_to(m) + 2.4 * assignments.get(m.tag, 0),
                            m.tag,
                        ),
                    )
                    if worker.gather(patch):
                        order = EvacOrder("gather", patch.position, now)
                        assignments[patch.tag] = assignments.get(patch.tag, 0) + 1
            if order is None and (threatened or self.margin(p) < 1.5):
                waypoint = self._escape(bot, worker, safe_halls)
                if waypoint is not None and worker.move(waypoint):
                    order = EvacOrder("move", waypoint, now)
                elif (
                    threatened
                    and assigned_point is not None
                    and self.margin(p) >= 0
                    and worker.stop()
                ):
                    order = EvacOrder("stop", p, now)
            if order is not None:
                self.orders[tag] = order
                new_orders += 1
        return new_orders
