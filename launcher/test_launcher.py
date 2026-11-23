"""Local GUI verification: never starts StarCraft II."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from launcher import server, worker


def picks():
    bots = server.catalog()
    webgpt = next(bot for bot in bots if bot["agent"].lower() == "webgpt")
    mizuki = next(bot for bot in bots if bot["agent"].lower() == "mizuki")
    return webgpt, mizuki


def test_latest_sc2_version_found():
    info = server.game_info()
    assert info["available"]
    assert Path(info["executable"]).is_file()
    assert "RedCityLE" in info["maps"]
    assert info["build"].startswith("Base")


def test_model_and_script_discovered_in_independent_worktrees():
    left, right = picks()
    assert left["source"] != right["source"]
    assert left["script"] == right["script"] == "bot.py"
    assert left["path"] != right["path"]


def test_selected_bots_parse_without_modifying_branches():
    left, right = picks()
    settings = server.parse_request({
        "left": left["id"], "opponent": "bot", "right": right["id"],
        "map": "RedCityLE", "duration": 800, "seed": 2026,
        "model_label": "GPT vs mizuki",
    })
    assert settings["left"]["path"] != settings["right"]["path"]
    assert settings["model_label"] == "GPT vs mizuki"


def test_mirror_is_allowed():
    left, _ = picks()
    settings = server.parse_request({
        "left": left["id"], "opponent": "bot", "right": left["id"],
        "map": "RedCityLE", "duration": 900, "seed": 42,
    })
    assert settings["left"]["path"] == settings["right"]["path"]


def test_unavailable_map_rejected():
    left, _ = picks()
    with pytest.raises(ValueError, match="地图"):
        server.parse_request({
            "left": left["id"], "opponent": "computer",
            "map": "__not_a_map__", "duration": 900,
        })


def test_bad_bot_id_rejected():
    with pytest.raises(ValueError, match="Bot"):
        server.parse_request({
            "left": "__unlisted__", "opponent": "computer",
            "map": "RedCityLE", "duration": 900,
        })


def test_two_modules_can_import_without_colliding():
    left, right = picks()
    first = worker.load_bot(left, "arena_test_left")
    second = worker.load_bot(right, "arena_test_right")
    assert first.__name__ == second.__name__ == "CompetitiveBot"
    assert first is not second
    assert first.__module__.startswith("arena_test_left")
    assert second.__module__.startswith("arena_test_right")


def test_config_is_json_serializable():
    left, right = picks()
    conf = server.parse_request({
        "left": left["id"], "opponent": "bot", "right": right["id"],
        "map": "RedCityLE", "duration": 900, "seed": 9,
    })
    assert json.loads(json.dumps(conf))["seed"] == 9
