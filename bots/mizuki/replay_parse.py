"""Translate Blizzard SC2 replay tracker records to a minimal offline event stream."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources, util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

LOOPS_PER_SECOND = 22.4
_FOOD_SCALE = 4096
_BASE_TYPES = {"CommandCenter", "Hatchery", "Nexus"}
_BUILDING_TYPES = _BASE_TYPES | {
    "OrbitalCommand",
    "PlanetaryFortress",
    "SupplyDepot",
    "Refinery",
    "Barracks",
    "Factory",
    "Starport",
    "EngineeringBay",
    "Armory",
    "FusionCore",
    "GhostAcademy",
    "Bunker",
    "MissileTurret",
    "SensorTower",
    "BarracksReactor",
    "BarracksTechLab",
    "FactoryReactor",
    "FactoryTechLab",
    "StarportReactor",
    "StarportTechLab",
    "Reactor",
    "TechLab",
    "Pylon",
    "Assimilator",
    "Gateway",
    "WarpGate",
    "Forge",
    "CyberneticsCore",
    "PhotonCannon",
    "ShieldBattery",
    "TwilightCouncil",
    "RoboticsFacility",
    "RoboticsBay",
    "Stargate",
    "FleetBeacon",
    "TemplarArchive",
    "DarkShrine",
    "Extractor",
    "SpawningPool",
    "RoachWarren",
    "BanelingNest",
    "EvolutionChamber",
    "SpineCrawler",
    "SporeCrawler",
    "HydraliskDen",
    "LurkerDenMP",
    "Spire",
    "Lair",
    "Hive",
    "GreaterSpire",
    "InfestationPit",
    "NydusNetwork",
    "NydusCanal",
    "UltraliskCavern",
}
_RESOURCE_STATS = {
    "minerals_current": "m_scoreValueMineralsCurrent",
    "vespene_current": "m_scoreValueVespeneCurrent",
    "minerals_collection_rate": "m_scoreValueMineralsCollectionRate",
    "vespene_collection_rate": "m_scoreValueVespeneCollectionRate",
    "workers_active": "m_scoreValueWorkersActiveCount",
    "army_minerals_current": "m_scoreValueMineralsUsedCurrentArmy",
    "army_vespene_current": "m_scoreValueVespeneUsedCurrentArmy",
    **{
        f"{resource}_lost_{category.lower()}": f"m_scoreValue{resource.title()}Lost{category}"
        for resource in ("minerals", "vespene")
        for category in ("Army", "Economy", "Technology")
    },
}
_RESULT = {1: "win", 2: "loss", 3: "tie"}
_PROTOCOL_FILE = re.compile(r"protocol(\d+)\.py")


class ReplayParseError(ValueError):
    """A replay cannot be read using the exact official decoder."""


@lru_cache(maxsize=1)
def _installed_builds() -> tuple[int, ...]:
    try:
        version_dir = resources.files("s2protocol").joinpath("versions")
        return tuple(
            sorted(
                int(match.group(1))
                for entry in version_dir.iterdir()
                if (match := _PROTOCOL_FILE.fullmatch(entry.name)) is not None
                and entry.is_file()
            )
        )
    except (ImportError, OSError) as exc:
        raise ReplayParseError(
            "Install the official Blizzard s2protocol package before decoding replays."
        ) from exc


@lru_cache(maxsize=16)
def _load_protocol(base_build: int) -> ModuleType:
    if base_build not in _installed_builds():
        raise ReplayParseError(
            f"No exact Blizzard s2protocol decoder for Base{base_build}. "
            "Install a release containing that build; the replay was not decoded with a neighboring build."
        )
    # Blizzard's generated modules support importlib directly; the upstream
    # versions/__init__.py wrapper uses imp, removed in Python 3.12.
    entry = resources.files("s2protocol").joinpath(
        "versions", f"protocol{base_build:05d}.py"
    )
    with resources.as_file(entry) as path:
        spec = util.spec_from_file_location(
            f"_mizuki_blizzard_protocol{base_build:05d}", path
        )
        if spec is None or spec.loader is None:
            raise ReplayParseError(
                f"Unable to load the exact Blizzard decoder for Base{base_build}."
            )
        decoder = util.module_from_spec(spec)
        try:
            spec.loader.exec_module(decoder)
        except (ImportError, OSError) as exc:
            raise ReplayParseError(
                f"Unable to load the exact Blizzard decoder for Base{base_build}."
            ) from exc
        return decoder


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _positive(value: Any) -> int | None:
    number = _int(value)
    return number if number is not None and number > 0 else None


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value.strip() or None
    return None


def _display_name(value: Any) -> str | None:
    name = _text(value)
    return re.sub(r"#\d+$", "", name) if name else None


def _header_fields(header: dict[str, Any]) -> tuple[int, int]:
    build = _positive((header.get("m_version") or {}).get("m_baseBuild"))
    loops = _int(header.get("m_elapsedGameLoops"))
    if build is None or loops is None or loops < 0:
        raise ReplayParseError(
            "Replay header lacks a valid base build or elapsed game loop count."
        )
    return build, loops


def _participant_rows(
    details: dict[str, Any],
    initdata: dict[str, Any],
    setups: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    raw_players = [
        player
        for player in details.get("m_playerList") or []
        if _int(player.get("m_observe")) in (None, 0)
    ]
    sync = initdata.get("m_syncLobbyState") or {}
    slots = (sync.get("m_lobbyState") or {}).get("m_slots") or []
    users = sync.get("m_userInitialData") or []
    matches: dict[int, set[int]] = {}
    for setup in setups:
        player_id = _positive(setup.get("m_playerId"))
        if player_id is None:
            continue
        slot_id = _int(setup.get("m_slotId"))
        user_id = _int(setup.get("m_userId"))
        working_slot = None
        if slot_id is not None and 0 <= slot_id < len(slots):
            slot = slots[slot_id]
            working_slot = _int(slot.get("m_workingSetSlotId"))
            if user_id is None:
                user_id = _int(slot.get("m_userId"))
        slot_matches = [
            i
            for i, participant in enumerate(raw_players)
            if working_slot is not None
            and working_slot == _int(participant.get("m_workingSetSlotId"))
        ]
        user_name = (
            _text(users[user_id].get("m_name"))
            if user_id is not None and 0 <= user_id < len(users)
            else None
        )
        name_matches = [
            i
            for i, participant in enumerate(raw_players)
            if user_name and user_name == _text(participant.get("m_name"))
        ]
        if (
            len(slot_matches) == 1
            and len(name_matches) == 1
            and slot_matches != name_matches
        ):
            continue  # Don't attribute an event to the wrong person on a conflicting join.
        candidates = slot_matches if len(slot_matches) == 1 else name_matches
        if len(candidates) == 1:
            matches.setdefault(candidates[0], set()).add(player_id)

    rows = []
    for i, player in enumerate(raw_players):
        candidates = matches.get(i, set())
        player_id = next(iter(candidates)) if len(candidates) == 1 else None
        if (
            player_id is not None
            and sum(player_id in ids for ids in matches.values()) != 1
        ):
            player_id = None
        rows.append(
            {
                "id": player_id,
                "name": _display_name(player.get("m_name")),
                "race": _text(player.get("m_race")),
                "result": _RESULT.get(_int(player.get("m_result")), "undecided"),
                "team": _int(player.get("m_teamId")),
            }
        )
    if any(row["id"] is None for row in rows):
        warnings.append(
            "At least one participant could not be joined to a tracker player ID; "
            "unmatched IDs are null rather than inferred from list order."
        )
    return rows


@dataclass
class _TrackedUnit:
    unit_type: str | None
    player: int | None
    x: int | None
    y: int | None
    construction_kind: str | None = None


def _tracker_events(
    records: Iterable[dict[str, Any]], unit_tag: Callable[[int, int], int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    timeline: list[dict[str, Any]] = []
    setups: list[dict[str, Any]] = []
    units: dict[int, _TrackedUnit] = {}

    def get_tag(data: dict[str, Any], stem: str) -> int | None:
        index = _int(data.get(f"{stem}TagIndex"))
        recycle = _int(data.get(f"{stem}TagRecycle"))
        return (
            unit_tag(index, recycle)
            if index is not None and recycle is not None
            else None
        )

    def emit(
        loop: int, player: int | None, kind: str, label: str, **fields: Any
    ) -> None:
        timeline.append(
            {
                "time": round(loop / LOOPS_PER_SECOND, 6),
                "loop": loop,
                "player": player,
                "kind": kind,
                "label": label,
                **fields,
            }
        )

    for raw in records:
        name = str(raw.get("_event", "")).rsplit(".", 1)[-1]
        if name == "SPlayerSetupEvent":
            setups.append(raw)
            continue
        loop = _int(raw.get("_gameloop"))
        if loop is None or loop < 0:
            continue
        if name == "SPlayerStatsEvent":
            player = _positive(raw.get("m_playerId"))
            values = raw.get("m_stats") or {}
            if not player or not values:
                continue
            stats: dict[str, float | int] = {}
            for normalized, original in _RESOURCE_STATS.items():
                if (value := _int(values.get(original))) is not None:
                    stats[normalized] = value
            for normalized, original in (
                ("food_used", "m_scoreValueFoodUsed"),
                ("food_made", "m_scoreValueFoodMade"),
            ):
                if (value := _int(values.get(original))) is not None:
                    stats[normalized] = value / _FOOD_SCALE
            for resource in ("minerals", "vespene"):
                parts = [
                    stats.get(f"{resource}_lost_{kind}")
                    for kind in ("army", "economy", "technology")
                ]
                a, b, c = parts
                if a is not None and b is not None and c is not None:
                    stats[f"{resource}_lost"] = a + b + c
            if "minerals_lost" in stats and "vespene_lost" in stats:
                stats["resources_lost"] = stats["minerals_lost"] + stats["vespene_lost"]
            if stats:
                emit(loop, player, "stats", "player_stats", **stats)
            continue
        if name == "SUpgradeEvent":
            player = _positive(raw.get("m_playerId"))
            upgrade = _text(raw.get("m_upgradeTypeName"))
            if player and upgrade:
                emit(loop, player, "upgrade", upgrade, count=_int(raw.get("m_count")))
            continue

        tag = get_tag(raw, "m_unit")
        if tag is None:
            continue
        unit = units.get(tag)
        if name in ("SUnitInitEvent", "SUnitBornEvent"):
            x, y = _int(raw.get("m_x")), _int(raw.get("m_y"))
            player = _positive(raw.get("m_upkeepPlayerId")) or _positive(
                raw.get("m_controlPlayerId")
            )
            unit_type = _text(raw.get("m_unitTypeName"))
            kind = (
                "initial"
                if loop == 0
                else "expansion"
                if unit_type in _BASE_TYPES
                else "building"
                if unit_type in _BUILDING_TYPES
                else "construction"
                if name == "SUnitInitEvent"
                else "production"
            )
            if name == "SUnitBornEvent" and unit and unit.construction_kind:
                # Init + Done/Born must not be counted as a second new unit.
                kind = unit.construction_kind
                stage = "completed"
            else:
                stage = "started" if name == "SUnitInitEvent" else "born"
            units[tag] = _TrackedUnit(
                unit_type, player, x, y, kind if stage == "started" else None
            )
            if player is not None and unit_type is not None:
                emit(
                    loop,
                    player,
                    kind,
                    unit_type,
                    stage=stage,
                    unit_tag=tag,
                    unit_type=unit_type,
                    x=x,
                    y=y,
                )
        elif name == "SUnitDoneEvent" and unit and unit.construction_kind is not None:
            if unit.player is not None and unit.unit_type is not None:
                emit(
                    loop,
                    unit.player,
                    unit.construction_kind,
                    unit.unit_type,
                    stage="completed",
                    unit_tag=tag,
                    unit_type=unit.unit_type,
                    x=unit.x,
                    y=unit.y,
                )
            unit.construction_kind = None
        elif name == "SUnitDiedEvent":
            player = unit.player if unit else None
            killer = _positive(raw.get("m_killerPlayerId"))
            killer_tag = get_tag(raw, "m_killerUnit")
            killer_unit = units.get(killer_tag) if killer_tag is not None else None
            victim_type = unit.unit_type if unit else None
            if player is not None or killer is not None or unit is None:
                emit(
                    loop,
                    player,
                    "loss",
                    victim_type or "unknown_unit",
                    unit_type=victim_type,
                    unit_tag=tag,
                    victim_player=player,
                    victim_tag=tag,
                    killer_player=killer,
                    killer_tag=killer_tag,
                    killer_type=killer_unit.unit_type if killer_unit else None,
                    x=_int(raw.get("m_x")),
                    y=_int(raw.get("m_y")),
                    stage="destroyed" if killer is not None else "died",
                )
            if unit:
                unit.construction_kind = None
        elif name == "SUnitOwnerChangeEvent" and unit:
            previous = unit.player
            unit.player = _positive(raw.get("m_upkeepPlayerId")) or _positive(
                raw.get("m_controlPlayerId")
            )
            if unit.player != previous and (
                unit.player is not None or previous is not None
            ):
                emit(
                    loop,
                    unit.player,
                    "owner_change",
                    unit.unit_type or "unknown_unit",
                    unit_tag=tag,
                    unit_type=unit.unit_type,
                    previous_player=previous,
                )
        elif name == "SUnitTypeChangeEvent" and unit:
            previous = unit.unit_type
            unit.unit_type = _text(raw.get("m_unitTypeName")) or previous
            if (
                unit.player is not None
                and unit.unit_type is not None
                and unit.unit_type != previous
            ):
                emit(
                    loop,
                    unit.player,
                    "type_change",
                    unit.unit_type,
                    unit_tag=tag,
                    unit_type=unit.unit_type,
                    previous_type=previous,
                )

    return timeline, setups


def parse_replay(path: str | Path) -> dict[str, Any]:
    """Read one replay from disk. Decode only details, init data, and tracker events."""
    try:
        from mpyq import MPQArchive
        from s2protocol.decoders import CorruptedError, TruncatedError
    except ImportError as exc:
        raise ReplayParseError(
            "Install mpyq and Blizzard s2protocol before reading SC2 replay archives."
        ) from exc
    installed = _installed_builds()
    if not installed:
        raise ReplayParseError("No Blizzard replay protocol modules are installed.")
    warnings: list[str] = []
    try:
        with Path(path).open("rb") as handle:
            archive = MPQArchive(handle, listfile=False)
            header_blob = archive.header["user_data_header"]["content"]
            # The shared versioned header format identifies the exact body decoder.
            header = _load_protocol(installed[-1]).decode_replay_header(header_blob)
            base, elapsed_loops = _header_fields(header)
            decoder = _load_protocol(base)
            details_blob = archive.read_file("replay.details")
            if not details_blob:
                raise ReplayParseError(
                    "The replay archive does not contain readable replay.details."
                )
            details = decoder.decode_replay_details(details_blob)
            try:
                init_blob = archive.read_file("replay.initData")
                initdata = (
                    decoder.decode_replay_initdata(init_blob) if init_blob else {}
                )
            except (CorruptedError, TruncatedError, OSError, ValueError):
                initdata = {}
                warnings.append(
                    "Replay init data could not be decoded; some player IDs may be unlinked."
                )
            tracker = archive.read_file("replay.tracker.events")
            if tracker:
                events, setups = _tracker_events(
                    decoder.decode_replay_tracker_events(tracker), decoder.unit_tag
                )
            else:
                warnings.append(
                    "The replay has no tracker stream; no gameplay timeline was inferred."
                )
                events, setups = [], []
    except ReplayParseError:
        raise
    except Exception as exc:
        raise ReplayParseError(
            f"Unable to decode the SC2 replay ({type(exc).__name__})."
        ) from exc

    version = header.get("m_version") or {}
    version_parts = tuple(
        _int(version.get(name))
        for name in ("m_major", "m_minor", "m_revision", "m_build")
    )
    return {
        "map": _text(details.get("m_title")),
        "build": base,
        "game_version": ".".join(map(str, version_parts))
        if None not in version_parts
        else None,
        "duration_seconds": round(elapsed_loops / LOOPS_PER_SECOND, 6),
        "duration_loops": elapsed_loops,
        "loops_per_second": LOOPS_PER_SECOND,
        "game_speed_code": _int(details.get("m_gameSpeed")),
        "players": _participant_rows(details, initdata, setups, warnings),
        "events": events,
        "warnings": warnings,
    }
