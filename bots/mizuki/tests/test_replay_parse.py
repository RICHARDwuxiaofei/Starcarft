"""Synthetic tracker records; real decoder selection tested separately from SC2."""

import json
from typing import ClassVar

import mpyq
import pytest

from bots.mizuki.replay_parse import (
    ReplayParseError,
    _header_fields,
    _load_protocol,
    _participant_rows,
    _tracker_events,
    parse_replay,
)


def event(name, loop, **values):
    return {"_event": f"NNet.Replay.Tracker.{name}", "_gameloop": loop, **values}


def unit(name, loop, index, **values):
    return event(name, loop, m_unitTagIndex=index, m_unitTagRecycle=1, **values)


def test_real_official_builds_load_under_python_3():
    a = _load_protocol(98310)
    b = _load_protocol(98745)
    assert a.__name__.endswith("protocol98310")
    assert b.__name__.endswith("protocol98745")
    assert a.unit_tag(7, 1) == b.unit_tag(7, 1) == 1835009
    with pytest.raises(ReplayParseError, match="No exact.*Base98311"):
        _load_protocol(98311)


def test_exact_body_selection_with_official_versioned_header_decoder(
    monkeypatch, tmp_path
):
    def vint(x):
        first, x = (x & 63) << 1, x >> 6
        out = [first | (128 if x else 0)]
        while x:
            octet, x = x & 127, x >> 7
            out.append(octet | (128 if x else 0))
        return bytes(out)

    def field(tag, x):
        return vint(tag) + b"\x09" + vint(x)

    def fake_header(base):
        version = (
            b"\x05"
            + vint(5)
            + field(1, 5)
            + field(2, 0)
            + field(3, 17)
            + field(4, base)
            + field(5, base)
        )
        return b"\x05" + vint(2) + vint(1) + version + field(3, 2240)

    class FakeArchive:
        base = 98310
        read_members: ClassVar[list[str]] = []

        def __init__(self, stream, *, listfile):
            assert stream.read() == b"test" and not listfile
            self.header = {"user_data_header": {"content": fake_header(self.base)}}

        def read_file(self, member):
            self.read_members.append(member)
            return b"details" if member == "replay.details" else None

    decoder = _load_protocol(98310)

    def fake_details(data):
        assert data == b"details"
        return {"m_title": b"Test Map", "m_gameSpeed": 4}

    monkeypatch.setattr(mpyq, "MPQArchive", FakeArchive)
    monkeypatch.setattr(decoder, "decode_replay_details", fake_details)
    replay = tmp_path / "test.SC2Replay"
    replay.write_bytes(b"test")
    result = parse_replay(replay)
    assert _header_fields(
        _load_protocol(98745).decode_replay_header(fake_header(98310))
    ) == (98310, 2240)
    assert result["build"] == 98310
    assert result["game_version"] == "5.0.17.98310"
    assert result["duration_seconds"] == 100
    assert result["map"] == "Test Map"
    assert result["events"] == []
    assert "no tracker stream" in result["warnings"][0]
    assert FakeArchive.read_members == [
        "replay.details",
        "replay.initData",
        "replay.tracker.events",
    ]

    FakeArchive.base = 98311
    FakeArchive.read_members.clear()
    with pytest.raises(ReplayParseError, match="No exact.*Base98311"):
        parse_replay(replay)
    assert FakeArchive.read_members == []


def test_expansion_morph_and_death_use_tags_and_preserve_only_recorded_facts():
    records = [
        unit(
            "SUnitBornEvent",
            0,
            1,
            m_unitTypeName=b"Nexus",
            m_upkeepPlayerId=1,
            m_x=18,
            m_y=19,
        ),
        unit(
            "SUnitInitEvent",
            224,
            2,
            m_unitTypeName=b"CommandCenter",
            m_upkeepPlayerId=2,
            m_x=52,
            m_y=53,
        ),
        unit("SUnitDoneEvent", 448, 2),
        unit("SUnitTypeChangeEvent", 456, 2, m_unitTypeName=b"OrbitalCommand"),
        unit(
            "SUnitBornEvent",
            500,
            3,
            m_unitTypeName=b"Marine",
            m_upkeepPlayerId=2,
            m_x=51,
            m_y=54,
        ),
        unit(
            "SUnitDiedEvent",
            672,
            1,
            m_killerPlayerId=2,
            m_killerUnitTagIndex=3,
            m_killerUnitTagRecycle=1,
            m_x=21,
            m_y=26,
        ),
        unit("SUnitDiedEvent", 700, 4),
    ]
    result, setups = _tracker_events(records, _load_protocol(98310).unit_tag)
    assert setups == []
    assert [(r["kind"], r["label"]) for r in result] == [
        ("initial", "Nexus"),
        ("expansion", "CommandCenter"),
        ("expansion", "CommandCenter"),
        ("type_change", "OrbitalCommand"),
        ("production", "Marine"),
        ("loss", "Nexus"),
        ("loss", "unknown_unit"),
    ]
    assert result[1]["stage"] == "started" and result[1]["time"] == 10
    assert result[2]["stage"] == "completed" and result[2]["x"] == 52
    death = result[5]
    assert death["victim_tag"] == result[0]["unit_tag"]
    assert death["victim_player"] == 1
    assert death["killer_tag"] == result[4]["unit_tag"]
    assert death["killer_player"] == 2 and death["killer_type"] == "Marine"
    assert (death["time"], death["x"], death["y"]) == (30, 21, 26)
    assert result[6]["victim_player"] is None and result[6]["killer_player"] is None
    json.dumps(result)


def test_player_setup_joins_through_lobby_slot_not_details_order_and_omits_toon_data():
    details = {
        "m_playerList": [
            {
                "m_name": b"AI",
                "m_race": b"Terran",
                "m_result": 2,
                "m_workingSetSlotId": 1,
            },
            {
                "m_name": b"Nagi#1234",
                "m_toon": {"m_id": 412734258},
                "m_race": b"Zerg",
                "m_result": 1,
                "m_workingSetSlotId": 0,
            },
            {"m_name": b"Observer", "m_observe": 1},
        ]
    }
    initdata = {
        "m_syncLobbyState": {
            "m_lobbyState": {
                "m_slots": [
                    {"m_workingSetSlotId": 12},
                    {"m_workingSetSlotId": 0, "m_userId": 0},
                    {"m_workingSetSlotId": 1},
                ]
            },
            "m_userInitialData": [{"m_name": b"Nagi#1234", "m_toonHandle": b"private"}],
        }
    }
    warnings = []
    setups = [{"m_playerId": 1, "m_slotId": 1}, {"m_playerId": 2, "m_slotId": 2}]
    players = _participant_rows(details, initdata, setups, warnings)
    assert [(r["id"], r["name"], r["result"]) for r in players] == [
        (2, "AI", "loss"),
        (1, "Nagi", "win"),
    ]
    assert "412734258" not in json.dumps(players) and "private" not in json.dumps(
        players
    )
    assert not warnings
    assert _participant_rows(details, {}, setups, warnings)[0]["id"] is None
    assert warnings


def test_stats_translate_food_and_do_not_fill_absent_resource_stats():
    result, _ = _tracker_events(
        [
            event(
                "SPlayerStatsEvent",
                448,
                m_playerId=1,
                m_stats={
                    "m_scoreValueFoodUsed": 15360,
                    "m_scoreValueWorkersActiveCount": 8,
                    "m_scoreValueMineralsLostArmy": 100,
                    "m_scoreValueMineralsLostEconomy": 25,
                    "m_scoreValueMineralsLostTechnology": 50,
                    "m_scoreValueVespeneLostArmy": 25,
                    "m_scoreValueVespeneLostEconomy": 0,
                    "m_scoreValueVespeneLostTechnology": 0,
                },
            ),
            event(
                "SPlayerStatsEvent",
                672,
                m_playerId=1,
                m_stats={"m_scoreValueMineralsCurrent": 375},
            ),
        ],
        _load_protocol(98310).unit_tag,
    )
    assert result[0]["food_used"] == 3.75 and result[0]["workers_active"] == 8
    assert result[0]["minerals_lost"] == 175 and result[0]["resources_lost"] == 200
    assert "resources_lost" not in result[1]
