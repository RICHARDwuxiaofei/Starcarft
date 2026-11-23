"""CompetitiveBot -- original Terran arena bot for StarCraft II 5.0.17.

This module is the only layer that talks to the game. All strategic decisions
live in the pure :mod:`bots.hunyuan.planner` module, which keeps the bot
deterministic and offline-testable.

The bot uses only normal vision (``self.units``, ``self.enemy_units``,
``self.structures`` and last-seen memory from visible enemies) and legal SC2 API
actions. It never inspects opponent processes, debug-cheats resources or calls
any external decision service during a match.
"""

from __future__ import annotations

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from . import planner
from .model import Contact, Memory, Order, Snapshot, UnitInfo

UNIT_IDS: dict[str, UnitTypeId] = {
    name: getattr(U, name)
    for name in dir(U)
    if name.isupper() and isinstance(getattr(U, name), UnitTypeId)
}

UPGRADE_IDS: dict[str, UpgradeId] = {
    name: getattr(UpgradeId, name)
    for name in dir(UpgradeId)
    if name.isupper() and isinstance(getattr(UpgradeId, name), UpgradeId)
}

ADDON_ABILITIES: dict[tuple[str, str], AbilityId] = {
    ("BARRACKS", "techlab"): AbilityId.BUILD_TECHLAB_BARRACKS,
    ("BARRACKS", "reactor"): AbilityId.BUILD_REACTOR_BARRACKS,
    ("FACTORY", "techlab"): AbilityId.BUILD_TECHLAB_FACTORY,
    ("FACTORY", "reactor"): AbilityId.BUILD_REACTOR_FACTORY,
    ("STARPORT", "techlab"): AbilityId.BUILD_TECHLAB_STARPORT,
    ("STARPORT", "reactor"): AbilityId.BUILD_REACTOR_STARPORT,
}

PENDING_KINDS = {
    "SCV", "MARINE", "MARAUDER", "SIEGETANK", "MEDIVAC", "THOR",
    "SUPPLYDEPOT", "REFINERY", "COMMANDCENTER", "BARRACKS", "FACTORY",
    "STARPORT", "ENGINEERINGBAY", "ARMORY", "ORBITALCOMMAND",
}


class CompetitiveBot(BotAI):
    """Terran bot ready for ``sc2.main.run_game`` with no constructor arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.seed: int = 20261123
        self.memory: Memory = Memory(seed=self.seed)
        self._enemy_memory: dict[int, Contact] = {}
        self._iteration: int = 0
        self._last_orders: list[Order] = []
        self.failed_placements: int = 0
        self.last_placement_error: str = ""

    # ---- lifecycle ------------------------------------------------------------
    async def on_start(self) -> None:
        self._enemy_memory.clear()
        await self.distribute_workers()

    async def on_step(self, iteration: int) -> None:
        self._iteration = iteration
        self._remember_enemies()
        snap = self._snapshot()
        orders = planner.plan(snap, self.memory)
        self._last_orders = orders
        for order in orders:
            await self._execute(order)

        # Rebalance workers after harassment or a new expansion; not every frame.
        if iteration % 40 == 0:
            await self.distribute_workers()
        self._assign_idle_workers()

    async def on_end(self, game_result) -> None:  # pragma: no cover - game only
        self._last_orders = []

    # ---- observation ----------------------------------------------------------
    def _remember_enemies(self) -> None:
        for unit in self.enemy_units | self.enemy_structures:
            self._enemy_memory[unit.tag] = Contact(
                kind=unit.type_id.name,
                x=float(unit.position.x),
                y=float(unit.position.y),
                last_seen=self.time,
                is_structure=bool(unit.is_structure),
                tag=unit.tag,
            )
        expired = [
            tag
            for tag, contact in self._enemy_memory.items()
            if self.time - contact.last_seen > (900.0 if contact.is_structure else 45.0)
        ]
        for tag in expired:
            del self._enemy_memory[tag]

    def _to_unit_info(self, unit) -> UnitInfo:
        addon = None
        if unit.add_on_tag in self.techlab_tags:
            addon = "techlab"
        elif unit.add_on_tag in self.reactor_tags:
            addon = "reactor"
        role = "structure" if unit.is_structure else ("worker" if unit.type_id == U.SCV else "army")
        return UnitInfo(
            tag=unit.tag,
            kind=unit.type_id.name,
            x=float(unit.position.x),
            y=float(unit.position.y),
            hp=float(unit.health),
            hp_max=float(unit.health_max),
            progress=float(unit.build_progress),
            energy=float(getattr(unit, "energy", 0.0)),
            addon=addon,
            role=role,
            idle=bool(unit.is_idle),
            order_abilities=tuple(o.ability.id for o in unit.orders),
        )

    def _snapshot(self) -> Snapshot:
        units = [self._to_unit_info(u) for u in list(self.units) + list(self.structures)]

        pending: dict[str, int] = {}
        for name in PENDING_KINDS:
            enum = UNIT_IDS.get(name)
            if enum is not None:
                pending[name] = int(self.already_pending(enum))

        upgrades: set[str] = set()
        try:
            upgrades = {u.name for u in self.state.upgrades}
        except (AttributeError, RuntimeError):  # pragma: no cover - no raw obs
            pass
        for name in upgrades:
            if name in UPGRADE_IDS:
                pending[f"UPGRADE:{name}"] = int(self.already_pending_upgrade(UPGRADE_IDS[name]))

        try:
            alerts = set(self.state.alerts)
        except (AttributeError, TypeError):  # pragma: no cover
            alerts = set()

        return Snapshot(
            time=self.time,
            minerals=int(self.minerals),
            vespene=int(self.vespene),
            supply_used=int(self.supply_used),
            supply_cap=int(self.supply_cap),
            supply_left=int(self.supply_left),
            units=units,
            enemy_visible=[self._to_unit_info(u) for u in self.enemy_units | self.enemy_structures],
            enemy_memory=list(self._enemy_memory.values()),
            expansion_points=[(float(p.x), float(p.y)) for p in self.expansion_locations_list],
            start=(float(self.start_location.x), float(self.start_location.y)),
            enemy_start=self._enemy_start(),
            map_center=(float(self.game_info.map_center.x), float(self.game_info.map_center.y)),
            pending=pending,
            upgrades=upgrades,
            alerts=alerts,
        )

    def _enemy_start(self) -> tuple[float, float]:
        # In burnysc2 7.3.0 ``enemy_start_locations`` simply returns all map
        # start locations, including our own. On a 1v1 map the enemy spawn is
        # therefore the start location farthest from our own.
        own = self.start_location
        candidates = [
            p for p in self.enemy_start_locations
            if (p.x - own.x) ** 2 + (p.y - own.y) ** 2 > 1.0
        ]
        if candidates:
            p = max(candidates, key=lambda q: (q.x - own.x) ** 2 + (q.y - own.y) ** 2)
            return (float(p.x), float(p.y))
        centre = self.game_info.map_center
        return (float(centre.x), float(centre.y))

    # ---- helpers --------------------------------------------------------------
    def _assign_idle_workers(self) -> None:
        if not self.mineral_field:
            return
        for worker in self.workers:
            if worker.is_idle:
                target = min(self.mineral_field, key=lambda m: (worker.distance_to(m), m.tag))
                worker.gather(target)

    def _point(self, order: Order, default: Point2) -> Point2:
        if order.x is not None and order.y is not None:
            return Point2((order.x, order.y))
        return default

    def _near(self, where: str | None) -> Point2:
        if where == "natural" and self.expansion_locations_list:
            return min(
                self.expansion_locations_list,
                key=lambda p: (p.distance_to(self.start_location), p.x, p.y),
            )
        if where == "rally":
            point = planner.rally_point(
                Snapshot(
                    time=0.0, minerals=0, vespene=0, supply_used=0,
                    supply_cap=0, supply_left=0,
                    start=(float(self.start_location.x), float(self.start_location.y)),
                    map_center=(float(self.game_info.map_center.x), float(self.game_info.map_center.y)),
                )
            )
            return Point2(point)
        return self.start_location

    def _unit_by_tag(self, tag: int):
        return self.all_own_units.find_by_tag(tag)

    # ---- order execution ------------------------------------------------------
    async def _execute(self, order: Order) -> None:
        kind = order.kind
        if kind == "train":
            self._do_train(order.unit)
        elif kind == "build":
            await self._do_build(order)
        elif kind == "expand":
            await self._do_expand()
        elif kind == "addon":
            self._do_addon(order)
        elif kind == "morph":
            self._do_morph()
        elif kind == "research":
            self._do_research(order.upgrade)
        elif kind == "scout":
            self._do_scout(order)
        elif kind == "scan":
            self._do_scan(order)
        elif kind == "attack":
            self._do_attack(order)
        elif kind == "move":
            self._do_move(order)
        elif kind == "worker_defend":
            self._do_worker_defend(order)
        # Unknown kinds are ignored: forward-compatible with planner changes.

    def _do_train(self, name: str | None) -> None:
        if not name:
            return
        unit_type = UNIT_IDS.get(name)
        if unit_type is not None:
            self.train(unit_type, amount=1, train_only_idle_buildings=True)

    async def _do_build(self, order: Order) -> None:
        if not order.unit:
            return
        unit_type = UNIT_IDS.get(order.unit)
        if unit_type is None:
            return
        try:
            if unit_type == U.REFINERY:
                geyser = self._free_geyser(self._near(order.near))
                if geyser is None:
                    return
                # For gas structures the API needs the geyser unit, not a point.
                await self.build(unit_type, near=geyser)
            else:
                await self.build(
                    unit_type,
                    near=self._near(order.near),
                    max_distance=12,
                    placement_step=2,
                    random_alternative=False,
                )
        except Exception as exc:  # noqa: BLE001 - placement queries can fail on contested maps; never crash a match
            self.failed_placements += 1
            self.last_placement_error = f"build {order.unit}: {exc}"

    def _free_geyser(self, near: Point2):
        """Nearest visible geyser with no refinery, closest to a townhall."""
        occupied = {g.position for g in self.gas_buildings}
        candidates = [
            g
            for g in self.vespene_geyser
            if not any((g.position - p).length < 1.5 for p in occupied)
            and self.townhalls
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda g: (
                g.distance_to(near),
                g.distance_to(self.townhalls.closest_to(g)),
                g.tag,
            ),
        )

    async def _do_expand(self) -> None:
        try:
            await self.expand_now(max_distance=10)
        except Exception as exc:  # noqa: BLE001 - expand_now may fail when a location is blocked
            self.failed_placements += 1
            self.last_placement_error = f"expand: {exc}"

    def _do_addon(self, order: Order) -> None:
        ability = ADDON_ABILITIES.get((order.unit or "", order.addon or ""))
        if ability is None:
            return
        category = {
            "BARRACKS": {U.BARRACKS, U.BARRACKSFLYING},
            "FACTORY": {U.FACTORY, U.FACTORYFLYING},
            "STARPORT": {U.STARPORT, U.STARPORTFLYING},
        }.get(order.unit or "", set())
        for structure in self.structures:
            if (
                structure.type_id in category
                and structure.is_ready
                and structure.is_idle
                and not structure.has_add_on
            ):
                structure(ability)
                return

    def _do_morph(self) -> None:
        if self.structures(U.ORBITALCOMMAND) or self.already_pending(U.ORBITALCOMMAND):
            return
        centers = self.structures(U.COMMANDCENTER).ready
        # calculate_cost(UnitTypeId.ORBITALCOMMAND) gives the correct morph cost.
        if centers and self.structures(U.BARRACKS).ready and self.can_afford(U.ORBITALCOMMAND):
            centers.first(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND)

    def _do_research(self, name: str | None) -> None:
        if name and name in UPGRADE_IDS:
            self.research(UPGRADE_IDS[name])

    def _do_scout(self, order: Order) -> None:
        if not order.tags:
            return
        target = Point2(self._enemy_start()) if order.target == "enemy_main" else self.start_location
        for tag in order.tags:
            unit = self._unit_by_tag(tag)
            if unit:
                unit.move(target)

    def _do_scan(self, order: Order) -> None:
        if not order.tags:
            return
        target = Point2(self._enemy_start())
        for tag in order.tags:
            unit = self._unit_by_tag(tag)
            if unit:
                unit(AbilityId.SCANNERSWEEP_SCAN, target)

    def _do_attack(self, order: Order) -> None:
        target = self._point(order, Point2(self._enemy_start()))
        for tag in order.tags:
            unit = self._unit_by_tag(tag)
            if unit:
                unit.attack(target)

    def _do_move(self, order: Order) -> None:
        target = self._point(order, self.start_location)
        for tag in order.tags:
            unit = self._unit_by_tag(tag)
            if unit:
                unit.move(target)

    def _do_worker_defend(self, order: Order) -> None:
        target = self._point(order, self.start_location)
        for tag in order.tags:
            unit = self._unit_by_tag(tag)
            if unit:
                unit.attack(target)
