import hashlib
import json
from unittest.mock import Mock

from sc2.data import Result

from bots.mizuki import replay_review, smoke


def row(t, player, kind, label, **fields):
    return {"time": t, "player": player, "kind": kind, "label": label, **fields}


def example():
    return {
        "map": "Red City LE",
        "build": 98310,
        "duration_seconds": 400,
        "players": [
            {"id": 1, "name": "Mizuki", "race": "Terran", "result": "Win"},
            {"id": 2, "name": "Other", "race": "Zerg", "result": "Loss"},
        ],
        "events": [
            row(0, 1, "expansion", "CommandCenter"),
            row(9, 1, "stats", "stats", food_used=15, food_made=15, workers_active=15),
            row(11, 1, "stats", "stats", food_used=15, food_made=23, workers_active=15),
            row(22, 1, "production", "SCV"),
            row(39, 1, "stats", "stats", food_used=200, food_made=200),
            row(50, 1, "building", "Barracks", phase="started"),
            row(86, 1, "production", "Marine"),
            row(150, 1, "expansion", "CommandCenter", phase="started"),
            row(221, 1, "loss", "SCV", killer_player=2, x=22, y=26),
            row(223, 1, "loss", "Marine", killer_player=2, x=24, y=27),
            row(225, 2, "loss", "Roach", killer_player=1, x=24, y=25),
            row(224, 1, "loss", "Marine", killer_player=2, x=90, y=81),
            row(225, 1, "loss", "Mule"),
            row(226, 2, "loss", "Larva", killer_player=1, x=24, y=26),
            row(285, 2, "loss", "Hatchery", killer_player=1, x=70, y=82),
        ],
    }


def test_compact_report_separates_battles_and_keeps_time_anchored_evidence():
    report = replay_review.build_review(example())
    p = report["players"][0]
    assert report["build"] == 98310
    assert p["economy"]["sub_200_zero_headroom_sample_times_seconds"] == [9]
    assert [x["t"] for x in p["economy"]["samples"]] == [9, 11, 39]
    assert [x["item"] for x in p["opening_events"]] == [
        "Barracks",
        "Marine",
        "CommandCenter",
    ]
    assert [x["t"] for x in p["expansions"]] == [150]
    assert p["recorded_losses_by_type"] == {"Marine": 2, "Mule": 1, "SCV": 1}
    fights = report["engagement_candidates"]
    assert len(fights) == 2
    assert fights[0]["recorded_losses"] == 3
    assert fights[0]["losses_by_player"]["1"]["workers"] == 1
    assert fights[1]["start_t"] == 285
    assert "events" not in report


def test_batch_keeps_successes_reports_errors_and_supports_anonymity(
    tmp_path, monkeypatch
):
    good = tmp_path / "Private Name.SC2Replay"
    missing = tmp_path / "Private Other.SC2Replay"
    out = tmp_path / "summary.json"
    good.write_bytes(b"test")

    def parse(path):
        if path == missing:
            raise replay_review.ReplayParseError(f"Unsupported replay: {path}")
        return example()

    monkeypatch.setattr(replay_review, "parse_replay", parse)
    assert (
        replay_review.main(
            [
                str(good),
                str(missing),
                "--anonymous",
                "--include-events",
                "--out",
                str(out),
            ]
        )
        == 1
    )
    text = out.read_text(encoding="utf-8")
    data = json.loads(text)
    assert (
        data["schema"] == replay_review.SCHEMA
        and len(data["matches"]) == len(data["errors"]) == 1
    )
    assert "Private" not in text and "Mizuki" not in text
    assert data["matches"][0]["players"][0]["name"] == "Player 1"
    assert data["matches"][0]["sha256"] == hashlib.sha256(b"test").hexdigest()
    assert len(data["matches"][0]["events"]) == len(example()["events"])
    assert data["errors"][0]["source"] == "replay-2"


def test_launcher_can_generate_review_json_after_replay(tmp_path, monkeypatch):
    replay = tmp_path / "battle.SC2Replay"
    out = tmp_path / "analysis.json"

    def game(*args, **kwargs):
        replay.write_bytes(b"engine stub")
        return Result.Victory

    monkeypatch.setattr(smoke.maps, "get", Mock(return_value=object()))
    monkeypatch.setattr(smoke, "run_game", game)
    monkeypatch.setattr(replay_review, "parse_replay", Mock(return_value=example()))
    assert (
        smoke.main(["--seed", "23", "--replay", str(replay), "--review-json", str(out)])
        == 0
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["matches"][0]["build"] == 98310
    assert result["local_smoke"]["seed"] == 23
    assert result["local_smoke"]["result"] == "Victory"


def test_friendly_fire_and_unknown_victim_do_not_manufacture_a_battle():
    data = example()
    data["players"][0]["team"] = data["players"][1]["team"] = 0
    data["players"].append({"id": None, "name": "Unmatched"})
    data["events"].extend(
        row(220 + i, None, "loss", "Marine", killer_player=1, x=22, y=26)
        for i in range(3)
    )
    report = replay_review.build_review(data)
    assert report["engagement_candidate_count"] == 0
    assert not report["players"][-1]["recorded_losses_by_type"]


def test_preserves_parser_stages_and_build_clock():
    data = example()
    data.update(game_version="5.0.17.98310", loops_per_second=22.4, game_speed_code=4)
    data["events"].append(
        row(162, 1, "expansion", "CommandCenter", stage="completed", unit_tag=77)
    )
    result = replay_review.build_review(data)
    assert result["game_version"] == "5.0.17.98310"
    assert result["loops_per_second"] == 22.4
    assert result["players"][0]["expansions"][-1]["stage"] == "completed"
    assert result["players"][0]["expansions"][-1]["unit_tag"] == 77


def test_missing_fresh_replay_still_exports_diagnostics_without_loading_old_file(
    tmp_path, monkeypatch
):
    replay = tmp_path / "battle.SC2Replay"
    replay.write_bytes(b"old replay")
    out = tmp_path / "analysis.json"
    game = Mock(return_value=Result.Defeat)
    parse = Mock(side_effect=AssertionError("An old replay must not be loaded"))
    monkeypatch.setattr(smoke.maps, "get", Mock(return_value=object()))
    monkeypatch.setattr(smoke, "run_game", game)
    monkeypatch.setattr(replay_review, "parse_replay", parse)
    assert smoke.main(["--replay", str(replay), "--review-json", str(out)]) != 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["matches"] == []
    assert "older replay was not reused" in result["errors"][0]["error"]
    assert result["local_smoke"]["decision_trace"]["steps_completed"] == 0
    parse.assert_not_called()
