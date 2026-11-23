"""真实 ScoreDetails 包装下的交换窗口验证；不运行客户端或重演战斗。"""

from types import SimpleNamespace

import pytest
from bots.webgpt.engagement import RollingTrade, TradeReport
from s2clientprotocol import score_pb2
from sc2.score import ScoreDetails


def score(**counters):
    proto = score_pb2.Score()
    for field, value in counters.items():
        resource, category = field.rsplit("_", 1)
        setattr(getattr(proto.score_details, resource), category, value)
    return ScoreDetails(proto)


def test_first_observation_ignores_all_previous_match_losses():
    report = RollingTrade().update(500, score(lost_minerals_army=9000, killed_minerals_army=100))
    assert report == TradeReport(available=True)
    assert not report.is_bad()


def test_real_score_interface_adds_minerals_and_gas_without_supply_netting():
    monitor = RollingTrade()
    monitor.update(500, score(lost_minerals_army=100, killed_minerals_army=200))
    # 军队数量/补兵速度不在接口中；补兵再多也不能抹去已发生的战损。
    report = monitor.update(
        510,
        score(lost_minerals_army=600, lost_vespene_army=125, killed_minerals_army=300),
    )
    assert report == TradeReport(available=True, loss=625, gain=100, seconds=10)
    assert report.is_bad()


def test_enemy_worker_and_building_progress_prevents_false_retreat():
    monitor = RollingTrade()
    monitor.update(0, score())
    report = monitor.update(
        20,
        score(
            lost_minerals_army=1000,
            killed_minerals_army=100,
            killed_minerals_economy=400,
            killed_minerals_technology=150,
            killed_vespene_technology=100,
        ),
    )
    assert report.loss == 1000
    assert report.gain == 750
    assert not report.is_bad()


def test_each_kill_category_is_counted_once():
    monitor = RollingTrade()
    monitor.update(0, score())
    counters = {
        f"killed_{resource}_{category}": index + 1
        for index, (resource, category) in enumerate(
            (resource, category)
            for resource in ("minerals", "vespene")
            for category in ("none", "army", "economy", "technology", "upgrade")
        )
    }
    report = monitor.update(1, score(**counters))
    assert report.gain == 55
    assert report.loss == 0
    assert not report.is_bad()


def test_own_economy_or_structure_loss_is_not_army_attrition():
    monitor = RollingTrade()
    monitor.update(0, score())
    report = monitor.update(
        10,
        score(
            lost_minerals_economy=3000,
            lost_vespene_economy=1000,
            lost_minerals_technology=1000,
            lost_vespene_technology=500,
        ),
    )
    assert report.loss == 0
    assert not report.is_bad()


def test_trade_guard_has_minimum_loss_and_strict_ratio_threshold():
    assert not TradeReport(True, 599, 0, 10).is_bad()
    assert TradeReport(True, 600, 0, 10).is_bad()
    assert not TradeReport(True, 900, 500, 10).is_bad()
    assert TradeReport(True, 901, 500, 10).is_bad()
    assert not TradeReport(False, 900, 0, 10).is_bad()


def test_large_losses_in_favorable_exchange_do_not_trigger_guard():
    monitor = RollingTrade()
    monitor.update(0, score())
    report = monitor.update(25, score(lost_minerals_army=1450, killed_minerals_army=2000))
    assert not report.is_bad()


def test_window_expires_losses_and_old_kill_credit_without_interpolation():
    monitor = RollingTrade(window_seconds=30)
    monitor.update(0, score())
    monitor.update(10, score(lost_minerals_army=600, killed_minerals_army=1000))
    report = monitor.update(30, score(lost_minerals_army=1300, killed_minerals_army=1000))
    assert report == TradeReport(True, 1300, 1000, 30)
    assert not report.is_bad()
    # 截止点越过第一个采样时刻后，无法准确分配的 0..10 秒整段不计入。
    report = monitor.update(31, score(lost_minerals_army=1300, killed_minerals_army=1000))
    assert report == TradeReport(True, 700, 0, 21)
    assert report.is_bad()
    report = monitor.update(61, score(lost_minerals_army=1300, killed_minerals_army=1000))
    assert report == TradeReport(True, 0, 0, 30)
    assert not report.is_bad()


def test_long_observation_gap_does_not_turn_old_losses_into_current_burst():
    monitor = RollingTrade()
    monitor.update(0, score())
    report = monitor.update(31, score(lost_minerals_army=4000))
    assert report == TradeReport(True)
    assert not report.is_bad()
    report = monitor.update(32, score(lost_minerals_army=4600))
    assert report == TradeReport(True, 600, 0, 1)
    assert report.is_bad()


def test_same_timestamp_replaces_latest_observation_without_double_counting():
    monitor = RollingTrade()
    monitor.update(0, score())
    monitor.update(10, score(lost_minerals_army=500))
    report = monitor.update(10, score(lost_minerals_army=600))
    assert report == TradeReport(True, 600, 0, 10)
    assert monitor.update(10, score(lost_minerals_army=600)) == report


def test_repeated_first_timestamp_does_not_invent_an_interval():
    monitor = RollingTrade()
    monitor.update(20, score())
    assert monitor.update(20, score(lost_minerals_army=800)) == TradeReport(True)
    assert monitor.update(21, score(lost_minerals_army=850)) == TradeReport(True, 50, 0, 1)


def test_counter_reset_starts_new_baseline_even_if_aggregate_would_increase():
    monitor = RollingTrade()
    monitor.update(0, score(lost_minerals_army=300, lost_vespene_army=100))
    report = monitor.update(10, score(lost_minerals_army=100, lost_vespene_army=500))
    assert report == TradeReport(True)
    report = monitor.update(11, score(lost_minerals_army=700, lost_vespene_army=500))
    assert report == TradeReport(True, 600, 0, 1)


def test_game_time_reset_does_not_keep_previous_match_trade_history():
    monitor = RollingTrade()
    monitor.update(40, score())
    monitor.update(50, score(lost_minerals_army=900))
    assert monitor.update(1, score(lost_minerals_army=1000)) == TradeReport(True)


@pytest.mark.parametrize("invalid", [None, SimpleNamespace(), score(lost_minerals_army=-1)])
def test_missing_or_negative_score_discards_stale_history(invalid):
    monitor = RollingTrade()
    monitor.update(0, score())
    monitor.update(10, score(lost_minerals_army=1000))
    assert monitor.update(11, invalid) == TradeReport()
    assert monitor.update(12, score(lost_minerals_army=2000)) == TradeReport(True)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_score_cannot_trigger_or_poison_future_report(value):
    monitor = RollingTrade()
    monitor.update(0, score())
    assert monitor.update(1, score(killed_minerals_army=value)) == TradeReport()
    assert monitor.update(2, score(lost_minerals_army=2000)) == TradeReport(True)


@pytest.mark.parametrize("now", [-1, float("nan"), float("inf")])
def test_invalid_game_time_clears_history(now):
    monitor = RollingTrade()
    monitor.update(0, score())
    monitor.update(1, score(lost_minerals_army=600))
    assert monitor.update(now, score(lost_minerals_army=600)) == TradeReport()
    assert monitor.update(10, score(lost_minerals_army=1000)) == TradeReport(True)


def test_explicit_reset_excludes_retreat_history_from_next_attack():
    monitor = RollingTrade()
    monitor.update(0, score())
    assert monitor.update(5, score(lost_minerals_army=800)).is_bad()
    monitor.reset()
    assert monitor.update(6, score(lost_minerals_army=1000)) == TradeReport(True)
    assert monitor.update(7, score(lost_minerals_army=1050)) == TradeReport(True, 50, 0, 1)


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf")])
def test_invalid_window_is_rejected(window):
    with pytest.raises(ValueError, match="有限正数"):
        RollingTrade(window_seconds=window)
