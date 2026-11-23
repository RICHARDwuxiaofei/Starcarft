"""Offline arena policy tests using burnysc2 units and synthetic observations."""

import importlib.util
import unittest
from importlib.metadata import version
from math import inf
from pathlib import Path
from types import SimpleNamespace

from s2clientprotocol import data_pb2, raw_pb2, sc2api_pb2
from sc2.bot_ai import BotAI
from sc2.data import Race
from sc2.game_data import GameData
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId as G
from sc2.position import Point2, Rect
from sc2.unit import Unit
from sc2.units import Units

from bots.codex.bot import CompetitiveBot

HOME = Point2((30.5, 30.5))
NATURAL = Point2((49.5, 48.5))
THIRD = Point2((57.5, 29.5))
FOURTH = Point2((63.5, 52.5))
ENEMY = Point2((109.5, 104.5))

# Minimal data for exercising the real Unit, UnitCommand, cost and pending helpers.
STRUCTURES = {
    U.COMMANDCENTER: (A.TERRANBUILD_COMMANDCENTER, 400, 0),
    U.ORBITALCOMMAND: (A.UPGRADETOORBITAL_ORBITALCOMMAND, 550, 0),
    U.SUPPLYDEPOT: (A.TERRANBUILD_SUPPLYDEPOT, 100, 0),
    U.BARRACKS: (A.TERRANBUILD_BARRACKS, 150, 0),
    U.REFINERY: (A.TERRANBUILD_REFINERY, 75, 0),
    U.FACTORY: (A.TERRANBUILD_FACTORY, 150, 100),
    U.STARPORT: (A.TERRANBUILD_STARPORT, 150, 100),
    U.BUNKER: (A.TERRANBUILD_BUNKER, 100, 0),
    U.ENGINEERINGBAY: (A.TERRANBUILD_ENGINEERINGBAY, 125, 0),
    U.ARMORY: (A.TERRANBUILD_ARMORY, 150, 100),
    U.MISSILETURRET: (A.TERRANBUILD_MISSILETURRET, 100, 0),
    U.BARRACKSTECHLAB: (A.BUILD_TECHLAB_BARRACKS, 50, 25),
    U.BARRACKSREACTOR: (A.BUILD_REACTOR_BARRACKS, 50, 50),
    U.FACTORYTECHLAB: (A.BUILD_TECHLAB_FACTORY, 50, 25),
}
UPGRADES = {
    G.STIMPACK: (A.BARRACKSTECHLABRESEARCH_STIMPACK, 100, 100),
    G.SHIELDWALL: (A.RESEARCH_COMBATSHIELD, 100, 100),
    G.PUNISHERGRENADES: (A.RESEARCH_CONCUSSIVESHELLS, 50, 50),
    G.TERRANINFANTRYWEAPONSLEVEL1: (A.ENGINEERINGBAYRESEARCH_TERRANINFANTRYWEAPONSLEVEL1, 100, 100),
    G.TERRANINFANTRYARMORSLEVEL1: (A.ENGINEERINGBAYRESEARCH_TERRANINFANTRYARMORLEVEL1, 100, 100),
}
TRAINED = {
    U.SCV: (A.COMMANDCENTERTRAIN_SCV, 50, 0, 1),
    U.MARINE: (A.BARRACKSTRAIN_MARINE, 50, 0, 1),
    U.REAPER: (A.BARRACKSTRAIN_REAPER, 50, 50, 1),
    U.MARAUDER: (A.BARRACKSTRAIN_MARAUDER, 100, 25, 2),
    U.HELLION: (A.FACTORYTRAIN_HELLION, 100, 0, 2),
    U.SIEGETANK: (A.FACTORYTRAIN_SIEGETANK, 150, 125, 3),
    U.MEDIVAC: (A.STARPORTTRAIN_MEDIVAC, 100, 100, 2),
    U.VIKINGFIGHTER: (A.STARPORTTRAIN_VIKINGFIGHTER, 150, 75, 2),
}
WEAPONS = {
    U.SCV: (data_pb2.Weapon.Ground, 0.1),
    U.MARINE: (data_pb2.Weapon.Any, 5),
    U.MARAUDER: (data_pb2.Weapon.Ground, 6),
    U.REAPER: (data_pb2.Weapon.Ground, 5),
    U.HELLION: (data_pb2.Weapon.Ground, 5),
    U.SIEGETANK: (data_pb2.Weapon.Ground, 7),
    U.SIEGETANKSIEGED: (data_pb2.Weapon.Ground, 13),
    U.STALKER: (data_pb2.Weapon.Any, 6),
    U.BANSHEE: (data_pb2.Weapon.Ground, 6),
    U.VIKINGFIGHTER: (data_pb2.Weapon.Air, 9),
}


def fixture_data():
    data = sc2api_pb2.ResponseData()
    for ability in A:
        if ability.value:
            data.abilities.add(ability_id=ability.value, link_name=ability.name)
    for kind, (ability, minerals, gas) in STRUCTURES.items():
        unit = data.units.add(
            unit_id=kind.value,
            name=kind.name,
            available=True,
            ability_id=ability.value,
            mineral_cost=minerals,
            vespene_cost=gas,
            attributes=[data_pb2.Structure],
        )
        if kind == U.ORBITALCOMMAND:
            unit.tech_alias.append(U.COMMANDCENTER.value)
    for kind, (ability, minerals, gas, supply) in TRAINED.items():
        data.units.add(
            unit_id=kind.value,
            name=kind.name,
            available=True,
            ability_id=ability.value,
            mineral_cost=minerals,
            vespene_cost=gas,
            food_required=supply,
        )
    for kind, (ability, minerals, gas) in UPGRADES.items():
        data.upgrades.add(
            upgrade_id=kind.value,
            name=kind.name,
            ability_id=ability.value,
            mineral_cost=minerals,
            vespene_cost=gas,
        )
    present = {u.unit_id: u for u in data.units}
    for kind in (*WEAPONS, U.MINERALFIELD, U.VESPENEGEYSER):
        unit = present.get(kind.value)
        if unit is None:
            unit = data.units.add(unit_id=kind.value, name=kind.name, available=True)
        if kind in WEAPONS:
            target, radius = WEAPONS[kind]
            unit.weapons.add(type=target, range=radius, damage=10, attacks=1, speed=1)
    return GameData(data)


class OfflineArena(CompetitiveBot):
    def __init__(self):
        super().__init__()
        self._initialize_variables()
        self._distances_override_functions(0)
        self.race = Race.Terran
        self.game_data = fixture_data()
        self.game_info = SimpleNamespace(
            player_start_location=HOME,
            start_locations=[ENEMY],
            map_center=Point2((70, 65)),
            playable_area=Rect((0, 0, 145, 140)),
        )
        self._expansion_positions_list = [HOME, NATURAL, THIRD, FOURTH, ENEMY]
        self.rejected_sites = set()
        self.unreachable_sites = set()
        self.placement_checks = []
        self.pathing_checks = []
        self.visible_points = {HOME}
        self.blocked_tiles = set()
        self.client = SimpleNamespace(query_pathing=self.fixture_path)
        self.state = SimpleNamespace(game_loop=0, upgrades=set())
        self.set_resources(50, 0, 12, 15)

    def set_resources(self, minerals, gas, used, cap):
        self.minerals, self.vespene = minerals, gas
        self.supply_used, self.supply_cap = used, cap
        self.supply_left = cap - used

    def frame(self, seconds):
        self.state.game_loop = round(seconds * 22.4)
        self.actions.clear()
        self.unit_tags_received_action = set()

    def observation(self, *, workers=(), army=(), buildings=(), enemies=(), enemy_buildings=()):
        self.workers = Units(workers, self)
        self.units = Units([*workers, *army], self)
        self.structures = Units(buildings, self)
        self.townhalls = self.structures({U.COMMANDCENTER, U.ORBITALCOMMAND})
        self.gas_buildings = self.structures(U.REFINERY)
        self.enemy_units = Units(enemies, self)
        self.enemy_structures = Units(enemy_buildings, self)

    def unit(self, kind, tag, point=HOME, *, order=None, **fields):
        values = {
            "tag": tag,
            "unit_type": kind.value,
            "pos": {"x": point.x, "y": point.y},
            "radius": 0.5,
            "build_progress": 1,
            "health": 45,
            "health_max": 45,
            "display_type": raw_pb2.Visible,
            "cloak": raw_pb2.NotCloaked,
        }
        values.update(fields)
        proto = raw_pb2.Unit(**values)
        if order:
            ability, target = order
            command = proto.orders.add(ability_id=ability.value)
            if isinstance(target, Point2):
                command.target_world_space_pos.x = target.x
                command.target_world_space_pos.y = target.y
            elif target is not None:
                command.target_unit_tag = target
        return Unit(proto, self, base_build=98745)

    def mineral_line(self):
        self.mineral_field = Units(
            [
                self.unit(U.MINERALFIELD, 80 + index, Point2((24, 27 + index)), mineral_contents=900)
                for index in range(8)
            ],
            self,
        )

    def is_visible(self, point):
        return point in self.visible_points

    def in_placement_grid(self, point):
        return point.rounded not in self.blocked_tiles

    def in_pathing_grid(self, point):
        return point.rounded not in self.blocked_tiles

    def has_creep(self, point):
        return False

    async def can_place(self, kind, points):
        self.placement_checks.extend((kind, p) for p in points)
        return [p not in self.rejected_sites for p in points]

    async def fixture_path(self, worker, point):
        self.pathing_checks.append(point)
        return None if point in self.unreachable_sites else worker.distance_to(point)

    async def get_available_abilities(self, units):
        return [[A.BUILD_TECHLAB, A.BUILD_REACTOR] for _ in units]

    @property
    def abilities(self):
        return [command.ability for command in self.actions]


class ArenaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = OfflineArena()
        await self.bot.on_start()

    def home(self, kind=U.COMMANDCENTER, **fields):
        return self.bot.unit(kind, 1, **({"ideal_harvesters": 16} | fields))

    def workers(self, count=12):
        return [
            self.bot.unit(U.SCV, tag, Point2((29, 27 + index % 7)))
            for index, tag in enumerate(range(10, 10 + count))
        ]

    def army(self, size=14, tanks=3, medivacs=2):
        return [
            *(self.bot.unit(U.MARINE, 200 + i, Point2((35 + i % 3, 34 + i % 2))) for i in range(size)),
            *(
                self.bot.unit(U.SIEGETANK, 230 + i, Point2((35, 35 + i)), health=175, health_max=175)
                for i in range(tanks)
            ),
            *(
                self.bot.unit(U.MEDIVAC, 240 + i, Point2((34, 34 + i)), is_flying=True)
                for i in range(medivacs)
            ),
        ]

    async def test_direct_file_entry_has_no_custom_loader_requirements(self):
        spec = importlib.util.spec_from_file_location("arena_entry", Path(__file__).with_name("bot.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIsInstance(module.CompetitiveBot(), BotAI)
        self.assertEqual(version("burnysc2"), "7.3.0")

    async def test_opening_actions_from_workers_to_factory(self):
        b = self.bot
        b.mineral_line()
        b.observation(workers=self.workers(), buildings=[self.home()])
        await b.on_step(0)
        self.assertIn(A.COMMANDCENTERTRAIN_SCV, b.abilities)
        self.assertEqual(b.abilities.count(A.HARVEST_GATHER), 12)
        self.assertEqual(b.minerals, 0)

        b.frame(13)
        b.set_resources(100, 0, 13, 15)
        b.observation(workers=self.workers(13), buildings=[self.home()])
        await b.on_step(1)
        self.assertIn(A.TERRANBUILD_SUPPLYDEPOT, b.abilities)
        self.assertEqual(b.minerals, 0)

        b.frame(34)
        b.set_resources(150, 0, 14, 23)
        depot = b.unit(U.SUPPLYDEPOT, 2, Point2((29, 24)))
        b.observation(workers=self.workers(14), buildings=[self.home(), depot])
        await b.on_step(2)
        self.assertIn(A.TERRANBUILD_BARRACKS, b.abilities)

        b.frame(82)
        b.set_resources(250, 0, 15, 23)
        barracks = b.unit(U.BARRACKS, 3, Point2((38.5, 33.5)))
        b.vespene_geyser = Units(
            [b.unit(U.VESPENEGEYSER, 92, Point2((35.5, 25.5)), vespene_contents=2250)], b
        )
        b.observation(workers=self.workers(15), buildings=[self.home(), depot, barracks])
        await b.on_step(3)
        self.assertIn(A.TERRANBUILD_REFINERY, b.abilities)
        self.assertIn(A.UPGRADETOORBITAL_ORBITALCOMMAND, b.abilities)
        self.assertEqual(b.minerals, 25)

        b.frame(108)
        b.set_resources(250, 100, 17, 23)
        refinery = b.unit(U.REFINERY, 4, Point2((35.5, 25.5)), vespene_contents=2250)
        b.observation(
            workers=self.workers(15), buildings=[self.home(U.ORBITALCOMMAND), depot, barracks, refinery]
        )
        await b.on_step(4)
        self.assertIn(A.TERRANBUILD_FACTORY, b.abilities)
        self.assertIn(A.BARRACKSTRAIN_MARINE, b.abilities)
        self.assertEqual(b.vespene, 0)
        self.assertEqual(b.abilities.count(A.HARVEST_GATHER), 14)

    async def test_resume_abandoned_depot_without_buying_another(self):
        b = self.bot
        b.frame(56)
        b.set_resources(150, 0, 15, 15)
        foundation = b.unit(U.SUPPLYDEPOT, 2, Point2((32, 26)), build_progress=0.52)
        b.observation(workers=self.workers(), buildings=[self.home(), foundation])
        await b.on_step(0)
        self.assertEqual(b.abilities, [A.SMART])
        self.assertEqual(b.actions[0].target.tag, foundation.tag)

    async def test_enroute_depot_counts_once(self):
        b = self.bot
        b.frame(56)
        b.set_resources(150, 0, 15, 15)
        point = Point2((32, 26))
        worker = b.unit(U.SCV, 10, order=(A.TERRANBUILD_SUPPLYDEPOT, point))
        b.observation(workers=[worker], buildings=[self.home()])
        await b.on_step(0)
        self.assertNotIn(A.TERRANBUILD_SUPPLYDEPOT, b.abilities)

    async def test_clearance_reserves_addon_and_future_command_center(self):
        b = self.bot
        b.blocked_tiles = {Point2((x, 33)) for x in range(30, 37)}
        b.observation(buildings=[self.home()])
        await b.on_step(0)
        point = await b._building_site(U.FACTORY, NATURAL)
        self.assertIsNotNone(point)
        self.assertIn((U.SUPPLYDEPOT, Point2((point.x + 2.5, point.y - 0.5))), b.placement_checks)
        self.assertTrue(abs(point.x - NATURAL.x) >= 4 or abs(point.y - NATURAL.y) >= 4)

    async def test_expansion_falls_through_blocked_and_unreachable_sites(self):
        b = self.bot
        b.frame(240)
        b.set_resources(400, 0, 4, 15)
        b.rejected_sites.add(THIRD)
        b.unreachable_sites.add(NATURAL)
        b.observation(workers=self.workers(4), buildings=[self.home()])
        b.hostiles = []
        self.assertTrue(await b._expand())
        self.assertEqual(b.abilities, [A.TERRANBUILD_COMMANDCENTER])
        self.assertEqual(b.actions[0].target, FOURTH)
        self.assertGreater(b.site_retry[NATURAL], b.now)
        self.assertGreater(b.site_retry[THIRD], b.now)
        self.assertEqual(b.minerals, 0)

    async def test_replaces_destroyed_last_base_when_workers_can_afford_it(self):
        b = self.bot
        b.frame(205)
        b.set_resources(400, 0, 0, 0)
        b.observation(workers=self.workers(4))
        await b.on_step(0)
        build = next(a for a in b.actions if a.ability == A.TERRANBUILD_COMMANDCENTER)
        self.assertEqual(build.target, HOME)

    async def test_same_step_production_cannot_overspend_last_supply(self):
        b = self.bot
        b.set_resources(100, 0, 22, 23)
        b.observation(buildings=[b.unit(U.BARRACKS, 2), b.unit(U.BARRACKS, 3)])
        await b._army_production(False, False)
        self.assertEqual(b.abilities, [A.BARRACKSTRAIN_MARINE])
        self.assertEqual(b.supply_left, 0)
        self.assertEqual(b.minerals, 50)

    async def test_factory_addon_uses_producer_specific_action(self):
        b = self.bot
        b.set_resources(160, 25, 20, 35)
        b.observation(buildings=[b.unit(U.FACTORY, 2)])
        await b._army_production(False, True)
        self.assertEqual(b.abilities, [A.BUILD_TECHLAB_FACTORY])
        self.assertEqual((b.minerals, b.vespene), (110, 0))

    async def test_tank_and_anti_air_production(self):
        b = self.bot
        b.air_count = 4
        b.set_resources(400, 300, 22, 45)
        b.observation(
            buildings=[
                b.unit(U.FACTORY, 2, add_on_tag=3),
                b.unit(U.FACTORYTECHLAB, 3),
                b.unit(U.STARPORT, 4),
                b.unit(U.STARPORT, 5),
            ]
        )
        await b._army_production(True, False)
        self.assertEqual(b.abilities, [A.STARPORTTRAIN_VIKINGFIGHTER, A.FACTORYTRAIN_SIEGETANK])
        self.assertEqual((b.minerals, b.vespene, b.supply_left), (100, 100, 18))

    async def test_viking_gets_first_gas_during_air_emergency(self):
        b = self.bot
        b.air_count = 3
        b.set_resources(300, 125, 22, 45)
        b.observation(
            buildings=[b.unit(U.FACTORY, 2, add_on_tag=3), b.unit(U.FACTORYTECHLAB, 3), b.unit(U.STARPORT, 4)]
        )
        await b._army_production(True, True)
        self.assertEqual(b.abilities, [A.STARPORTTRAIN_VIKINGFIGHTER])

    async def test_research_uses_two_buildings_and_charges_both_orders(self):
        b = self.bot
        b.set_resources(200, 200, 22, 45)
        b.observation(buildings=[b.unit(U.BARRACKSTECHLAB, 3), b.unit(U.ENGINEERINGBAY, 4)])
        b._upgrades()
        self.assertEqual(
            b.abilities,
            [
                A.BARRACKSTECHLABRESEARCH_STIMPACK,
                A.ENGINEERINGBAYRESEARCH_TERRANINFANTRYWEAPONSLEVEL1,
            ],
        )
        self.assertEqual((b.minerals, b.vespene), (0, 0))

    async def test_reactor_appends_without_overwriting_first_order(self):
        b = self.bot
        b.set_resources(100, 0, 22, 30)
        b.observation(
            buildings=[
                b.unit(U.BARRACKS, 2, add_on_tag=3, order=(A.BARRACKSTRAIN_MARINE, None)),
                b.unit(U.BARRACKSREACTOR, 3),
            ]
        )
        await b._army_production(False, False)
        self.assertEqual(b.abilities, [A.BARRACKSTRAIN_MARINE])
        self.assertTrue(b.actions[0].queue)

    async def test_second_barracks_precedes_starport_in_ground_opening(self):
        b = self.bot
        b.frame(220)
        b.set_resources(200, 110, 24, 45)
        b.observation(
            workers=self.workers(18),
            army=self.army(4, 0, 0),
            buildings=[
                self.home(U.ORBITALCOMMAND),
                b.unit(U.COMMANDCENTER, 2, NATURAL, build_progress=0.4),
                b.unit(U.SUPPLYDEPOT, 3),
                b.unit(U.BARRACKS, 4),
                b.unit(U.FACTORY, 5),
            ],
        )
        await b.on_step(0)
        self.assertIn(A.TERRANBUILD_BARRACKS, b.abilities)
        self.assertNotIn(A.TERRANBUILD_STARPORT, b.abilities)

    async def test_air_alert_keeps_starport_priority(self):
        b = self.bot
        b.frame(220)
        b.last_air = b.time
        b.set_resources(200, 110, 24, 45)
        b.observation(
            workers=self.workers(18),
            army=self.army(4, 0, 0),
            buildings=[
                self.home(U.ORBITALCOMMAND),
                b.unit(U.SUPPLYDEPOT, 3),
                b.unit(U.BARRACKS, 4),
                b.unit(U.FACTORY, 5),
            ],
        )
        await b.on_step(0)
        self.assertIn(A.TERRANBUILD_STARPORT, b.abilities)
        self.assertNotIn(A.TERRANBUILD_BARRACKS, b.abilities)

    async def test_early_addons_leave_a_marine_queue_available(self):
        b = self.bot
        b.scout_used = True
        b.set_resources(150, 100, 20, 45)
        first = b.unit(U.BARRACKS, 2)
        second = b.unit(U.BARRACKS, 3)
        b.observation(army=self.army(5, 0, 0), buildings=[first])
        await b._army_production(False, False)
        self.assertEqual(b.abilities, [A.BARRACKSTRAIN_MARINE])

        b.frame(221)
        b.set_resources(150, 100, 20, 45)
        b.observation(army=self.army(5, 0, 0), buildings=[first, second])
        await b._army_production(False, False)
        self.assertEqual(b.abilities, [A.BUILD_TECHLAB_BARRACKS, A.BARRACKSTRAIN_MARINE])

    async def test_sweep_preserves_progressing_goal_until_seen(self):
        b = self.bot
        b.now = 700
        b.visible = []
        b.visible_points.add(ENEMY)
        self.assertEqual(b._attack_goal(Point2((39, 40))), NATURAL)

        b.now = 717
        self.assertEqual(b._attack_goal(Point2((52, 47))), NATURAL)
        b.now = 725
        self.assertEqual(b._attack_goal(Point2((59, 51))), NATURAL)

        b.visible_points.add(NATURAL)
        self.assertNotEqual(b._attack_goal(Point2((59, 51))), NATURAL)
        self.assertEqual(b.sweep_retry[NATURAL], 815)

    async def test_sweep_changes_objective_when_progress_stops(self):
        b = self.bot
        b.now = 700
        b.visible = []
        point = Point2((44, 46))
        self.assertEqual(b._attack_goal(point), ENEMY)
        b.now = 717
        self.assertEqual(b._attack_goal(point), ENEMY)
        b.now = 718
        self.assertEqual(b._attack_goal(point), NATURAL)
        self.assertEqual(b.sweep_retry[ENEMY], 763)

    async def test_push_holds_rear_rendezvous_and_does_not_recall_lead_units(self):
        b = self.bot
        b.next_macro = inf
        b.mode = "push"
        rear, lead = 220, 221

        def observe(center):
            army = [
                *(b.unit(U.MARINE, tag, center) for tag in range(200, 207)),
                b.unit(U.MARINE, rear, Point2((20, 30))),
                b.unit(U.MARINE, lead, Point2((80, 74))),
            ]
            b.observation(army=army, buildings=[self.home()])

        start = Point2((50, 50))
        b.frame(700)
        observe(start)
        await b.on_step(0)
        self.assertEqual(b.commands[rear][0], (A.ATTACK, start))
        self.assertEqual(b.commands[lead][0], (A.ATTACK, ENEMY))

        b.frame(700.5)
        observe(Point2((51, 51)))
        await b.on_step(1)
        self.assertFalse(any(a.unit.tag == rear for a in b.actions))

        advanced = Point2((57, 51))
        b.frame(704)
        observe(advanced)
        await b.on_step(2)
        self.assertEqual(b.commands[rear][0], (A.ATTACK, advanced))
        self.assertEqual(b.commands[lead][0], (A.ATTACK, ENEMY))

    async def test_first_push_then_loss_and_second_push(self):
        b = self.bot
        b.next_macro = inf
        b.frame(224)
        b.observation(army=self.army(), buildings=[self.home()])
        await b.on_step(0)
        self.assertEqual(b.mode, "push")
        self.assertTrue(any(a.ability == A.ATTACK and a.target == ENEMY for a in b.actions))

        b.frame(240)
        b.observation(army=self.army(4, 1, 0), buildings=[self.home()])
        await b.on_step(1)
        self.assertEqual(b.mode, "regroup")
        self.assertTrue(all(a.target != ENEMY for a in b.actions))

        b.frame(250)
        b.observation(army=self.army(), buildings=[self.home()])
        await b.on_step(2)
        self.assertEqual(b.mode, "regroup")

        b.frame(268)
        await b.on_step(3)
        self.assertEqual(b.mode, "push")

    async def test_tank_sieges_for_ground_contact_then_moves_up(self):
        b = self.bot
        b.next_macro = inf
        b.frame(251)
        contact = b.unit(U.STALKER, 600, Point2((47, 36)))
        b.observation(army=self.army(), buildings=[self.home()], enemies=[contact])
        await b.on_step(0)
        self.assertIn(A.SIEGEMODE_SIEGEMODE, b.abilities)

        b.frame(255)
        mobile = self.army(tanks=0)
        siege = b.unit(U.SIEGETANKSIEGED, 230, Point2((35, 35)), health=175, health_max=175)
        b.observation(army=[*mobile, siege], buildings=[self.home()])
        b.mode = "push"
        await b.on_step(1)
        self.assertIn(A.UNSIEGE_UNSIEGE, b.abilities)

    async def test_siege_defense_withdraws_exposed_infantry_and_keeps_counter_tank(self):
        b = self.bot
        b.next_macro = inf
        b.frame(510)
        hostile = b.unit(U.SIEGETANKSIEGED, 620, Point2((47, 32)))
        exposed = b.unit(U.MARINE, 201, Point2((38, 32)), health=21)
        counter = b.unit(U.SIEGETANK, 202, Point2((38, 34)))
        medic = b.unit(U.MEDIVAC, 203, Point2((38, 32)), is_flying=True, energy=20)
        b.observation(army=[exposed, counter, medic], buildings=[self.home()], enemies=[hostile])
        await b.on_step(0)

        orders = {a.unit.tag: a for a in b.actions}
        self.assertEqual(orders[exposed.tag].ability, A.MOVE)
        hold = orders[exposed.tag].target
        self.assertGreater(hostile.distance_to(hold), 14)
        self.assertEqual(orders[counter.tag].ability, A.SIEGEMODE_SIEGEMODE)
        self.assertEqual((orders[medic.tag].ability, orders[medic.tag].target), (A.MOVE, hold))

        b.frame(511)
        arrived = b.unit(U.MARINE, exposed.tag, hold, order=(A.ATTACK, hostile.tag))
        b.observation(army=[arrived], buildings=[self.home()], enemies=[hostile])
        await b.on_step(1)
        self.assertEqual(b.abilities, [A.HOLDPOSITION])

    async def test_siege_defense_allows_safe_local_fire_and_releases_after_unsiege(self):
        b = self.bot
        b.next_macro = inf
        b.frame(510)
        hostile = b.unit(U.SIEGETANKSIEGED, 620, Point2((47, 32)))
        exposed = b.unit(U.MARINE, 201, Point2((38, 32)))
        guard = b.unit(U.MARINE, 202, Point2((29, 30)))
        attacker = b.unit(U.MARINE, 621, Point2((28, 34)))
        b.observation(army=[exposed, guard], buildings=[self.home()], enemies=[hostile, attacker])
        await b.on_step(0)
        orders = {a.unit.tag: a for a in b.actions}
        self.assertEqual(orders[exposed.tag].ability, A.MOVE)
        self.assertEqual((orders[guard.tag].ability, orders[guard.tag].target.tag), (A.ATTACK, attacker.tag))

        b.frame(513)
        moving_tank = b.unit(U.SIEGETANK, hostile.tag, hostile.position)
        b.observation(army=[exposed], buildings=[self.home()], enemies=[moving_tank])
        await b.on_step(1)
        self.assertEqual([(a.ability, a.target) for a in b.actions], [(A.ATTACK, hostile.position)])

    async def test_worker_evacuates_to_safe_minerals_under_attack(self):
        b = self.bot
        b.next_macro = inf
        b.frame(89)
        b.mineral_line()
        attacker = b.unit(U.STALKER, 500, Point2((33, 33)))
        worker = b.unit(U.SCV, 10, Point2((31, 31)))
        b.observation(workers=[worker], buildings=[self.home()], enemies=[attacker])
        await b.on_step(0)
        self.assertTrue(b._pressured())
        self.assertIn(A.HARVEST_GATHER_SCV, b.abilities)

    async def test_snapshot_ignored_but_observed_structure_remembered_until_rechecked(self):
        b = self.bot
        b.next_macro = inf
        b.frame(60)
        snapshot = b.unit(U.COMMANDCENTER, 602, ENEMY, display_type=raw_pb2.Snapshot)
        seen = b.unit(U.COMMANDCENTER, 601, NATURAL)
        b.observation(buildings=[self.home()], enemy_buildings=[snapshot, seen])
        await b.on_step(0)
        self.assertEqual(b.enemy_sites, {601: NATURAL})

        b.frame(65)
        b.observation(buildings=[self.home()])
        await b.on_step(1)
        self.assertEqual(b.enemy_sites, {601: NATURAL})

        b.visible_points.add(NATURAL)
        b.frame(70)
        await b.on_step(2)
        self.assertFalse(b.enemy_sites)

    async def test_cloak_contact_spends_reserved_scan(self):
        b = self.bot
        b.next_macro = inf
        b.frame(290)
        b.mineral_line()
        orbital = self.home(U.ORBITALCOMMAND, energy=50)
        b.observation(buildings=[orbital])
        await b.on_step(0)
        self.assertNotIn(A.CALLDOWNMULE_CALLDOWNMULE, b.abilities)

        b.frame(294)
        point = Point2((32, 28))
        enemy = b.unit(U.BANSHEE, 601, point, is_flying=True, cloak=raw_pb2.Cloaked)
        b.observation(buildings=[orbital], enemies=[enemy])
        await b.on_step(1)
        self.assertEqual(b.abilities, [A.SCANNERSWEEP_SCAN])
        self.assertEqual(b.actions[0].target, point)
