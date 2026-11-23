"""离线测试：观察/记忆边界，不把模拟对象当作 SC2 实战证据。"""

from types import SimpleNamespace

import pytest
from bots.webgpt.config import BotConfig
from bots.webgpt.intelligence import Intelligence, unit_supply
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2


def unit(tag=1, kind=U.MARINE, position=(80, 80), **changes):
    values = {
        "tag": tag,
        "type_id": kind,
        "position": Point2(position),
        "is_visible": True,
        "is_snapshot": False,
        "is_structure": False,
        "is_flying": False,
        "is_armored": False,
        "can_attack_ground": True,
        "can_attack_air": True,
        "ground_dps": 10,
        "air_dps": 10,
        "shield_health_percentage": 1.0,
        "build_progress": 1.0,
        "is_cloaked": False,
        "is_burrowed": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def world(enemies=(), structures=(), now=0.0, visible=False):
    supplies = {
        U.MARINE: 1,
        U.BANSHEE: 3,
        U.COLOSSUS: 6,
        U.SCV: 1,
        U.BANELING: 0.5,
        U.BATTLECRUISER: 6,
        U.MEDIVAC: 2,
    }
    return SimpleNamespace(
        time=now,
        enemy_units=list(enemies),
        enemy_structures=list(structures),
        townhalls=[unit(tag=999, kind=U.COMMANDCENTER, position=(10, 10), is_structure=True)],
        start_location=Point2((10, 10)),
        game_info=SimpleNamespace(map_center=Point2((50, 50))),
        game_data=SimpleNamespace(
            units={
                kind.value: SimpleNamespace(_proto=SimpleNamespace(food_required=amount))
                for kind, amount in supplies.items()
            }
        ),
        is_visible=(visible if callable(visible) else lambda point: visible),
        in_map_bounds=lambda point: 0 <= point.x <= 100 and 0 <= point.y <= 100,
        enemy_start_locations=[Point2((90, 90))],
        expansion_locations_list=[Point2((10, 10)), Point2((70, 70)), Point2((35, 35))],
    )


def test_visible_contact_then_decay_without_refresh_from_snapshot():
    intel = Intelligence(BotConfig())
    bot = world([unit()], now=10)
    first = intel.update(bot)
    assert first.ground_supply == 1
    bot.time = 30
    bot.enemy_units = [unit(is_visible=False, is_snapshot=True)]
    later = intel.update(bot)
    assert intel.contacts[1].seen_at == 10
    assert later.ground_supply == pytest.approx(1 - 20 / 45)
    bot.time = 56
    intel.update(bot)
    assert 1 not in intel.contacts


def test_unseen_snapshot_cannot_create_new_intelligence():
    intel = Intelligence(BotConfig())
    result = intel.update(world([unit(is_visible=False, is_snapshot=True)]))
    assert not intel.contacts
    assert result.visible_enemies == 0


def test_mobile_contact_not_declared_dead_when_old_position_visible():
    intel = Intelligence(BotConfig())
    bot = world([unit()], visible=True)
    intel.update(bot)
    bot.enemy_units = []
    bot.time = 5
    intel.update(bot)
    assert 1 in intel.contacts


def test_stationary_building_removed_when_whole_area_revisited():
    intel = Intelligence(BotConfig())
    building = unit(
        kind=U.BARRACKS,
        is_structure=True,
        can_attack_ground=False,
        can_attack_air=False,
    )
    bot = world(structures=[building], visible=True)
    intel.update(bot)
    bot.time = 20
    bot.enemy_structures = []
    intel.update(bot)
    assert not intel.contacts


def test_fringe_visibility_does_not_destroy_building_memory():
    intel = Intelligence(BotConfig())
    building = unit(kind=U.NEXUS, is_structure=True, can_attack_ground=False, can_attack_air=False)
    bot = world(structures=[building], visible=lambda p: p.x <= 80)
    intel.update(bot)
    bot.enemy_structures = []
    bot.time = 100
    result = intel.update(bot)
    assert result.enemy_bases == 1


def test_flying_structure_uses_mobile_memory_not_static_clear_rule():
    intel = Intelligence(BotConfig())
    flying = unit(
        kind=U.COMMANDCENTERFLYING,
        is_structure=True,
        is_flying=True,
        can_attack_ground=False,
        can_attack_air=False,
    )
    bot = world(structures=[flying], visible=True)
    intel.update(bot)
    bot.enemy_structures = []
    bot.time = 10
    intel.update(bot)
    assert 1 in intel.contacts
    bot.time = 46
    intel.update(bot)
    assert not intel.contacts


def test_destroyed_event_forgets_only_requested_tag():
    intel = Intelligence(BotConfig())
    intel.update(world([unit(tag=1), unit(tag=2)]))
    intel.forget(1)
    intel.forget(123456)
    assert set(intel.contacts) == {2}


def test_one_worker_scout_does_not_trigger_defense_but_worker_rush_does():
    intel = Intelligence(BotConfig())
    bot = world([unit(kind=U.SCV, position=(15, 10))], now=100)
    assert intel.update(bot).home_threat is None
    bot.enemy_units = [unit(tag=i, kind=U.SCV, position=(15 + i, 10)) for i in range(7)]
    result = intel.update(bot)
    assert result.home_threat is not None
    assert result.rush


def test_proxy_pylon_warns_before_cannon_is_finished():
    intel = Intelligence(BotConfig())
    pylon = unit(
        kind=U.PYLON,
        position=(22, 12),
        is_structure=True,
        can_attack_ground=False,
        can_attack_air=False,
        build_progress=0.2,
    )
    result = intel.update(world(structures=[pylon], now=75))
    assert result.home_threat == pylon.position
    assert result.rush


def test_mobile_cloak_evidence_persists_after_its_short_contact_expires():
    intel = Intelligence(BotConfig())
    bot = world([unit(kind=U.BANSHEE, is_flying=True, is_cloaked=True)], now=100)
    assert intel.update(bot).cloak_risk
    bot.enemy_units = []
    bot.time = 170
    assert intel.update(bot).cloak_risk
    assert not intel.contacts
    bot.time = 281
    assert not intel.update(bot).cloak_risk


def test_total_supply_handles_morphed_zerg_unit():
    bot = world()
    assert unit_supply(bot, unit(kind=U.BANELING)) == 0.5


def test_armed_air_and_colossus_are_separate_composition_evidence():
    intel = Intelligence(BotConfig())
    result = intel.update(
        world(
            [
                unit(tag=1, kind=U.BATTLECRUISER, is_flying=True),
                unit(tag=2, kind=U.COLOSSUS, is_armored=True),
                unit(
                    tag=3,
                    kind=U.MEDIVAC,
                    is_flying=True,
                    can_attack_ground=False,
                    can_attack_air=False,
                ),
            ]
        )
    )
    assert result.air_supply == 6  # 没把没有武器的医疗运输机当作战列巡航舰。
    assert result.ground_supply == 6
    assert result.armored_ground_supply == 6
    assert result.colossi == 1


def test_search_visits_enemy_start_then_unseen_expansions():
    intel = Intelligence(BotConfig())
    bot = world(now=10)
    assert intel.target(bot) == Point2((90, 90))
    bot.is_visible = lambda p: p.x >= 85 and p.y >= 85
    assert intel.target(bot) == Point2((70, 70))
    bot.time = 20
    bot.is_visible = lambda p: p.x >= 65 and p.y >= 65
    assert intel.target(bot) == Point2((35, 35))


def test_known_flying_building_remains_attack_search_target():
    intel = Intelligence(BotConfig())
    flying = unit(
        kind=U.COMMANDCENTERFLYING,
        is_structure=True,
        is_flying=True,
        position=(77, 70),
        can_attack_ground=False,
        can_attack_air=False,
    )
    bot = world(structures=[flying])
    intel.update(bot)
    assert intel.target(bot) == flying.position


def test_structure_memory_is_bounded_even_if_never_revisited():
    intel = Intelligence(BotConfig(structure_memory_seconds=120))
    bot = world(structures=[unit(kind=U.BARRACKS, is_structure=True)])
    intel.update(bot)
    bot.enemy_structures = []
    bot.time = 121
    assert not intel.update(bot).known_structures
