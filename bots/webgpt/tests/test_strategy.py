"""离线战略状态机测试；不依赖客户端，不代表已验证游戏胜率。"""

from types import SimpleNamespace

from bots.webgpt.config import BotConfig
from bots.webgpt.model import Contact, IntelReport, Stance
from bots.webgpt.strategy import Strategist, composition_for
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2


def world(army_size=45, workers=40, now=300):
    units = [
        SimpleNamespace(
            tag=i + 100,
            type_id=U.MARINE,
            position=Point2((18, 18)),
            can_attack_ground=True,
            can_attack_air=True,
            is_structure=False,
            ground_dps=10,
            air_dps=10,
            shield_health_percentage=1.0,
        )
        for i in range(army_size)
    ]
    return SimpleNamespace(
        time=now,
        workers=[None] * workers,
        units=units,
        townhalls=[SimpleNamespace(tag=1, position=Point2((10, 10)), is_flying=False)],
        start_location=Point2((10, 10)),
        game_info=SimpleNamespace(map_center=Point2((50, 50))),
        in_map_bounds=lambda p: 0 <= p.x <= 100 and 0 <= p.y <= 100,
        in_pathing_grid=lambda p: True,
        supply_army=army_size,
        supply_used=army_size + workers,
        minerals=400,
        game_data=SimpleNamespace(
            units={U.MARINE.value: SimpleNamespace(_proto=SimpleNamespace(food_required=1))}
        ),
    )


def intelligence(report=None, contacts=()):
    return SimpleNamespace(
        report=report or IntelReport(),
        contacts={c.tag: c for c in contacts},
        target=lambda bot: Point2((90, 90)),
    )


def test_opening_holds_small_army_and_plans_normal_expansion():
    strategist = Strategist(BotConfig())
    plan = strategist.update(world(army_size=4, workers=18, now=80), intelligence())
    assert plan.stance == Stance.HOLD
    assert plan.desired_bases == 2
    assert plan.desired_barracks == 1
    assert plan.allow_expand


def test_sufficient_army_actively_searches_without_free_enemy_information():
    strategist = Strategist(BotConfig())
    plan = strategist.update(world(), intelligence())
    assert plan.stance == Stance.SEARCH
    assert plan.objective == Point2((90, 90))


def test_known_enemy_structure_makes_push_an_attack():
    strategist = Strategist(BotConfig())
    report = IntelReport(known_structures=(Point2((90, 90)),), enemy_power=20)
    assert strategist.update(world(), intelligence(report)).stance == Stance.ATTACK


def test_current_home_attack_overrides_attack_state():
    strategist = Strategist(BotConfig())
    bot = world()
    strategist.update(bot, intelligence())
    report = IntelReport(home_threat=Point2((13, 10)), home_threat_power=50)
    plan = strategist.update(bot, intelligence(report))
    assert plan.stance == Stance.DEFEND
    assert plan.objective == report.home_threat
    assert plan.emergency
    assert not plan.allow_expand


def test_defense_lingers_then_releases_army():
    strategist = Strategist(BotConfig())
    bot = world()
    threat = IntelReport(home_threat=Point2((13, 10)), home_threat_power=8)
    strategist.update(bot, intelligence(threat))
    bot.time += 3
    assert strategist.update(bot, intelligence()).stance == Stance.DEFEND
    bot.time += 4
    assert strategist.update(bot, intelligence()).stance == Stance.SEARCH


def test_minor_strength_fluctuation_does_not_reverse_a_committed_push():
    strategist = Strategist(BotConfig())
    bot = world()
    strategist.update(bot, intelligence(IntelReport(enemy_power=20)))
    bot.time += 1
    # 全图估计略强于己方；本地没有新敌人/惨重损失，不会立刻掉头。
    plan = strategist.update(bot, intelligence(IntelReport(enemy_power=50)))
    assert plan.stance == Stance.SEARCH


def test_clear_local_disadvantage_triggers_retreat():
    strategist = Strategist(BotConfig())
    bot = world()
    strategist.update(bot, intelligence())
    contact = Contact(7, U.ULTRALISK, Point2((20, 20)), bot.time, False, False, 6, 90)
    plan = strategist.update(bot, intelligence(contacts=[contact]))
    assert plan.stance == Stance.RETREAT
    assert plan.objective == plan.fallback


def test_retreat_has_minimum_duration_and_can_eventually_recover():
    strategist = Strategist(BotConfig())
    bot = world()
    strategist.update(bot, intelligence())
    bot.units = bot.units[:10]
    bot.supply_army = 10
    assert strategist.update(bot, intelligence()).stance == Stance.RETREAT
    bot.time += 5
    bot.units = world().units
    bot.supply_army = 45
    assert strategist.update(bot, intelligence()).stance == Stance.RETREAT
    bot.time += 6
    assert strategist.update(bot, intelligence()).stance == Stance.SEARCH


def test_base_loss_allows_rebuilding_even_during_emergency():
    strategist = Strategist(BotConfig())
    bot = world(army_size=8)
    bot.townhalls = []
    report = IntelReport(home_threat=Point2((12, 12)), home_threat_power=90, rush=True)
    plan = strategist.update(bot, intelligence(report))
    assert plan.allow_expand
    assert plan.desired_bases >= 1


def test_economic_growth_increases_production_and_respects_base_cap():
    strategist = Strategist(BotConfig(max_bases=4))
    bot = world(workers=70, army_size=110, now=1300)
    bot.minerals = 1500
    plan = strategist.update(bot, intelligence(IntelReport(air_supply=20)))
    assert plan.desired_bases == 4
    assert plan.desired_barracks >= 7
    assert plan.desired_starports == 2


def test_early_rush_suppresses_greedy_base_target():
    strategist = Strategist(BotConfig())
    bot = world(army_size=6, workers=26, now=180)
    report = IntelReport(rush=True, home_threat=Point2((20, 10)), home_threat_power=10)
    plan = strategist.update(bot, intelligence(report))
    assert plan.desired_bases == 1
    assert not plan.allow_expand


def test_air_army_changes_marines_vikings_and_tank_budget():
    base = composition_for(IntelReport(), 60, 60, 400)
    air = composition_for(IntelReport(air_supply=20, ground_supply=2), 60, 60, 400)
    assert air.viking_goal > base.viking_goal
    assert air.tank_goal < base.tank_goal
    assert air.marauder_fraction < base.marauder_fraction


def test_colossus_prompts_anti_air_even_though_it_is_ground_unit():
    composition = composition_for(IntelReport(colossi=2, ground_supply=12), 60, 45, 400)
    assert composition.viking_goal >= 4


def test_armor_evidence_increases_marauders_and_tanks():
    neutral = composition_for(IntelReport(ground_supply=20), 60, 40, 400)
    armor = composition_for(IntelReport(ground_supply=20, armored_ground_supply=20), 60, 40, 400)
    assert armor.marauder_fraction > neutral.marauder_fraction
    assert armor.tank_goal > neutral.tank_goal


def test_cloak_changes_detection_and_energy_policy():
    strategist = Strategist(BotConfig())
    plan = strategist.update(world(), intelligence(IntelReport(cloak_risk=True)))
    assert plan.composition.raven_goal == 1
    assert plan.save_scan


def test_late_smaller_army_does_not_stall_forever():
    strategist = Strategist(BotConfig())
    bot = world(army_size=30, now=600)
    plan = strategist.update(bot, intelligence())
    assert plan.stance == Stance.SEARCH


def test_large_visible_enemy_delays_undersized_attack():
    strategist = Strategist(BotConfig())
    bot = world(army_size=45, now=320)
    plan = strategist.update(bot, intelligence(IntelReport(enemy_power=100)))
    assert plan.stance == Stance.HOLD


def test_rally_avoids_an_unpathable_candidate():
    strategist = Strategist(BotConfig())
    bot = world(army_size=8)
    bot.in_pathing_grid = lambda p: p.distance_to(bot.start_location) < 4.5
    plan = strategist.update(bot, intelligence())
    assert bot.in_pathing_grid(plan.rally)


def test_composition_is_bounded_for_extreme_observations():
    report = IntelReport(
        air_supply=1000, ground_supply=1000, armored_ground_supply=1000, colossi=100
    )
    comp = composition_for(report, 1000, 1000, 10000)
    assert 0 <= comp.marauder_fraction <= 0.5
    assert 0 <= comp.viking_goal <= 16
    assert 0 <= comp.tank_goal <= 10
    assert 0 <= comp.medivac_goal <= 8


def test_different_instances_do_not_share_tactical_state():
    a = Strategist(BotConfig())
    b = Strategist(BotConfig())
    a.update(world(), intelligence())
    assert a.stance == Stance.SEARCH
    assert b.stance == Stance.HOLD


def test_support_only_force_is_not_sent_on_a_solo_attack():
    strategist = Strategist(BotConfig())
    bot = world(army_size=45)
    for unit in bot.units:
        unit.can_attack_air = False
        unit.can_attack_ground = False
    assert strategist.update(bot, intelligence()).stance == Stance.HOLD


def test_unfinished_forward_expansion_does_not_move_rally_into_danger():
    strategist = Strategist(BotConfig())
    bot = world(army_size=5)
    bot.townhalls[0].is_ready = True
    bot.townhalls.append(
        SimpleNamespace(
            tag=2,
            position=Point2((60, 60)),
            is_flying=False,
            is_ready=False,
        )
    )
    plan = strategist.update(bot, intelligence())
    assert plan.rally.distance_to(bot.start_location) < 10
