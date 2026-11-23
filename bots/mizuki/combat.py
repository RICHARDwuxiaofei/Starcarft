"""Visibility-limited field command for Mizuki's Terran army."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from math import cos, hypot, sin
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.effect_id import EffectId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI
    from sc2.unit import Unit

_BIO = {U.MARINE, U.MARAUDER}
_TANKS = {U.SIEGETANK, U.SIEGETANKSIEGED}
_VIKINGS = {U.VIKINGFIGHTER, U.VIKINGASSAULT}
_FIELD = _BIO | _TANKS | _VIKINGS
_ESCORT = {U.MEDIVAC, U.RAVEN}
_STATIC = {
    U.BUNKER,
    U.PHOTONCANNON,
    U.SPINECRAWLER,
    U.SPORECRAWLER,
    U.MISSILETURRET,
    U.PLANETARYFORTRESS,
}
_ECONOMY = {U.SCV, U.PROBE, U.DRONE, U.MULE}
_HIGH_VALUE = {
    U.DISRUPTOR,
    U.HIGHTEMPLAR,
    U.INFESTOR,
    U.VIPER,
    U.GHOST,
    U.BANELING,
    U.SIEGETANKSIEGED,
    U.COLOSSUS,
    U.LURKERMPBURROWED,
}
_TRANSFORMS = {
    AbilityId.SIEGEMODE_SIEGEMODE,
    AbilityId.UNSIEGE_UNSIEGE,
    AbilityId.MORPH_VIKINGASSAULTMODE,
    AbilityId.MORPH_VIKINGFIGHTERMODE,
}


class _Phase(Enum):
    GATHER = auto()
    PRESSURE = auto()
    RETREAT = auto()


@dataclass
class _Sweep:
    visits: dict[Point2, float] = field(default_factory=dict)
    waypoint: Point2 | None = None
    waypoint_since: float = 0.0


@dataclass
class _CombatState:
    phase: _Phase = _Phase.GATHER
    retreat_until: float = 0.0
    last_air_seen: float = 0.0
    last_tank_contact: dict[int, float] = field(default_factory=dict)
    air_evade_until: dict[int, float] = field(default_factory=dict)
    last: dict[int, tuple[tuple, float]] = field(default_factory=dict)
    route: tuple[Point2, ...] = ()
    ground_sweep: _Sweep = field(default_factory=_Sweep)
    air_sweep: _Sweep = field(default_factory=_Sweep)
    core_anchor: Point2 | None = None
    natural_tag: int | None = None
    natural_post: Point2 | None = None
    reserve_tank: int | None = None
    reserve_bio: tuple[int, ...] = ()
    outer_contest_until: float = 0.0
    unfinished_outer_tags: frozenset[int] = frozenset()
    post_handoff_until: float = 0.0
    outer_handoff_used: bool = False


@dataclass(frozen=True)
class _SiegeGuard:
    rally: Point2
    guns: tuple[Unit, ...]


def _center(group: list[Unit], fallback: Point2) -> Point2:
    if not group:
        return fallback
    return Point2(
        (
            sum(u.position.x for u in group) / len(group),
            sum(u.position.y for u in group) / len(group),
        )
    )


def _observed(unit: Unit) -> bool:
    return (
        unit.is_visible
        and not unit.is_snapshot
        and (not unit.is_cloaked or unit.is_revealed)
    )


def _could_hit(attacker: Unit, target: Unit) -> bool:
    return (
        attacker.can_attack_air if target.is_flying else attacker.can_attack_ground
    ) or (target.type_id == U.COLOSSUS and attacker.can_attack_air)


def _danger(unit: Unit) -> bool:
    return (
        unit.can_attack_ground
        or unit.can_attack_air
        or unit.type_id in _STATIC
        or unit.type_id in _HIGH_VALUE
    )


def _weight(unit: Unit) -> float:
    if unit.type_id in _ECONOMY:
        return 0.3
    if unit.type_id in _TANKS:
        return 3.5
    if unit.type_id in {
        U.BATTLECRUISER,
        U.CARRIER,
        U.TEMPEST,
        U.THOR,
        U.THORAP,
        U.ULTRALISK,
        U.COLOSSUS,
    }:
        return 5.0
    if unit.type_id in _STATIC:
        return 3.0 if unit.type_id != U.PLANETARYFORTRESS else 5.0
    if unit.type_id in _HIGH_VALUE:
        return 2.4
    if unit.type_id in _VIKINGS or unit.type_id == U.MARAUDER:
        return 1.65
    return 1.0 if _danger(unit) else 0.0


def _strength(group: list[Unit]) -> float:
    return sum(
        _weight(u) * (0.45 + 0.55 * max(u.health_percentage, u.shield_percentage))
        for u in group
    )


def _cohort(
    units: list[Unit], fallback: Point2, previous: Point2 | None
) -> tuple[Point2, list[Unit]]:
    """Anchor on an actual unit in the strongest locally concentrated force."""
    if not units:
        return fallback, []
    weighted = [(u, _strength([u])) for u in units]
    best_score = (-1.0, -1.0, 0)
    best_cluster: list[Unit] = []
    for unit, _ in weighted:
        ux, uy = unit.position
        neighboring = [
            (ally, weight)
            for ally, weight in weighted
            if (ally.position.x - ux) ** 2 + (ally.position.y - uy) ** 2 <= 12**2
        ]
        cluster = [ally for ally, _ in neighboring]
        score = (
            sum(weight for _, weight in neighboring),
            -unit.distance_to(previous or fallback),
            -unit.tag,
        )
        if score > best_score:
            best_score, best_cluster = score, cluster
    mean = _center(best_cluster, fallback)
    anchor = min(best_cluster, key=lambda u: (u.distance_to(mean), u.tag)).position
    return anchor, [u for u in units if u.distance_to(anchor) <= 13]


def _armor_screen(ground: list[Unit], center: Point2, goal: Point2) -> Point2 | None:
    if center.distance_to(goal) < 12:
        return None
    tanks = [u for u in ground if u.type_id in _TANKS and u.distance_to(center) <= 19]
    if not tanks:
        return None
    tank = min(tanks, key=lambda u: (u.distance_to(goal), u.tag))
    return tank.position.towards(goal, min(8, tank.distance_to(goal)))


def _screen_fallback(
    bot: BotAI, pos: Point2, screen: Point2 | None, goal: Point2
) -> Point2 | None:
    if screen is None:
        return None
    length = screen.distance_to(goal)
    if length < 3:
        return None
    dx, dy = (goal.x - screen.x) / length, (goal.y - screen.y) / length
    excess = (pos.x - screen.x) * dx + (pos.y - screen.y) * dy
    if excess <= 2.5:
        return None
    lane = Point2((pos.x - dx * (excess + 1), pos.y - dy * (excess + 1)))
    for candidate in (lane, screen):
        if _inside(bot, candidate) and bot.in_pathing_grid(candidate):
            return candidate
    return None


def _chase(unit: Unit, target: Unit, goal: Point2, core: Point2) -> bool:
    """A short weapon-range correction, not a solo pursuit away from the army."""
    reach = unit.air_range if target.is_flying else unit.ground_range
    return (
        unit.distance_to(target) <= reach + unit.radius + target.radius + 2.5
        and unit.distance_to(core) <= 12
        and target.distance_to(core) <= 14
        and target.distance_to(goal) <= unit.distance_to(goal) + 1.5
    )


def _inside(bot: BotAI, point: Point2) -> bool:
    area = bot.game_info.playable_area
    return (
        area.x + 1 <= point.x <= area.right - 1
        and area.y + 1 <= point.y <= area.top - 1
    )


def _home(bot: BotAI, ground: list[Unit]) -> Point2:
    ready = [h for h in bot.townhalls if h.is_ready and not h.is_flying]
    if ready:
        anchor = min(
            ready, key=lambda h: (h.distance_to(bot.start_location), h.tag)
        ).position
    elif bot.townhalls:
        anchor = min(
            bot.townhalls, key=lambda h: (h.distance_to(bot.start_location), h.tag)
        ).position
    else:
        # If the original base has fallen, regroup on the remaining army instead of
        # repeatedly moving into the ruins of that base.
        return _center(ground, bot.start_location)
    rally = anchor.towards(bot.game_info.map_center, 4)
    return rally if _inside(bot, rally) and bot.in_pathing_grid(rally) else anchor


def _away(bot: BotAI, unit: Unit, threat: Unit, distance: float) -> Point2 | None:
    pos = unit.position
    dx, dy = pos.x - threat.position.x, pos.y - threat.position.y
    length = hypot(dx, dy)
    if length < 0.01:
        return None
    dx, dy = dx / length, dy / length
    for step in (distance, distance * 0.55):
        for rotation in (0, 0.68, -0.68, 1.15, -1.15):
            x = dx * cos(rotation) - dy * sin(rotation)
            y = dx * sin(rotation) + dy * cos(rotation)
            point = Point2((pos.x + x * step, pos.y + y * step))
            midpoint = pos.towards(point, step / 2)
            if _inside(bot, point) and (
                unit.is_flying
                or (bot.in_pathing_grid(point) and bot.in_pathing_grid(midpoint))
            ):
                return point
    return None


# These damage zones are exposed in normal API observations. Liberator
# exclusion is conditional: a visible mine/own zone must not repel our army.
_SPLASH = {
    EffectId.PSISTORMPERSISTENT: (1.5, False),
    EffectId.RAVAGERCORROSIVEBILECP: (0.65, False),
    EffectId.NUKEPERSISTENT: (8.0, False),
    EffectId.LIBERATORTARGETMORPHPERSISTENT: (4.5, True),
    EffectId.LIBERATORTARGETMORPHDELAYPERSISTENT: (4.5, True),
}


def _hazards(bot: BotAI) -> list[tuple[Point2, float, bool]]:
    zones = []
    for effect in getattr(bot.state, "effects", ()):
        try:
            known = _SPLASH.get(effect.id)
        except ValueError:
            continue  # new client effect not mapped by the pinned API release
        if known is None or (known[1] and not effect.is_enemy):
            continue
        zones.extend(
            (p, max(effect.radius, known[0]), known[1]) for p in effect.positions
        )
    return sorted(zones, key=lambda z: (z[0].x, z[0].y, z[1], z[2]))


def _evade(
    bot: BotAI,
    state: _CombatState,
    now: float,
    unit: Unit,
    zones: list[tuple[Point2, float, bool]],
) -> bool:
    if not zones:
        return False
    relevant = [
        (p, r + unit.radius + 0.25)
        for p, r, ground_only in zones
        if not (unit.is_flying and ground_only) and unit.distance_to(p) <= r + 12
    ]
    if not any(unit.distance_to(p) < r for p, r in relevant):
        return False
    if unit.type_id == U.SIEGETANKSIEGED:
        _send(state, now, unit, AbilityId.UNSIEGE_UNSIEGE)
        return True
    origin = unit.position
    rotation = ((unit.tag * 13) % 16) * 0.392699
    for step in (2.5, 4.0, 6.0, 9.0):
        for k in range(12):
            angle = rotation + k * 0.523599
            goal = origin.offset((step * cos(angle), step * sin(angle)))
            if not _inside(bot, goal):
                continue
            if not unit.is_flying and not all(
                bot.in_pathing_grid(origin.towards(goal, step * f))
                for f in (0.33, 0.67, 1)
            ):
                continue
            if all(goal.distance_to(p) >= r for p, r in relevant):
                _send(state, now, unit, "move", goal)
                return True
    return False


def _send(
    state: _CombatState, now: float, unit: Unit, kind: str | AbilityId, target=None
) -> None:
    target_key = getattr(target, "tag", target)
    if isinstance(target_key, Point2):
        target_key = (round(target_key.x), round(target_key.y))
    key = (kind, target_key)
    previous = state.last.get(unit.tag)
    # Don't repeat non-movement abilities while the result is still pending.
    ability = isinstance(kind, AbilityId)
    if (
        previous
        and previous[0] == key
        and (
            now - previous[1] < (1.35 if ability else 0.8)
            or (not ability and not unit.is_idle and now - previous[1] < 2.0)
        )
    ):
        return
    if unit.orders and unit.orders[0].ability.id in _TRANSFORMS:
        return
    if kind == "move":
        unit.move(target)
    elif kind == "attack":
        unit.attack(target)
    elif target is None:
        unit(kind)
    else:
        unit(kind, target)
    state.last[unit.tag] = (key, now)


def _defense(
    bot: BotAI, contacts: list[Unit], army: Point2
) -> tuple[Point2, list[Unit]] | None:
    options: list[tuple[float, int, Point2, list[Unit]]] = []
    for base in bot.townhalls:
        if base.is_flying:
            continue
        enemies = [
            e
            for e in contacts
            if e.distance_to(base) < 17
            and (
                _danger(e)
                and (e.can_attack_ground or e.type_id in _STATIC | _HIGH_VALUE)
                or e.type_id in _ECONOMY
                and e.distance_to(base) < 10
            )
        ]
        if not enemies:
            continue
        # Prefer a seriously threatened base, then one the army can reach quickly.
        score = (
            _strength(enemies)
            - 0.025 * base.distance_to(army)
            + 3.0 * (1 - base.health_percentage)
        )
        options.append((score, -base.tag, base.position, enemies))
    if not options:
        return None
    _, _, anchor, enemies = max(options, key=lambda entry: entry[:2])
    return anchor, enemies


def _inside_siege(point: Point2, guns: tuple[Unit, ...], padding: float = 0.8) -> bool:
    return any(
        gun.distance_to(point) < max(13, gun.ground_range) + gun.radius + padding
        for gun in guns
    )


def _siege_guard(
    bot: BotAI, enemies: list[Unit], ground: list[Unit], core: Point2, home: Point2
) -> _SiegeGuard | None:
    """Defend on our side of a visible, materially stronger deployed tank line."""
    deployed = [e for e in enemies if e.type_id == U.SIEGETANKSIEGED]
    if not deployed or not ground:
        return None
    candidates = []
    for base in bot.townhalls:
        if base.is_flying:
            continue
        guns = tuple(
            sorted(
                (e for e in deployed if e.distance_to(base) <= 23),
                key=lambda e: (e.distance_to(base), e.tag),
            )
        )
        if not guns:
            continue
        frontline = guns[0]
        allied = [
            u for u in ground if u.distance_to(base) <= 18 and u.distance_to(core) <= 15
        ]
        armor = sum(u.type_id in _TANKS for u in allied)
        enemy = [
            e
            for e in enemies
            if not e.is_flying and _danger(e) and e.distance_to(frontline) <= 12
        ]
        own_power, enemy_power = _strength(allied), _strength(enemy)
        # A genuinely concentrated advantage may counterattack. A distant reserve
        # doesn't count as covering units already beneath the opposing guns.
        if len(guns) == 1:
            holding = armor == 0 and (own_power < 6 or enemy_power > own_power)
        else:
            covering_tank = any(
                u.type_id in _TANKS
                and u.health_percentage >= 0.45
                and u.distance_to(core) <= 9
                for u in allied
            )
            can_trade = armor >= len(guns) and own_power >= enemy_power * 1.15
            supported_push = (
                len(guns) <= 3
                and covering_tank
                and len(allied) - armor >= 9
                and own_power >= enemy_power * 1.55
            )
            massed_infantry = (
                len(guns) <= 2 and len(allied) >= 14 and own_power >= enemy_power * 2.2
            )
            holding = not (can_trade or supported_push or massed_infantry)
        if holding:
            rank = (
                len(guns),
                -frontline.distance_to(base),
                -base.distance_to(core),
                -base.tag,
            )
            candidates.append((rank, base.position, guns))
    if not candidates:
        return None
    _, base, guns = max(candidates, key=lambda entry: entry[0])
    frontline = guns[0].position
    dx, dy = base.x - frontline.x, base.y - frontline.y
    length = hypot(dx, dy)
    if length == 0:
        dx, dy = home.x - frontline.x, home.y - frontline.y
        length = hypot(dx, dy)
    if length == 0:
        dx, dy, length = 1.0, 0.0, 1.0
    dx, dy = dx / length, dy / length
    # Project behind the threatened base rather than aiming at the siege tanks.
    # Try sideways offsets for ramps, minerals and map edges.
    options = [
        Point2((base.x + dx * depth - dy * lane, base.y + dy * depth + dx * lane))
        for depth in (4, 7, 10, 14, 18)
        for lane in (0, 4, -4, 8, -8)
    ]
    safe = [
        p
        for p in (*options, home)
        if _inside(bot, p)
        and bot.in_pathing_grid(p)
        and not _inside_siege(p, guns, 1.4)
    ]
    if not safe:
        # The existing home is preferable to the enemy line even if it is exposed.
        return _SiegeGuard(home, guns)
    rally = min(
        safe, key=lambda p: (p.distance_to(base) + 0.18 * p.distance_to(core), p.x, p.y)
    )
    return _SiegeGuard(rally, guns)


def _guard_line(
    bot: BotAI,
    state: _CombatState,
    now: float,
    unit: Unit,
    enemies: list[Unit],
    guard: _SiegeGuard,
) -> None:
    rally, guns = guard.rally, guard.guns
    threatening = min(guns, key=lambda e: (unit.distance_to(e), e.tag))
    exposed = _inside_siege(unit.position, guns, unit.radius + 0.35)
    target, in_range = _select(
        unit, enemies, max_distance=15 if unit.type_id in _TANKS else 10
    )
    if unit.type_id == U.SIEGETANKSIEGED:
        if (
            target
            and in_range
            and (
                not exposed
                or unit.health_percentage >= 0.32
                or unit.weapon_cooldown <= 0
            )
        ):
            _send(state, now, unit, "attack", target)
            return
        if exposed or unit.distance_to(rally) > 10:
            _send(state, now, unit, AbilityId.UNSIEGE_UNSIEGE)
        return
    if not exposed and target and in_range:
        _send(state, now, unit, "attack", target)
        return
    if (
        not exposed
        and unit.type_id == U.SIEGETANK
        and unit.distance_to(rally) <= 5
        and unit.distance_to(threatening) <= 18
    ):
        _send(state, now, unit, AbilityId.SIEGEMODE_SIEGEMODE)
        return

    # Offset the fallback across the front instead of stacking infantry on one
    # artillery splash target. A pathable rally is the secondary choice.
    angle_x, angle_y = (
        rally.x - threatening.position.x,
        rally.y - threatening.position.y,
    )
    axis = hypot(angle_x, angle_y) or 1.0
    lane = ((unit.tag % 5) - 2) * 1.6
    offset = Point2((rally.x - angle_y * lane / axis, rally.y + angle_x * lane / axis))
    if (
        not _inside(bot, offset)
        or not bot.in_pathing_grid(offset)
        or _inside_siege(offset, guns)
    ):
        offset = rally
    distance = unit.distance_to(offset)
    if distance > 2:
        first_step = unit.position.towards(offset, min(distance, 3.5))
        if exposed and first_step.distance_to(threatening.position) < unit.distance_to(
            threatening
        ):
            offset = _away(bot, unit, threatening, 5) or offset
        elif not exposed and any(
            _inside_siege(unit.position.towards(offset, distance * factor), guns)
            for factor in (0.25, 0.5, 0.75)
        ):
            # Stay out of the firing arc rather than crossing it to touch a marker.
            if not unit.is_idle:
                _send(state, now, unit, AbilityId.HOLDPOSITION_HOLD)
            return
        _send(state, now, unit, "move", offset)
    elif not unit.is_idle:
        # Cancel an old target order; no out-of-range chase from the defensive line.
        _send(state, now, unit, AbilityId.HOLDPOSITION_HOLD)


def _natural_reserve(
    bot: BotAI,
    state: _CombatState,
    now: float,
    phase: _Phase,
    ground: list[Unit],
    enemies: list[Unit],
    defense: tuple[Point2, list[Unit]] | None,
    siege_guard: _SiegeGuard | None,
) -> tuple[Point2, set[int]] | None:
    """Post one tank at the natural without making the entire army a garrison."""
    halls = sorted(
        (h for h in bot.townhalls if not h.is_flying),
        key=lambda h: (h.distance_to(bot.start_location), h.tag),
    )
    tanks = [u for u in ground if u.type_id in _TANKS]
    if (
        not tanks
        or len(halls) < 2
        or halls[0].distance_to(bot.start_location) > 9
        or not halls[1].is_ready
    ):
        state.reserve_tank, state.reserve_bio = None, ()
        state.unfinished_outer_tags = frozenset()
        state.post_handoff_until = 0.0
        return None
    natural = halls[1]
    if natural.tag != state.natural_tag:
        state.natural_tag = natural.tag
        state.reserve_tank, state.reserve_bio = None, ()
        state.outer_contest_until = 0.0
        state.unfinished_outer_tags = frozenset()
        state.post_handoff_until = 0.0
        state.outer_handoff_used = False
        center = bot.game_info.map_center
        if natural.distance_to(center) < 1:
            center = natural.position.offset(natural.position - bot.start_location)
        axis = natural.distance_to(center) or 1.0
        dx = (center.x - natural.position.x) / axis
        dy = (center.y - natural.position.y) / axis
        candidates = [
            natural.position.offset((dx * depth - dy * lane, dy * depth + dx * lane))
            for depth, lane in ((6, 0), (5, 3), (5, -3), (4, 5), (4, -5), (4, 0))
        ]
        state.natural_post = next(
            (p for p in candidates if _inside(bot, p) and bot.in_pathing_grid(p)),
            None,
        )
    post = state.natural_post
    if post is None:
        return None
    outer = halls[2:]
    posted_tank = (
        tanks[0]
        if len(tanks) == 1
        and tanks[0].tag == state.reserve_tank
        and tanks[0].type_id == U.SIEGETANKSIEGED
        and tanks[0].distance_to(post) <= 3
        else None
    )
    if posted_tank is None:
        state.post_handoff_until = 0.0
    elif not state.outer_handoff_used and any(
        h.is_ready and h.tag in state.unfinished_outer_tags for h in outer
    ):
        # Finishing a CC doesn't make it defendable. Keep an already-positioned
        # sole tank through the handoff while replacement armor can finish.
        state.post_handoff_until = now + 45.0
        state.outer_handoff_used = True
    state.unfinished_outer_tags = frozenset(h.tag for h in outer if not h.is_ready)
    handoff = now < state.post_handoff_until
    if defense and defense[0] == halls[0].position:
        return None
    if not handoff and (
        phase is _Phase.RETREAT or (defense and defense[0] == natural.position)
    ):
        return None
    ground_threats = [
        e
        for e in enemies
        if not e.is_flying
        and (e.can_attack_ground or e.type_id in _STATIC | _HIGH_VALUE)
    ]
    threatened_outer = [
        (h, [e for e in ground_threats if e.distance_to(h) <= 21]) for h in outer
    ]
    threatened_outer = [(h, threats) for h, threats in threatened_outer if threats]
    if threatened_outer:
        state.outer_contest_until = now + 9.0
        if siege_guard is None and all(
            _strength(
                [
                    u
                    for u in ground
                    if u.distance_to(h) <= 13 and (not handoff or u is not posted_tank)
                ]
            )
            >= max(12.0 if handoff else 7.0, 1.45 * _strength(threats))
            for h, threats in threatened_outer
        ):
            # Don't hold armor back from a real, already concentrated counterattack.
            state.outer_contest_until = now
            state.post_handoff_until = now
            return None
    if handoff and posted_tank is not None:
        deployed = tuple(e for e in ground_threats if e.type_id == U.SIEGETANKSIEGED)
        if not (
            _inside_siege(post, deployed, 2)
            or any(e.distance_to(posted_tank) < 3 for e in ground_threats)
        ):
            # The field force still follows the regular orders, including during
            # retreat or a direct natural attack. Only the posted tank stays.
            return post, {posted_tank.tag}
        state.post_handoff_until = now
        return None
    contested = now < state.outer_contest_until
    preparing = any(not h.is_ready for h in outer)
    if not contested and phase is not _Phase.GATHER and not preparing:
        return None
    if siege_guard and _inside_siege(post, siege_guard.guns, 2):
        return None
    # Don't pull front-line armor back through opposing guns, or pin the sole tank
    # to home when an already committed small force has no substitute.
    viable = [
        u
        for u in tanks
        if (
            u.distance_to(post) <= 16
            or (phase is _Phase.GATHER and u.distance_to(halls[0]) <= 13)
        )
        and (
            not siege_guard
            or not any(
                _inside_siege(
                    u.position.towards(post, u.distance_to(post) * f),
                    siege_guard.guns,
                    1,
                )
                for f in (0.0, 0.25, 0.5, 0.75)
            )
        )
    ]
    if not viable or (
        phase is _Phase.PRESSURE
        and len(tanks) == 1
        and _strength([u for u in ground if u.type_id in _BIO]) < 12
    ):
        return None
    tank = min(
        viable,
        key=lambda u: (u.tag != state.reserve_tank, u.distance_to(post), u.tag),
    )
    state.reserve_tank = tank.tag
    bio = [
        u
        for u in ground
        if u.type_id in _BIO
        and u.distance_to(post) <= 10
        and (not siege_guard or not _inside_siege(u.position, siege_guard.guns, 1))
    ]
    infantry_count = sum(u.type_id in _BIO for u in ground)
    state.reserve_bio = tuple(
        u.tag
        for u in sorted(
            bio,
            key=lambda u: (u.tag not in state.reserve_bio, u.distance_to(post), u.tag),
        )[: min(2, infantry_count // 8) if contested else 0]
    )
    return post, {tank.tag, *state.reserve_bio}


def _post_unit(
    state: _CombatState, now: float, unit: Unit, enemies: list[Unit], post: Point2
) -> None:
    target, in_range = _select(unit, enemies, max_distance=14)
    distance = unit.distance_to(post)
    if unit.type_id == U.SIEGETANKSIEGED:
        if target and in_range:
            _send(state, now, unit, "attack", target)
        elif distance > 3:
            _send(state, now, unit, AbilityId.UNSIEGE_UNSIEGE)
    elif (
        unit.type_id == U.SIEGETANK
        and distance <= 2
        and not any(
            _could_hit(unit, e) and _danger(e) and unit.distance_to(e) < 3
            for e in enemies
        )
    ):
        _send(state, now, unit, AbilityId.SIEGEMODE_SIEGEMODE)
    elif target and in_range:
        _send(state, now, unit, "attack", target)
    elif distance > 2:
        _send(state, now, unit, "move", post)
    elif not unit.is_idle:
        _send(state, now, unit, AbilityId.HOLDPOSITION_HOLD)


def _next_phase(
    state: _CombatState,
    now: float,
    ours: float,
    theirs: float,
    home_distance: float,
    bases: int,
) -> _Phase:
    if (
        state.phase == _Phase.PRESSURE
        and bases
        and ours < 100
        and theirs > max(5.0, 1.36 * ours + 2)
    ):
        state.phase = _Phase.RETREAT
        state.retreat_until = now + 11
    if state.phase == _Phase.RETREAT:
        # Don't flip back merely because withdrawing units lost line of sight.
        if now < state.retreat_until or (
            home_distance > 12 and now < state.retreat_until + 13
        ):
            return state.phase
        state.phase = _Phase.GATHER
    launch = 15 if now < 420 else 11 if now < 600 else 6 if now < 750 else 0.45
    if not bases:
        launch = 0.45
    if (
        state.phase == _Phase.GATHER
        and ours >= launch
        and (not bases or theirs < max(4.0, ours * 1.12))
    ):
        state.phase = _Phase.PRESSURE
    return state.phase


def _route(bot: BotAI) -> tuple[Point2, ...]:
    area = bot.game_info.playable_area
    # Starts and expansion pads first; the grid also covers map edges where lifted
    # buildings can hide. Candidates are observations to seek, not enemy data.
    special = [*bot.enemy_start_locations, *bot.expansion_locations_list]
    grid = [
        Point2((area.x + fx * area.width, area.y + fy * area.height))
        for fx in (0.06, 0.28, 0.5, 0.72, 0.94)
        for fy in (0.06, 0.28, 0.5, 0.72, 0.94)
    ]
    return tuple(dict.fromkeys(p for p in [*special, *grid] if _inside(bot, p)))


def _explore(
    bot: BotAI, state: _CombatState, now: float, center: Point2, *, can_fly: bool
) -> Point2:
    if not state.route:
        state.route = _route(bot)
    sweep = state.air_sweep if can_fly else state.ground_sweep
    if sweep.waypoint is not None and (
        bot.is_visible(sweep.waypoint)
        or (can_fly and sweep.waypoint.distance_to(center) < 3.5)
        or now - sweep.waypoint_since > 29
    ):
        sweep.visits[sweep.waypoint] = now
        sweep.waypoint = None
    if sweep.waypoint is None:
        for point in state.route:
            if bot.is_visible(point):
                sweep.visits[point] = now
        locations = [p for p in state.route if can_fly or bot.in_pathing_grid(p)]
        locations = [p for p in locations if not bot.is_visible(p)] or locations
        if not locations:
            return bot.game_info.map_center

        def cost(point: Point2) -> tuple[float, ...]:
            recent = sweep.visits.get(point)
            likely_base = (
                point in bot.enemy_start_locations
                or point in bot.expansion_locations_list
            )
            fresh_visit = recent is not None and now - recent < 90
            return (
                int(fresh_visit),
                recent if fresh_visit else 0.0,
                (0 if likely_base and recent is None else 12)
                + center.distance_to(point),
                point.x,
                point.y,
            )

        sweep.waypoint = min(locations, key=cost)
        sweep.waypoint_since = now
    return sweep.waypoint


def _select(
    unit: Unit, enemies: list[Unit], *, max_distance: float = 12
) -> tuple[Unit | None, bool]:
    candidates = [
        e
        for e in enemies
        if _could_hit(unit, e)
        and unit.distance_to(e) <= max_distance + e.radius
        and (unit.type_id != U.SIEGETANKSIEGED or unit.distance_to(e) >= 2.1)
    ]
    if not candidates:
        return None, False
    in_range = [e for e in candidates if unit.target_in_range(e)]

    def score(e: Unit) -> tuple[float, int]:
        proximity = unit.distance_to(e)
        special = 4.0 if e.type_id in _HIGH_VALUE else 0.0
        dangerous = 2.8 if _danger(e) else 0.0
        if unit.type_id in _TANKS and not e.is_structure:
            dangerous += 1
        if unit.type_id == U.VIKINGFIGHTER and e.is_armored:
            dangerous += 1.5
        low_hp = 1.8 * (1 - max(e.health_percentage, e.shield_percentage))
        return (proximity * 0.33 - special - dangerous - low_hp, e.tag)

    return min(in_range or candidates, key=score), bool(in_range)


def _ground_objective(
    bot: BotAI,
    state: _CombatState,
    now: float,
    center: Point2,
    enemies: list[Unit],
    floating: list[Unit],
    *,
    can_shoot_air: bool,
) -> Point2:
    buildings = [e for e in enemies if e.is_structure and not e.is_flying]
    if buildings:
        return min(buildings, key=lambda e: (e.distance_to(center), e.tag)).position
    nearby = [
        e
        for e in enemies
        if not e.is_flying and _danger(e) and e.distance_to(center) < 15
    ]
    if nearby:
        return min(nearby, key=lambda e: (e.distance_to(center), e.tag)).position
    if can_shoot_air and floating:
        for target in sorted(floating, key=lambda e: (e.distance_to(center), e.tag)):
            tiles = [
                target.position.towards(
                    Point2((target.position.x + dx, target.position.y + dy)), step
                )
                for step in (2, 4, 5.5)
                for dx, dy in (
                    (1, 0),
                    (1, 1),
                    (0, 1),
                    (-1, 1),
                    (-1, 0),
                    (-1, -1),
                    (0, -1),
                    (1, -1),
                )
            ]
            landing = min(
                (
                    p
                    for p in [target.position, *tiles]
                    if _inside(bot, p) and bot.in_pathing_grid(p)
                ),
                key=center.distance_to,
                default=None,
            )
            if landing is not None:
                return landing
    return _explore(bot, state, now, center, can_fly=False)


def _tank(
    state: _CombatState,
    now: float,
    unit: Unit,
    contacts: list[Unit],
    goal: Point2,
    withdrawing: bool,
) -> None:
    targets = [
        e for e in contacts if not e.is_flying and unit.distance_to(e) < 14 + e.radius
    ]
    valid_siege = [e for e in targets if 2.1 <= unit.distance_to(e) <= 13 + e.radius]
    mobile = unit.type_id == U.SIEGETANK
    if valid_siege:
        state.last_tank_contact[unit.tag] = now
    if withdrawing:
        if not mobile:
            _send(state, now, unit, AbilityId.UNSIEGE_UNSIEGE)
        elif unit.distance_to(goal) > 2.5:
            _send(state, now, unit, "move", goal)
        return
    if mobile:
        worth_sieging = [
            e
            for e in valid_siege
            if 3.5 < unit.distance_to(e) and (_danger(e) or e.is_structure)
        ]
        if worth_sieging and not any(
            _danger(e) and unit.distance_to(e) < 3 for e in targets
        ):
            _send(state, now, unit, AbilityId.SIEGEMODE_SIEGEMODE)
            return
        target, in_range = _select(unit, targets)
        if target and in_range:
            _send(state, now, unit, "attack", target)
        elif unit.distance_to(goal) > 3:
            _send(state, now, unit, "attack", goal)
        return
    target, in_range = _select(unit, valid_siege, max_distance=14)
    if target and in_range:
        _send(state, now, unit, "attack", target)
    elif (
        not valid_siege
        and unit.distance_to(goal) > 6
        and (
            now - state.last_tank_contact.get(unit.tag, 0) > 5
            or any(unit.distance_to(e) < 2.1 and _danger(e) for e in targets)
        )
    ):
        _send(state, now, unit, AbilityId.UNSIEGE_UNSIEGE)


def _infantry(
    bot: BotAI,
    state: _CombatState,
    now: float,
    unit: Unit,
    contacts: list[Unit],
    goal: Point2,
    *,
    withdrawing: bool,
    committing: bool,
    pressure: bool,
    core: Point2,
    screen: Point2 | None,
) -> None:
    # A defensive shot is useful only once already back at the rally point.
    if withdrawing and unit.distance_to(goal) > 3:
        _send(state, now, unit, "move", goal)
        return
    target, in_range = _select(unit, contacts, max_distance=9.5)
    guard = (
        _screen_fallback(bot, unit.position, screen, goal)
        if pressure and not in_range
        else None
    )
    if target:
        nearby = [
            e
            for e in contacts
            if _danger(e)
            and (e.can_attack_ground or e.type_id in _STATIC | _HIGH_VALUE)
            and unit.distance_to(e) <= 10 + e.radius
        ]
        closest_threat = min(
            nearby, key=lambda e: (unit.distance_to(e), e.tag), default=None
        )
        if (
            in_range
            and committing
            and UpgradeId.STIMPACK in bot.state.upgrades
            and len(nearby) >= 2
            and unit.health_percentage >= 0.72
            and not unit.has_buff(BuffId.STIMPACK)
            and not unit.has_buff(BuffId.STIMPACKMARAUDER)
        ):
            ability = (
                AbilityId.EFFECT_STIM_MARINE
                if unit.type_id == U.MARINE
                else AbilityId.EFFECT_STIM_MARAUDER
            )
            _send(state, now, unit, ability)
            return
        if (
            closest_threat
            and 0 < unit.weapon_cooldown
            and (
                unit.health_percentage < 0.38
                or (
                    closest_threat.target_in_range(unit, bonus_distance=0.85)
                    and (
                        not in_range
                        or unit.ground_range >= closest_threat.ground_range + 1.0
                    )
                )
            )
        ):
            fallback = _away(bot, unit, closest_threat, 2.5)
            if fallback and (committing or fallback.distance_to(goal) < 10):
                _send(state, now, unit, "move", fallback)
                return
        pursue = (
            committing
            and guard is None
            and (
                _chase(unit, target, goal, core)
                if pressure
                else unit.distance_to(goal) <= 14
            )
        )
        if in_range or pursue:
            _send(state, now, unit, "attack", target)
            return
    if guard is not None:
        _send(state, now, unit, "move", guard)
        return
    if pressure and target and not in_range and unit.distance_to(goal) > 2.7:
        # A move command suppresses the engine's own off-axis chase. Advance a
        # few tiles and re-evaluate the visible contact before firing.
        step = unit.position.towards(goal, min(4, unit.distance_to(goal)))
        if _inside(bot, step) and bot.in_pathing_grid(step):
            _send(state, now, unit, "move", step)
            return
    if unit.distance_to(goal) > 2.7:
        _send(state, now, unit, "move" if withdrawing else "attack", goal)


def _unsafe_air(
    unit: Unit, point: Point2, enemies: list[Unit], padding: float = 1.3
) -> bool:
    return any(
        e.distance_to(point)
        < (
            8
            if e.type_id == U.BUNKER
            else e.air_range + e.radius + unit.radius + padding
        )
        for e in enemies
    )


def _air_support(
    bot: BotAI,
    state: _CombatState,
    now: float,
    unit: Unit,
    allies: list[Unit],
    anti_air: list[Unit],
    center: Point2,
    goal: Point2,
    *,
    withdrawing: bool,
) -> None:
    danger = min(
        (e for e in anti_air if _unsafe_air(unit, unit.position, [e])),
        key=lambda e: (unit.distance_to(e), e.tag),
        default=None,
    )
    if danger is not None:
        fallback = _away(bot, unit, danger, 5.5)
        if fallback:
            state.air_evade_until[unit.tag] = now + 2.5
            _send(state, now, unit, "move", fallback)
            return
    if withdrawing and unit.distance_to(goal) > 4:
        _send(state, now, unit, "move", goal)
        return
    if now < state.air_evade_until.get(unit.tag, 0):
        return
    if unit.type_id == U.MEDIVAC and unit.energy >= 5 and not withdrawing:
        injured = [
            a
            for a in allies
            if a.type_id in _BIO
            and a.health_percentage < 0.97
            and unit.distance_to(a) < 4.5
            and not _unsafe_air(unit, a.position, anti_air)
        ]
        if injured:
            patient = min(
                injured, key=lambda a: (a.health_percentage, unit.distance_to(a), a.tag)
            )
            _send(state, now, unit, AbilityId.MEDIVACHEAL_HEAL, patient)
            return
    guard_candidates = (
        [center.towards(goal, -distance) for distance in (3, 6, 9)]
        if center.distance_to(goal) > 5
        else [center]
    )
    guard = next(
        (
            p
            for p in guard_candidates
            if _inside(bot, p) and not _unsafe_air(unit, p, anti_air)
        ),
        None,
    )
    if guard is not None and unit.distance_to(guard) > 4:
        _send(state, now, unit, "move", guard)


def _viking(
    bot: BotAI,
    state: _CombatState,
    now: float,
    unit: Unit,
    contacts: list[Unit],
    center: Point2,
    goal: Point2,
    *,
    withdrawing: bool,
    committing: bool,
    has_ground: bool,
    floating_cleanup: list[Unit],
) -> None:
    airborne = unit.type_id == U.VIKINGFIGHTER
    if withdrawing:
        if (
            not airborne
            and unit.health_percentage > 0.42
            and unit.distance_to(goal) > 9
        ):
            _send(state, now, unit, AbilityId.MORPH_VIKINGFIGHTERMODE)
        elif unit.distance_to(goal) > 4:
            _send(state, now, unit, "move", goal)
        return
    air = (
        [e for e in contacts if _could_hit(unit, e)]
        if airborne
        else [
            e
            for e in contacts
            if (e.is_flying or e.type_id == U.COLOSSUS) and unit.distance_to(e) < 13
        ]
    )
    if airborne:
        target, in_range = _select(unit, air, max_distance=20 if not has_ground else 15)
        supported_air_chase = target is not None and (
            (not has_ground and committing)
            or (
                target.distance_to(center) <= 16
                and (committing or unit.distance_to(center) <= 7)
            )
        )
        if target and (in_range or supported_air_chase):
            if (
                0 < unit.weapon_cooldown
                and target.target_in_range(unit, bonus_distance=0.45)
                and target.air_range < unit.air_range
            ):
                fallback = _away(bot, unit, target, 2.8)
                if fallback:
                    _send(state, now, unit, "move", fallback)
                    return
            _send(state, now, unit, "attack", target)
            return
        if floating_cleanup:
            closest = min(floating_cleanup, key=lambda e: (unit.distance_to(e), e.tag))
            _send(state, now, unit, "attack", closest)
            return
        if committing and not has_ground and air:
            closest = min(air, key=lambda e: (unit.distance_to(e), e.tag))
            _send(state, now, unit, "attack", closest)
            return
        buildings = [e for e in contacts if not e.is_flying and e.is_structure]
        nearest_building = min(
            buildings, key=lambda e: (unit.distance_to(e), e.tag), default=None
        )

        def landing_safe(point: Point2) -> bool:
            return not any(
                (e.can_attack_ground or e.type_id in _HIGH_VALUE | {U.BUNKER})
                and e.distance_to(point)
                < max(5, e.ground_range + e.radius + unit.radius + 1)
                for e in contacts
            )

        min_health = 0.28 if not has_ground else 0.66
        if (
            committing
            and nearest_building
            and unit.distance_to(nearest_building) < 8
            and (not has_ground or now > 600)
            and now - state.last_air_seen > 22
            and unit.health_percentage > min_health
            and bot.in_pathing_grid(unit.position)
            and landing_safe(unit.position)
        ):
            _send(state, now, unit, AbilityId.MORPH_VIKINGASSAULTMODE)
            return
        if (
            committing
            and not has_ground
            and nearest_building
            and unit.health_percentage > min_health
            and now - state.last_air_seen > 22
        ):
            # Pick an adjacent landing tile, not the building footprint.
            building = nearest_building.position
            pads = [
                Point2(
                    (building.x + cos(angle) * radius, building.y + sin(angle) * radius)
                )
                for radius in (5, 7)
                for angle in (0, 0.79, 1.57, 2.36, 3.14, 3.93, 4.71, 5.50)
            ]
            landing = min(
                (
                    p
                    for p in pads
                    if _inside(bot, p) and bot.in_pathing_grid(p) and landing_safe(p)
                ),
                key=unit.distance_to,
                default=None,
            )
            if landing is not None and unit.distance_to(landing) > 0.9:
                _send(state, now, unit, "move", landing)
                return
            desired = _explore(bot, state, now, unit.position, can_fly=True)
        elif committing and (not has_ground or (now > 560 and not contacts)):
            desired = _explore(bot, state, now, unit.position, can_fly=True)
        else:
            desired = (
                center.towards(goal, min(4.0, center.distance_to(goal)))
                if center.distance_to(goal) > 1
                else center
            )
        if unit.distance_to(desired) > 3.5:
            _send(state, now, unit, "move", desired)
    else:
        if (
            air
            or floating_cleanup
            or (
                not has_ground
                and committing
                and not any(
                    not e.is_flying and e.distance_to(unit) < 12 for e in contacts
                )
            )
        ):
            _send(state, now, unit, AbilityId.MORPH_VIKINGFIGHTERMODE)
            return
        target, in_range = _select(unit, contacts, max_distance=10)
        if target and (in_range or committing):
            _send(state, now, unit, "attack", target)
        elif unit.distance_to(goal) > 3:
            _send(state, now, unit, "attack", goal)


async def control_combat(bot: BotAI, iteration: int) -> None:
    """Dispatch one combat update. Leave SCVs, Reapers, and production untouched."""
    if iteration % 2:
        return
    state = getattr(bot, "_mizuki_combat_state", None)
    if state is None:
        state = _CombatState()
        bot._mizuki_combat_state = state
    now = bot.time
    army = sorted(
        (u for u in bot.units if u.type_id in _FIELD and u.is_ready),
        key=lambda u: u.tag,
    )
    air_support = sorted(
        (u for u in bot.units if u.type_id in _ESCORT and u.is_ready),
        key=lambda u: u.tag,
    )
    ground = [u for u in army if not u.is_flying]
    units_for_center = ground or army or air_support
    home = _home(bot, units_for_center)
    center, core = _cohort(ground or army, home, state.core_anchor)
    state.core_anchor = center if core else None
    enemies = [e for e in (*bot.enemy_units, *bot.enemy_structures) if _observed(e)]
    if any(e.is_flying and (_danger(e) or e.is_structure) for e in enemies):
        state.last_air_seen = now
    defense = _defense(bot, enemies, center)
    siege_guard = _siege_guard(bot, enemies, ground, center, home)
    local_contacts = [e for e in enemies if e.distance_to(center) <= 18]
    local_air = [u for u in army if u.is_flying and u.distance_to(center) < 13]
    if local_contacts and all(e.is_flying for e in local_contacts):
        relevant = [u for u in core if u.can_attack_air] + [
            u for u in local_air if u not in core
        ]
    else:
        relevant = core
    phase = _next_phase(
        state,
        now,
        _strength(relevant),
        _strength(local_contacts),
        center.distance_to(home),
        sum(not h.is_flying for h in bot.townhalls),
    )
    defending = defense is not None or siege_guard is not None
    withdrawing = phase == _Phase.RETREAT and not defending
    pressure = phase == _Phase.PRESSURE and not defending
    committing = defense is not None or pressure
    floating_cleanup = (
        [e for e in enemies if e.is_structure and e.is_flying]
        if pressure and not any(e.is_structure and not e.is_flying for e in enemies)
        else []
    )
    goal = home
    contacts = enemies
    if siege_guard is not None:
        goal = siege_guard.rally
    elif defense is not None:
        anchor, attackers = defense
        goal = min(attackers, key=lambda e: (e.distance_to(anchor), e.tag)).position
        contacts = [
            e
            for e in enemies
            if e.distance_to(anchor) <= 21 or e.distance_to(center) <= 10
        ]
    elif pressure and army:
        if ground:
            goal = _ground_objective(
                bot,
                state,
                now,
                center,
                contacts,
                floating_cleanup,
                can_shoot_air=any(u.can_attack_air for u in ground),
            )
        else:
            goal = _explore(bot, state, now, center, can_fly=True)
    elif not withdrawing:
        contacts = [
            e
            for e in enemies
            if e.distance_to(home) <= 11 or e.distance_to(center) <= 10
        ]
    anti_air = [e for e in enemies if e.can_attack_air or e.type_id == U.BUNKER]
    alive = {u.tag for u in (*army, *air_support)}
    state.last = {tag: intent for tag, intent in state.last.items() if tag in alive}
    state.last_tank_contact = {
        tag: when for tag, when in state.last_tank_contact.items() if tag in alive
    }
    state.air_evade_until = {
        tag: when
        for tag, when in state.air_evade_until.items()
        if tag in alive and when > now
    }
    reserve = _natural_reserve(
        bot, state, now, phase, ground, enemies, defense, siege_guard
    )
    post, reserve_tags = reserve if reserve else (None, set())
    screen = (
        _armor_screen([u for u in ground if u.tag not in reserve_tags], center, goal)
        if pressure and not floating_cleanup
        else None
    )
    zones = _hazards(bot)
    for unit in army:
        if _evade(bot, state, now, unit, zones):
            continue
        if post is not None and unit.tag in reserve_tags:
            _post_unit(state, now, unit, enemies, post)
            continue
        if siege_guard is not None and not unit.is_flying:
            _guard_line(bot, state, now, unit, contacts, siege_guard)
            continue
        if unit.type_id in _TANKS:
            _tank(state, now, unit, contacts, goal, withdrawing)
        elif unit.type_id in _BIO:
            _infantry(
                bot,
                state,
                now,
                unit,
                contacts,
                goal,
                withdrawing=withdrawing,
                committing=committing,
                pressure=pressure,
                core=center,
                screen=screen,
            )
        elif unit.type_id in _VIKINGS:
            has_ground = bool(ground) and (len(ground) > 1 or ground[0].tag != unit.tag)
            _viking(
                bot,
                state,
                now,
                unit,
                contacts,
                siege_guard.rally if siege_guard else center,
                goal,
                withdrawing=withdrawing,
                committing=committing and siege_guard is None,
                has_ground=has_ground,
                floating_cleanup=floating_cleanup,
            )
    for unit in air_support:
        if _evade(bot, state, now, unit, zones):
            continue
        escort_center = siege_guard.rally if siege_guard else center
        escort_goal = siege_guard.guns[0].position if siege_guard else goal
        _air_support(
            bot,
            state,
            now,
            unit,
            army,
            anti_air,
            escort_center,
            escort_goal,
            withdrawing=withdrawing,
        )
