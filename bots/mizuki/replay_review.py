"""Produce a compact, local-only JSON digest of one or more StarCraft II replays."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .replay_parse import ReplayParseError, parse_replay

SCHEMA = "mizuki.sc2-replay-review/v1"
Event = dict[str, Any]
WORKERS = {"scv", "probe", "drone", "droneburrowed", "mule"}
EPHEMERAL = {
    "larva",
    "egg",
    "broodling",
    "locustmp",
    "locustmpflying",
    "changeling",
    "interceptor",
    "adeptphaseshift",
    "infestedterrans",
    "pointdefensedrone",
    "autoturret",
    "disruptorphased",
}
TOWNHALLS = {
    "commandcenter",
    "commandcenterflying",
    "orbitalcommand",
    "orbitalcommandflying",
    "planetaryfortress",
    "nexus",
    "hatchery",
    "lair",
    "hive",
}
STRUCTURE_TYPES = {
    "supplydepot",
    "supplydepotlowered",
    "barracks",
    "barracksflying",
    "refinery",
    "factory",
    "factoryflying",
    "starport",
    "starportflying",
    "engineeringbay",
    "armory",
    "bunker",
    "missileturret",
    "sensortower",
    "fusioncore",
    "ghostacademy",
    "pylon",
    "gateway",
    "warpgate",
    "assimilator",
    "forge",
    "cyberneticscore",
    "photoncannon",
    "shieldbattery",
    "roboticsfacility",
    "roboticsbay",
    "stargate",
    "fleetbeacon",
    "templararchive",
    "darkshrine",
    "twilightcouncil",
    "spawningpool",
    "extractor",
    "roachwarren",
    "banelingnest",
    "evolutionchamber",
    "hydraden",
    "hydraliskden",
    "lurkerdenmp",
    "infestationpit",
    "spire",
    "greaterspire",
    "nydusnetwork",
    "nyduscanal",
    "spinecrawler",
    "sporecrawler",
    "ultraliskcavern",
    "creep tumor",
}
KEY_UNITS = {
    "reaper",
    "marine",
    "marauder",
    "siegetank",
    "medivac",
    "vikingfighter",
    "raven",
    "battlecruiser",
    "ghost",
    "cyclone",
    "banshee",
    "liberator",
    "zergling",
    "roach",
    "ravager",
    "baneling",
    "hydralisk",
    "lurker",
    "lurkermp",
    "mutalisk",
    "corruptor",
    "queen",
    "infestor",
    "viper",
    "zealot",
    "stalker",
    "adept",
    "observer",
    "immortal",
    "colossus",
    "oracle",
    "voidray",
    "carrier",
    "hightemplar",
    "darktemplar",
    "disruptor",
}
STATS = (
    "workers_active",
    "food_used",
    "food_made",
    "minerals_current",
    "vespene_current",
    "minerals_collection_rate",
    "vespene_collection_rate",
    "minerals_lost",
    "vespene_lost",
)
OPENING_KINDS = {
    "building",
    "construction",
    "building_started",
    "building_completed",
    "production",
    "unit",
    "unit_born",
    "expansion",
    "upgrade",
    "morph",
    "type_change",
}
LOSS_KINDS = {"loss", "death", "unit_died"}


def _kind(e: Event) -> str:
    return str(e.get("kind", "")).lower()


def _unit(e: Event) -> str:
    return str(e.get("unit_type") or e.get("label") or "Unknown")


def _time(e: Event) -> float:
    return float(e["time"])


def _category(e: Event) -> str:
    name = _unit(e).lower()
    role = e.get("role")
    if name in EPHEMERAL or name.startswith(
        ("changeling", "creeptumor", "locust", "egg")
    ):
        return "ephemeral"
    if name in WORKERS:
        return "worker"
    if (
        role == "structure"
        or name in TOWNHALLS
        or name in STRUCTURE_TYPES
        or name.endswith(("techlab", "reactor"))
    ):
        return "structure"
    return "unit"


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts = Counter(values)
    return dict(sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def _event_digest(e: Event) -> Event:
    row: Event = {"t": _time(e), "kind": _kind(e), "item": _unit(e)}
    for key in (
        "loop",
        "x",
        "y",
        "stage",
        "phase",
        "killer_player",
        "killer_type",
        "killer_tag",
        "unit_tag",
        "previous_type",
        "role",
    ):
        if e.get(key) is not None:
            row[key] = e[key]
    return row


def _economy(stats: list[Event], interval: int) -> dict[str, Any]:
    buckets: dict[int, Event] = {}
    low_supply: list[float] = []
    for e in stats:
        t = _time(e)
        buckets[int(t) // interval] = e
        cap, used = e.get("food_made"), e.get("food_used")
        if (
            isinstance(cap, (int, float))
            and isinstance(used, (int, float))
            and 0 < cap < 200
            and used >= cap
        ):
            low_supply.append(t)
    return {
        "samples_seen": len(stats),
        "bucket_seconds": interval,
        "sample_selection": "last recording in each populated bucket, plus first recording",
        "samples": [
            {"t": _time(e), **{k: e[k] for k in STATS if e.get(k) is not None}}
            for e in (
                [stats[0]]
                if stats
                and buckets.get(int(_time(stats[0])) // interval) is not stats[0]
                else []
            )
            + list(buckets.values())
        ],
        "sub_200_zero_headroom_sample_count": len(low_supply),
        "sub_200_zero_headroom_sample_times_seconds": low_supply[:100],
        "zero_headroom_times_truncated": len(low_supply) > 100,
    }


def _group_losses(losses: list[Event]) -> list[list[Event]]:
    """Temporal clusters only; separate simultaneous fronts if coordinates prove separation."""
    clusters: list[list[Event]] = []
    active: list[list[Event]] = []
    for e in losses:
        x, y = e.get("x"), e.get("y")
        compatible: list[tuple[float, list[Event]]] = []
        active = [
            g
            for g in active
            if _time(e) - _time(g[-1]) <= 12 and _time(e) - _time(g[0]) <= 70
        ]
        for group in active:
            gap = _time(e) - _time(group[-1])
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                coords = [
                    (g["x"], g["y"])
                    for g in group[-12:]
                    if isinstance(g.get("x"), (int, float))
                    and isinstance(g.get("y"), (int, float))
                ]
                if (
                    coords
                    and min((gx - x) ** 2 + (gy - y) ** 2 for gx, gy in coords) > 22**2
                ):
                    continue
            compatible.append((gap, group))
        if compatible:
            min(compatible, key=lambda x: x[0])[1].append(e)
        else:
            group = [e]
            clusters.append(group)
            active.append(group)
    return clusters


def _engagements(
    events: list[Event], teams: dict[int, int | None]
) -> tuple[list[dict[str, Any]], int]:
    def opponents(e: Event) -> bool:
        victim, killer = e.get("player"), e.get("killer_player")
        if (
            not isinstance(victim, int)
            or not isinstance(killer, int)
            or killer in (0, victim)
        ):
            return False
        victim_team, killer_team = teams.get(victim), teams.get(killer)
        return victim_team is None or killer_team is None or victim_team != killer_team

    source = [
        e
        for e in events
        if _kind(e) in LOSS_KINDS and opponents(e) and _category(e) != "ephemeral"
    ]
    groups = [
        g
        for g in _group_losses(source)
        if len(g) >= 3 or any(_unit(e).lower() in TOWNHALLS for e in g)
    ]
    # Keep the digest small without silently dropping how many useful candidates exist.
    selected = sorted(
        sorted(groups, key=lambda g: (-len(g), _time(g[0])))[:20],
        key=lambda g: _time(g[0]),
    )
    result = []
    for group in selected:
        by_player: dict[int, list[Event]] = defaultdict(list)
        for e in group:
            by_player[e["player"]].append(e)
        coords = [
            (e["x"], e["y"])
            for e in group
            if isinstance(e.get("x"), (int, float))
            and isinstance(e.get("y"), (int, float))
        ]
        result.append(
            {
                "location_estimate_xy": [
                    round(sum(p[i] for p in coords) / len(coords), 1) for i in (0, 1)
                ]
                if coords
                else None,
                "loss_locations_seen": len(coords),
                "start_t": _time(group[0]),
                "end_t": _time(group[-1]),
                "recorded_losses": len(group),
                "losses_by_player": {
                    str(pid): {
                        "units": _counts(_unit(e) for e in rows),
                        "workers": sum(_category(e) == "worker" for e in rows),
                        "structures": sum(_category(e) == "structure" for e in rows),
                    }
                    for pid, rows in sorted(by_player.items())
                },
                "first_losses": [
                    _event_digest(e) | {"player": e["player"]} for e in group[:5]
                ],
            }
        )
    return result, len(groups)


def build_review(
    data: dict[str, Any], *, interval: int = 30, include_events: bool = False
) -> dict[str, Any]:
    """Keep observed facts separate from inferred fight windows and supply indicators."""
    events = sorted(data.get("events", []), key=_time)
    players = [dict(p) for p in data.get("players", [])]
    by_player: dict[Any, list[Event]] = defaultdict(list)
    for e in events:
        by_player[e.get("player")].append(e)
    summaries = []
    for player in players:
        rows = by_player[player["id"]] if player["id"] is not None else []
        stats = [e for e in rows if _kind(e) == "stats"]
        produced = [
            e
            for e in rows
            if _kind(e) in OPENING_KINDS
            and _time(e) > 0
            and _category(e) != "ephemeral"
        ]
        losses = [
            e for e in rows if _kind(e) in LOSS_KINDS and _category(e) != "ephemeral"
        ]
        opening = [e for e in produced if _time(e) <= 300 and _category(e) != "worker"]
        milestones: dict[str, Event] = {}
        for e in produced:
            unit = _unit(e)
            if _kind(e) in {"expansion", "upgrade"} or unit.lower() in KEY_UNITS:
                milestones.setdefault(
                    f"{_kind(e)}:{e.get('stage') or e.get('phase', '')}:{unit}",
                    _event_digest(e),
                )
        summaries.append(
            {
                **player,
                "economy": _economy(stats, interval),
                "opening_events": [_event_digest(e) for e in opening[:60]],
                "opening_event_count": len(opening),
                "milestones": list(milestones.values())[:120],
                "milestone_count": len(milestones),
                "expansions": [
                    _event_digest(e) for e in produced if _kind(e) == "expansion"
                ],
                "recorded_losses_by_type": _counts(_unit(e) for e in losses),
                "first_worker_losses": [
                    _event_digest(e) for e in losses if _category(e) == "worker"
                ][:12],
                "losses_without_enemy_attribution": sum(
                    e.get("killer_player") in (None, 0, player["id"]) for e in losses
                ),
            }
        )
    teams = {p["id"]: p.get("team") for p in players if p["id"] is not None}
    fights, fight_count = _engagements(events, teams)
    report = {
        "map": data.get("map"),
        "build": data.get("build"),
        "game_version": data.get("game_version") or data.get("version"),
        "duration_seconds": data.get("duration_seconds"),
        "game_speed_code": data.get("game_speed_code"),
        "loops_per_second": data.get("loops_per_second"),
        "duration_loops": data.get("duration_loops"),
        "unmatched_tracker_player_ids": sorted(
            pid for pid in by_player if isinstance(pid, int) and pid not in teams
        ),
        "warnings": data.get("warnings", []),
        "players": summaries,
        "engagement_candidates": fights,
        "engagement_candidate_count": fight_count,
        "event_counts": _counts(_kind(e) for e in events),
    }
    if include_events:
        report["events"] = events
    return report


def _fingerprint(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".replay-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            name = handle.name
            json.dump(
                result,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main(
    argv: list[str] | None = None,
    *,
    local_run: dict[str, Any] | None = None,
    skip_reason: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", nargs="+", type=Path, help=".SC2Replay paths")
    parser.add_argument("--out", type=Path, default=Path("results/mizuki-review.json"))
    parser.add_argument("--sample-seconds", type=int, default=30)
    parser.add_argument(
        "--include-events",
        action="store_true",
        help="preserve full normalized tracker timeline",
    )
    parser.add_argument(
        "--anonymous",
        action="store_true",
        help="replace player display names and source filenames",
    )
    args = parser.parse_args(argv)
    if not 5 <= args.sample_seconds <= 300:
        parser.error("--sample-seconds must be between 5 and 300")
    if args.out.suffix.lower() != ".json" or any(
        p.resolve() == args.out.resolve() for p in args.replay
    ):
        parser.error("--out must be a .json file distinct from the input replays")
    reports, failures = [], []
    for index, replay in enumerate(args.replay, 1):
        name = f"replay-{index}" if args.anonymous else replay.name
        if skip_reason is not None:
            failures.append({"source": name, "error": skip_reason})
            continue
        try:
            data = parse_replay(replay)
            if args.anonymous:
                data["players"] = [
                    {**p, "name": f"Player {p['id']}"} for p in data["players"]
                ]
            report = build_review(
                data, interval=args.sample_seconds, include_events=args.include_events
            )
            reports.append({"source": name, "sha256": _fingerprint(replay), **report})
        except (ReplayParseError, OSError) as exc:
            message = str(exc).replace(str(replay), name)
            failures.append({"source": name, "error": message})
    output = {
        "schema": SCHEMA,
        "clock": "Faster-equivalent SC2 seconds (game loops / 22.4); not wall clock at other speeds",
        "interpretation": {
            "tracker": "Recorded completions, construction starts, upgrades, ownership and deaths; not queued or rejected bot commands.",
            "engagements": "Heuristic clusters of attributed enemy losses, not a count of all battles or a causal verdict.",
            "supply": "Zero headroom at recorded samples does not establish a blocked production command or exact duration.",
        },
        "matches": reports,
        "errors": failures,
    }
    if local_run is not None:
        output["local_smoke"] = local_run
    try:
        _write_json(args.out, output)
    except OSError as exc:
        print(f"Cannot write review JSON: {exc}", file=sys.stderr)
        return 2
    print(f"{args.out}: {len(reports)} parsed, {len(failures)} failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
