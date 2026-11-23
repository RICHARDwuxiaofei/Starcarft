"""Offline adapter tests using in-memory fakes; SC2 is never launched.

These tests exercise the BotAI glue: observation -> Snapshot -> Orders -> unit
commands. Fakes imitate just the attributes the adapter reads.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bots.hunyuan.bot import ADDON_ABILITIES, UNIT_IDS, UPGRADE_IDS, CompetitiveBot
from bots.hunyuan.model import Contact, Order


class FakeUnit:
    def __init__(
        self,
        tag: int,
        kind: U,
        x: float,
        y: float,
        *,
        structure: bool = False,
        health: float = 100.0,
        health_max: float = 100.0,
        progress: float = 1.0,
        energy: float = 0.0,
        add_on_tag: int = 0,
        is_idle: bool = True,
        orders: list | None = None,
    ) -> None:
        self.tag = tag
        self.type_id = kind
        self.position = Point2((x, y))
        self.is_structure = structure
        self.health = health
        self.health_max = health_max
        self.build_progress = progress
        self.energy = energy
        self.add_on_tag = add_on_tag
        self.is_idle = is_idle
        self.orders = orders or []
        self.commands: list[tuple] = []

    def __call__(self, ability, target=None, queue=False):
        self.commands.append(("ability", ability, target))
        return True

    def attack(self, target):
        self.commands.append(("attack", target))
        return True

    def move(self, target):
        self.commands.append(("move", target))
        return True

    def gather(self, target):
        self.commands.append(("gather", target))
        return True

    @property
    def is_ready(self):
        return self.build_progress >= 0.99

    @property
    def has_add_on(self):
        return bool(self.add_on_tag)

    def distance_to(self, other):
        return ((self.position.x - other.position.x) ** 2 + (self.position.y - other.position.y) ** 2) ** 0.5


class FakeUnits(list):
    def __or__(self, other):
        return FakeUnits(list(self) + list(other))

    def __call__(self, kind=None):
        if kind is None:
            return self
        return FakeUnits([u for u in self if u.type_id == kind])

    @property
    def ready(self):
        return FakeUnits([u for u in self if u.is_ready])

    @property
    def first(self):
        return self[0]

    def find_by_tag(self, tag):
        return next((u for u in self if u.tag == tag), None)

    def closest_to(self, other):
        return min(self, key=lambda u: u.distance_to(other))


def make_bot() -> CompetitiveBot:
    """Create a CompetitiveBot without running BotAI's game bootstrap."""
    bot = CompetitiveBot()
    cc = FakeUnit(1, U.COMMANDCENTER, 10, 10, structure=True, health=1500, health_max=1500)
    depot = FakeUnit(2, U.SUPPLYDEPOT, 13, 12, structure=True)
    barracks = FakeUnit(3, U.BARRACKS, 15, 13, structure=True, add_on_tag=0)
    scv = FakeUnit(100, U.SCV, 11, 9)
    marine = FakeUnit(200, U.MARINE, 20, 20)
    geyser = FakeUnit(500, U.VESPENEGEYSER, 14, 8)
    mineral = FakeUnit(600, U.MINERALFIELD, 12, 8)

    bot.units = FakeUnits([scv, marine])
    bot.structures = FakeUnits([cc, depot, barracks])
    bot.all_own_units = FakeUnits([scv, marine, cc, depot, barracks])
    bot.workers = FakeUnits([scv])
    bot.townhalls = FakeUnits([cc])
    bot.gas_buildings = FakeUnits([])
    bot.vespene_geyser = FakeUnits([geyser])
    bot.mineral_field = FakeUnits([mineral])
    bot.enemy_units = FakeUnits([])
    bot.enemy_structures = FakeUnits([])
    bot.techlab_tags = set()
    bot.reactor_tags = set()
    bot.minerals = 800
    bot.vespene = 500
    bot.supply_used = 15
    bot.supply_cap = 38
    bot.supply_left = 23
    bot._generated_frame = 1
    bot.state = SimpleNamespace(
        game_loop=448,  # time = 20s
        upgrades=set(),
        alerts=[],
    )
    bot.game_info = SimpleNamespace(
        player_start_location=Point2((10, 10)),
        map_center=Point2((80, 80)),
        player_races={1: 1, 2: 1, 3: 1, 4: 1},
        start_locations=[Point2((10, 10)), Point2((150, 150))],
    )
    bot._expansion_positions_list = [Point2((40, 40)), Point2((120, 120))]
    bot.cache = {}  # reset property_cache_once_per_frame caches
    bot.actions = []
    bot.unit_tags_received_action = set()

    # Patch API calls that would need a live client.
    bot.already_pending = lambda unit: 0
    bot.already_pending_upgrade = lambda upgrade: 0
    bot.can_afford = lambda item: True
    bot.train = lambda unit_type, amount=1, closest_to=None, train_only_idle_buildings=True: amount
    bot.research = lambda upgrade: True
    bot.distribute_workers = _async_noop
    bot.build = _fake_build(bot)
    bot.expand_now = _async_noop
    return bot


async def _async_noop(*args, **kwargs):
    return None


def _fake_build(bot):
    async def build(unit_type, near=None, **kwargs):
        bot.last_build = (unit_type, near)
        return True

    return build


# ---------------------------------------------------------------------------
def test_snapshot_uses_only_visible_information():
    bot = make_bot()
    snap = bot._snapshot()
    assert snap.time == 20.0
    assert snap.minerals == 800
    assert any(u.kind == "SCV" for u in snap.units)
    assert any(u.kind == "COMMANDCENTER" for u in snap.units)
    assert snap.enemy_visible == []
    assert snap.start == (10.0, 10.0)
    assert snap.enemy_start == (150.0, 150.0)
    assert snap.expansion_points == [(40.0, 40.0), (120.0, 120.0)]


def test_remember_enemies_uses_visible_units_and_expires():
    bot = make_bot()
    zergling = FakeUnit(900, U.ZERGLING, 12, 12)
    bot.enemy_units = FakeUnits([zergling])
    bot._remember_enemies()
    assert 900 in bot._enemy_memory
    assert isinstance(bot._enemy_memory[900], Contact)
    # Simulate time passing beyond the memory window.
    bot.state.game_loop = 448 + 22.4 * 60
    bot.enemy_units = FakeUnits([])
    bot.enemy_structures = FakeUnits([])
    bot._remember_enemies()
    assert 900 not in bot._enemy_memory


def test_translate_constants_exist():
    assert UNIT_IDS["MARINE"] == U.MARINE
    assert UNIT_IDS["SIEGETANK"] == U.SIEGETANK
    assert "STIMPACK" in UPGRADE_IDS
    assert ("BARRACKS", "techlab") in ADDON_ABILITIES
    assert ADDON_ABILITIES[("BARRACKS", "techlab")] == AbilityId.BUILD_TECHLAB_BARRACKS


def test_execute_attack_move_translates_to_unit_command():
    bot = make_bot()
    marine = bot.all_own_units.find_by_tag(200)
    order = Order(kind="attack", tags=(200,), x=140.0, y=140.0, target="enemy_main")
    asyncio.run(bot._execute(order))
    assert marine.commands and marine.commands[-1][0] == "attack"
    target = marine.commands[-1][1]
    assert isinstance(target, Point2)
    assert (target.x, target.y) == (140.0, 140.0)


def test_execute_scout_and_scan_commands():
    bot = make_bot()
    scv = bot.all_own_units.find_by_tag(100)
    asyncio.run(bot._execute(Order(kind="scout", tags=(100,), target="enemy_main")))
    assert scv.commands[-1][0] == "move"

    orbital = FakeUnit(7, U.ORBITALCOMMAND, 10, 10, structure=True)
    bot.all_own_units.append(orbital)
    asyncio.run(bot._execute(Order(kind="scan", tags=(7,), target="enemy_main")))
    assert orbital.commands[-1][0] == "ability"
    assert orbital.commands[-1][1] == AbilityId.SCANNERSWEEP_SCAN


def test_execute_addon_builds_techlab_on_idle_barracks():
    bot = make_bot()
    barracks = bot.structures.find_by_tag(3)
    asyncio.run(bot._execute(Order(kind="addon", unit="BARRACKS", addon="techlab")))
    assert barracks.commands[-1][1] == AbilityId.BUILD_TECHLAB_BARRACKS


def test_refinery_uses_geyser_unit_not_point():
    bot = make_bot()
    asyncio.run(bot._execute(Order(kind="build", unit="REFINERY", near="main")))
    unit_type, near = bot.last_build
    assert unit_type == U.REFINERY
    assert isinstance(near, FakeUnit)
    assert near.type_id == U.VESPENEGEYSER


def test_full_on_step_without_game_does_not_raise():
    bot = make_bot()
    asyncio.run(bot.on_step(iteration=0))
    assert bot._last_orders, "planner should have produced orders"
    assert any(o.kind in {"train", "build", "scout"} for o in bot._last_orders)
